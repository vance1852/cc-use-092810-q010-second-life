from __future__ import annotations

import unittest
from decimal import Decimal

from battery_retirement.models import AssessmentWindows, Component, MeasurementRecord, RetirementPolicy
from battery_retirement.policy import (
    CASCADE,
    CONTINUE_SERVICE,
    DERATE,
    PENDING_EVIDENCE,
    RECYCLE,
    evaluate_component,
)


POLICY_RAW = {
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


def component(baseline: str | None = "10") -> Component:
    raw = {
        "component_id": "rack-1",
        "station_id": "s1",
        "model_name": "簇",
        "chemistry": "LFP",
        "rated_capacity_kwh": "100",
        "acquisition_cost_cny": "100000",
        "baseline_resistance_milliohm": baseline,
        "commissioned_at": "2018-06-01T00:00:00Z",
    }
    return Component.from_dict(raw)


def rec(kind: str, at: str, value=None, **extra) -> MeasurementRecord:
    raw = {
        "component_id": "rack-1",
        "kind": kind,
        "measured_at": at,
        "source_batch": "lab",
        "source_row": f"{kind}-{at}",
        "evidence_ref": "doc",
        "value": value,
        **extra,
    }
    return MeasurementRecord.from_dict(raw)


def healthy_records() -> list[MeasurementRecord]:
    return [
        rec("capacity", "2026-08-20T00:00:00Z", "95"),
        rec("resistance", "2026-08-20T01:00:00Z", "10.5"),
        rec("repair", "2026-03-01T00:00:00Z", severity="low", status="closed"),
        rec("safety", "2026-03-02T00:00:00Z", severity="low", status="closed"),
    ]


class PolicyEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = RetirementPolicy.from_dict(POLICY_RAW)
        self.windows = AssessmentWindows.from_dict(WINDOWS)

    def test_continue_service(self) -> None:
        result = evaluate_component(component(), healthy_records(), self.windows, self.policy)
        self.assertEqual(result.recommendation, CONTINUE_SERVICE)
        self.assertEqual(result.metrics["soh_percent"], "95.000")
        self.assertEqual(result.residual_value_cny, Decimal("95000.00"))
        self.assertFalse(result.evidence_gaps)
        self.assertTrue(any("SOH=95.000%" in line for line in result.explanations))

    def test_derate(self) -> None:
        records = healthy_records()
        records[0] = rec("capacity", "2026-08-20T00:00:00Z", "85")
        records[1] = rec("resistance", "2026-08-20T01:00:00Z", "13")  # +30%
        result = evaluate_component(component(), records, self.windows, self.policy)
        self.assertEqual(result.recommendation, DERATE)
        # 100000 * 0.85 * 0.75
        self.assertEqual(result.residual_value_cny, Decimal("63750.00"))

    def test_cascade_utilization(self) -> None:
        records = healthy_records()
        records[0] = rec("capacity", "2026-08-20T00:00:00Z", "70")
        records[1] = rec("resistance", "2026-08-20T01:00:00Z", "12")  # +20%
        result = evaluate_component(component(), records, self.windows, self.policy)
        self.assertEqual(result.recommendation, CASCADE)
        # 100 kWh * 220 * 0.70
        self.assertEqual(result.residual_value_cny, Decimal("15400.00"))

    def test_recycle_due_to_low_soh(self) -> None:
        records = healthy_records()
        records[0] = rec("capacity", "2026-08-20T00:00:00Z", "40")
        records[1] = rec("resistance", "2026-08-20T01:00:00Z", "18")
        result = evaluate_component(component(), records, self.windows, self.policy)
        self.assertEqual(result.recommendation, RECYCLE)
        # 100 kWh * 60
        self.assertEqual(result.residual_value_cny, Decimal("6000.00"))

    def test_open_critical_safety_forces_recycle_even_without_other_evidence(self) -> None:
        records = [rec("safety", "2026-08-10T00:00:00Z", severity="critical", status="open")]
        result = evaluate_component(component(), records, self.windows, self.policy)
        self.assertEqual(result.recommendation, RECYCLE)
        self.assertTrue(any("critical 安全事件" in line for line in result.explanations))

    def test_open_high_safety_blocks_continue_but_allows_cascade(self) -> None:
        records = healthy_records()
        records[3] = rec("safety", "2026-08-10T00:00:00Z", severity="high", status="open")
        result = evaluate_component(component(), records, self.windows, self.policy)
        # SOH 95 本应继续服役，但 high 安全事件禁止继续服役与降额，落到梯次利用。
        self.assertEqual(result.recommendation, CASCADE)

    def test_pending_evidence_when_capacity_missing_in_window(self) -> None:
        records = [
            rec("capacity", "2026-07-20T00:00:00Z", "95"),  # 窗口外
            rec("resistance", "2026-08-20T01:00:00Z", "10.5"),
            rec("repair", "2026-03-01T00:00:00Z", severity="low", status="closed"),
            rec("safety", "2026-03-02T00:00:00Z", severity="low", status="closed"),
        ]
        result = evaluate_component(component(), records, self.windows, self.policy)
        self.assertEqual(result.recommendation, PENDING_EVIDENCE)
        self.assertIsNone(result.residual_value_cny)
        self.assertTrue(any(gap.startswith("capacity:") for gap in result.evidence_gaps))

    def test_missing_ledger_category_is_a_gap(self) -> None:
        records = [
            rec("capacity", "2026-08-20T00:00:00Z", "95"),
            rec("resistance", "2026-08-20T01:00:00Z", "10.5"),
        ]
        result = evaluate_component(component(), records, self.windows, self.policy)
        self.assertEqual(result.recommendation, PENDING_EVIDENCE)
        self.assertEqual(len(result.evidence_gaps), 2)

    def test_missing_baseline_resistance_is_evidence_gap(self) -> None:
        records = [
            rec("capacity", "2026-08-20T00:00:00Z", "95"),
            rec("resistance", "2026-08-20T01:00:00Z", "10.5"),
            rec("repair", "2026-03-01T00:00:00Z", severity="low", status="closed"),
            rec("safety", "2026-03-02T00:00:00Z", severity="low", status="closed"),
        ]
        result = evaluate_component(component(baseline=None), records, self.windows, self.policy)
        self.assertEqual(result.recommendation, PENDING_EVIDENCE)
        self.assertTrue(any("基线内阻" in gap for gap in result.evidence_gaps))

    def test_late_records_outside_window_are_ignored(self) -> None:
        records = healthy_records()
        # 晚到的恶劣结果落在窗口外，结论必须保持"继续服役"。
        records.append(rec("capacity", "2026-09-02T00:00:00Z", "20"))
        records.append(rec("safety", "2026-09-03T00:00:00Z", severity="critical", status="open"))
        result = evaluate_component(component(), records, self.windows, self.policy)
        self.assertEqual(result.recommendation, CONTINUE_SERVICE)

    def test_uses_at_most_three_latest_capacity_samples(self) -> None:
        records = healthy_records()
        records[0:1] = [
            rec("capacity", "2026-08-10T00:00:00Z", "20"),
            rec("capacity", "2026-08-11T00:00:00Z", "20"),
            rec("capacity", "2026-08-18T00:00:00Z", "90"),
            rec("capacity", "2026-08-19T00:00:00Z", "90"),
            rec("capacity", "2026-08-20T00:00:00Z", "90"),
        ]
        result = evaluate_component(component(), records, self.windows, self.policy)
        # 最近 3 次均值 = 90
        self.assertEqual(result.metrics["soh_percent"], "90.000")

    def test_evaluation_is_deterministic(self) -> None:
        first = evaluate_component(component(), healthy_records(), self.windows, self.policy)
        second = evaluate_component(component(), healthy_records(), self.windows, self.policy)
        self.assertEqual(first.as_dict(), second.as_dict())

    def test_invalid_policy_threshold_order_rejected(self) -> None:
        raw = {**POLICY_RAW, "rules": {**POLICY_RAW["rules"], "reuse_min_soh": "95"}}
        with self.assertRaises(ValueError):
            RetirementPolicy.from_dict(raw)


if __name__ == "__main__":
    unittest.main()
