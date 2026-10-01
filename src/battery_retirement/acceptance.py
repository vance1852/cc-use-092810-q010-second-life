"""退役评估与梯次利用管理的完整离线验收。"""

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
    "policy_id": "retirement-policy",
    "version": 1,
    "title": "储能电站退役判定政策 v1",
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


def _component(component_id: str, rated: str, cost: str, baseline: str | None = "10.000") -> dict:
    return {
        "component_id": component_id,
        "station_id": "station-north",
        "model_name": "LFP-280Ah 电池簇",
        "chemistry": "LFP",
        "rated_capacity_kwh": rated,
        "acquisition_cost_cny": cost,
        "baseline_resistance_milliohm": baseline,
        "commissioned_at": "2018-06-01T00:00:00Z",
    }


def _rec(component_id: str, kind: str, at: str, batch: str, row: str, **extra) -> dict:
    return {
        "component_id": component_id,
        "kind": kind,
        "measured_at": at,
        "source_batch": batch,
        "source_row": row,
        "evidence_ref": f"doc:{batch}/{row}",
        **extra,
    }


def run(workspace: Path) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="battery-retirement-") as temporary:
        database = Path(temporary) / "retirement.sqlite3"
        connection = connect(database)
        try:
            clock = FrozenClock(datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc))
            service = RetirementService(connection, clock)
            for user_id, role in (
                ("operator-1", "operator"),
                ("approver-1", "approver"),
                ("cascade-1", "cascade_manager"),
                ("auditor-1", "auditor"),
            ):
                service.create_user(user_id, user_id, role)

            service.publish_policy("approver-1", POLICY)
            service.register_component("operator-1", _component("rack-a", "100", "120000"))
            service.register_component("operator-1", _component("rack-b", "100", "120000"))
            service.register_component("operator-1", _component("rack-c", "100", "120000"))
            service.register_component("operator-1", _component("rack-d", "100", "120000", baseline=None))

            windows_v1 = {
                "as_of": "2026-09-01T00:00:00Z",
                "capacity_window": {"starts_at": "2026-08-01T00:00:00Z", "ends_at": "2026-08-31T23:59:59Z"},
                "resistance_window": {"starts_at": "2026-08-01T00:00:00Z", "ends_at": "2026-08-31T23:59:59Z"},
                "events_after": "2025-09-01T00:00:00Z",
            }
            service.record_measurements("operator-1", [
                _rec("rack-a", "capacity", "2026-08-20T03:00:00Z", "lab", "a-cap-1", value="95"),
                _rec("rack-a", "resistance", "2026-08-20T03:30:00Z", "lab", "a-res-1", value="10.5"),
                _rec("rack-a", "safety", "2026-03-10T09:00:00Z", "site", "a-saf-1",
                     severity="low", status="closed"),
                _rec("rack-a", "repair", "2026-02-10T09:00:00Z", "site", "a-rep-1",
                     severity="medium", status="closed"),
                # rack-b：梯次利用候选
                _rec("rack-b", "capacity", "2026-08-21T03:00:00Z", "lab", "b-cap-1", value="72"),
                _rec("rack-b", "resistance", "2026-08-21T03:30:00Z", "lab", "b-res-1", value="12.0"),
                _rec("rack-b", "safety", "2026-04-10T09:00:00Z", "site", "b-saf-1",
                     severity="low", status="closed"),
                _rec("rack-b", "repair", "2026-04-11T09:00:00Z", "site", "b-rep-1",
                     severity="low", status="closed"),
                # rack-c：拆解回收
                _rec("rack-c", "capacity", "2026-08-22T03:00:00Z", "lab", "c-cap-1", value="40"),
                _rec("rack-c", "resistance", "2026-08-22T03:30:00Z", "lab", "c-res-1", value="16.0"),
                _rec("rack-c", "safety", "2026-04-10T09:00:00Z", "site", "c-saf-1",
                     severity="low", status="closed"),
                _rec("rack-c", "repair", "2026-04-11T09:00:00Z", "site", "c-rep-1",
                     severity="low", status="closed"),
                # rack-d：有容量但缺内阻台账 → 等待补证
                _rec("rack-d", "capacity", "2026-08-23T03:00:00Z", "lab", "d-cap-1", value="96"),
                _rec("rack-d", "repair", "2026-04-11T09:00:00Z", "site", "d-rep-1",
                     severity="low", status="closed"),
                _rec("rack-d", "safety", "2026-04-12T09:00:00Z", "site", "d-saf-1",
                     severity="low", status="closed"),
            ])

            assessment_a = service.prepare_assessment(
                "operator-1", "assess-a-v1", "rack-a", "retirement-policy", 1, windows_v1
            )
            assessment_b = service.prepare_assessment(
                "operator-1", "assess-b-v1", "rack-b", "retirement-policy", 1, windows_v1
            )
            assessment_c = service.prepare_assessment(
                "operator-1", "assess-c-v1", "rack-c", "retirement-policy", 1, windows_v1
            )
            assessment_d = service.prepare_assessment(
                "operator-1", "assess-d-v1", "rack-d", "retirement-policy", 1, windows_v1
            )
            assert assessment_a["recommendation"] == "continue_service"
            assert assessment_b["recommendation"] == "cascade_utilization"
            assert assessment_c["recommendation"] == "recycle"
            assert assessment_d["recommendation"] == "pending_evidence"
            assert assessment_d["evidence_gaps"]

            # 提交态结论不生效；独立审批（不能自审）后才生效。
            assert service.component_disposition("rack-a")["effective_assessment_id"] is None
            for assessment_id in ("assess-a-v1", "assess-b-v1", "assess-c-v1", "assess-d-v1"):
                service.decide_assessment("approver-1", assessment_id, True, "证据与阈值核对一致，批准")

            # 晚到检测结果进入台账，但不改变已生效结论。
            service.record_measurements("operator-1", [
                _rec("rack-a", "capacity", "2026-08-25T03:00:00Z", "lab-late", "a-cap-late", value="70"),
                _rec("rack-a", "resistance", "2026-08-25T03:30:00Z", "lab-late", "a-res-late", value="12.0"),
            ])
            effective_a = service.component_disposition("rack-a")
            assert effective_a["effective_recommendation"] == "continue_service"
            recomputed_a = service.recompute_assessment("auditor-1", "assess-a-v1")
            assert recomputed_a["checks"]["matches"]

            # 新证据只能触发复核申请；受理后在新窗口派生新版本。
            review = service.request_review(
                "operator-1", "assess-a-v1", "晚到容量检测显示 SOH 明显下降",
                new_windows={
                    "as_of": "2026-09-05T00:00:00Z",
                    "capacity_window": {"starts_at": "2026-08-15T00:00:00Z", "ends_at": "2026-09-04T23:59:59Z"},
                    "resistance_window": {"starts_at": "2026-08-15T00:00:00Z", "ends_at": "2026-09-04T23:59:59Z"},
                    "events_after": "2025-09-01T00:00:00Z",
                },
            )
            service.decide_review("approver-1", review["review_id"], True, "同意按新窗口复评")
            # 复核通过前 rack-a 不能直接进梯次批次（生效结论仍是继续服役）。
            assessment_a2 = service.prepare_assessment(
                "operator-1", "assess-a-v2", "rack-a", "retirement-policy", 1,
                {
                    "as_of": "2026-09-05T00:00:00Z",
                    "capacity_window": {"starts_at": "2026-08-15T00:00:00Z", "ends_at": "2026-09-04T23:59:59Z"},
                    "resistance_window": {"starts_at": "2026-08-15T00:00:00Z", "ends_at": "2026-09-04T23:59:59Z"},
                    "events_after": "2025-09-01T00:00:00Z",
                },
                source_review_id=review["review_id"],
            )
            # 新窗口采信晚到记录：SOH 降到 82.5%、内阻增幅 20% → 新版本结论变为降额使用。
            assert assessment_a2["recommendation"] == "derate", assessment_a2["recommendation"]
            # 新版本尚未审批，生效结论仍是冻结的 v1（继续服役）。
            assert service.component_disposition("rack-a")["effective_assessment_id"] == "assess-a-v1"

            # 梯次利用：rack-b 组成带来源候选批次。
            service.create_cascade_batch("cascade-1", "batch-2026-09", "9 月梯次候选")
            service.add_to_cascade_batch("cascade-1", "batch-2026-09", "rack-b")
            sealed = service.seal_cascade_batch("cascade-1", "batch-2026-09")
            assert sealed["state"] == "sealed"

            # 两个项目不能重复占用同一容量：项目一预留 72kWh 后，项目二无容量可用。
            service.reserve_project("cascade-1", "proj-1", "batch-2026-09", "72", hold_days=30)
            try:
                service.reserve_project("cascade-1", "proj-2", "batch-2026-09", "10", hold_days=30)
                raise RuntimeError("重复占用应当被拒绝")
            except Exception as exc:
                assert "空闲容量不足" in str(exc)

            # 项目一撤回：容量安全释放，项目二可以占用；历史保留。
            service.withdraw_project("cascade-1", "proj-1", "梯次项目立项取消")
            history = service.project("proj-1")
            assert history["state"] == "released"
            service.reserve_project("cascade-1", "proj-2", "batch-2026-09", "72", hold_days=30)

            # 过期预留由清扫任务安全释放。
            clock.advance(days=31)
            swept = service.expire_holds("cascade-1")
            assert swept["expired_projects"] == ["proj-2"]
            service.reserve_project("cascade-1", "proj-3", "batch-2026-09", "72", hold_days=15)
            # 项目失败同样安全释放，组件可进入新批次。
            service.mark_project_failed("cascade-1", "proj-3", "梯次场景落地失败")
            service.withdraw_cascade_batch("cascade-1", "batch-2026-09", "候选批次整体撤回重组")

            # 重新组批并由成功项目确认最终去向。
            service.create_cascade_batch("cascade-1", "batch-2026-10", "10 月梯次候选")
            service.add_to_cascade_batch("cascade-1", "batch-2026-10", "rack-b")
            service.seal_cascade_batch("cascade-1", "batch-2026-10")
            service.reserve_project("cascade-1", "proj-final", "batch-2026-10", "72", hold_days=30)
            service.confirm_project("cascade-1", "proj-final", "contract-2026-10/cascade-rack-b")
            disposition_b = service.component_disposition("rack-b")
            assert disposition_b["final_destination"] == "cascade"

            # 非梯次路径：rack-c 拆解回收，去向必须与生效结论一致。
            service.confirm_disposition("operator-1", "rack-c", "recycled", "manifest-recycle-001")
            try:
                service.confirm_disposition("operator-1", "rack-c", "continued", "wrong")
                raise RuntimeError("去向与结论不一致应当被拒绝")
            except Exception as exc:
                assert "生效结论" in str(exc)

            register = service.disposition_register("auditor-1")
            schema = inspect_schema(connection)
            audit = service.audit_chain("auditor-1")
        finally:
            connection.close()
    return {
        "status": "ok",
        "initial": {
            "rack-a": assessment_a["recommendation"],
            "rack-b": assessment_b["recommendation"],
            "rack-c": assessment_c["recommendation"],
            "rack-d": assessment_d["recommendation"],
        },
        "rack-a_v2_after_late_evidence": assessment_a2["recommendation"],
        "rack-b_final": disposition_b["final_destination"],
        "register_consistent": register["consistent"],
        "violations": register["violations"],
        "components": len(register["components"]),
        "audit_valid": audit["valid"],
        "audit_events": audit["events"],
        "schema": schema,
        "workspace": workspace.name,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行退役评估与梯次利用管理离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace.resolve()), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
