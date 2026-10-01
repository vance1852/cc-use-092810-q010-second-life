"""退役评估与梯次利用完整流程的离线验收入口。"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import RetirementService
from .storage import connect, inspect_schema


POLICY = {
    "policy_id": "retirement-standard",
    "version": 1,
    "title": "储能电站退役评估与梯次利用基准政策",
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


def _component(component_id: str, rated: str):
    return {
        "component_id": component_id,
        "revision": 1,
        "model_name": "LFP-280Ah 电池簇",
        "chemistry": "LFP",
        "nominal_capacity_kwh": "300",
        "rated_capacity_kwh": rated,
        "commissioned_at": "2020-06-01T00:00:00Z",
        "replaced_parts": [],
    }


def _measurement(record_id, kind, value, recorded_at, *, measured_at=None, source="站内检测班"):
    return {
        "record_id": record_id,
        "kind": kind,
        "value": value,
        "source": source,
        "measured_at": measured_at or "2026-09-15T08:00:00Z",
        "recorded_at": recorded_at,
    }


def run(workspace: Path) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="battery-retirement-") as temporary:
        database = Path(temporary) / "retirement.sqlite3"
        connection = connect(database)
        try:
            clock = FrozenClock(datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc))
            service = RetirementService(connection, clock)
            for user_id, role in (
                ("engineer-1", "engineer"),
                ("approver-1", "approver"),
                ("planner-1", "planner"),
                ("auditor-1", "auditor"),
            ):
                service.create_user(user_id, user_id, role)

            service.publish_policy("engineer-1", POLICY)

            rated = {
                "comp-keep": "300", "comp-derate": "250", "comp-cascade": "200",
                "comp-cascade-2": "180", "comp-recycle": "150", "comp-pending": "100",
            }
            for component_id, capacity in rated.items():
                service.register_component("engineer-1", _component(component_id, capacity))

            window_start = "2026-09-01T00:00:00Z"
            cutoff = "2026-09-30T23:59:59Z"

            service.import_measurements("engineer-1", "comp-keep", [
                _measurement("keep-cap", "capacity_retention_percent", "92", "2026-09-20T10:00:00Z"),
                _measurement("keep-res", "internal_resistance_percent", "110", "2026-09-20T10:05:00Z"),
            ])
            service.import_measurements("engineer-1", "comp-derate", [
                _measurement("derate-cap", "capacity_retention_percent", "80", "2026-09-20T10:00:00Z"),
                _measurement("derate-res", "internal_resistance_percent", "115", "2026-09-20T10:05:00Z"),
            ])
            service.import_measurements("engineer-1", "comp-cascade", [
                _measurement("casc-cap", "capacity_retention_percent", "73", "2026-09-20T10:00:00Z"),
                _measurement("casc-res", "internal_resistance_percent", "140", "2026-09-20T10:05:00Z"),
            ])
            service.import_measurements("engineer-1", "comp-cascade-2", [
                _measurement("casc2-cap", "capacity_retention_percent", "72", "2026-09-20T10:00:00Z"),
                _measurement("casc2-res", "internal_resistance_percent", "138", "2026-09-20T10:05:00Z"),
            ])
            service.import_measurements("engineer-1", "comp-recycle", [
                _measurement("rec-cap", "capacity_retention_percent", "60", "2026-09-20T10:00:00Z"),
                _measurement("rec-res", "internal_resistance_percent", "160", "2026-09-20T10:05:00Z"),
            ])
            service.record_quality_event("engineer-1", "comp-recycle", {
                "event_id": "rec-critical-safety",
                "category": "safety",
                "severity": "critical",
                "source": "场站安全台账",
                "occurred_at": "2026-09-10T00:00:00Z",
                "recorded_at": "2026-09-21T09:00:00Z",
                "resolved": False,
                "note": "热失控告警未闭环",
            })

            conclusions = {}
            for component_id, assessment_id in (
                ("comp-keep", "a-keep-1"),
                ("comp-derate", "a-derate-1"),
                ("comp-cascade", "a-cascade-1"),
                ("comp-cascade-2", "a-cascade-2-1"),
                ("comp-recycle", "a-recycle-1"),
                ("comp-pending", "a-pending-1"),
            ):
                opened = service.open_assessment(
                    "engineer-1", assessment_id, component_id, 1,
                    "retirement-standard", 1, window_start, cutoff,
                )
                conclusions[component_id] = opened["conclusion"]
                if component_id != "comp-pending":
                    service.submit_assessment("engineer-1", assessment_id)
                    service.approve_assessment("approver-1", assessment_id, "证据充分，同意结论")

            # 等待补证：证据缺口可查，且不形成生效去向。
            gaps = service.evidence_gaps("engineer-1", "a-pending-1")

            # 委员会复算：冻结版本在晚到证据入库后仍与原结论一致。
            before_late = service.recompute("auditor-1", "a-cascade-1")
            service.import_measurements("engineer-1", "comp-cascade", [
                _measurement("casc-cap-new", "capacity_retention_percent", "91", "2026-10-15T10:00:00Z",
                             measured_at="2026-10-14T08:00:00Z"),
                _measurement("casc-res-new", "internal_resistance_percent", "112", "2026-10-15T10:05:00Z",
                             measured_at="2026-10-14T08:05:00Z"),
            ])
            after_late = service.recompute("auditor-1", "a-cascade-1")

            # 梯次利用：带来源候选批次并封存。
            service.create_candidate_batch("planner-1", "batch-cascade", "2026 秋季梯次候选批次")
            service.add_candidate_component("planner-1", "batch-cascade", "comp-cascade")
            service.add_candidate_component("planner-1", "batch-cascade", "comp-cascade-2")
            sealed_batch = service.seal_candidate_batch("planner-1", "batch-cascade")

            for project_id, name in (
                ("project-a", "园区备电项目 A"),
                ("project-b", "通信基站项目 B"),
                ("project-c", "路灯储能项目 C"),
                ("project-d", "户用储能项目 D"),
            ):
                service.create_reuse_project("planner-1", project_id, name)

            service.reserve_capacity("planner-1", "res-a", "project-a", "batch-cascade", "comp-cascade")
            double_occupation_blocked = False
            try:
                service.reserve_capacity("planner-1", "res-b", "project-b", "batch-cascade", "comp-cascade")
            except Exception:
                double_occupation_blocked = True

            service.reserve_capacity("planner-1", "res-c", "project-c", "batch-cascade", "comp-cascade-2")

            # 撤回与项目失败都安全释放容量，历史保留。
            service.withdraw_reservation("planner-1", "res-a", "项目 A 暂缓，撤回容量预留")
            failed_project = service.fail_project("planner-1", "project-c", "集成商退出，项目终止")

            # 重新预留后再演示到期自动释放。
            service.reserve_capacity("planner-1", "res-e", "project-a", "batch-cascade", "comp-cascade")
            service.reserve_capacity("planner-1", "res-d", "project-d", "batch-cascade", "comp-cascade-2")
            clock.advance(days=31)
            expired = service.expire_due_reservations("planner-1")
            # 到期释放后容量可被新项目再次占用。
            service.reserve_capacity("planner-1", "res-f", "project-d", "batch-cascade", "comp-cascade-2")
            history = service.reservation_history("auditor-1", "comp-cascade-2")

            # 新证据不覆盖旧结论：容量释放后，复核触发一个仍需独立审批的新版本。
            review = service.request_review(
                "engineer-1", "a-cascade-1", "复检容量恢复，申请重评",
                ["casc-cap-new", "casc-res-new"],
            )
            decided = service.decide_review("approver-1", review["review_id"], True, "新证据有效，重开评估")
            successor = decided["new_assessment_id"]
            service.submit_assessment("engineer-1", successor)
            service.approve_assessment("approver-1", successor, "复检后满足继续服役条件")
            cascade_disposition = service.disposition_report("auditor-1", "comp-cascade")

            residual = service.residual_value("auditor-1", "a-recycle-1")
            reconciliation = service.reconciliation("auditor-1")
            audit = service.audit_chain("auditor-1")
            schema = inspect_schema(connection)
        finally:
            connection.close()

    history_states = [row["state"] for row in history["reservations"]]
    return {
        "status": "ok",
        "conclusions": conclusions,
        "pending_gap_signals": [gap["signal"] for gap in gaps["evidence_gaps"]],
        "frozen_recompute_matches_before_late": before_late["input_matches"]
        and before_late["conclusion_matches"],
        "frozen_recompute_matches_after_late": after_late["input_matches"]
        and after_late["conclusion_matches"],
        "late_evidence_sequestered": len(after_late["recomputed"]["late_evidence"]) == 2,
        "double_occupation_blocked": double_occupation_blocked,
        "batch_sha256": sealed_batch["content_sha256"],
        "project_failure_released": failed_project["released_reservations"],
        "expired_reservations": expired["expired"],
        "reservation_history_states": history_states,
        "reoccupied_after_expiry": history_states[-1] == "held",
        "successor_conclusion": cascade_disposition["final_destination"]["conclusion"],
        "single_effective_destination": cascade_disposition["consistent_single_destination"],
        "recycle_residual_value_cny": residual["residual_value"]["residual_value_cny"],
        "reconciliation_consistent": reconciliation["consistent"],
        "components": reconciliation["component_count"],
        "audit_valid": audit["valid"],
        "audit_events": audit["events"],
        "schema": schema,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行退役评估与梯次利用离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace.resolve()), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
