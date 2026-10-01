from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from battery_retirement.clock import FrozenClock
from battery_retirement.errors import Conflict, Forbidden, InvalidState, NotFound
from battery_retirement.service import RetirementService


POLICY = {
    "policy_id": "p",
    "version": 1,
    "title": "基准政策",
    "thresholds": {
        "capacity_continue_percent": "85",
        "capacity_cascade_percent": "70",
        "resistance_good_percent": "120",
        "resistance_cascade_percent": "150",
        "maintenance_major_limit": 2,
    },
    "valuation": {
        "reference_unit_value_cny_per_kwh": "100",
        "continue_factor": "1.0",
        "derating_factor": "0.7",
        "cascade_factor": "0.4",
        "recycle_unit_value_cny_per_kwh": "10",
    },
    "reservation_hold_days": 30,
}

WINDOW_START = "2026-09-01T00:00:00Z"
CUTOFF = "2026-09-30T23:59:59Z"


def config(component_id, rated="200", revision=1):
    return {
        "component_id": component_id,
        "revision": revision,
        "model_name": "LFP",
        "chemistry": "LFP",
        "nominal_capacity_kwh": "300",
        "rated_capacity_kwh": rated,
        "commissioned_at": "2020-06-01T00:00:00Z",
        "replaced_parts": [],
    }


