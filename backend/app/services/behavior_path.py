"""行为路径分析引擎：基于「一次确认的会话明细」构建 Sankey 图并版本化报告。

报告不直接读原始事件，而是读会话切割落库的已确认明细快照。补报触发重算时：

* 会话明细整份换成新版本（或边界未变时沿用旧版本）；
* 只有显式重新分析才会产出新的报告版本，旧报告保持原样；
* 新旧报告之间能逐条看出哪些连边受影响、哪些补报仍保留在旧报表版本里。
"""
from __future__ import annotations

import datetime as _dt
from collections import defaultdict, Counter
from typing import Any

from app.store import store
from app.schemas import PathResult

PATH_MODULE = "behavior_path"
REPORT_ARTIFACT = "behavior_path"
BACKFILL_MODULE = "late_backfill"


def _event_category(event_id: str) -> str:
    event = store.find_by_field("event_track", "event_id", event_id)
    if event:
        return event.get("category", "unknown")
    return "unknown"


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_path_sankey(model_id: str, trigger: str = "manual") -> PathResult:
    """构建（或复用）行为路径的 Sankey 数据。

    复用条件：模型定义、会话明细版本、路径过滤都没有变化。否则报告版本 +1，
    并记录相对上一版的受影响连边与补报归属。
    """
    model = store.find_by_field(PATH_MODULE, "model_id", model_id)
    if model is None:
        raise ValueError(f"Path model not found: {model_id}")

    strategy_id = model.get("session_split_rule", "")
    detail = store.get_artifact("session_split_detail", strategy_id)
    if detail is None or not detail.get("confirmed"):
        raise ValueError(
            f"会话明细未确认，不能作为路径分析输入：{strategy_id or '未配置会话切割规则'}"
        )

    fingerprint = _fingerprint(model, detail)
    previous = store.get_artifact(REPORT_ARTIFACT, model_id)
    if previous and previous.get("fingerprint") == fingerprint:
        return PathResult(**{k: v for k, v in previous.items() if k != "fingerprint"})

    report = _compute_report(model, detail, previous, trigger, fingerprint)
    store.set_artifact(REPORT_ARTIFACT, model_id, {**report.model_dump(), "fingerprint": fingerprint})

    store.update(PATH_MODULE, model["id"], {
        "status": "已完成",
        "pending": False,
    })
    return report


