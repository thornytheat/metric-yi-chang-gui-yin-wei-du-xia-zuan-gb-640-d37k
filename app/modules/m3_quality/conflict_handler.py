"""冲突组裁定入口：列组、看单组证据链、采纳指定快照、撤销回整组隔离。

所有写操作都要求 ``expected_version``：两人并发时后提交者拿到 409，
不会出现「拿旧状态覆盖新状态」。``request_id`` 可选，用于安全重试。
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Query, Request

from app import errors
from app.modules.m3_quality import adjudication, adjudication_service
from app.web import state

router = APIRouter()


def _run(request: Request) -> dict:
    app_state = state(request)
    run = app_state.current_run()
    if run is None:
        raise errors.NotFound("还没有运行记录", code="E_NO_RUN")
    return run


def _request_body(body: dict | None, expected_version: str | None) -> dict:
    payload = dict(body or {})
    if payload.get("expected_version") is None and expected_version is not None:
        payload["expected_version"] = expected_version
    return payload


def _decision_request(payload: dict) -> adjudication.DecisionRequest:
    raw_version = payload.get("expected_version")
    if raw_version is None or str(raw_version).strip() == "":
        raise errors.BadInput("缺少依据版本 expected_version", code="E_DECISION_VERSION_REQUIRED")
    try:
        version = int(raw_version)
    except (TypeError, ValueError) as exc:
        raise errors.BadInput("依据版本不是整数：" + str(raw_version),
                              code="E_DECISION_VERSION_REQUIRED") from exc
    line_no = payload.get("line_no")
    if line_no is not None and str(line_no).strip() != "":
        try:
            line_no = int(line_no)
        except (TypeError, ValueError) as exc:
            raise errors.BadInput("快照行号不是整数：" + str(line_no),
                                  code="E_DECISION_UNKNOWN_LINE") from exc
    else:
        line_no = None
    return adjudication.DecisionRequest(
        actor=str(payload.get("actor", "")),
        reason=str(payload.get("reason", "")),
        expected_version=version,
        line_no=line_no,
        request_id=str(payload.get("request_id", "")),
    )


@router.get("/api/conflicts")
def list_conflicts(request: Request, run_id: str = Query(default="")):
    app_state = state(request)
    run = app_state.store.get_run(run_id) if run_id else app_state.current_run()
    if run is None:
        raise errors.NotFound("还没有运行记录", code="E_NO_RUN")
    return adjudication_service.groups_payload(app_state.store, run)


@router.get("/api/conflicts/{group_id}")
def get_conflict(group_id: str, request: Request, run_id: str = Query(default="")):
    app_state = state(request)
    run = app_state.store.get_run(run_id) if run_id else app_state.current_run()
    if run is None:
        raise errors.NotFound("还没有运行记录", code="E_NO_RUN")
    group = adjudication_service.group_state(app_state.store, run, group_id)
    return {
        "run_id": run["run_id"],
        "input_sha256": run["input_sha256"],
        "group": group.as_dict(),
    }


@router.post("/api/conflicts/{group_id}/adopt")
def adopt_conflict(group_id: str, request: Request, body: dict | None = Body(default=None),
                   expected_version: str | None = Query(default=None),
                   run_id: str = Query(default="")):
    app_state = state(request)
    run = app_state.store.get_run(run_id) if run_id else app_state.current_run()
    if run is None:
        raise errors.NotFound("还没有运行记录", code="E_NO_RUN")
    req = _decision_request(_request_body(body, expected_version))
    result = adjudication_service.decide(
        app_state.store, run, group_id, adjudication.ACTION_ADOPT, req)
    result["run_id"] = run["run_id"]
    result["revenue"] = adjudication_service.groups_payload(
        app_state.store, run)["revenue"]
    return result


@router.post("/api/conflicts/{group_id}/revoke")
def revoke_conflict(group_id: str, request: Request, body: dict | None = Body(default=None),
                    expected_version: str | None = Query(default=None),
                    run_id: str = Query(default="")):
    app_state = state(request)
    run = app_state.store.get_run(run_id) if run_id else app_state.current_run()
    if run is None:
        raise errors.NotFound("还没有运行记录", code="E_NO_RUN")
    req = _decision_request(_request_body(body, expected_version))
    result = adjudication_service.decide(
        app_state.store, run, group_id, adjudication.ACTION_REVOKE, req)
    result["run_id"] = run["run_id"]
    result["revenue"] = adjudication_service.groups_payload(
        app_state.store, run)["revenue"]
    return result
