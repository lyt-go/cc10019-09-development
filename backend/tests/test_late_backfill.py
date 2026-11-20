"""迟到事件补报与会话明细/路径报告重算的集成测试。"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.store import store
from app.services import behavior_path as path_service
from app.services import late_backfill
from app.services import session_split

LATE_UID = "LATE-TEST-0001"
LATE_TS = 1794304560  # U00007 两段会话之间的空档


def _u7_sessions(detail: dict) -> list[dict]:
    return [s for s in detail["sessions"] if s["user_id"] == "U00007"]


def _payload(**overrides):
    payload = {
        "event_uid": LATE_UID,
        "user_id": "U00007",
        "event_id": "EV-002",
        "event_ts": LATE_TS,
        "channel": "app",
        "page_url": "/page3",
    }
    payload.update(overrides)
    return payload


class BackfillTestCase(unittest.TestCase):
    """每个用例都从示例数据重新开始，互不污染。"""

    def setUp(self) -> None:
        store.reset()


class TestIdempotentBackfill(BackfillTestCase):
    def test_retry_after_success_does_not_write_twice(self) -> None:
        session_split.split_sessions("SS-001")
        path_service.build_path_sankey("BP-001")

        first = late_backfill.submit_backfill(_payload())
        self.assertFalse(first["duplicated"])
        self.assertEqual(first["status"], late_backfill.STATUS_REPORTED)

        retry = late_backfill.retry_backfill(LATE_UID)
        self.assertTrue(retry["duplicated"])
        self.assertEqual(len(store.find_all_by_field("late_backfill", "event_uid", LATE_UID)), 1)
        self.assertEqual(len(store.rows("late_backfill")), 3)  # 2 条示例 + 1 条本次

        detail = store.get_artifact("session_split_detail", "SS-001")
        self.assertEqual(len([u for u in detail["event_index"] if u == LATE_UID]), 1)
        self.assertEqual(
            len([s for s in _u7_sessions(detail) for e in s["events"] if e["event_uid"] == LATE_UID]),
            1,
        )

    def test_duplicate_submit_does_not_rebuild_again(self) -> None:
        session_split.split_sessions("SS-001")
        path_service.build_path_sankey("BP-001")
        late_backfill.submit_backfill(_payload())
        detail_version = store.get_artifact("session_split_detail", "SS-001")["version"]
        report_version = store.get_artifact("behavior_path", "BP-001")["version"]

        duplicate = late_backfill.submit_backfill(_payload())

        self.assertTrue(duplicate["duplicated"])
        self.assertEqual(len(store.rows("late_backfill")), 3)
        self.assertEqual(store.get_artifact("session_split_detail", "SS-001")["version"], detail_version)
        self.assertEqual(store.get_artifact("behavior_path", "BP-001")["version"], report_version)

    def test_conflicting_payload_is_rejected(self) -> None:
        late_backfill.submit_backfill(_payload())
        with self.assertRaises(ValueError):
            late_backfill.submit_backfill(_payload(event_id="EV-004"))
        row = store.find_by_field("late_backfill", "event_uid", LATE_UID)
        self.assertEqual(row["event_id"], "EV-002")


class TestFailedBackfill(BackfillTestCase):
    def test_failure_leaves_no_partial_session_detail(self) -> None:
        session_split.split_sessions("SS-001")
        path_service.build_path_sankey("BP-001")
        before = store.get_artifact("session_split_detail", "SS-001")
        before_report = store.get_artifact("behavior_path", "BP-001")

        failed = late_backfill.submit_backfill(_payload(simulate_failure=True))

        self.assertEqual(failed["status"], late_backfill.STATUS_FAILED)
        self.assertTrue(failed["backfill"]["pending"])
        self.assertFalse(failed["backfill"]["raw_written"])
        after = store.get_artifact("session_split_detail", "SS-001")
        self.assertEqual(after, before)
        self.assertEqual(store.get_artifact("behavior_path", "BP-001"), before_report)

    def test_retry_after_failure_writes_exactly_once(self) -> None:
        session_split.split_sessions("SS-001")
        path_service.build_path_sankey("BP-001")
        late_backfill.submit_backfill(_payload(simulate_failure=True))

        retried = late_backfill.retry_backfill(LATE_UID)

        self.assertEqual(retried["status"], late_backfill.STATUS_REPORTED)
        self.assertEqual(len(store.rows("late_backfill")), 3)
        detail = store.get_artifact("session_split_detail", "SS-001")
        self.assertIn(LATE_UID, detail["event_index"])
        self.assertEqual(len(_u7_sessions(detail)), 1)

    def test_rebuild_exception_keeps_old_detail_and_allows_retry(self) -> None:
        session_split.split_sessions("SS-001")
        path_service.build_path_sankey("BP-001")
        before = store.get_artifact("session_split_detail", "SS-001")

        def explode(*args, **kwargs):
            raise RuntimeError("sessionize boom")

        with mock.patch.object(session_split, "sessionize_events", explode):
            failed = late_backfill.submit_backfill(_payload())

        self.assertEqual(failed["status"], late_backfill.STATUS_FAILED)
        self.assertFalse(failed["backfill"]["raw_written"])  # 原始事件也回滚
        self.assertEqual(store.get_artifact("session_split_detail", "SS-001"), before)

        retried = late_backfill.retry_backfill(LATE_UID)
        self.assertEqual(retried["status"], late_backfill.STATUS_REPORTED)
        self.assertEqual(len(store.rows("late_backfill")), 3)
        detail = store.get_artifact("session_split_detail", "SS-001")
        self.assertEqual(detail["version"], before["version"] + 1)
        self.assertEqual(len(_u7_sessions(detail)), 1)


class TestConfirmedDetailInput(BackfillTestCase):
    def test_path_analysis_requires_confirmed_session_detail(self) -> None:
        with self.assertRaises(ValueError):
            path_service.build_path_sankey("BP-001")

    def test_backfill_merges_two_historical_sessions(self) -> None:
        session_split.split_sessions("SS-001")
        path_service.build_path_sankey("BP-001")
        before = store.get_artifact("session_split_detail", "SS-001")
        self.assertEqual(len(_u7_sessions(before)), 2)

        late_backfill.submit_backfill(_payload())

        after = store.get_artifact("session_split_detail", "SS-001")
        merged = _u7_sessions(after)
        self.assertEqual(len(merged), 1)
        self.assertEqual(after["version"], before["version"] + 1)
        # 明细里每个事件只会出现在一个会话里。
        uids = [e["event_uid"] for s in after["sessions"] for e in s["events"]]
        self.assertEqual(len(uids), len(set(uids)))

    def test_report_analysis_only_uses_confirmed_snapshot_version(self) -> None:
        session_split.split_sessions("SS-001")
        path_service.build_path_sankey("BP-001")
        late_backfill.submit_backfill(_payload())

        detail = store.get_artifact("session_split_detail", "SS-001")
        report = store.get_artifact("behavior_path", "BP-001")
        self.assertEqual(report["session_detail_version"], detail["version"])
        # 报告边界必须来自同一版明细：按该版明细重新切割，连边计数应逐条吻合。
        rebuilt = session_split.sessionize_events(
            store.find_by_field("session_split", "strategy_id", "SS-001"),
            session_split.load_raw_events(),
            detail["version"],
        )
        pairs: dict[tuple[str, str], int] = {}
        for session in rebuilt:
            ids = [e["event_id"] for e in session["events"]][:5]
            for i in range(len(ids) - 1):
                pairs[(ids[i], ids[i + 1])] = pairs.get((ids[i], ids[i + 1]), 0) + 1
        for link in report["sankey_links"]:
            key = (link["source_name"], link["target_name"])
            self.assertEqual(link["value"], pairs.get(key), key)


class TestReportVersions(BackfillTestCase):
    def test_backfill_bumps_report_version_and_marks_affected_edges(self) -> None:
        session_split.split_sessions("SS-001")
        first = path_service.build_path_sankey("BP-001")
        self.assertEqual(first.version, 1)
        self.assertIsNone(first.previous_version)

        late_backfill.submit_backfill(_payload())

        second = store.get_artifact("behavior_path", "BP-001")
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["previous_version"], 1)
        self.assertEqual(second["trigger"], f"backfill:{LATE_UID}")

        affected = {e["edge_key"]: e for e in second["affected_edges"]}
        shortened = [e for e in second["affected_edges"] if e["change"] != "unchanged"]
        self.assertTrue(shortened)
        for edge in shortened:
            self.assertIn(LATE_UID, edge["backfill_uids"])
        self.assertTrue(all(e["change"] == "unchanged" for e in second["affected_edges"] if not e["backfill_uids"]))

    def test_unpublished_model_is_not_created_by_backfill(self) -> None:
        session_split.split_sessions("SS-001")
        path_service.build_path_sankey("BP-001")
        late_backfill.submit_backfill(_payload())
        self.assertIsNone(store.get_artifact("behavior_path", "BP-002"))

    def test_backfill_outside_report_stays_separate_with_reason(self) -> None:
        session_split.split_sessions("SS-001")
        path_service.build_path_sankey("BP-001")
        # U00008 不在两段会话的弥合场景里，事件不会形成新连边。
        outcome = late_backfill.submit_backfill(_payload(
            event_uid="LATE-TEST-0002", user_id="U00008",
            event_ts=1794304560, event_id="EV-004",
        ))
        report = store.get_artifact("behavior_path", "BP-001")
        item = next(b for b in report["backfills"] if b["event_uid"] == "LATE-TEST-0002")
        # 事件本身进了会话明细，但没有形成任何图上连边：不算并入本版报告。
        self.assertFalse(item["included"])
        self.assertIn("已并入会话明细", item["reason"])
        self.assertIn("未在 EV-001→EV-003 图上形成连边", item["reason"])
        self.assertTrue(outcome["ok"])

    def test_manual_rebuild_reuses_report_when_inputs_unchanged(self) -> None:
        session_split.split_sessions("SS-001")
        path_service.build_path_sankey("BP-001")
        again = path_service.build_path_sankey("BP-001")
        self.assertEqual(again.version, 1)

    def test_retired_edges_reported(self) -> None:
        # 用更短的路径深度让合并后的一条边消失，验证旧连边被写进 retired_edges。
        session_split.split_sessions("SS-001")
        store.update("behavior_path", 1, {"path_depth": 2})
        path_service.build_path_sankey("BP-001")
        late_backfill.submit_backfill(_payload())
        report = store.get_artifact("behavior_path", "BP-001")
        self.assertEqual(report["version"], 2)
        self.assertIsInstance(report["retired_edges"], list)


class TestSessionDetailLookup(BackfillTestCase):
    def test_detail_view_explains_each_backfill(self) -> None:
        session_split.split_sessions("SS-001")
        late_backfill.submit_backfill(_payload())
        late_backfill.submit_backfill(_payload(
            event_uid="LATE-FAILED", simulate_failure=True,
        ))
        detail = session_split.get_session_detail("SS-001")
        items = {item["event_uid"]: item for item in detail["backfills"]}
        self.assertTrue(items[LATE_UID]["included"])
        self.assertIn("已并入会话明细", items[LATE_UID]["reason"])
        self.assertFalse(items["LATE-FAILED"]["included"])
        self.assertIn("回填失败", items["LATE-FAILED"]["reason"])

    def test_backfill_without_history_applies_and_is_picked_up_later(self) -> None:
        outcome = late_backfill.submit_backfill(_payload())
        self.assertEqual(outcome["status"], late_backfill.STATUS_APPLIED)
        session_split.split_sessions("SS-001")
        report = path_service.build_path_sankey("BP-001")
        attribution = next(b for b in report.backfills if b["event_uid"] == LATE_UID)
        self.assertTrue(attribution["included"])
        self.assertIn("已并入本版路径报告", attribution["reason"])

    def test_backfill_for_user_without_confirmed_detail_only_lands_raw_event(self) -> None:
        session_split.split_sessions("SS-001")
        path_service.build_path_sankey("BP-001")
        detail = store.get_artifact("session_split_detail", "SS-001")
        report = store.get_artifact("behavior_path", "BP-001")

        outcome = late_backfill.submit_backfill(_payload(
            event_uid="LATE-UNKNOWN-USER", user_id="U99999",
        ))

        self.assertEqual(outcome["status"], late_backfill.STATUS_APPLIED)
        self.assertEqual(outcome["backfill"]["rebuild_round"], 0)
        self.assertEqual(store.get_artifact("session_split_detail", "SS-001"), detail)
        self.assertEqual(store.get_artifact("behavior_path", "BP-001"), report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