def _fingerprint(model: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    """决定报告是否需要重新分析的输入指纹。"""
    return {
        "session_detail_version": int(detail.get("version", 0)),
        "session_detail_generated_at": detail.get("generated_at", ""),
        "start_event": model.get("start_event"),
        "target_event": model.get("target_event"),
        "path_depth": int(model.get("path_depth", 5)),
        "path_filter": model.get("path_filter", ""),
        "time_window": int(model.get("time_window", 86400)),
    }


def _compute_report(
    model: dict[str, Any],
    detail: dict[str, Any],
    previous: dict[str, Any] | None,
    trigger: str,
    fingerprint: dict[str, Any],
) -> PathResult:
    model_id = model["model_id"]
    start_event = model["start_event"]
    target_event = model["target_event"]
    path_depth = int(model.get("path_depth", 5))

    # 路径以「会话明细里的事件顺序」为准：同一会话内按时间排序，跨会话按开始时间排序。
    sessions = sorted(detail.get("sessions", []), key=lambda s: (s["user_id"], s["start_time"]))
    event_lookup = detail.get("event_index", {})

    user_paths: dict[str, list[list[dict[str, Any]]]] = defaultdict(list)
    for session in sessions:
        user_paths[session["user_id"]].append(session["events"])

    edge_counts: Counter[tuple[str, str]] = Counter()
    node_counts: Counter[str] = Counter()
    edge_sources: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(lambda: {"uids": set(), "users": set()})
    conversion_paths: list[list[str]] = []
    total_conversions = 0

    for user_id, user_sessions in user_paths.items():
        for events in user_sessions:
            path_events = events[:path_depth]
            if len(path_events) < 2:
                continue
            path = [e["event_id"] for e in path_events]
            for node in path:
                node_counts[node] += 1
            for i in range(len(path) - 1):
                edge = (path[i], path[i + 1])
                edge_counts[edge] += 1
                edge_sources[edge]["uids"].update(
                    [path_events[i]["event_uid"], path_events[i + 1]["event_uid"]]
                )
                edge_sources[edge]["users"].add(user_id)
            if target_event in path:
                conversion_paths.append(path)
                total_conversions += 1

    nodes: list[dict[str, Any]] = []
    node_index: dict[str, int] = {}
    for node, count in sorted(node_counts.items(), key=lambda item: (-item[1], item[0])):
        node_index[node] = len(nodes)
        nodes.append({"name": node, "value": count, "category": _event_category(node)})

    links: list[dict[str, Any]] = []
    for (source, target), count in sorted(edge_counts.items(), key=lambda item: (-item[1], item[0])):
        links.append({
            "source": node_index[source],
            "target": node_index[target],
            "value": count,
            "source_name": source,
            "target_name": target,
            "edge_key": f"{source}->{target}",
        })

    previous_version = int(previous["version"]) if previous and "version" in previous else None
    version = (previous_version or 0) + 1
    affected_edges = _diff_edges(previous, edge_counts, edge_sources)
    retired_edges = _retired_edges(previous, edge_counts)
    edge_summary = {
        "unchanged": sum(1 for e in affected_edges if e["change"] == "unchanged"),
        "shortened": sum(1 for e in affected_edges if e["change"] == "shortened"),
        "added": sum(1 for e in affected_edges if e["change"] == "added"),
        "removed": len(retired_edges),
    }
    backfills = _backfill_attribution(detail, model_id, version, edge_sources, start_event, target_event)

    return PathResult(
        model_id=model_id,
        start_event=start_event,
        target_event=target_event,
        sankey_nodes=nodes,
        sankey_links=links,
        conversion_paths=conversion_paths[:50],
        total_conversions=total_conversions,
        version=version,
        previous_version=previous_version,
        session_detail_version=int(detail.get("version", 0)),
        trigger=trigger,
        generated_at=_now_iso(),
        reused=False,
        affected_edges=affected_edges,
        retired_edges=retired_edges,
        edge_summary=edge_summary,
        backfills=backfills,
    )


def _prev_edge_keys(previous: dict[str, Any] | None) -> dict[str, int]:
    if not previous:
        return {}
    return {
        link.get("edge_key") or f"{link['source_name']}->{link['target_name']}": int(link["value"])
        for link in previous.get("sankey_links", [])
    }


def _diff_edges(
    previous: dict[str, Any] | None,
    edge_counts: Counter[tuple[str, str]],
    edge_sources: dict[tuple[str, str], dict[str, set[str]]],
) -> list[dict[str, Any]]:
    """逐条说明当前报告的连边相对上一版的变化。"""
    prev = _prev_edge_keys(previous)
    backfill_uids = {row["event_uid"] for row in store.rows(BACKFILL_MODULE) if row.get("raw_written")}
    rows: list[dict[str, Any]] = []
    for (source, target), count in sorted(edge_counts.items(), key=lambda item: (-item[1], item[0])):
        key = f"{source}->{target}"
        before = prev.get(key)
        if before is None:
            change = "added"
        elif before != count:
            change = "shortened" if count < before else "changed"
        else:
            change = "unchanged"
        rows.append({
            "edge_key": key,
            "source": source,
            "target": target,
            "value": count,
            "previous_value": before,
            "change": change,
            "backfill_uids": sorted(edge_sources[(source, target)]["uids"] & backfill_uids),
        })
    return rows


def _retired_edges(previous: dict[str, Any] | None, edge_counts: Counter[tuple[str, str]]) -> list[dict[str, Any]]:
    current = {f"{s}->{t}" for (s, t) in edge_counts}
    return [
        {"edge_key": key, "previous_value": value}
        for key, value in sorted(_prev_edge_keys(previous).items())
        if key not in current
    ]


def _is_backfill_uid(uid: str) -> bool:
    row = store.find_by_field(BACKFILL_MODULE, "event_uid", uid)
    return bool(row and row.get("raw_written"))


def _backfill_attribution(
    detail: dict[str, Any],
    model_id: str,
    report_version: int,
    edge_sources: dict[tuple[str, str], dict[str, set[str]]],
    start_event: str,
    target_event: str,
) -> list[dict[str, Any]]:
    """报告视角下每条补报事件的归属：已并入本版 / 仍与旧报表版本分开及原因。"""
    in_detail = set(detail.get("event_index", {}))
    rows: list[dict[str, Any]] = []
    for backfill in store.rows(BACKFILL_MODULE):
        uid = backfill["event_uid"]
        related_edges = sorted(
            key for key, sources in edge_sources.items() if uid in sources["uids"]
        )
        if uid not in in_detail:
            if not backfill.get("raw_written"):
                reason = f"补报状态为「{backfill.get('status', '待处理')}」，未写入原始事件"
            else:
                reason = "补报未落在本次会话明细的事件范围内"
            rows.append(_backfill_view(backfill, model_id, report_version, False, reason, []))
            continue
        if not related_edges:
            session_ids = sorted({detail["event_index"][uid]["session_id"]})
            reason = (
                f"已并入会话明细 v{detail['version']}（{'、'.join(session_ids)}），"
                "但该会话只有这一条事件或未进入路径深度，"
                f"未在 {start_event}→{target_event} 图上形成连边，仍与本报表版本分开"
            )
            rows.append(_backfill_view(backfill, model_id, report_version, False, reason, []))
            continue
        edge_labels = [f"{s}->{t}" for s, t in related_edges]
        rows.append(_backfill_view(
            backfill,
            model_id,
            report_version,
            True,
            f"已并入本版路径报告 v{report_version}，参与连边 {'、'.join(edge_labels)}",
            edge_labels,
        ))
    return rows


def _backfill_view(
    backfill: dict[str, Any],
    model_id: str,
    report_version: int,
    included: bool,
    reason: str,
    edges: list[str],
) -> dict[str, Any]:
    return {
        "event_uid": backfill["event_uid"],
        "user_id": backfill.get("user_id"),
        "event_id": backfill.get("event_id"),
        "status": backfill.get("status"),
        "included": included,
        "reason": reason,
        "affected_edges": edges,
        "report_model": model_id,
        "report_version": report_version,
    }


def rebuild_after_backfill(module: str, entry_id: int, round_no: int) -> list[dict[str, Any]]:
    """补报并进会话明细后，重新分析引用了这些明细的路径模型。

    每个模型的旧报告保持原版本不变，新报告版本 +1 并携带受影响连边清单。
    """
    row = store.find(module, entry_id)
    if row is None:
        raise ValueError(f"Backfill not found: {entry_id}")

    details = store.list_artifacts("session_split_detail")
    touched_strategies = {
        detail["strategy_id"]
        for detail in details
        if detail.get("confirmed") and row["event_uid"] in detail.get("event_index", {})
    }

    refreshed: list[dict[str, Any]] = []
    for model in store.rows(PATH_MODULE):
        if model.get("session_split_rule") not in touched_strategies:
            continue
        # 只升级已经发布过的报告；没跑过分析的模型不该因为一条补报凭空出现 v1。
        if store.get_artifact(REPORT_ARTIFACT, model["model_id"]) is None:
            continue
        report = build_path_sankey(model["model_id"], trigger=f"backfill:{row['event_uid']}")
        if report.reused:
            continue
        refreshed.append({
            "model_id": report.model_id,
            "new_version": report.version,
            "previous_version": report.previous_version,
            "round": round_no,
            "affected_edges": [e["edge_key"] for e in report.affected_edges if e["change"] != "unchanged"],
        })
    return refreshed


def get_path_result(model_id: str) -> dict[str, Any] | None:
    """获取（必要时构建）路径分析结果。"""
    try:
        result = build_path_sankey(model_id)
        return result.model_dump()
    except ValueError:
        return None


def get_report_versions(model_id: str) -> dict[str, Any] | None:
    """报告版本与上一版对比，供运营核对哪些连边实际受影响。"""
    current = store.get_artifact(REPORT_ARTIFACT, model_id)
    if current is None:
        try:
            build_path_sankey(model_id)
        except ValueError:
            return None
        current = store.get_artifact(REPORT_ARTIFACT, model_id)
    if current is None:
        return None
    return {
        "model_id": model_id,
        "version": current.get("version"),
        "previous_version": current.get("previous_version"),
        "session_detail_version": current.get("session_detail_version"),
        "trigger": current.get("trigger"),
        "generated_at": current.get("generated_at"),
        "edge_summary": current.get("edge_summary", {}),
        "affected_edges": current.get("affected_edges", []),
        "retired_edges": current.get("retired_edges", []),
        "backfills": current.get("backfills", []),
    }


def find_common_paths(model_id: str, min_count: int = 3) -> list[dict[str, Any]]:
    """查找高频路径模式。"""
    result = get_path_result(model_id)
    if result is None:
        return []

    path_counter: Counter[tuple[str, ...]] = Counter()
    for path in result.get("conversion_paths", []):
        path_counter[tuple(path)] += 1

    return [
        {"path": list(path), "count": count}
        for path, count in path_counter.most_common(20)
        if count >= min_count
    ]
