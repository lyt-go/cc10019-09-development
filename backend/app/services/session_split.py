"""会话切割引擎：按超时阈值和跨天/跨渠道策略切割用户会话。

切割结果不只存一份摘要（``SessionResult``），还会落一份**会话明细**
（版本号 + 确认标记 + 事件 -> 会话归属索引）。行为路径分析只读取这份已确认的
明细快照，因此补报后重算时要么整份换成新版本，要么继续沿用旧版本，不会出现
一半旧边界、一半连到未完成新会话的中间态。
"""
from __future__ import annotations

import copy
import datetime as _dt
from typing import Any
from collections import defaultdict

from app.store import store
from app.schemas import SessionResult

# 与迟到回填服务共享的表名；这里只读，不反向依赖该服务。
BACKFILL_MODULE = "late_backfill"

BASE_TS = 1794297600  # 2026-11-10T08:00:00Z，与示例路径模型的分析区间对齐


def _uid(user_id: str, ordinal: int) -> str:
    return f"EVT-{user_id}-{ordinal:03d}"


def _generate_raw_events() -> list[dict[str, Any]]:
    """生成确定性的原始事件流。

    绝大多数用户每 5 分钟一个事件，落在同一个会话里；``user_idx % 7 == 0`` 的用户
    被刻意安排成「两段会话 + 一个可被迟到事件弥合的间隔」（间隔 2880 秒，阈值 1800 秒），
    用于演示迟到事件把两段历史会话合并成一段。
    """
    events: list[dict[str, Any]] = []
    for user_idx in range(1, 51):
        user_id = f"U{user_idx:05d}"
        device_id = f"D{user_idx % 10 + 1:03d}"
        channel = "app" if user_idx % 2 else "web"
        num_events = 3 + user_idx % 6
        base = BASE_TS + (user_idx % 4) * 1800
        two_segments = user_idx % 7 == 0

        for ev_idx in range(num_events):
            if two_segments and ev_idx >= 2:
                offset = 3000 + (ev_idx - 2) * 120
            else:
                offset = ev_idx * (120 if two_segments else 300)

            if ev_idx == 0:
                event_id = "EV-001"
            elif ev_idx == num_events - 1 and user_idx % 3 == 0:
                event_id = "EV-003"
            else:
                event_id = ["EV-001", "EV-002", "EV-003", "EV-004"][(user_idx + ev_idx) % 4]

            events.append({
                "event_uid": _uid(user_id, ev_idx),
                "user_id": user_id,
                "device_id": device_id,
                "event_id": event_id,
                "timestamp": base + offset,
                "channel": channel,
                "page_url": f"/page{ev_idx % 5 + 1}",
                "source": "raw",
            })
    return events


def _backfill_event(row: dict[str, Any]) -> dict[str, Any]:
    """把一条已成功写入的补报记录转成原始事件。"""
    return {
        "event_uid": row["event_uid"],
        "user_id": row["user_id"],
        "device_id": row.get("device_id"),
        "event_id": row["event_id"],
        "timestamp": int(row["event_ts"]),
        "channel": row.get("channel"),
        "page_url": row.get("page_url"),
        "source": "backfill",
    }


def _written_backfills() -> list[dict[str, Any]]:
    """已成功写入原始事件的补报记录；失败/待处理的不参与切割。"""
    return [row for row in store.rows(BACKFILL_MODULE) if row.get("raw_written")]


def load_raw_events() -> list[dict[str, Any]]:
    """原始事件流 = 常规事件 + 已成功写入的补报事件。"""
    events = _generate_raw_events()
    events.extend(_backfill_event(row) for row in _written_backfills())
    return sorted(events, key=lambda e: (e["timestamp"], e["event_uid"]))


