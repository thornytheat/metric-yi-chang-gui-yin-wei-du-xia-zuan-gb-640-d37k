"""冲突组裁定：原证据不改写、只追加、版本并发、幂等重试、撤销恢复隔离、重导保留。

这些测试覆盖的需求点：

- 按冲突组选择原快照并记录理由，采信的快照参与收入 / 检测 / 归因；
- 撤销后整组恢复隔离，历史裁决一条不丢；
- 原始冲突行与 ``Q_DUP_CONFLICT`` 原因码保持不变（审计不把人工决定当原报）；
- 两人并发裁决时拒绝过期版本；
- 失败后带请求号重试不重复入账；
- 重导同一文件不抹掉已有决定。
"""
from __future__ import annotations

import threading
from decimal import Decimal

import pytest

from app import config
from app.modules.m3_quality import adjudication, adjudication_service
from app.modules.m5_attrib import service as batch_service

ADOPT_REASON = "财务核实后以该快照为准"


def _group(client) -> dict:
    payload = client.get("/api/conflicts").json()
    assert payload["total"] == 1, payload
    return payload["groups"][0]


def _conflict_lines(group: dict) -> list[int]:
    return sorted(item["line_no"] for item in group["snapshots"])


def _adopt(client, group_id, line_no, version, actor="finance-a",
           reason=ADOPT_REASON, request_id=""):
    return client.post(f"/api/conflicts/{group_id}/adopt", json={
        "line_no": line_no, "actor": actor, "reason": reason,
        "expected_version": version, "request_id": request_id})


def _revoke(client, group_id, version, actor="finance-b",
            reason="复核后撤回采信", request_id=""):
    return client.post(f"/api/conflicts/{group_id}/revoke", json={
        "actor": actor, "reason": reason,
        "expected_version": version, "request_id": request_id})


# ---- 1. 原证据永远不改写 ------------------------------------------------

def test_conflict_group_lists_original_evidence(client):
    group = _group(client)
    assert group["state"] == "isolated"
    assert group["version"] == 0
    assert len(group["snapshots"]) == 2
    assert _conflict_lines(group) == [493, 1008]
    for snap in group["snapshots"]:
        assert snap["raw"].startswith("H02,蓝湾·中心,member,2026-11-10")
        # 同一快照时间，两个不同的净收入 —— 这才是冲突的证据本身
        assert snap["snapshot_utc"] == "2026-11-10T22:00:00Z"
    assert {snap["revenue_net"] for snap in group["snapshots"]} == {
        "29004.80", "30004.79"}


def test_adopt_does_not_rewrite_quarantine_or_conflict_rows(client):
    before = client.get("/api/quarantine").json()
    conflict_before = [entry for entry in before["entries"]
                       if entry["code"] == "Q_DUP_CONFLICT"]
    group = _group(client)
    _adopt(client, group["group_id"], 493, 0)

    after = client.get("/api/quarantine").json()
    conflict_after = [entry for entry in after["entries"]
                      if entry["code"] == "Q_DUP_CONFLICT"]
    # 隔离行数量、原因码、原始行完全不变
    assert after["total"] == before["total"]
    assert after["accounts"]["rows_quarantine"] == 11
    assert conflict_after == conflict_before
    assert len(conflict_after) == 2
    # 原证据行仍在，仍标 Q_DUP_CONFLICT
    detail = client.get(f"/api/conflicts/{group['group_id']}").json()
    assert detail["group"]["evidence_code"] == "Q_DUP_CONFLICT"
    assert len(detail["group"]["snapshots"]) == 2


# ---- 2. 采信点参与计算，且可拆分 ----------------------------------------

