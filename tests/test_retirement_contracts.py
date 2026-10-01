from __future__ import annotations

import unittest

from battery_retirement.contracts import (
    ComponentConfig,
    EvidenceBundle,
    Measurement,
    Policy,
    QualityEvent,
    ValidationError,
)


def policy_raw(**overrides) -> dict:
    raw = {
        "policy_id": "retirement-standard",
        "version": 1,
        "title": "退役基准政策",
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
    if overrides:
        raw.update(overrides)
    return raw


def config_raw(**overrides) -> dict:
    raw = {
        "component_id": "comp-1",
        "revision": 1,
        "model_name": "LFP 簇",
        "chemistry": "LFP",
        "nominal_capacity_kwh": "300",
        "rated_capacity_kwh": "240",
        "commissioned_at": "2020-06-01T00:00:00Z",
        "replaced_parts": ["bms"],
    }
    raw.update(overrides)
    return raw


class ContractTests(unittest.TestCase):
    def test_policy_parses(self) -> None:
        policy = Policy.from_dict(policy_raw())
        self.assertEqual(policy.reservation_hold_days, 30)
        self.assertEqual(policy.thresholds.capacity_continue_percent, 85)

    def test_policy_requires_ordered_capacity_thresholds(self) -> None:
        raw = policy_raw()
        raw["thresholds"]["capacity_cascade_percent"] = "90"
        with self.assertRaisesRegex(ValidationError, "必须低于"):
            Policy.from_dict(raw)

    def test_policy_rejects_zero_hold_days(self) -> None:
        with self.assertRaisesRegex(ValidationError, "reservation_hold_days"):
            Policy.from_dict(policy_raw(reservation_hold_days=0))

    def test_config_rejects_rated_above_nominal(self) -> None:
        with self.assertRaisesRegex(ValidationError, "不能大于"):
            ComponentConfig.from_dict(config_raw(rated_capacity_kwh="400"))

    def test_measurement_rejects_unknown_kind(self) -> None:
        raw = {
            "record_id": "m1", "kind": "voltage", "value": "1", "source": "班",
            "measured_at": "2026-09-01T00:00:00Z", "recorded_at": "2026-09-01T01:00:00Z",
        }
        with self.assertRaisesRegex(ValidationError, "容量或内阻"):
            Measurement.from_dict(raw, "m")

    def test_measurement_recorded_before_measured_rejected(self) -> None:
        raw = {
            "record_id": "m1", "kind": "capacity_retention_percent", "value": "90", "source": "班",
            "measured_at": "2026-09-02T00:00:00Z", "recorded_at": "2026-09-01T00:00:00Z",
        }
        with self.assertRaisesRegex(ValidationError, "recorded_at"):
            Measurement.from_dict(raw, "m")

    def test_event_requires_known_category_and_severity(self) -> None:
        base = {
            "event_id": "e1", "category": "safety", "severity": "critical", "source": "台账",
            "occurred_at": "2026-09-01T00:00:00Z", "recorded_at": "2026-09-01T01:00:00Z",
            "resolved": False, "note": None,
        }
        bad = dict(base, category="warranty")
        with self.assertRaisesRegex(ValidationError, "maintenance 或 safety"):
            QualityEvent.from_dict(bad, "e")
        bad = dict(base, severity="apocalyptic")
        with self.assertRaisesRegex(ValidationError, "severity"):
            QualityEvent.from_dict(bad, "e")

    def test_bundle_rejects_duplicate_ids(self) -> None:
        measurement = {
            "record_id": "m1", "kind": "capacity_retention_percent", "value": "90", "source": "班",
            "measured_at": "2026-09-01T00:00:00Z", "recorded_at": "2026-09-01T01:00:00Z",
        }
        with self.assertRaisesRegex(ValidationError, "record_id 不能重复"):
            EvidenceBundle.from_dict({"measurements": [measurement, dict(measurement)], "events": []})


if __name__ == "__main__":
    unittest.main()
