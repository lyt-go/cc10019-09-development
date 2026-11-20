"""行为路径路由：构建路径Sankey并获取分析结果。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.schemas import ActionPayload, ActionResult
from app.services.behavior_path import (
    build_path_sankey,
    find_common_paths,
    get_path_result,
    get_report_versions,
)
from app.store import store

router = APIRouter(prefix="/api/behavior-path", tags=["behavior_path"])


@router.get("")
def index(page: int = 1, size: int = 20) -> dict[str, Any]:
    rows = store.rows("behavior_path")
    total = len(rows)
    start = (page - 1) * size
    return {"items": rows[start : start + size], "total": total, "page": page, "size": size}




@router.get("/export")
def export_entries() -> dict[str, Any]:
    rows = store.rows("behavior_path")
    return {"items": rows, "total": len(rows)}
@router.get("/{entry_id}")
def detail(entry_id: int) -> dict[str, Any]:
    row = store.find("behavior_path", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    return row


@router.post("/{model_id}/build")
def build(model_id: str) -> ActionResult:
    try:
        result = build_path_sankey(model_id)
        return ActionResult(ok=True, message="Built", entry=result.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{model_id}/result")
def result(model_id: str) -> dict[str, Any]:
    res = get_path_result(model_id)
    if res is None:
        raise HTTPException(status_code=404, detail="Result not found")
    return res


@router.get("/{model_id}/versions")
def versions(model_id: str) -> dict[str, Any]:
    """报告版本对比：哪些连边受影响、哪些补报仍与旧报表版本分开。"""
    res = get_report_versions(model_id)
    if res is None:
        raise HTTPException(status_code=404, detail="Result not found")
    return res


@router.get("/{model_id}/common-paths")
def common_paths(model_id: str, min_count: int = 3) -> list[dict[str, Any]]:
    return find_common_paths(model_id, min_count)


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    row = store.find("behavior_path", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    action = payload.action
    row["status"] = action
    row["pending"] = False
    return ActionResult(ok=True, message=action, entry=row)