def test_adopted_snapshot_joins_income_and_split_is_explicit(client, run):
    group = _group(client)
    before = client.get("/api/conflicts").json()["revenue"]
    assert before["adjudicated"] == "0.00"
    assert before["reported_points"] == 1001

    response = _adopt(client, group["group_id"], 493, 0)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["group"]["state"] == "adopted"
    assert body["group"]["effective"]["revenue_net"] == "29004.80"
    assert body["group"]["effective"]["source"] == "adjudicated"
    assert body["revenue"]["adjudicated"] == "29004.80"
    assert body["revenue"]["adjudicated_points"] == 1
    # 原报段一分不动，总账 = 原报 + 裁定
    assert body["revenue"]["reported"] == before["reported"]
    assert (Decimal(body["revenue"]["total"])
            == Decimal(body["revenue"]["reported"])
            + Decimal(body["revenue"]["adjudicated"]))

    reconcile = client.get("/api/reconcile").json()
    assert reconcile["identity_ok"] is True
    accounts = reconcile["accounts"]
    assert accounts["revenue_adjudicated"] == "29004.80"
    assert accounts["revenue_accepted_reported"] == run["accounts"]["revenue_accepted"]
    assert (Decimal(accounts["revenue_accepted"])
            == Decimal(accounts["revenue_accepted_reported"])
            + Decimal(accounts["revenue_adjudicated"]))
    # 网格守恒仍然成立
    assert (accounts["points_grid"]
            == accounts["points_valid"] + accounts["points_rejected"]
            + accounts["points_missing"])


def test_adopted_point_changes_attribution_and_detection(client):
    group = _group(client)
    # 冲突日 2026-11-10 落在默认 7 天当期窗口之前，用 14 天窗口把它纳入
    before = client.get("/api/attribution?cur_days=14").json()
    _adopt(client, group["group_id"], 493, 0)
    after = client.get("/api/attribution?cur_days=14").json()
    assert after["identity_ok"] is True
    assert after["share_sum"] == "1.0000"
    # 采信快照位于当期，归因窗口内的收入随之变化
    assert after["total"]["rev_cur"] != before["total"]["rev_cur"]
    assert (Decimal(after["total"]["rev_cur"]) - Decimal(before["total"]["rev_cur"])
            == Decimal("29004.80"))

    anomalies = client.get("/api/anomalies").json()
    revenue = client.get("/api/conflicts").json()["revenue"]
    assert anomalies["summary"]["points"] == (
        revenue["reported_points"] + revenue["adjudicated_points"])


# ---- 3. 撤销恢复整组隔离，历次裁决保留 ----------------------------------

def test_revoke_restores_isolation_and_keeps_history(client):
    group = _group(client)
    _adopt(client, group["group_id"], 1008, 0)
    revoked = _revoke(client, group["group_id"], 1)
    assert revoked.status_code == 200, revoked.text
    state = revoked.json()["group"]
    assert state["state"] == "isolated"
    assert state["adopted_line"] is None
    assert [item["action"] for item in state["history"]] == ["adopt", "revoke"]
    assert [item["version"] for item in state["history"]] == [1, 2]
    assert state["history"][0]["line_no"] == 1008

    payload = client.get("/api/conflicts").json()
    assert payload["isolated"] == 1 and payload["adopted"] == 0
    assert payload["revenue"]["adjudicated"] == "0.00"
    assert payload["revenue"]["total"] == payload["revenue"]["reported"]


def test_repeated_adopt_and_revoke_accumulates_versions(client):
    group = _group(client)
    version = 0
    for line_no in (493, 1008):
        assert _adopt(client, group["group_id"], line_no, version).status_code == 200
        version += 1
        assert _revoke(client, group["group_id"], version).status_code == 200
        version += 1
    state = client.get(f"/api/conflicts/{group['group_id']}").json()["group"]
    assert state["version"] == 4
    assert [item["action"] for item in state["history"]] == [
        "adopt", "revoke", "adopt", "revoke"]
    assert state["state"] == "isolated"


# ---- 4. 并发裁决拒绝过期版本 --------------------------------------------

