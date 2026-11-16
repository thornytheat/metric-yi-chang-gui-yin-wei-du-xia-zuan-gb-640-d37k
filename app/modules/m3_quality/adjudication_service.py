"""冲突组裁定的编排：证据落库、读组、采纳 / 撤销、有效点叠加。

分层：

- ``snapshots``：导入时由 M3 产出的原始候选行，按文件 sha256 落库，只写一次；
- ``decisions``：人工裁决，只追加；
- ``groups``：上面两者折叠出的当前状态，页面与归因都读这一层。

重导同一文件（同 sha256）时证据键相同，``replace`` 后仍是同一批行；
已有决定按 group_id 命中，不会被抹掉。
"""
from __future__ import annotations

from app import errors
from app.modules.m3_quality import adjudication


def evidence_payload(quality, input_sha256: str) -> list[dict]:
    """把 M3 的冲突行整理成快照行，按组身份补上 group_id。"""
    out = []
    for row in quality.conflict_rows:
        out.append({
            "input_sha256": input_sha256,
            "group_id": adjudication.group_id_of(
                input_sha256, row.hotel_code, row.channel, row.biz_date.isoformat()),
            "hotel_code": row.hotel_code,
            "channel": row.channel,
            "biz_date": row.biz_date.isoformat(),
            "line_no": row.line_no,
            "snapshot_utc": row.snapshot_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "hotel_name": row.hotel_name,
            "stratum": row.stratum,
            "rooms_available": row.rooms_available,
            "rooms_sold": row.rooms_sold,
            "adr": str(row.adr),
            "revenue_net": str(row.revenue_net),
            "late_days": row.late_days,
            "late_flag": int(row.late_flag),
            "out_of_window": int(row.out_of_window),
            "raw": row.raw,
        })
    return out


def persist_evidence(store, input_sha256: str, snapshots: list[dict]) -> None:
    store.replace_conflict_snapshots(input_sha256, snapshots)


def _snapshots_by_group(store, run: dict) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in store.load_conflict_snapshots(run["input_sha256"]):
        grouped.setdefault(row["group_id"], []).append(row)
    return grouped


def groups_for_run(store, run: dict) -> list[adjudication.GroupState]:
    by_group = _snapshots_by_group(store, run)
    groups = []
    for group_id in sorted(by_group):
        snapshots = by_group[group_id]
        first = snapshots[0]
        base = {
            "group_id": group_id,
            "input_sha256": first["input_sha256"],
            "run_id": run["run_id"],
            "hotel_code": first["hotel_code"],
            "channel": first["channel"],
            "biz_date": first["biz_date"],
        }
        history = store.decisions_for_group(group_id)
        groups.append(adjudication.build_group(base, snapshots, history))
    return groups


def group_state(store, run: dict, group_id: str) -> adjudication.GroupState:
    for group in groups_for_run(store, run):
        if group.group_id == group_id:
            return group
    raise errors.NotFound("冲突组不存在：" + group_id, code="E_CONFLICT_GROUP_NOT_FOUND")


def _stale(store, group_id: str) -> errors.Conflict:
    current = adjudication.next_version(store.decisions_for_group(group_id)) - 1
    return errors.Conflict(
        "版本已过期：组内当前版本 %d" % current, code="E_DECISION_STALE",
        current_version=current)


def decide(store, run: dict, group_id: str, action: str,
           req: adjudication.DecisionRequest) -> dict:
    """提交一次裁决。并发由 store 的事务与唯一索引双重兜底。"""
    from app.store import DecisionVersionConflict

    group = group_state(store, run, group_id)
    if action not in adjudication.ACTIONS:
        raise errors.BadInput("未知裁决动作：" + str(action), code="E_DECISION_ACTION")

    # 幂等重试：同一请求号命中已有决定，直接返回，不重复入账。
    # 放在状态校验之前：重试时组状态可能已因这条决定而改变。
    if req.request_id:
        existing = store.decision_for_request(group_id, req.request_id)
        if existing:
            return {"group": group_state(store, run, group_id).as_dict(),
                    "decision": adjudication._decision_brief(existing),
                    "replayed": True, "reused": True}

    if action == adjudication.ACTION_REVOKE and group.version == 0:
        raise errors.BadInput("该组还没有可撤销的裁决", code="E_DECISION_NOTHING_TO_REVOKE")
    if action == adjudication.ACTION_REVOKE and not group.decided:
        raise errors.BadInput("该组当前已是隔离状态", code="E_DECISION_ALREADY_ISOLATED")

    adjudication.validate(req, group, action)
    entry = adjudication.decision_entry(
        group, action, req, version=group.version + 1)
    try:
        store.append_decision(entry)
    except DecisionVersionConflict:
        raise _stale(store, group_id) from None
    return {
        "group": group_state(store, run, group_id).as_dict(),
        "decision": adjudication._decision_brief(entry),
        "replayed": False,
        "reused": False,
    }


def effective_points(store, run: dict) -> list[dict]:
    """原报有效点 + 裁定采信点；归因、检测、复盘都从这一层取数。"""
    base = store.load_points(run["run_id"])
    groups = groups_for_run(store, run)
    return adjudication.apply_decisions(base, groups)


def groups_payload(store, run: dict) -> dict:
    groups = groups_for_run(store, run)
    adopted = [group for group in groups if group.decided]
    points = effective_points(store, run)
    split = adjudication.revenue_split(points)
    return {
        "run_id": run["run_id"],
        "input_sha256": run["input_sha256"],
        "total": len(groups),
        "adopted": len(adopted),
        "isolated": len(groups) - len(adopted),
        "groups": [group.as_dict() for group in groups],
        "revenue": split,
    }


def accounts_view(store, run: dict) -> dict:
    """对账用的账目视图：不覆盖进原始 summary，只在其上叠加裁定采信。

    - 行账保持原报：冲突行仍记为隔离，``rows_read = 采信 + 被覆盖 + 隔离`` 不变；
    - 点账按有效态修正：裁定采信的键从「隔离点」移到「有效点」，
      ``网格 = 有效 + 隔离 + 缺失`` 仍然成立；
    - 金额账拆成「原报采信」与「裁定采信」两段，合计 = 有效点合计。
    """
    base = dict(run["summary"].get("accounts", {}))
    groups = groups_for_run(store, run)
    adopted = [group for group in groups if group.decided]
    split = adjudication.revenue_split(effective_points(store, run))
    valid_reported = int(base.get("points_valid", 0))
    rejected = int(base.get("points_rejected", 0))
    view = dict(base)
    view.update({
        "adjudicated_groups": len(adopted),
        "points_valid_reported": valid_reported,
        "points_valid_effective": valid_reported + len(adopted),
        "points_rejected_effective": max(0, rejected - len(adopted)),
        "revenue_accepted_reported": base.get("revenue_accepted", "0.00"),
        "revenue_adjudicated": split["adjudicated"],
        "revenue_effective": split["total"],
        "points_valid": valid_reported + len(adopted),
        "points_rejected": max(0, rejected - len(adopted)),
        "revenue_accepted": split["total"],
    })
    return view