def measurement(record_id, kind, value, *, recorded="2026-09-20T10:00:00Z", measured=None):
    return {
        "record_id": record_id,
        "kind": kind,
        "value": value,
        "source": "检测班",
        "measured_at": measured or "2026-09-15T08:00:00Z",
        "recorded_at": recorded,
    }


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc))
        self.service = RetirementService(self.connection, self.clock)
        for user_id, role in (
            ("eng", "engineer"),
            ("app", "approver"),
            ("plan", "planner"),
            ("aud", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.publish_policy("eng", POLICY)

    def tearDown(self) -> None:
        self.connection.close()

    # ── 辅助 ──────────────────────────────────────────────────────────────

    def make_component(self, component_id="c1", rated="200", cap="73", res="140"):
        self.service.register_component("eng", config(component_id, rated))
        self.service.import_measurements("eng", component_id, [
            measurement(f"{component_id}-cap", "capacity_retention_percent", cap),
            measurement(f"{component_id}-res", "internal_resistance_percent", res),
        ])
        return component_id

    def assess_and_approve(self, component_id, serial="1", conclusion=None):
        assessment_id = f"{component_id}-a{serial}"
        opened = self.service.open_assessment(
            "eng", assessment_id, component_id, 1, "p", 1, WINDOW_START, CUTOFF
        )
        if conclusion is not None:
            self.assertEqual(opened["conclusion"], conclusion)
        self.service.submit_assessment("eng", assessment_id)
        self.service.approve_assessment("app", assessment_id, "同意")
        return assessment_id

    # ── 冻结、审批与唯一去向 ────────────────────────────────────────────────

    def test_conclusion_not_effective_before_independent_approval(self) -> None:
        self.make_component()
        opened = self.service.open_assessment("eng", "c1-a1", "c1", 1, "p", 1, WINDOW_START, CUTOFF)
        self.assertEqual(opened["state"], "open")
        report = self.service.disposition_report("aud", "c1")
        self.assertEqual(report["effective_count"], 0)
        self.service.submit_assessment("eng", "c1-a1")
        # 提交人不能审批自己的版本。
        with self.assertRaises(Forbidden):
            self.service.approve_assessment("eng", "c1-a1", "自批")
        self.service.approve_assessment("app", "c1-a1", "独立同意")
        report = self.service.disposition_report("aud", "c1")
        self.assertEqual(report["effective_count"], 1)
        self.assertEqual(report["final_destination"]["conclusion"], "cascade")

    def test_pending_evidence_cannot_be_ratified(self) -> None:
        self.service.register_component("eng", config("c2", "100"))
        self.service.open_assessment("eng", "c2-a1", "c2", 1, "p", 1, WINDOW_START, CUTOFF)
        self.service.submit_assessment("eng", "c2-a1")
        with self.assertRaises(InvalidState):
            self.service.approve_assessment("app", "c2-a1", "尝试让等待补证生效")

    def test_new_version_supersedes_old_keeping_single_destination(self) -> None:
        self.make_component("c1")
        first = self.assess_and_approve("c1", conclusion="cascade")
        # 复检恢复的晚到证据触发新版本。
        self.service.import_measurements("eng", "c1", [
            measurement("c1-cap-new", "capacity_retention_percent", "92",
                        recorded="2026-10-15T10:00:00Z", measured="2026-10-14T08:00:00Z"),
            measurement("c1-res-new", "internal_resistance_percent", "108",
                        recorded="2026-10-15T10:05:00Z", measured="2026-10-14T08:05:00Z"),
        ])
        review = self.service.request_review("eng", first, "复检恢复", ["c1-cap-new", "c1-res-new"])
        self.clock.advance(days=16)
        decided = self.service.decide_review("app", review["review_id"], True, "重开")
        successor = decided["new_assessment_id"]
        # 新版本生效前，旧去向仍然唯一有效。
        self.assertEqual(self.service.disposition_report("aud", "c1")["effective_count"], 1)
        self.service.submit_assessment("eng", successor)
        self.service.approve_assessment("app", successor, "恢复服役")
        report = self.service.disposition_report("aud", "c1")
        self.assertEqual(report["effective_count"], 1)
        self.assertEqual(report["final_destination"]["conclusion"], "continue_service")
        states = {v["assessment_id"]: v["state"] for v in report["versions"]}
        self.assertEqual(states[first], "superseded")

    # ── 冻结复算 ──────────────────────────────────────────────────────────

    def test_late_evidence_does_not_rewrite_frozen_version(self) -> None:
        self.make_component()
        assessment_id = self.assess_and_approve("c1", conclusion="cascade")
        before = self.service.recompute("aud", assessment_id)
        self.assertTrue(before["input_matches"] and before["conclusion_matches"])
        self.service.import_measurements("eng", "c1", [
            measurement("late-cap", "capacity_retention_percent", "95",
                        recorded="2026-10-20T10:00:00Z", measured="2026-10-19T08:00:00Z"),
        ])
        after = self.service.recompute("aud", assessment_id)
        self.assertTrue(after["input_matches"] and after["conclusion_matches"])
        self.assertEqual(after["recomputed"]["conclusion"], "cascade")
        self.assertEqual(len(after["recomputed"]["late_evidence"]), 1)

    # ── 复核申请规则 ────────────────────────────────────────────────────────

    def test_review_rejects_in_window_evidence_and_duplicate(self) -> None:
        self.make_component()
        assessment_id = self.assess_and_approve("c1", conclusion="cascade")
        # 窗口内证据不算新证据。
        with self.assertRaises(Exception):
            self.service.request_review("eng", assessment_id, "重评", ["c1-cap"])
        self.service.import_measurements("eng", "c1", [
            measurement("new-cap", "capacity_retention_percent", "92",
                        recorded="2026-10-21T10:00:00Z", measured="2026-10-20T08:00:00Z"),
        ])
        self.service.request_review("eng", assessment_id, "重评", ["new-cap"])
        # 同一版本只能有一个待处理复核。
        with self.assertRaises(Conflict):
            self.service.request_review("eng", assessment_id, "再次", ["new-cap"])
        # 申请人不能审批自己的复核。
        review_id = self.connection.execute(
            "SELECT review_id FROM review_requests ORDER BY review_id DESC LIMIT 1"
        ).fetchone()[0]
        with self.assertRaises(Forbidden):
            self.service.decide_review("eng", review_id, True, "自审")

    def test_review_rejected_keeps_effective_destination(self) -> None:
        self.make_component()
        assessment_id = self.assess_and_approve("c1", conclusion="cascade")
        self.service.import_measurements("eng", "c1", [
            measurement("new-cap", "capacity_retention_percent", "92",
                        recorded="2026-10-21T10:00:00Z", measured="2026-10-20T08:00:00Z"),
        ])
        review = self.service.request_review("eng", assessment_id, "重评", ["new-cap"])
        result = self.service.decide_review("app", review["review_id"], False, "证据不足，驳回")
        self.assertIsNone(result["new_assessment_id"])
        self.assertEqual(self.service.disposition_report("aud", "c1")["final_destination"]["conclusion"], "cascade")

    # ── 候选批次与来源 ──────────────────────────────────────────────────────

    def test_candidate_batch_requires_effective_cascade_and_provenance(self) -> None:
        self.make_component("c1")
        self.make_component("c2", cap="92", res="108")
        self.assess_and_approve("c1", conclusion="cascade")
        self.assess_and_approve("c2", conclusion="continue_service")
        self.service.create_candidate_batch("plan", "b1")
        self.service.add_candidate_component("plan", "b1", "c1")
        # 非梯次去向组件不能入批。
        with self.assertRaises(Conflict):
            self.service.add_candidate_component("plan", "b1", "c2")
        sealed = self.service.seal_candidate_batch("plan", "b1")
        self.assertEqual(len(sealed["content_sha256"]), 64)
        self.assertEqual(sealed["items"][0]["source_assessment_id"], "c1-a1")
        # 封存后不能再加组件。
        with self.assertRaises(InvalidState):
            self.service.add_candidate_component("plan", "b1", "c2")

    def test_component_cannot_join_two_open_batches(self) -> None:
        self.make_component("c1")
        self.assess_and_approve("c1", conclusion="cascade")
        self.service.create_candidate_batch("plan", "b1")
        self.service.create_candidate_batch("plan", "b2")
        self.service.add_candidate_component("plan", "b1", "c1")
        with self.assertRaises(Conflict):
            self.service.add_candidate_component("plan", "b2", "c1")

    def test_empty_batch_cannot_seal(self) -> None:
        self.service.create_candidate_batch("plan", "b1")
        with self.assertRaises(InvalidState):
            self.service.seal_candidate_batch("plan", "b1")

    # ── 容量预留：期限、不重复占用、安全释放 ─────────────────────────────────

    def _sealed_cascade_batch(self, component_id="c1", rated="200"):
        self.make_component(component_id, rated=rated)
        self.assess_and_approve(component_id, conclusion="cascade")
        self.service.create_candidate_batch("plan", "b1")
        self.service.add_candidate_component("plan", "b1", component_id)
        self.service.seal_candidate_batch("plan", "b1")
        self.service.create_reuse_project("plan", "pa", "项目 A")
        self.service.create_reuse_project("plan", "pb", "项目 B")

    def test_capacity_cannot_be_double_occupied(self) -> None:
        self._sealed_cascade_batch()
        self.service.reserve_capacity("plan", "r1", "pa", "b1", "c1")
        with self.assertRaises(Conflict):
            self.service.reserve_capacity("plan", "r2", "pb", "b1", "c1")

    def test_expiry_releases_and_allows_reoccupation_but_keeps_history(self) -> None:
        self._sealed_cascade_batch()
        self.service.reserve_capacity("plan", "r1", "pa", "b1", "c1")
        self.clock.advance(days=31)
        result = self.service.expire_due_reservations("plan")
        self.assertEqual(result["expired"], 1)
        self.service.reserve_capacity("plan", "r2", "pb", "b1", "c1")
        history = self.service.reservation_history("aud", "c1")
        states = [row["state"] for row in history["reservations"]]
        self.assertEqual(states, ["expired", "held"])

    def test_withdraw_and_project_failure_release_capacity(self) -> None:
        self._sealed_cascade_batch()
        self.service.reserve_capacity("plan", "r1", "pa", "b1", "c1")
        self.service.withdraw_reservation("plan", "r1", "暂缓")
        self.service.reserve_capacity("plan", "r2", "pb", "b1", "c1")
        failed = self.service.fail_project("plan", "pb", "终止")
        self.assertEqual(failed["released_reservations"], 1)
        # 失败释放后可再占用。
        self.service.reserve_capacity("plan", "r3", "pa", "b1", "c1")

    def test_successful_project_consumes_capacity_terminally(self) -> None:
        self._sealed_cascade_batch()
        self.service.reserve_capacity("plan", "r1", "pa", "b1", "c1")
        self.service.close_project("plan", "pa")
        # 已消耗是终态占用：不能再为其他项目预留。
        with self.assertRaises(Conflict):
            self.service.reserve_capacity("plan", "r2", "pb", "b1", "c1")

    def test_cannot_change_destination_while_capacity_occupied(self) -> None:
        self._sealed_cascade_batch()
        self.service.reserve_capacity("plan", "r1", "pa", "b1", "c1")
        self.service.import_measurements("eng", "c1", [
            measurement("new-cap", "capacity_retention_percent", "60",
                        recorded="2026-10-21T10:00:00Z", measured="2026-10-20T08:00:00Z"),
            measurement("new-res", "internal_resistance_percent", "170",
                        recorded="2026-10-21T10:05:00Z", measured="2026-10-20T08:05:00Z"),
        ])
        review = self.service.request_review("eng", "c1-a1", "劣化加重", ["new-cap", "new-res"])
        self.clock.advance(days=22)
        decided = self.service.decide_review("app", review["review_id"], True, "重开")
        successor = decided["new_assessment_id"]
        self.assertEqual(decided["new_assessment_id"] is not None, True)
        self.service.submit_assessment("eng", successor)
        with self.assertRaises(InvalidState):
            self.service.approve_assessment("app", successor, "改为回收")

    # ── 角色、不可变证据与全局核对 ───────────────────────────────────────────

    def test_role_separation(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.register_component("aud", config("x"))
        with self.assertRaises(Forbidden):
            self.service.approve_assessment("eng", "n/a", "x")

    def test_evidence_is_immutable(self) -> None:
        self.make_component()
        with self.assertRaises(Conflict):
            self.service.import_measurements("eng", "c1", [
                measurement("c1-cap", "capacity_retention_percent", "50"),
            ])

    def test_reconciliation_reports_single_destination(self) -> None:
        self.make_component("c1")
        self.make_component("c2", rated="150", cap="60", res="160")
        self.assess_and_approve("c1", conclusion="cascade")
        self.assess_and_approve("c2", conclusion="recycle")
        result = self.service.reconciliation("aud")
        self.assertTrue(result["consistent"])
        self.assertEqual(result["components_with_effective_destination"], 2)

    def test_residual_value_and_gaps_endpoints(self) -> None:
        self.service.register_component("eng", config("c9", "150"))
        self.service.import_measurements("eng", "c9", [
            measurement("c9-cap", "capacity_retention_percent", "60"),
            measurement("c9-res", "internal_resistance_percent", "160"),
        ])
        assessment_id = "c9-a1"
        self.service.open_assessment("eng", assessment_id, "c9", 1, "p", 1, WINDOW_START, CUTOFF)
        value = self.service.residual_value("aud", assessment_id)
        self.assertEqual(value["residual_value"]["residual_value_cny"], "1500.00")
        gaps = self.service.evidence_gaps("aud", assessment_id)
        self.assertEqual(gaps["evidence_gaps"], [])

    def test_missing_entities_raise_not_found(self) -> None:
        with self.assertRaises(NotFound):
            self.service.get_assessment("nope")
        with self.assertRaises(NotFound):
            self.service.disposition_report("aud", "ghost")

    def test_rejection_is_not_effective_and_a_new_version_can_follow(self) -> None:
        self.make_component()
        self.service.open_assessment("eng", "c1-a1", "c1", 1, "p", 1, WINDOW_START, CUTOFF)
        self.service.submit_assessment("eng", "c1-a1")
        rejected = self.service.reject_assessment("app", "c1-a1", "证据链不完整")
        self.assertEqual(rejected["state"], "rejected")
        self.assertEqual(self.service.disposition_report("aud", "c1")["effective_count"], 0)
        # 被驳回后可另开新版本，审批通过才生效。
        self.service.open_assessment("eng", "c1-a2", "c1", 1, "p", 1, WINDOW_START, CUTOFF)
        self.service.submit_assessment("eng", "c1-a2")
        self.service.approve_assessment("app", "c1-a2", "补证后同意")
        report = self.service.disposition_report("aud", "c1")
        self.assertEqual(report["effective_count"], 1)
        self.assertEqual(report["final_destination"]["assessment_id"], "c1-a2")

    def test_config_revision_is_sequential_and_can_be_assessed(self) -> None:
        self.make_component("c1")
        updated = config("c1", rated="210", revision=2)
        result = self.service.add_config_revision("eng", updated)
        self.assertEqual(result["current_config_revision"], 2)
        # 不接受跳号。
        with self.assertRaises(Conflict):
            self.service.add_config_revision("eng", config("c1", rated="220", revision=4))
        self.service.import_measurements("eng", "c1", [
            measurement("c1-cap2", "capacity_retention_percent", "92",
                        recorded="2026-10-20T10:00:00Z", measured="2026-10-19T08:00:00Z"),
            measurement("c1-res2", "internal_resistance_percent", "108",
                        recorded="2026-10-20T10:05:00Z", measured="2026-10-19T08:05:00Z"),
        ])
        self.clock.advance(days=21)
        self.service.open_assessment(
            "eng", "c1-a1b", "c1", 2, "p", 1, WINDOW_START, self.clock.now().isoformat().replace("+00:00", "Z")
        )
        version = self.service.get_assessment("c1-a1b")
        self.assertEqual(version["config_revision"], 2)
        self.assertEqual(version["conclusion"], "continue_service")


if __name__ == "__main__":
    unittest.main()
