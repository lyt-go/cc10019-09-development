"""迟到事件补报：接收迟到埋点、幂等落库，并驱动会话明细重算与路径报告版本化。

三条不变量：

1. 幂等：同一 ``event_uid`` 的重复补报只保留一份原始记录，重试返回首次的结果。
2. 原子：一次重算先基于「已确认」的会话明细（还包含新会话的旧状态）算出完整新明细，
   全部成功后一次性替换；中途失败不留下半新半旧的边界。
3. 隔离：重算只推进会话明细，已发布的路径报告不被原地改写，需要显式重新分析才会生成新版本。
"""
from __future__ import annotations

import copy
import datetime as _dt
from typing import Any

from app.store import store

MODULE = "late_backfill"

# 迟到事件状态机：失败可重试；已回填/已并入会话/已并入报告是终态，重复提交只回显。
STATUS_PENDING = "待处理"
STATUS_FAILED = "回填失败"
STATUS_APPLIED = "已回填"
STATUS_SEGMENTED = "已并入会话"
STATUS_REPORTED = "已并入报告"
STATUS_NEEDS_REBUILD = "待重算"


# ========== 时间工具 ==========

def _parse_ts(value: Any) -> int | None:
    """把秒级时间戳或 ISO8601 字符串转成整数秒；无法解析返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return int(text)
        candidate = text.replace("Z", "+00:00")
        try:
            parsed = _dt.datetime.fromisoformat(candidate)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.timezone.utc)
        return int(parsed.timestamp())
    return None


# ========== 事件记录 ==========

def list_backfills() -> list[dict[str, Any]]:
    """列出全部补报记录（按首次到达顺序）。"""
    return store.rows(MODULE)


def get_backfill(event_uid: str) -> dict[str, Any] | None:
    """按业务事件号查询补报记录。"""
    return store.find_by_field(MODULE, "event_uid", event_uid)


def _same_event(row: dict[str, Any], payload: dict[str, Any], ts: int) -> bool:
    """判断再次提交的载荷是否与首次登记完全一致。"""
    return (
        row.get("user_id") == payload["user_id"]
        and int(row.get("event_ts", 0)) == ts
        and row.get("event_id") == payload["event_id"]
    )


def submit_backfill(payload: dict[str, Any]) -> dict[str, Any]:
    """接收一条迟到事件并尝试回填。

    重复提交相同 ``event_uid`` 不会写出第二份原始记录：载荷一致时返回首次结果并
    标记为重复提交，载荷冲突时拒绝覆盖已到达的事实。
    """
    normalized = _normalize_payload(payload)
    existing = get_backfill(normalized["event_uid"])
    if existing is not None:
        if not _same_event(existing, normalized, normalized["event_ts"]):
            raise ValueError(
                f"事件 {normalized['event_uid']} 已存在且内容不同，不能覆盖已到达的原始事件"
            )
        return _duplicate_result(existing)

    inserted = store.insert(MODULE, {
        "event_uid": normalized["event_uid"],
        "user_id": normalized["user_id"],
        "event_id": normalized["event_id"],
        "event_ts": normalized["event_ts"],
        "received_at": normalized["received_at"],
        "device_id": normalized["device_id"],
        "channel": normalized["channel"],
        "page_url": normalized["page_url"],
        "path_model": normalized["path_model"],
        "status": STATUS_PENDING,
        "pending": True,
        "abnormal": False,
        "simulate_failure": normalized["simulate_failure"],
        "raw_written": False,
        "attempts": 1,
        "related_objects": _dedupe(list(normalized["related_objects"])),
        "rebuild_status": "未开始",
        "rebuild_round": 0,
        "rebuild_ids": [],
        "error_log": "",
        "message": "",
    })
    # store.insert 存的是副本，后续状态要改在仓库里的那一份上。
    return _finish_submission(store.find(MODULE, inserted["id"]))


def retry_backfill(event_uid: str) -> dict[str, Any]:
    """失败后重试同一事件；成功回填过的记录直接按重复提交返回。"""
    row = get_backfill(event_uid)
    if row is None:
        raise ValueError(f"补报事件不存在: {event_uid}")
    if row["status"] in (STATUS_APPLIED, STATUS_SEGMENTED, STATUS_REPORTED):
        return _duplicate_result(row)
    row["attempts"] = int(row.get("attempts", 0)) + 1
    row["pending"] = True
    row["error_log"] = ""
    # simulate_failure 只是首次写入的测试开关，重试按真实写入执行。
    row["simulate_failure"] = False
    return _finish_submission(row, is_retry=True)


def _normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """校验并规整补报入参，缺字段时抛出可读错误。"""
    event_uid = str(payload.get("event_uid") or "").strip()
    user_id = str(payload.get("user_id") or "").strip()
    event_id = str(payload.get("event_id") or "").strip()
    if not event_uid:
        raise ValueError("event_uid 不能为空")
    if not user_id:
        raise ValueError("user_id 不能为空")
    if not event_id:
        raise ValueError("event_id 不能为空")
    event_ts = _parse_ts(payload.get("event_ts", payload.get("timestamp")))
    if event_ts is None:
        raise ValueError("event_ts 需要秒级时间戳或 ISO8601 时间")
    received_ts = _parse_ts(payload.get("received_at")) or int(_dt.datetime.now(_dt.timezone.utc).timestamp())
    related = payload.get("related_objects") or []
    if isinstance(related, str):
        related = [related]
    related = [str(item) for item in related if str(item).strip()]
    return {
        "event_uid": event_uid,
        "user_id": user_id,
        "event_id": event_id,
        "event_ts": event_ts,
        "received_at": received_ts,
        "device_id": payload.get("device_id") or None,
        "channel": payload.get("channel") or None,
        "page_url": payload.get("page_url") or None,
        "path_model": payload.get("path_model") or None,
        "simulate_failure": bool(payload.get("simulate_failure")),
        "related_objects": related,
    }


def _dedupe(items: list[Any]) -> list[Any]:
    seen: list[Any] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def _finish_submission(row: dict[str, Any], is_retry: bool = False) -> dict[str, Any]:
    """执行回填动作；失败时把记录留在可重试状态而不是写入会话明细。"""
    # 延迟导入，避免 behavior_path → late_backfill 的循环依赖。
    from app.services import behavior_path as path_service
    from app.services import session_split as session_service

    if row.get("simulate_failure"):
        row.update({
            "status": STATUS_FAILED,
            "pending": True,
            "abnormal": True,
            "raw_written": False,
            "error_log": "回填写入失败（模拟），可重试同一事件",
        })
        return _summary(row, duplicated=False, retried=is_retry)

    # 原始事件在这里才真正落库；重试复用同一 event_uid，天然只保留一份。
    row["raw_written"] = True

    if not _has_confirmed_history():
        # 还没有任何已确认的会话明细：补报仍然落库，只是没有历史边界需要改写。
        row.update({
            "status": STATUS_APPLIED,
            "pending": False,
            "abnormal": False,
            "error_log": "",
            "message": "尚无已确认的会话明细，迟到事件已落库；下一次会话切割会直接包含它",
        })
        return _summary(row, duplicated=False, retried=is_retry)

    try:
        outcome = session_service.rebuild_sessions_for_backfill(MODULE, row["id"])
    except Exception as exc:  # noqa: BLE001 - 失败要落到记录上供运营重试
        # 重算失败时原始事件也不保留：整条补报回滚到未写入，等待重试，
        # 避免「事件已在原始流里、会话明细却还是旧边界」的半成品。
        row.update({
            "status": STATUS_FAILED,
            "pending": True,
            "abnormal": True,
            "raw_written": False,
            "error_log": f"会话明细重算失败：{exc}",
        })
        return _summary(row, duplicated=False, retried=is_retry)

    row.update({
        "status": STATUS_SEGMENTED if outcome["strategy_ids"] else STATUS_APPLIED,
        "pending": False,
        "abnormal": False,
        "error_log": "",
        "rebuild_status": "已完成" if outcome["strategy_ids"] else "无需重算",
        "rebuild_round": outcome["round"],
        "rebuild_ids": outcome["session_ids"],
        "message": outcome["message"],
    })
    if not outcome["strategy_ids"]:
        # 该用户没有任何已确认的会话明细，补报只落原始事件，不牵动报告。
        return _summary(row, duplicated=False, retried=is_retry)
    # 重算后自动刷新受影响的路径报告，让运营能在报告里看到新连边。
    try:
        refreshes = path_service.rebuild_after_backfill(MODULE, row["id"], outcome["round"])
        row["rebuild_ids"] = _dedupe(
            list(outcome["session_ids"]) + [r["model_id"] for r in refreshes]
        )
        if refreshes:
            row["status"] = STATUS_REPORTED
        row["message"] = f"{outcome['message']}；{_describe_refreshes(refreshes)}"
    except Exception as exc:  # noqa: BLE001 - 报告刷新失败不回滚会话明细，记录原因
        row["abnormal"] = True
        row["status"] = STATUS_NEEDS_REBUILD
        row["message"] = f"{outcome['message']}；路径报告刷新失败：{exc}"
    return _summary(row, duplicated=False, retried=is_retry)


def _describe_refreshes(refreshes: list[dict[str, Any]]) -> str:
    if not refreshes:
        return "没有路径报告需要重新分析"
    parts = [
        f"{item['model_id']} v{item['previous_version']}→v{item['new_version']}"
        for item in refreshes
    ]
    return "路径报告已升级：" + "、".join(parts)


def _has_confirmed_history() -> bool:
    """是否已经有被下游消费过的会话明细。"""
    return any(
        detail.get("confirmed")
        for detail in store.list_artifacts("session_split_detail")
    )


def _duplicate_result(row: dict[str, Any]) -> dict[str, Any]:
    """重复提交：只回显首次结果，不新建记录、不再次重算。"""
    return {
        "ok": True,
        "duplicated": True,
        "status": row["status"],
        "message": f"同一事件 {row['event_uid']} 已接收（{row['status']}），未重复写入",
        "backfill": _project(row),
    }


def _project(row: dict[str, Any]) -> dict[str, Any]:
    """对外投影：去掉内部模拟开关。"""
    public = {k: copy.deepcopy(v) for k, v in row.items() if k != "simulate_failure"}
    return public


def _summary(row: dict[str, Any], duplicated: bool, retried: bool) -> dict[str, Any]:
    return {
        "ok": True,
        "duplicated": duplicated,
        "retried": retried,
        "status": row["status"],
        "message": row.get("message") or row.get("error_log") or row["status"],
        "backfill": _project(row),
    }
