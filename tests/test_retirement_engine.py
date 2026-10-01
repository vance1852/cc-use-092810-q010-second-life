from __future__ import annotations

import unittest
from decimal import Decimal

from battery_retirement.contracts import (
    ComponentConfig,
    EvidenceBundle,
    Policy,
)
from battery_retirement.engine import ENGINE_VERSION, admit_evidence, evaluate, residual_value


POLICY = Policy.from_dict({
    "policy_id": "p",
    "version": 1,
    "title": "t",
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
})

CONFIG = ComponentConfig.from_dict({
    "component_id": "comp",
    "revision": 1,
    "model_name": "LFP",
    "chemistry": "LFP",
    "nominal_capacity_kwh": "300",
    "rated_capacity_kwh": "200",
    "commissioned_at": "2020-06-01T00:00:00Z",
    "replaced_parts": [],
})

WINDOW_START = "2026-09-01T00:00:00Z"
CUTOFF = "2026-09-30T23:59:59Z"


def measurement(record_id, kind, value, *, measured="2026-09-15T08:00:00Z", recorded=None):
    return {
        "record_id": record_id,
        "kind": kind,
        "value": value,
        "source": "检测班",
        "measured_at": measured,
        "recorded_at": recorded or "2026-09-20T10:00:00Z",
    }


def event(event_id, severity, *, category="safety", resolved=False, recorded="2026-09-21T09:00:00Z"):
    return {
        "event_id": event_id,
        "category": category,
        "severity": severity,
        "source": "台账",
        "occurred_at": "2026-09-10T00:00:00Z",
        "recorded_at": recorded,
        "resolved": resolved,
        "note": None,
    }


def bundle(measurements=(), events=()) -> EvidenceBundle:
    return EvidenceBundle.from_dict({"measurements": list(measurements), "events": list(events)})


