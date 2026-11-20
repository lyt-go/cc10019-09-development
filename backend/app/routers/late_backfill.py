"""迟到事件补报路由：接收迟到事件、重试失败补报、回查归属与下游影响。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.schemas import ActionResult, LateBackfillPayload
from app.services.late_backfill import (
    get_backfill,
    list_backfills,
    retry_backfill,
    submit_backfill,
)
from app.store import store

router = APIRouter(prefix="/api/late-backfill", tags=["late_backfill"])


@router.get("")
def index() -> dict[str, Any]:
    rows = list_backfills()
    return {"items": rows, "total": len(rows)}


@router.post("")
def submit(payload: LateBackfillPayload) -> ActionResult:
    try:
        outcome = submit_backfill(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return ActionResult(ok=True, message=outcome["message"], entry=outcome)


@router.get("/{event_uid}")
def detail(event_uid: str) -> dict[str, Any]:
    row = get_backfill(event_uid)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    return row


@router.post("/{event_uid}/retry")
def retry(event_uid: str) -> ActionResult:
    try:
        outcome = retry_backfill(event_uid)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return ActionResult(ok=True, message=outcome["message"], entry=outcome)


@router.get("/{event_uid}/impact")
def impact(event_uid: str) -> dict[str, Any]:
    """回查一条补报事件的归属：进了哪份会话明细、影响了哪些报告的哪些连边。"""
    row = get_backfill(event_uid)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")

    sessions: list[dict[str, Any]] = []
    for detail in store.list_artifacts("session_split_detail"):
        entry = detail.get("event_index", {}).get(event_uid)
        if entry:
            sessions.append({
                "strategy_id": detail["strategy_id"],
                "session_detail_version": detail["version"],
                "session_id": entry["session_id"],
            })

    reports: list[dict[str, Any]] = []
    for artifact in store.list_artifacts("behavior_path"):
        attribution = next(
            (item for item in artifact.get("backfills", []) if item["event_uid"] == event_uid),
            None,
        )
        if attribution is not None:
            reports.append(attribution)

    return {
        "event_uid": event_uid,
        "status": row["status"],
        "raw_written": bool(row.get("raw_written")),
        "session_details": sessions,
        "reports": reports,
        "reason": next((r["reason"] for r in reports if not r["included"]), ""),
    }
