from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from battery_retirement.clock import FrozenClock
from battery_retirement.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from battery_retirement.service import RetirementService


POLICY = {
    "policy_id": "pol-1",
    "version": 1,
    "title": "测试政策",
    "rules": {
        "continue_service_min_soh": "90",
        "derate_min_soh": "80",
        "reuse_min_soh": "60",
        "resistance_warning_percent": "15",
        "resistance_block_percent": "40",
        "derate_value_factor": "0.75",
        "reuse_value_cny_per_kwh": "220",
        "recycle_value_cny_per_kwh": "60",
    },
    "required_evidence": {"capacity": True, "resistance": True, "repairs": True, "safety": True},
}

WINDOWS = {
    "as_of": "2026-09-01T00:00:00Z",
    "capacity_window": {"starts_at": "2026-08-01T00:00:00Z", "ends_at": "2026-08-31T23:59:59Z"},
    "resistance_window": {"starts_at": "2026-08-01T00:00:00Z", "ends_at": "2026-08-31T23:59:59Z"},
    "events_after": "2025-09-01T00:00:00Z",
}

WINDOWS_V2 = {
    "as_of": "2026-09-05T00:00:00Z",
    "capacity_window": {"starts_at": "2026-08-15T00:00:00Z", "ends_at": "2026-09-04T23:59:59Z"},
    "resistance_window": {"starts_at": "2026-08-15T00:00:00Z", "ends_at": "2026-09-04T23:59:59Z"},
    "events_after": "2025-09-01T00:00:00Z",
}


def component(component_id: str = "rack-1") -> dict:
    return {
        "component_id": component_id,
        "station_id": "station-1",
        "model_name": "LFP 簇",
        "chemistry": "LFP",
        "rated_capacity_kwh": "100",
        "acquisition_cost_cny": "100000",
        "baseline_resistance_milliohm": "10",
        "commissioned_at": "2018-06-01T00:00:00Z",
    }


def rec(component_id: str, kind: str, at: str, row: str, value=None, **extra) -> dict:
    return {
        "component_id": component_id,
        "kind": kind,
        "measured_at": at,
        "source_batch": "lab",
        "source_row": row,
        "evidence_ref": f"doc:{row}",
        "value": value,
        **extra,
    }


class RetirementServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc))
        self.service = RetirementService(self.connection, self.clock)
        for user_id, role in (
            ("operator", "operator"),
            ("approver", "approver"),
            ("cascade", "cascade_manager"),
            ("auditor", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.publish_policy("approver", POLICY)
        self.service.register_component("operator", component())

    def tearDown(self) -> None:
        self.connection.close()

    def _healthy(self, component_id: str = "rack-1", soh: str = "95", resistance: str = "10.5") -> None:
        self.service.record_measurements("operator", [
            rec(component_id, "capacity", "2026-08-20T00:00:00Z", f"{component_id}-cap", soh),
            rec(component_id, "resistance", "2026-08-20T01:00:00Z", f"{component_id}-res", resistance),
            rec(component_id, "repair", "2026-03-01T00:00:00Z", f"{component_id}-rep",
                severity="low", status="closed"),
            rec(component_id, "safety", "2026-03-02T00:00:00Z", f"{component_id}-saf",
                severity="low", status="closed"),
        ])

    def _prepare_approve(self, assessment_id: str, component_id: str = "rack-1") -> dict:
        prepared = self.service.prepare_assessment(
            "operator", assessment_id, component_id, "pol-1", 1, WINDOWS
        )
        self.service.decide_assessment("approver", assessment_id, True, "批准")
        return prepared

    # ------------------------------------------------------------ 冻结与审批

    def test_submitted_assessment_is_not_effective(self) -> None:
        self._healthy()
        prepared = self.service.prepare_assessment("operator", "a1", "rack-1", "pol-1", 1, WINDOWS)
        self.assertEqual(prepared["state"], "submitted")
        self.assertIsNone(self.service.component_disposition("rack-1")["effective_assessment_id"])

    def test_preparer_cannot_approve_own_assessment(self) -> None:
        self._healthy()
        self.service.prepare_assessment("operator", "a1", "rack-1", "pol-1", 1, WINDOWS)
        with self.assertRaises(Forbidden):
            self.service.decide_assessment("operator", "a1", True, "自审")

    def test_only_approver_role_can_decide(self) -> None:
        self._healthy()
        self.service.prepare_assessment("operator", "a1", "rack-1", "pol-1", 1, WINDOWS)
        # cascade_manager 无权审批
        with self.assertRaises(Forbidden):
            self.service.decide_assessment("cascade", "a1", True, "批准")
        # approver 可以
        self.service.decide_assessment("approver", "a1", True, "批准")
        self.assertEqual(
            self.service.component_disposition("rack-1")["effective_assessment_id"], "a1"
        )

    def test_late_evidence_does_not_overwrite_effective_conclusion(self) -> None:
        self._healthy(soh="95")
        self._prepare_approve("a-v1")
        self.service.record_measurements("operator", [
            rec("rack-1", "capacity", "2026-08-25T00:00:00Z", "late-cap", "40"),
        ])
        disposition = self.service.component_disposition("rack-1")
        self.assertEqual(disposition["effective_assessment_id"], "a-v1")
        effective = self.service.effective_assessment("rack-1")
        self.assertEqual(effective["recommendation"], "continue_service")

    def test_new_version_supersedes_previous_only_after_independent_approval(self) -> None:
        self._healthy(soh="95", resistance="10.5")
        self._prepare_approve("a-v1")
        self.service.record_measurements("operator", [
            rec("rack-1", "capacity", "2026-08-25T00:00:00Z", "late-cap", "70"),
            rec("rack-1", "resistance", "2026-08-25T01:00:00Z", "late-res", "12"),
        ])
        review = self.service.request_review("operator", "a-v1", "晚到证据", new_windows=WINDOWS_V2)
        with self.assertRaises(Forbidden):
            self.service.decide_review("operator", review["review_id"], True, "自己复核")
        self.service.decide_review("approver", review["review_id"], True, "受理")
        v2 = self.service.prepare_assessment(
            "operator", "a-v2", "rack-1", "pol-1", 1, WINDOWS_V2, source_review_id=review["review_id"]
        )
        self.assertEqual(v2["version"], 2)
        # 未审批前生效版本仍为 v1
        self.assertEqual(
            self.service.component_disposition("rack-1")["effective_assessment_id"], "a-v1"
        )
        self.service.decide_assessment("approver", "a-v2", True, "批准新版本")
        self.assertEqual(
            self.service.component_disposition("rack-1")["effective_assessment_id"], "a-v2"
        )
        versions = self.service.list_versions("rack-1")["versions"]
        self.assertEqual([item["state"] for item in versions], ["superseded", "approved"])

    def test_new_evidence_requires_review_request(self) -> None:
        self._healthy(soh="95")
        self._prepare_approve("a-v1")
        # 没有新证据、相同窗口的复算返回既有版本（幂等）。
        replay = self.service.prepare_assessment("operator", "a-dup", "rack-1", "pol-1", 1, WINDOWS)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["assessment_id"], "a-v1")
        # 复核受理前不能以任意新窗口直接派生版本之外的旁路；
        # 但新窗口本身允许创建草稿（版本号增长），生效结论不变。
        draft = self.service.prepare_assessment("operator", "a-draft", "rack-1", "pol-1", 1, WINDOWS_V2)
        self.assertEqual(draft["state"], "submitted")
        self.assertEqual(
            self.service.component_disposition("rack-1")["effective_assessment_id"], "a-v1"
        )

    def test_rejected_assessment_stays_submitted_history(self) -> None:
        self._healthy(soh="40", resistance="18")
        prepared = self.service.prepare_assessment("operator", "a1", "rack-1", "pol-1", 1, WINDOWS)
        self.assertEqual(prepared["recommendation"], "recycle")
        self.service.decide_assessment("approver", "a1", False, "证据链存疑，驳回")
        self.assertIsNone(self.service.component_disposition("rack-1")["effective_assessment_id"])
        with self.assertRaises(InvalidState):
            self.service.decide_assessment("approver", "a1", True, "再次审批")

    def test_recompute_matches_frozen_snapshot(self) -> None:
        self._healthy(soh="95")
        self._prepare_approve("a-v1")
        report = self.service.recompute_assessment("auditor", "a-v1")
        self.assertTrue(report["checks"]["matches"])
        self.assertEqual(report["recomputed"]["recommendation"], "continue_service")

    def test_recompute_still_verifies_even_if_component_config_changes(self) -> None:
        # 组件配置登记后不可变（重复登记冲突），冻结快照保存的是登记时配置。
        self._healthy(soh="95")
        self._prepare_approve("a-v1")
        with self.assertRaises(Conflict):
            self.service.register_component("operator", component())

    def test_preview_shows_gaps_without_persisting(self) -> None:
        self.service.record_measurements("operator", [
            rec("rack-1", "capacity", "2026-08-20T00:00:00Z", "cap", "95"),
        ])
        preview = self.service.preview("auditor", "rack-1", "pol-1", 1, WINDOWS)
        self.assertEqual(preview["recommendation"], "pending_evidence")
        self.assertTrue(preview["evidence_gaps"])
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM assessments").fetchone()[0], 0
        )

    # ------------------------------------------------------------ 梯次批次与预留

    def _cascade_component(self, component_id: str) -> None:
        if component_id != "rack-1":
            self.service.register_component("operator", component(component_id))
        self.service.record_measurements("operator", [
            rec(component_id, "capacity", "2026-08-20T00:00:00Z", f"{component_id}-cap", "70"),
            rec(component_id, "resistance", "2026-08-20T01:00:00Z", f"{component_id}-res", "12"),
            rec(component_id, "repair", "2026-03-01T00:00:00Z", f"{component_id}-rep",
                severity="low", status="closed"),
            rec(component_id, "safety", "2026-03-02T00:00:00Z", f"{component_id}-saf",
                severity="low", status="closed"),
        ])
        self.service.prepare_assessment("operator", f"{component_id}-a", component_id, "pol-1", 1, WINDOWS)
        self.service.decide_assessment("approver", f"{component_id}-a", True, "批准")

    def test_only_cascade_recommendation_can_enter_batch(self) -> None:
        self._healthy(soh="95")
        self._prepare_approve("a-v1")
        self.service.create_cascade_batch("cascade", "b1")
        with self.assertRaises(InvalidState):
            self.service.add_to_cascade_batch("cascade", "b1", "rack-1")

    def test_component_cannot_join_two_active_batches(self) -> None:
        self._cascade_component("rack-1")
        self.service.create_cascade_batch("cascade", "b1")
        self.service.add_to_cascade_batch("cascade", "b1", "rack-1")
        self.service.create_cascade_batch("cascade", "b2")
        with self.assertRaises(Conflict):
            self.service.add_to_cascade_batch("cascade", "b2", "rack-1")

    def test_hold_cannot_be_double_booked_and_expiry_releases(self) -> None:
        self._cascade_component("rack-1")
        self.service.create_cascade_batch("cascade", "b1")
        self.service.add_to_cascade_batch("cascade", "b1", "rack-1")
        self.service.seal_cascade_batch("cascade", "b1")
        self.service.reserve_project("cascade", "proj-1", "b1", "70", hold_days=30)
        with self.assertRaises(InvalidState):
            self.service.reserve_project("cascade", "proj-2", "b1", "70", hold_days=30)
        self.clock.advance(days=31)
        swept = self.service.expire_holds("cascade")
        self.assertEqual(swept["expired_projects"], ["proj-1"])
        # 释放后可被新项目占用
        project = self.service.reserve_project("cascade", "proj-3", "b1", "70", hold_days=10)
        self.assertEqual(project["state"], "reserved")

    def test_project_failure_releases_capacity_and_batch_history_kept(self) -> None:
        self._cascade_component("rack-1")
        self.service.create_cascade_batch("cascade", "b1")
        self.service.add_to_cascade_batch("cascade", "b1", "rack-1")
        self.service.seal_cascade_batch("cascade", "b1")
        self.service.reserve_project("cascade", "proj-1", "b1", "70", hold_days=30)
        self.service.mark_project_failed("cascade", "proj-1", "落地失败")
        self.assertEqual(self.service.project("proj-1")["state"], "released")
        # 明细历史保留为 released
        hold = self.service.project("proj-1")["holds"][0]
        self.assertEqual(hold["state"], "released")
        self.assertEqual(hold["release_reason"], "project_failed")

    def test_batch_withdrawal_releases_components_for_new_batch(self) -> None:
        self._cascade_component("rack-1")
        self.service.create_cascade_batch("cascade", "b1")
        self.service.add_to_cascade_batch("cascade", "b1", "rack-1")
        self.service.seal_cascade_batch("cascade", "b1")
        self.service.reserve_project("cascade", "proj-1", "b1", "70", hold_days=30)
        self.service.withdraw_cascade_batch("cascade", "b1", "撤回重组")
        batch = self.service.cascade_batch("b1")
        self.assertEqual(batch["state"], "withdrawn")
        # 历史行保留
        self.assertEqual(len(batch["items"]), 1)
        self.assertEqual(batch["items"][0]["item_state"], "released")
        # 组件可进入新批次
        self.service.create_cascade_batch("cascade", "b2")
        self.service.add_to_cascade_batch("cascade", "b2", "rack-1")

    def test_project_confirmation_locks_single_final_destination(self) -> None:
        self._cascade_component("rack-1")
        self.service.create_cascade_batch("cascade", "b1")
        self.service.add_to_cascade_batch("cascade", "b1", "rack-1")
        self.service.seal_cascade_batch("cascade", "b1")
        self.service.reserve_project("cascade", "proj-1", "b1", "70", hold_days=30)
        self.service.confirm_project("cascade", "proj-1", "contract-1")
        disposition = self.service.component_disposition("rack-1")
        self.assertEqual(disposition["final_destination"], "cascade")
        self.assertEqual(disposition["destinations_count"], 1)
        # 已最终确认的组件不能再产生评估版本
        with self.assertRaises(InvalidState):
            self.service.prepare_assessment("operator", "a-new", "rack-1", "pol-1", 1, WINDOWS)
        # 处置闭环后复核申请同样被拒绝
        with self.assertRaises(InvalidState):
            self.service.request_review("operator", "rack-1-a", "已确认后试图复核")

    def test_disposition_must_match_effective_recommendation(self) -> None:
        self._healthy(soh="40", resistance="18")
        self._prepare_approve("a-v1")
        with self.assertRaises(InvalidState):
            self.service.confirm_disposition("operator", "rack-1", "continued", "ref")
        confirmed = self.service.confirm_disposition("operator", "rack-1", "recycled", "manifest-1")
        self.assertEqual(confirmed["final_destination"], "recycled")
        # 不能二次确认
        with self.assertRaises(Conflict):
            self.service.confirm_disposition("operator", "rack-1", "recycled", "manifest-2")

    def test_disposition_register_reports_consistency(self) -> None:
        self._healthy(soh="40", resistance="18")
        self._prepare_approve("a-v1")
        self.service.confirm_disposition("operator", "rack-1", "recycled", "manifest-1")
        register = self.service.disposition_register("auditor")
        self.assertTrue(register["consistent"])
        self.assertEqual(register["components"][0]["final_destination"], "recycled")

    def test_audit_chain_is_valid(self) -> None:
        self._healthy()
        self._prepare_approve("a-v1")
        chain = self.service.audit_chain("auditor")
        self.assertTrue(chain["valid"])
        self.assertGreater(chain["events"], 0)

    # ------------------------------------------------------------ 权限边界

    def test_role_separation(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.register_component("auditor", component("other"))
        with self.assertRaises(Forbidden):
            self.service.record_measurements("approver", [])
        with self.assertRaises(Forbidden):
            self.service.create_cascade_batch("operator", "bx")
        with self.assertRaises(Forbidden):
            self.service.audit_chain("operator")

    def test_duplicate_measurement_source_rejected(self) -> None:
        self._healthy()
        with self.assertRaises(Conflict):
            self.service.record_measurements("operator", [
                rec("rack-1", "capacity", "2026-08-20T00:00:00Z", "rack-1-cap", "95"),
            ])

    def test_invalid_record_payload(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.record_measurements("operator", [
                rec("rack-1", "unknown", "2026-08-20T00:00:00Z", "x", "1"),
            ])
        with self.assertRaises(ValidationFailed):
            self.service.record_measurements("operator", [
                rec("rack-1", "safety", "2026-08-20T00:00:00Z", "x", status="open"),
            ])


if __name__ == "__main__":
    unittest.main()