def test_stale_version_rejected_after_someone_else_adopted(client):
    group = _group(client)
    first = _adopt(client, group["group_id"], 493, 0, actor="finance-a")
    assert first.status_code == 200
    stale = _adopt(client, group["group_id"], 1008, 0, actor="finance-b")
    assert stale.status_code == 409
    body = stale.json()
    assert body["error"] == "E_DECISION_STALE"
    assert body["current_version"] == 1
    state = client.get(f"/api/conflicts/{group['group_id']}").json()["group"]
    # 只有第一个人的决定生效
    assert state["version"] == 1
    assert state["adopted_line"] == 493
    assert [item["actor"] for item in state["history"]] == ["finance-a"]


def test_concurrent_store_commits_allow_exactly_one(settings, store, dirty_bytes):
    run = batch_service.run_batch(settings, store, dirty_bytes, "dirty.csv")
    group_id = adjudication_service.groups_for_run(store, run)[0].group_id
    outcomes: list[str] = []

    def worker(index: int) -> None:
        try:
            adjudication_service.decide(
                store, run, group_id, adjudication.ACTION_ADOPT,
                adjudication.DecisionRequest(
                    actor=f"user-{index}", reason="并发裁决同一版本",
                    expected_version=0, line_no=493, request_id=f"req-{index}"))
            outcomes.append("ok")
        except Exception as exc:  # noqa: BLE001 - 只关心是不是版本冲突
            outcomes.append(getattr(exc, "code", type(exc).__name__))

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count("ok") == 1
    assert outcomes.count("E_DECISION_STALE") == 5
    assert len(store.decisions_for_group(group_id)) == 1


# ---- 5. 幂等重试不重复入账 ----------------------------------------------

def test_replayed_request_is_not_booked_twice(client):
    group = _group(client)
    first = _adopt(client, group["group_id"], 493, 0, request_id="req-adopt-1")
    assert first.status_code == 200 and first.json()["reused"] is False
    replay = _adopt(client, group["group_id"], 493, 0, request_id="req-adopt-1")
    assert replay.status_code == 200
    body = replay.json()
    assert body["reused"] is True and body["replayed"] is True
    state = body["group"]
    assert state["version"] == 1
    assert len(state["history"]) == 1
    assert body["revenue"]["adjudicated_points"] == 1
    assert body["revenue"]["adjudicated"] == "29004.80"


def test_revocation_replay_after_state_change_is_idempotent(client):
    group = _group(client)
    _adopt(client, group["group_id"], 493, 0)
    first = _revoke(client, group["group_id"], 1, request_id="req-revoke-1")
    assert first.status_code == 200 and first.json()["reused"] is False
    replay = _revoke(client, group["group_id"], 1, request_id="req-revoke-1")
    assert replay.status_code == 200
    body = replay.json()
    assert body["reused"] is True
    assert body["group"]["version"] == 2
    assert len(body["group"]["history"]) == 2


# ---- 6. 重导同一文件不抹掉决定 ------------------------------------------

def test_reimport_same_file_preserves_decision(client):
    group = _group(client)
    _adopt(client, group["group_id"], 1008, 0)
    client.post("/api/import", data={"sample": "daily_metrics_dirty.csv"})
    state = client.get(f"/api/conflicts/{group['group_id']}").json()["group"]
    assert state["state"] == "adopted"
    assert state["adopted_line"] == 1008
    assert state["version"] == 1
    assert len(state["history"]) == 1


def test_rerun_same_file_preserves_decision(client, run):
    group = _group(client)
    _adopt(client, group["group_id"], 493, 0)
    rerun = client.post("/api/attribution/rerun", json={}).json()
    assert rerun["reused"] is True
    state = client.get(f"/api/conflicts/{group['group_id']}").json()["group"]
    assert state["state"] == "adopted"
    assert state["adopted_line"] == 493


