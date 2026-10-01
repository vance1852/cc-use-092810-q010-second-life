from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone

from battery_retirement.api import JsonApplication
from battery_retirement.clock import FrozenClock
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


class RetirementApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc))
        self.service = RetirementService(self.connection, self.clock)
        self.app = JsonApplication(self.service)
        for user_id, role in (
            ("operator", "operator"),
            ("approver", "approver"),
            ("cascade", "cascade_manager"),
            ("auditor", "auditor"),
        ):
            self.app.handle("POST", "/users", body=json.dumps({
                "user_id": user_id, "display_name": user_id, "role": role,
            }).encode())

    def tearDown(self) -> None:
        self.connection.close()

    def _call(self, method: str, path: str, actor: str | None = None, payload: dict | None = None):
        headers = {} if actor is None else {"X-Actor-Id": actor}
        body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        return self.app.handle(method, path, headers=headers, body=body)

    def test_health(self) -> None:
        response = self._call("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_missing_actor_header(self) -> None:
        response = self._call("POST", "/components", payload={"component_id": "x"})
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_full_flow_via_http(self) -> None:
        r = self._call("POST", "/policies", "approver", POLICY)
        self.assertEqual(r.status, 201, r.body)
        r = self._call("POST", "/components", "operator", {
            "component_id": "rack-1", "station_id": "station-1", "model_name": "簇",
            "chemistry": "LFP", "rated_capacity_kwh": "100", "acquisition_cost_cny": "100000",
            "baseline_resistance_milliohm": "10", "commissioned_at": "2018-06-01T00:00:00Z",
        })
        self.assertEqual(r.status, 201, r.body)
        records = [
            {"component_id": "rack-1", "kind": "capacity", "measured_at": "2026-08-20T00:00:00Z",
             "source_batch": "lab", "source_row": "cap", "value": "70", "evidence_ref": "d1"},
            {"component_id": "rack-1", "kind": "resistance", "measured_at": "2026-08-20T01:00:00Z",
             "source_batch": "lab", "source_row": "res", "value": "12", "evidence_ref": "d2"},
            {"component_id": "rack-1", "kind": "repair", "measured_at": "2026-03-01T00:00:00Z",
             "source_batch": "site", "source_row": "rep", "severity": "low", "status": "closed",
             "evidence_ref": "d3"},
            {"component_id": "rack-1", "kind": "safety", "measured_at": "2026-03-02T00:00:00Z",
             "source_batch": "site", "source_row": "saf", "severity": "low", "status": "closed",
             "evidence_ref": "d4"},
        ]
        r = self._call("POST", "/measurements", "operator", {"records": records})
        self.assertEqual(r.status, 201, r.body)

        r = self._call("POST", "/assessments", "operator", {
            "assessment_id": "a1", "component_id": "rack-1",
            "policy_id": "pol-1", "policy_version": 1, "windows": WINDOWS,
        })
        self.assertEqual(r.status, 201, r.body)
        self.assertEqual(r.body["recommendation"], "cascade_utilization")

        r = self._call("POST", "/assessments/a1/decision", "approver",
                       {"approve": True, "note": "批准"})
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(r.body["state"], "approved")

        r = self._call("GET", "/assessments/a1/recompute", "auditor")
        self.assertEqual(r.status, 200, r.body)
        self.assertTrue(r.body["checks"]["matches"])

        r = self._call("POST", "/cascade/batches", "cascade", {"batch_id": "b1", "note": ""})
        self.assertEqual(r.status, 201, r.body)
        r = self._call("POST", "/cascade/batches/b1/items", "cascade", {"component_id": "rack-1"})
        self.assertEqual(r.status, 201, r.body)
        r = self._call("POST", "/cascade/batches/b1/seal", "cascade")
        self.assertEqual(r.status, 200, r.body)
        r = self._call("POST", "/projects", "cascade", {
            "project_id": "proj-1", "batch_id": "b1",
            "requested_capacity_kwh": "70", "hold_days": 30,
        })
        self.assertEqual(r.status, 201, r.body)
        r = self._call("POST", "/projects/proj-1/confirm", "cascade", {"reference": "contract-1"})
        self.assertEqual(r.status, 200, r.body)
        r = self._call("GET", "/components/rack-1/disposition", "auditor")
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(r.body["final_destination"], "cascade")
        r = self._call("GET", "/dispositions/register", "auditor")
        self.assertEqual(r.status, 200, r.body)
        self.assertTrue(r.body["consistent"])
        r = self._call("GET", "/audit/chain", "auditor")
        self.assertEqual(r.status, 200, r.body)
        self.assertTrue(r.body["valid"])

    def test_error_shape_on_unknown_route(self) -> None:
        response = self._call("GET", "/nope", "auditor")
        self.assertEqual(response.status, 404)
        self.assertEqual(response.body["error"]["code"], "route_not_found")

    def test_preview_route(self) -> None:
        self._call("POST", "/policies", "approver", POLICY)
        self._call("POST", "/components", "operator", {
            "component_id": "rack-9", "station_id": "station-1", "model_name": "簇",
            "chemistry": "LFP", "rated_capacity_kwh": "100", "acquisition_cost_cny": "100000",
            "baseline_resistance_milliohm": "10", "commissioned_at": "2018-06-01T00:00:00Z",
        })
        r = self._call("POST", "/assessments/rack-9/preview", "auditor", {
            "policy_id": "pol-1", "policy_version": 1, "windows": WINDOWS,
        })
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(r.body["recommendation"], "pending_evidence")


if __name__ == "__main__":
    unittest.main()
