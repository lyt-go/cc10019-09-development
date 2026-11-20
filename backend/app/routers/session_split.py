"""会话切割路由：执行会话切割并获取结果。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.schemas import ActionPayload, ActionResult, SessionSplitDetail
from app.services.session_split import (
    get_session_detail,
    get_session_result,
    resegment_with_strategy,
    split_sessions,
)
from app.store import store, query_filters

router = APIRouter(prefix="/api/session-split", tags=["session_split"])


@router.get("")
def index(request: Request, page: int = 1, size: int = 20) -> dict[str, Any]:
    rows = store.rows("session_split")
    filters=query_filters(request)
    if filters:
        rows = [r for r in rows if all(str(r.get(k)) == str(v) for k, v in filters.items())]
    total = len(rows)
    start = (page - 1) * size
    return {"items": rows[start : start + size], "total": total, "page": page, "size": size}


@router.get("/{entry_id}")
def detail(entry_id: int) -> dict[str, Any]:
    row = store.find("session_split", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    return row


@router.post("/{strategy_id}/run")
def run(strategy_id: str) -> ActionResult:
    try:
        result = split_sessions(strategy_id)
        return ActionResult(ok=True, message="Split", entry=result.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{strategy_id}/result")
def result(strategy_id: str) -> dict[str, Any]:
    res = get_session_result(strategy_id)
    if res is None:
        raise HTTPException(status_code=404, detail="Result not found")
    return res


@router.get("/{strategy_id}/detail", response_model=SessionSplitDetail)
def detail_sessions(strategy_id: str) -> dict[str, Any]:
    """会话明细：版本、确认状态、每条事件的会话归属与补报并入情况。"""
    res = get_session_detail(strategy_id)
    if res is None:
        raise HTTPException(status_code=404, detail="Result not found")
    return res


@router.post("/{strategy_id}/resegment")
def resegment(strategy_id: str, payload: dict[str, Any]) -> ActionResult:
    timeout = payload.get("timeout_sec")
    try:
        result = resegment_with_strategy(strategy_id, timeout)
        return ActionResult(ok=True, message="Resegmented", entry=result.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    row = store.find("session_split", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    action = payload.action
    row["status"] = action
    row["pending"] = False
    return ActionResult(ok=True, message=action, entry=row)