def test_decision_survives_run_with_different_params(settings, store, dirty_bytes):
    """证据与决定都以文件 sha256 为身份，换参数重跑产生新 run_id 也不丢决定。"""
    import dataclasses

    run = batch_service.run_batch(settings, store, dirty_bytes, "dirty.csv")
    group_id = adjudication_service.groups_for_run(store, run)[0].group_id
    adjudication_service.decide(
        store, run, group_id, adjudication.ACTION_ADOPT,
        adjudication.DecisionRequest(actor="finance-a", reason="财务核实理由",
                                     expected_version=0, line_no=493))

    other = dataclasses.replace(settings, top_n=3)
    rerun = batch_service.run_batch(other, store, dirty_bytes, "dirty.csv")
    assert rerun["run_id"] != run["run_id"]
    state = adjudication_service.group_state(store, rerun, group_id)
    assert state.decided is True
    assert state.adopted_line == 493
    assert state.run_id == rerun["run_id"]
    points = adjudication_service.effective_points(store, rerun)
    assert sum(1 for point in points if point["source"] == "adjudicated") == 1


# ---- 7. 校验与错误码 ----------------------------------------------------

def test_decision_requires_reason_and_actor(client):
    group = _group(client)
    short = client.post(f"/api/conflicts/{group['group_id']}/adopt", json={
        "line_no": 493, "actor": "a", "reason": "短", "expected_version": 0})
    assert short.status_code == 400
    assert short.json()["error"] == "E_DECISION_REASON_REQUIRED"
    no_actor = client.post(f"/api/conflicts/{group['group_id']}/adopt", json={
        "line_no": 493, "actor": "", "reason": ADOPT_REASON, "expected_version": 0})
    assert no_actor.status_code == 400
    assert no_actor.json()["error"] == "E_DECISION_ACTOR_REQUIRED"


def test_decision_rejects_line_outside_group(client):
    group = _group(client)
    response = _adopt(client, group["group_id"], 9999, 0)
    assert response.status_code == 400
    assert response.json()["error"] == "E_DECISION_UNKNOWN_LINE"
    assert response.json()["candidates"] == [493, 1008]


def test_missing_expected_version_rejected(client):
    group = _group(client)
    response = client.post(f"/api/conflicts/{group['group_id']}/adopt", json={
        "line_no": 493, "actor": "a", "reason": ADOPT_REASON})
    assert response.status_code == 400
    assert response.json()["error"] == "E_DECISION_VERSION_REQUIRED"


def test_revoke_without_decision_rejected(client):
    group = _group(client)
    response = _revoke(client, group["group_id"], 0)
    assert response.status_code == 400
    assert response.json()["error"] == "E_DECISION_NOTHING_TO_REVOKE"


def test_unknown_group_is_404(client):
    response = client.get("/api/conflicts/CG-nope|H99|ota|2026-01-01")
    assert response.status_code == 404
    assert response.json()["error"] == "E_CONFLICT_GROUP_NOT_FOUND"


# ---- 8. 页面可达与内容 --------------------------------------------------

def test_conflicts_page_renders_evidence_and_decision(client):
    group = _group(client)
    _adopt(client, group["group_id"], 493, 0)
    page = client.get("/conflicts")
    assert page.status_code == 200
    text = page.text
    assert "原证据" in text or "原报事实" in text
    assert "历次裁决" in text
    assert "冲突组" in text
    # 页面把当前采信行与证据一起下发
    assert "493" in text


def test_conflicts_page_without_run(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings = config.Settings(db_path=str(tmp_path / "empty.sqlite"),
                               sample_dir=str(tmp_path), seed_on_start=False)
    with TestClient(create_app(settings)) as client:
        page = client.get("/conflicts")
        assert page.status_code == 200
        assert "还没有批次" in page.text


def test_attribution_identity_survives_adjudication(client):
    group = _group(client)
    _adopt(client, group["group_id"], 493, 0)
    _revoke(client, group["group_id"], 1)
    _adopt(client, group["group_id"], 1008, 2)
    attribution = client.get("/api/attribution").json()
    assert attribution["identity_ok"] is True
    assert attribution["total"]["residual"] == "0.00"
    reconcile = client.get("/api/reconcile").json()
    assert reconcile["identity_ok"] is True
    assert reconcile["accounts"]["revenue_adjudicated"] == "30004.79"
