from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone

from battery_retirement.api import JsonApplication
from battery_retirement.clock import FrozenClock
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


def component(component_id, rated="200"):
    return {
        "component_id": component_id,
        "revision": 1,
        "model_name": "LFP",
        "chemistry": "LFP",
        "nominal_capacity_kwh": "300",
        "rated_capacity_kwh": rated,
        "commissioned_at": "2020-06-01T00:00:00Z",
        "replaced_parts": [],
    }


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        clock = FrozenClock(datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc))
        self.app = JsonApplication(RetirementService(self.connection, clock))

    def tearDown(self) -> None:
        self.connection.close()

    def request(self, method, path, payload=None, actor="eng"):
        body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if actor is not None:
            headers["X-Actor-Id"] = actor
        return self.app.handle(method, path, headers, body)

    def seed(self):
        self.request("POST", "/users", {"user_id": "eng", "display_name": "工程师", "role": "engineer"}, actor=None)
        self.request("POST", "/users", {"user_id": "app", "display_name": "审批人", "role": "approver"})
        self.request("POST", "/users", {"user_id": "plan", "display_name": "规划师", "role": "planner"})
        self.request("POST", "/users", {"user_id": "aud", "display_name": "审计", "role": "auditor"})
        self.request("POST", "/policies", POLICY)

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_requires_actor(self) -> None:
        response = self.request("POST", "/components", component("c1"), actor=None)
        self.assertEqual(response.status, 422)

    def test_malformed_json(self) -> None:
        response = self.app.handle(
            "POST", "/components", {"X-Actor-Id": "eng"}, b"not-json"
        )
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_unknown_route(self) -> None:
        response = self.app.handle("GET", "/nope", {"X-Actor-Id": "eng"})
        self.assertEqual(response.status, 404)

    def test_full_cascade_flow_and_double_occupation_conflict(self) -> None:
        self.seed()
        response = self.request("POST", "/components", component("c1"))
        self.assertEqual(response.status, 201)
        response = self.request("POST", "/components/c1/measurements", {"measurements": [
            {"record_id": "cap", "kind": "capacity_retention_percent", "value": "73", "source": "班",
             "measured_at": "2026-09-15T08:00:00Z", "recorded_at": "2026-09-20T10:00:00Z"},
            {"record_id": "res", "kind": "internal_resistance_percent", "value": "140", "source": "班",
             "measured_at": "2026-09-15T08:05:00Z", "recorded_at": "2026-09-20T10:05:00Z"},
        ]})
        self.assertEqual(response.status, 201)
        response = self.request("POST", "/assessments", {
            "assessment_id": "c1-a1", "component_id": "c1", "config_revision": 1,
            "policy_id": "p", "policy_version": 1,
            "window_start": "2026-09-01T00:00:00Z", "window_cutoff": "2026-09-30T23:59:59Z",
        })
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["conclusion"], "cascade")
        self.assertEqual(self.request("POST", "/assessments/c1-a1/submit", {}).status, 200)
        # 提交人不能独立审批。
        forbidden = self.request("POST", "/assessments/c1-a1/approve", {"note": "自批"}, actor="eng")
        self.assertEqual(forbidden.status, 403)
        self.assertEqual(self.request("POST", "/assessments/c1-a1/approve", {"note": "同意"}, actor="app").status, 200)

        recompute = self.request("POST", "/assessments/c1-a1/recompute", {}, actor="aud")
        self.assertTrue(recompute.body["input_matches"])
        gaps = self.request("GET", "/assessments/c1-a1/evidence_gaps", actor="aud")
        self.assertEqual(gaps.body["evidence_gaps"], [])

        self.assertEqual(self.request("POST", "/candidate_batches", {"batch_id": "b1"}, actor="plan").status, 201)
        self.assertEqual(self.request("POST", "/candidate_batches/b1/components", {"component_id": "c1"}, actor="plan").status, 201)
        self.assertEqual(self.request("POST", "/candidate_batches/b1/seal", {}, actor="plan").status, 200)
        self.request("POST", "/reuse_projects", {"project_id": "pa", "name": "A"}, actor="plan")
        self.request("POST", "/reuse_projects", {"project_id": "pb", "name": "B"}, actor="plan")
        reserve = {
            "reservation_id": "r1", "project_id": "pa", "batch_id": "b1", "component_id": "c1",
        }
        self.assertEqual(self.request("POST", "/reservations", reserve, actor="plan").status, 201)
        conflict = self.request("POST", "/reservations", {**reserve, "reservation_id": "r2", "project_id": "pb"}, actor="plan")
        self.assertEqual(conflict.status, 409)
        self.assertEqual(conflict.body["error"]["code"], "conflict")

        reconciliation = self.request("GET", "/reconciliation", actor="aud")
        self.assertTrue(reconciliation.body["consistent"])


if __name__ == "__main__":
    unittest.main()
