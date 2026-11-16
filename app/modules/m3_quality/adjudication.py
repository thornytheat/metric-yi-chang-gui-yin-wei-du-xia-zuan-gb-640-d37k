"""冲突组裁定：把「整组隔离」升级为「可选其一」，但不改写原证据。

设计约束（审计视角）：

- 原始冲突行与隔离原因永远不改写。裁决只是叠在隔离之上的第二层事实，
  页面必须能并排显示「原报事实」与「人工决定」，否则会把人工选择误认成原报；
- 裁决只追加：每次采纳 / 撤销都新增一条记录并递增版本，历史一条不删；
- 采纳点参与收入计算，但它带 ``source="adjudicated"`` 标记，任何合计都能拆出
  「原报采信」与「裁定采信」两段；
- 版本号用于并发：两人同时读到 v3，只有一个人能提交 v4，另一个收到 409；
- 组以 ``(input_sha256, hotel_code, channel, biz_date)`` 为身份，重导同一文件命中
  同一组，已有决定不丢；换参数重跑同理。
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from decimal import Decimal

from app import reasons

ACTION_ADOPT = "adopt"
ACTION_REVOKE = "revoke"
ACTIONS = (ACTION_ADOPT, ACTION_REVOKE)
SOURCE_REPORTED = "reported"
SOURCE_ADJUDICATED = "adjudicated"


def group_id_of(input_sha256: str, hotel_code: str, channel: str, biz_date: str) -> str:
    """组身份：文件内容 + 业务键。重导同一文件必然命中同一组。"""
    return "CG-" + "|".join((input_sha256[:12], hotel_code, channel, biz_date))


@dataclass
class GroupState:
    """一个冲突组的当前状态：原证据 + 裁决链 + 当前决定。"""

    group_id: str
    input_sha256: str
    run_id: str
    hotel_code: str
    channel: str
    biz_date: str
    snapshots: list[dict] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    version: int = 0
    action: str = ""                 # "" / adopt / revoke
    adopted_line: int | None = None
    adopted: dict | None = None
    effective: dict | None = None
    last_decided_at: str = ""

    @property
    def decided(self) -> bool:
        return self.action == ACTION_ADOPT

    @property
    def isolated(self) -> bool:
        return not self.decided

    @property
    def state(self) -> str:
        return "adopted" if self.decided else "isolated"

    def as_dict(self) -> dict:
        return {
            "group_id": self.group_id,
            "input_sha256": self.input_sha256,
            "run_id": self.run_id,
            "hotel_code": self.hotel_code,
            "channel": self.channel,
            "biz_date": self.biz_date,
            "snapshots": self.snapshots,
            "history": self.history,
            "version": self.version,
            "action": self.action,
            "adopted_line": self.adopted_line,
            "adopted": self.adopted,
            "effective": self.effective,
            "last_decided_at": self.last_decided_at,
            "state": self.state,
            "evidence_code": "Q_DUP_CONFLICT",
            "evidence_label": reasons.label("Q_DUP_CONFLICT"),
        }


def snapshot_view(row: dict) -> dict:
    """原证据的一行：数值用字符串，行号与快照时间都在，便于逐条对照。"""
    return {
        "line_no": int(row["line_no"]),
        "snapshot_utc": row["snapshot_utc"],
        "hotel_code": row["hotel_code"],
        "hotel_name": row.get("hotel_name", ""),
        "channel": row["channel"],
        "biz_date": row["biz_date"],
        "stratum": row.get("stratum", ""),
        "rooms_available": int(row["rooms_available"]),
        "rooms_sold": int(row["rooms_sold"]),
        "adr": str(row["adr"]),
        "revenue_net": str(row["revenue_net"]),
        "late_days": int(row.get("late_days", 0)),
        "raw": row.get("raw", ""),
    }


def _decision_brief(entry: dict) -> dict:
    return {
        "decision_id": entry["decision_id"],
        "version": int(entry["version"]),
        "action": entry["action"],
        "line_no": entry["line_no"],
        "reason": entry["reason"],
        "actor": entry["actor"],
        "created_at": entry["created_at"],
        "request_id": entry.get("request_id", ""),
    }


def fold_history(history: list[dict]) -> dict:
    """把决定链折叠成当前状态；撤销只让状态回到隔离，不删任何历史。"""
    state = {"version": 0, "action": "", "adopted_line": None, "last_decided_at": ""}
    for entry in history:
        state["version"] = int(entry["version"])
        state["action"] = entry["action"]
        state["adopted_line"] = entry["line_no"] if entry["action"] == ACTION_ADOPT else None
        state["last_decided_at"] = entry["created_at"]
    return state


def build_group(base: dict, snapshots: list[dict], history: list[dict]) -> GroupState:
    group = GroupState(
        group_id=base["group_id"], input_sha256=base["input_sha256"],
        run_id=base["run_id"], hotel_code=base["hotel_code"],
        channel=base["channel"], biz_date=base["biz_date"],
        snapshots=[snapshot_view(row) for row in snapshots],
        history=[_decision_brief(entry) for entry in history],
    )
    state = fold_history(history)
    group.version = state["version"]
    group.action = state["action"]
    group.adopted_line = state["adopted_line"]
    group.last_decided_at = state["last_decided_at"]
    if group.action == ACTION_ADOPT:
        chosen = next((item for item in group.snapshots
                       if item["line_no"] == group.adopted_line), None)
        if chosen is not None:
            group.adopted = {**chosen, "source": SOURCE_ADJUDICATED,
                             "decision_version": group.version}
            group.effective = adopted_point(group)
    return group


def adopted_point(group: GroupState) -> dict:
    """把裁定采信的候选快照转成标准指标点，与 M3 产出的点同形。"""
    chosen = group.adopted
    if chosen is None:
        return {}
    return {
        "hotel_code": chosen["hotel_code"],
        "hotel_name": chosen["hotel_name"],
        "channel": chosen["channel"],
        "biz_date": chosen["biz_date"],
        "stratum": chosen["stratum"],
        "rooms_available": int(chosen["rooms_available"]),
        "rooms_sold": int(chosen["rooms_sold"]),
        "adr": str(chosen["adr"]),
        "revenue_net": str(chosen["revenue_net"]),
        "late_days": int(chosen["late_days"]),
        "late_flag": 0,
        "out_of_window": 0,
        "source_line": int(chosen["line_no"]),
        "updated_utc": chosen["snapshot_utc"],
        "source": SOURCE_ADJUDICATED,
        "decision_version": group.version,
        "group_id": group.group_id,
    }


def apply_decisions(base_points: list[dict], groups: list[GroupState]) -> list[dict]:
    """在原始有效点之上叠加裁定采信点；原有点保持 source=reported。"""
    adopted = {group.group_id: group for group in groups if group.decided}
    if not adopted:
        return [{**point, "source": SOURCE_REPORTED} for point in base_points]
    blocked = {(group.hotel_code, group.channel, group.biz_date)
               for group in adopted.values()}
    out = [{**point, "source": SOURCE_REPORTED} for point in base_points
           if (point["hotel_code"], point["channel"], point["biz_date"]) not in blocked]
    out.extend(group.effective for group in adopted.values())
    out.sort(key=lambda point: (point["hotel_code"], point["channel"], point["biz_date"]))
    return out


def revenue_split(points: list[dict]) -> dict:
    """把有效点收入拆成「原报采信」与「裁定采信」两段，金额账据此对平。"""
    reported = sum((Decimal(point["revenue_net"]) for point in points
                    if point.get("source") != SOURCE_ADJUDICATED), Decimal("0"))
    adjudicated = sum((Decimal(point["revenue_net"]) for point in points
                       if point.get("source") == SOURCE_ADJUDICATED), Decimal("0"))
    return {
        "reported": str(reported.quantize(Decimal("0.01"))),
        "adjudicated": str(adjudicated.quantize(Decimal("0.01"))),
        "total": str((reported + adjudicated).quantize(Decimal("0.01"))),
        "reported_points": sum(1 for point in points
                               if point.get("source") != SOURCE_ADJUDICATED),
        "adjudicated_points": sum(1 for point in points
                                  if point.get("source") == SOURCE_ADJUDICATED),
    }


def next_version(history: list[dict]) -> int:
    return max((int(entry["version"]) for entry in history), default=0) + 1


def make_decision_id(group_id: str, version: int) -> str:
    # 带文件摘要前缀，两个不同文件的同名业务键不会撞号
    return "CD-%s-v%d" % (group_id[3:].replace("|", "-"), version)


def now_stamp() -> str:
    """决定时间用东八区本地分钟，和页面其他时间一致。"""
    from app.modules.m2_calendar import tz

    return tz.local_stamp(dt.datetime.now(dt.timezone.utc))


@dataclass
class DecisionRequest:
    actor: str
    reason: str
    expected_version: int
    line_no: int | None = None
    request_id: str = ""


def validate(req: DecisionRequest, group: GroupState, action: str = ACTION_ADOPT) -> None:
    from app import errors

    reason_text = (req.reason or "").strip()
    if len(reason_text) < 4:
        raise errors.BadInput("裁决理由至少 4 个字", code="E_DECISION_REASON_REQUIRED")
    if not (req.actor or "").strip():
        raise errors.BadInput("裁决人不能为空", code="E_DECISION_ACTOR_REQUIRED")
    if req.expected_version != group.version:
        raise errors.Conflict(
            "版本已过期：组内当前版本 %d，提交依据 %d" % (group.version, req.expected_version),
            code="E_DECISION_STALE", current_version=group.version,
            expected_version=req.expected_version)
    if action == ACTION_ADOPT:
        lines = {item["line_no"] for item in group.snapshots}
        if req.line_no not in lines:
            raise errors.BadInput(
                "所选快照行 %s 不属于该冲突组" % req.line_no, code="E_DECISION_UNKNOWN_LINE",
                candidates=sorted(lines))


def decision_entry(group: GroupState, action: str, req: DecisionRequest,
                   version: int) -> dict:
    line_no = req.line_no if action == ACTION_ADOPT else None
    snapshot = next((item for item in group.snapshots if item["line_no"] == line_no), None)

    return {
        "decision_id": make_decision_id(group.group_id, version),
        "group_id": group.group_id,
        "input_sha256": group.input_sha256,
        "run_id": group.run_id,
        "hotel_code": group.hotel_code,
        "channel": group.channel,
        "biz_date": group.biz_date,
        "version": version,
        "action": action,
        "line_no": line_no,
        "reason": req.reason.strip(),
        "actor": req.actor.strip(),
        "request_id": (req.request_id or "").strip(),
        "snapshot_json": json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        if snapshot else "",
        "created_at": now_stamp(),
    }