def sessionize_events(
    strategy: dict[str, Any],
    events: list[dict[str, Any]],
    version: int = 0,
) -> list[dict[str, Any]]:
    """按策略参数把事件切成会话，返回带稳定事件归属的会话列表。"""
    timeout_sec = int(strategy["timeout_sec"])
    cross_day_policy = strategy.get("cross_day_policy", "keep")
    cross_channel_policy = strategy.get("cross_channel_policy", "merge")
    session_key = strategy.get("session_key", "user_id")
    strategy_id = strategy["strategy_id"]

    keyed: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        key = event.get(session_key) or event.get("user_id")
        keyed[str(key)].append(event)

    sessions: list[dict[str, Any]] = []
    for key in sorted(keyed):
        evts = sorted(keyed[key], key=lambda e: (e["timestamp"], e["event_uid"]))
        current: list[dict[str, Any]] = []

        def close() -> None:
            if not current:
                return
            sessions.append({
                "session_id": "",  # 收集完统一定号，保证顺序稳定
                "strategy_id": strategy_id,
                "session_key": key,
                "user_id": current[0]["user_id"],
                "events": [dict(e) for e in current],
                "event_uids": [e["event_uid"] for e in current],
                "event_count": len(current),
                "start_time": current[0]["timestamp"],
                "end_time": current[-1]["timestamp"],
                "duration": current[-1]["timestamp"] - current[0]["timestamp"],
            })
            current.clear()

        for event in evts:
            if not current:
                current.append(event)
                continue
            prev = current[-1]
            gap = event["timestamp"] - prev["timestamp"]
            should_split = gap > timeout_sec

            if not should_split and cross_day_policy == "split":
                if prev["timestamp"] // 86400 != event["timestamp"] // 86400:
                    should_split = True

            # 渠道缺失时不做跨渠道判定，避免补报把一段会话误切碎。
            if not should_split and cross_channel_policy == "split":
                prev_channel = prev.get("channel")
                curr_channel = event.get("channel")
                if prev_channel and curr_channel and prev_channel != curr_channel:
                    should_split = True

            if should_split:
                close()
            current.append(event)
        close()

    prefix = f"{strategy_id}-v{version}"
    for idx, session in enumerate(sessions, start=1):
        session["session_id"] = f"{prefix}-S{idx:03d}"
    return sessions


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _compute_detail(strategy: dict[str, Any], version: int) -> dict[str, Any]:
    """完整算出一份新明细；纯函数部分，失败时不会碰到已确认的旧明细。"""
    events = load_raw_events()
    sessions = sessionize_events(strategy, events, version)

    event_index: dict[str, dict[str, Any]] = {}
    for session in sessions:
        for event in session["events"]:
            event_index[event["event_uid"]] = {
                "session_id": session["session_id"],
                "session_key": session["session_key"],
                "user_id": event["user_id"],
                "event_id": event["event_id"],
                "timestamp": event["timestamp"],
                "source": event.get("source", "raw"),
            }

    applied = sorted(
        row["event_uid"] for row in _written_backfills() if row["event_uid"] in event_index
    )
    return {
        "strategy_id": strategy["strategy_id"],
        "version": version,
        "confirmed": True,
        "timeout_sec": int(strategy["timeout_sec"]),
        "session_key": strategy.get("session_key", "user_id"),
        "total_events": len(events),
        "session_count": len(sessions),
        "sessions": sessions,
        "event_index": event_index,
        "session_keys": sorted({s["session_key"] for s in sessions}),
        "applied_backfills": applied,
        "generated_at": _now_iso(),
    }


def _signature(detail: dict[str, Any]) -> list[tuple[Any, ...]]:
    """用于判断重算是否真的改变了会话边界（忽略版本号与生成时间）。"""
    return [
        (
            s["session_key"],
            s["start_time"],
            s["end_time"],
            tuple(s["event_uids"]),
        )
        for s in detail.get("sessions", [])
    ]


def _find_strategy(strategy_id: str) -> dict[str, Any]:
    strategy = store.find_by_field("session_split", "strategy_id", strategy_id)
    if strategy is None:
        raise ValueError(f"Strategy not found: {strategy_id}")
    return strategy


def _legacy_result(detail: dict[str, Any]) -> SessionResult:
    sessions = detail["sessions"]
    avg_duration = sum(s["duration"] for s in sessions) / len(sessions) if sessions else 0
    return SessionResult(
        strategy_id=detail["strategy_id"],
        total_events=detail["total_events"],
        session_count=detail["session_count"],
        avg_session_duration=round(avg_duration, 2),
        sessions=[
            {
                "session_id": s["session_id"],
                "session_key": s["session_key"],
                "event_count": s["event_count"],
                "start_time": s["start_time"],
                "end_time": s["end_time"],
                "duration": s["duration"],
            }
            for s in sessions[:20]
        ],
    )


def split_sessions(strategy_id: str) -> SessionResult:
    """执行会话切割并确认一份新的会话明细（边界未变时沿用旧版本）。"""
    strategy = _find_strategy(strategy_id)
    existing = store.get_artifact("session_split_detail", strategy_id)

    next_version = int(existing["version"]) + 1 if existing else 1
    candidate = _compute_detail(strategy, next_version)

    if existing and existing.get("confirmed") and _signature(existing) == _signature(candidate):
        detail = existing
    else:
        detail = candidate

    # 整份替换：只有完整算完才写入，读方永远看到一份自洽的明细。
    store.set_artifact("session_split_detail", strategy_id, detail)
    result = _legacy_result(detail)
    store.set_artifact("session_split", strategy_id, result.model_dump())

    store.update("session_split", strategy["id"], {
        "status": "已完成",
        "pending": False,
    })
    return result


