from __future__ import annotations

import unittest
from pathlib import Path

from battery_retirement.acceptance import run


ROOT = Path(__file__).resolve().parents[1]


class RetirementAcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["conclusions"]["comp-keep"], "continue_service")
        self.assertEqual(result["conclusions"]["comp-derate"], "derating")
        self.assertEqual(result["conclusions"]["comp-cascade"], "cascade")
        self.assertEqual(result["conclusions"]["comp-recycle"], "recycle")
        self.assertEqual(result["conclusions"]["comp-pending"], "pending_evidence")
        self.assertEqual(
            result["pending_gap_signals"],
            ["capacity_retention_percent", "internal_resistance_percent"],
        )
        self.assertTrue(result["frozen_recompute_matches_before_late"])
        self.assertTrue(result["frozen_recompute_matches_after_late"])
        self.assertTrue(result["late_evidence_sequestered"])
        self.assertTrue(result["double_occupation_blocked"])
        self.assertEqual(len(result["batch_sha256"]), 64)
        self.assertEqual(result["project_failure_released"], 1)
        self.assertEqual(result["expired_reservations"], 2)
        self.assertEqual(result["reservation_history_states"], ["failed", "expired", "held"])
        self.assertTrue(result["reoccupied_after_expiry"])
        self.assertEqual(result["successor_conclusion"], "continue_service")
        self.assertTrue(result["single_effective_destination"])
        self.assertEqual(result["recycle_residual_value_cny"], "1500.00")
        self.assertTrue(result["reconciliation_consistent"])
        self.assertTrue(result["audit_valid"])
        self.assertEqual(result["schema"]["missing_tables"], [])
        self.assertEqual(result["schema"]["schema_version"], "1")


if __name__ == "__main__":
    unittest.main()