class EngineTests(unittest.TestCase):
    def test_continue_service_when_signals_strong(self) -> None:
        data = bundle([
            measurement("cap", "capacity_retention_percent", "90"),
            measurement("res", "internal_resistance_percent", "110"),
        ])
        result = evaluate(CONFIG, POLICY, data, WINDOW_START, CUTOFF)
        self.assertEqual(result["conclusion"], "continue_service")
        self.assertEqual(result["evidence_gaps"], [])

    def test_derating_in_middle_band(self) -> None:
        data = bundle([
            measurement("cap", "capacity_retention_percent", "80"),
            measurement("res", "internal_resistance_percent", "115"),
        ])
        result = evaluate(CONFIG, POLICY, data, WINDOW_START, CUTOFF)
        self.assertEqual(result["conclusion"], "derating")

    def test_cascade_when_capacity_low_but_safe(self) -> None:
        data = bundle([
            measurement("cap", "capacity_retention_percent", "73"),
            measurement("res", "internal_resistance_percent", "140"),
        ])
        result = evaluate(CONFIG, POLICY, data, WINDOW_START, CUTOFF)
        self.assertEqual(result["conclusion"], "cascade")

    def test_recycle_below_cascade_floor(self) -> None:
        data = bundle([
            measurement("cap", "capacity_retention_percent", "60"),
            measurement("res", "internal_resistance_percent", "160"),
        ])
        result = evaluate(CONFIG, POLICY, data, WINDOW_START, CUTOFF)
        self.assertEqual(result["conclusion"], "recycle")

    def test_open_critical_safety_event_forces_recycle(self) -> None:
        data = bundle(
            [measurement("cap", "capacity_retention_percent", "95"),
             measurement("res", "internal_resistance_percent", "105")],
            [event("e1", "critical")],
        )
        result = evaluate(CONFIG, POLICY, data, WINDOW_START, CUTOFF)
        self.assertEqual(result["conclusion"], "recycle")

    def test_resolved_safety_event_does_not_force_recycle(self) -> None:
        data = bundle(
            [measurement("cap", "capacity_retention_percent", "95"),
             measurement("res", "internal_resistance_percent", "105")],
            [event("e1", "critical", resolved=True)],
        )
        result = evaluate(CONFIG, POLICY, data, WINDOW_START, CUTOFF)
        self.assertEqual(result["conclusion"], "continue_service")

    def test_missing_measurements_yield_pending_evidence(self) -> None:
        result = evaluate(CONFIG, POLICY, bundle(), WINDOW_START, CUTOFF)
        self.assertEqual(result["conclusion"], "pending_evidence")
        signals = {gap["signal"] for gap in result["evidence_gaps"]}
        self.assertEqual(signals, {"capacity_retention_percent", "internal_resistance_percent"})

    def test_late_evidence_is_sequestered_and_does_not_change_conclusion(self) -> None:
        stale = bundle([
            measurement("cap", "capacity_retention_percent", "73"),
            measurement("res", "internal_resistance_percent", "140"),
        ])
        first = evaluate(CONFIG, POLICY, stale, WINDOW_START, CUTOFF)
        self.assertEqual(first["conclusion"], "cascade")
        with_late = bundle([
            measurement("cap", "capacity_retention_percent", "73"),
            measurement("res", "internal_resistance_percent", "140"),
            measurement("cap-new", "capacity_retention_percent", "95",
                         measured="2026-10-14T08:00:00Z", recorded="2026-10-15T10:00:00Z"),
            measurement("res-new", "internal_resistance_percent", "108",
                         measured="2026-10-14T08:05:00Z", recorded="2026-10-15T10:05:00Z"),
        ])
        second = evaluate(CONFIG, POLICY, with_late, WINDOW_START, CUTOFF)
        self.assertEqual(second["conclusion"], "cascade")
        self.assertEqual(len(second["late_evidence"]), 2)

    def test_latest_measurement_wins_within_window(self) -> None:
        data = bundle([
            measurement("cap-old", "capacity_retention_percent", "72",
                        measured="2026-09-05T08:00:00Z", recorded="2026-09-06T10:00:00Z"),
            measurement("cap-new", "capacity_retention_percent", "92",
                        measured="2026-09-25T08:00:00Z", recorded="2026-09-26T10:00:00Z"),
            measurement("res", "internal_resistance_percent", "110"),
        ])
        result = evaluate(CONFIG, POLICY, data, WINDOW_START, CUTOFF)
        self.assertEqual(result["conclusion"], "continue_service")

    def test_evaluation_is_deterministic(self) -> None:
        data = bundle([
            measurement("cap", "capacity_retention_percent", "73"),
            measurement("res", "internal_resistance_percent", "140"),
        ])
        first = evaluate(CONFIG, POLICY, data, WINDOW_START, CUTOFF)
        second = evaluate(CONFIG, POLICY, data, WINDOW_START, CUTOFF)
        self.assertEqual(first, second)
        self.assertEqual(first["engine_version"], ENGINE_VERSION)

    def test_residual_value_tracks_conclusion(self) -> None:
        self.assertEqual(residual_value(CONFIG, POLICY, "continue_service")["residual_value_cny"], "20000.00")
        self.assertEqual(residual_value(CONFIG, POLICY, "derating")["residual_value_cny"], "14000.00")
        self.assertEqual(residual_value(CONFIG, POLICY, "cascade")["residual_value_cny"], "8000.00")
        self.assertEqual(residual_value(CONFIG, POLICY, "recycle")["residual_value_cny"], "2000.00")
        pending = residual_value(CONFIG, POLICY, "pending_evidence")
        self.assertTrue(pending["provisional"])

    def test_rejects_window_start_after_cutoff(self) -> None:
        with self.assertRaises(ValueError):
            evaluate(CONFIG, POLICY, bundle(), CUTOFF, WINDOW_START)

    def test_admit_evidence_classifies_before_and_late(self) -> None:
        data = bundle([
            measurement("early", "capacity_retention_percent", "90",
                        measured="2026-08-30T00:00:00Z", recorded="2026-08-31T23:59:00Z"),
            measurement("in", "capacity_retention_percent", "90",
                        measured="2026-09-14T00:00:00Z", recorded="2026-09-15T00:00:00Z"),
            measurement("late", "capacity_retention_percent", "90",
                        measured="2026-09-30T00:00:00Z", recorded="2026-10-01T00:00:00Z"),
        ])
        measurements, _events, late, before = admit_evidence(data, WINDOW_START, CUTOFF)
        self.assertEqual([m.record_id for m in measurements], ["in"])
        self.assertEqual(late[0]["record_id"], "late")
        self.assertEqual(before[0]["record_id"], "early")


if __name__ == "__main__":
    unittest.main()