def get_session_result(strategy_id: str) -> dict[str, Any] | None:
    """获取会话切割摘要结果。"""
    artifact = store.get_artifact("session_split", strategy_id)
    if artifact:
        return artifact
    try:
        return split_sessions(strategy_id).model_dump()
    except ValueError:
        return None


def get_session_detail(strategy_id: str) -> dict[str, Any] | None:
    """获取已确认的会话明细，并附带每条补报事件的归属与未并入原因。"""
    detail = store.get_artifact("session_split_detail", strategy_id)
    if detail is None:
        if store.find_by_field("session_split", "strategy_id", strategy_id) is None:
            return None
        split_sessions(strategy_id)
        detail = store.get_artifact("session_split_detail", strategy_id)
    if detail is None:
        return None

    view = copy.deepcopy(detail)
    view["backfills"] = _backfill_attribution(view)
    return view


def _backfill_attribution(detail: dict[str, Any]) -> list[dict[str, Any]]:
    """逐条说明补报事件落在哪个会话；未并入的给出可核对的原因。"""
    index = detail.get("event_index", {})
    items: list[dict[str, Any]] = []
    for row in store.rows(BACKFILL_MODULE):
        uid = row["event_uid"]
        entry = index.get(uid)
        if not row.get("raw_written"):
            reason = f"补报当前状态为「{row.get('status', '待处理')}」，未写入原始事件，不进入会话明细"
        elif entry is None:
            reason = "事件不在本次会话明细覆盖的事件范围内"
        else:
            reason = f"已并入会话明细 v{detail['version']}，归属会话 {entry['session_id']}"
        items.append({
            "event_uid": uid,
            "user_id": row.get("user_id"),
            "event_id": row.get("event_id"),
            "event_ts": row.get("event_ts"),
            "status": row.get("status"),
            "included": entry is not None,
            "session_ids": [entry["session_id"]] if entry else [],
            "reason": reason,
        })
    return items


def rebuild_sessions_for_backfill(module: str, entry_id: int) -> dict[str, Any]:
    """把一条迟到补报并进受影响的会话明细，逐策略整体重算后原子替换。

    只有已经确认过明细、且明细里出现过该用户的策略才算受影响；重算结果与旧版
    一致时沿用旧版本。任何一份策略重算抛错都不会提交半成品。
    """
    row = store.find(module, entry_id)
    if row is None:
        raise ValueError(f"Backfill not found: {entry_id}")
    if not row.get("raw_written"):
        return {"round": int(row.get("rebuild_round", 0)), "strategy_ids": [], "session_ids": [], "message": "补报尚未成功写入，未触发会话重算"}

    user_id = row["user_id"]
    details = store.list_artifacts("session_split_detail")
    affected = [
        detail for detail in details
        if detail.get("confirmed") and any(s.get("user_id") == user_id for s in detail.get("sessions", []))
    ]
    if not affected:
        return {
            "round": int(row.get("rebuild_round", 0)),
            "strategy_ids": [],
            "session_ids": [],
            "message": "该用户没有已确认的会话明细，无需重算",
        }

    new_details: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for detail in affected:
        strategy = _find_strategy(detail["strategy_id"])
        rebuilt = _compute_detail(strategy, int(detail["version"]) + 1)
        if _signature(rebuilt) == _signature(detail):
            rebuilt = detail
        new_details.append((detail, rebuilt))

    # 全部算完再提交，逐份整替换。
    session_ids: list[str] = []
    for _, rebuilt in new_details:
        store.set_artifact("session_split_detail", rebuilt["strategy_id"], rebuilt)
        store.set_artifact("session_split", rebuilt["strategy_id"], _legacy_result(rebuilt).model_dump())
        entry = rebuilt["event_index"].get(row["event_uid"])
        if entry:
            session_ids.append(entry["session_id"])

    rebuilt_version = max(int(item[1]["version"]) for item in new_details)
    merged = bool(session_ids)
    message = (
        f"会话明细已重算至 v{rebuilt_version}，补报事件并入 {', '.join(session_ids)}"
        if merged
        else f"会话明细已重算至 v{rebuilt_version}，补报事件未改变既有会话边界"
    )
    return {
        "round": int(row.get("rebuild_round", 0)) + 1,
        "version": rebuilt_version,
        "strategy_ids": [item[1]["strategy_id"] for item in new_details],
        "session_ids": session_ids,
        "message": message,
    }


def resegment_with_strategy(strategy_id: str, new_timeout: int | None = None) -> SessionResult:
    """使用新参数重新切割会话。"""
    strategy = _find_strategy(strategy_id)
    if new_timeout is not None:
        store.update("session_split", strategy["id"], {"timeout_sec": new_timeout})
        store.invalidate_compute_cache(f"session_split:{strategy_id}")
    return split_sessions(strategy_id)
