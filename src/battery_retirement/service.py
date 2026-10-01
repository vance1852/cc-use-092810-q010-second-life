"""退役评估与梯次利用管理的领域用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .clock import SystemClock, isoformat
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json, content_digest
from .models import (
    AssessmentWindows,
    Component,
    MeasurementRecord,
    RetirementPolicy,
)
from .policy import (
    CASCADE,
    CONTINUE_SERVICE,
    DERATE,
    PENDING_EVIDENCE,
    RECYCLE,
    POLICY_ENGINE_VERSION,
    evaluate_component,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "operator": {
        "component.write", "measurement.write", "assessment.prepare",
        "review.request", "disposition.confirm",
    },
    "approver": {"assessment.approve", "review.review", "report.read"},
    "cascade_manager": {"batch.write", "project.write", "hold.expire"},
    "auditor": {"report.read", "audit.read"},
}

_DESTINATION_BY_RECOMMENDATION = {
    CONTINUE_SERVICE: "continued",
    DERATE: "derated",
    CASCADE: "cascade",
    RECYCLE: "recycled",
}

_QUANT = Decimal("0.001")
_MONEY_QUANT = Decimal("0.01")


def _record_payload(item: MeasurementRecord) -> dict[str, Any]:
    return {
        "component_id": item.component_id,
        "kind": item.kind,
        "measured_at": item.measured_at,
        "source_batch": item.source_batch,
        "source_row": item.source_row,
        "value": None if item.value is None else format(item.value, "f"),
        "severity": item.severity,
        "status": item.status,
        "evidence_ref": item.evidence_ref,
    }


def _row_to_record(row: sqlite3.Row) -> MeasurementRecord:
    return MeasurementRecord(
        component_id=row["component_id"],
        kind=row["kind"],
        measured_at=row["measured_at"],
        source_batch=row["source_batch"],
        source_row=row["source_row"],
        value=None if row["value"] is None else Decimal(row["value"]),
        severity=row["severity"],
        status=row["status"],
        evidence_ref=row["evidence_ref"],
    )


class RetirementService:
    """在单个 SQLite 连接上提供退役评估与梯次利用全部操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return isoformat(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM retirement_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"用户不存在: {user_id}")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM retirement_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO retirement_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    # ------------------------------------------------------------------ 用户

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed(f"未知角色: {role}")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO retirement_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"用户已存在: {user_id}") from exc
        return {"user_id": user_id.strip(), "role": role}

    # ------------------------------------------------------------------ 政策

    def publish_policy(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        # 政策版本属于治理前置物，由独立审批角色（委员会）发布。
        self._require(actor_id, "assessment.approve")
        try:
            policy = RetirementPolicy.from_dict(raw)
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        text = canonical_json(raw)
        sha = content_digest(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO policy_versions(policy_id,version,title,canonical_json,content_sha256,"
                    "published_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (policy.policy_id, policy.version, policy.title, text, sha, actor_id, self._now()),
                )
                self._audit(
                    "policy",
                    f"{policy.policy_id}@{policy.version}",
                    "policy.published",
                    actor_id,
                    {"sha256": sha},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("政策版本编号或内容摘要已经存在") from exc
        return {"policy_id": policy.policy_id, "version": policy.version, "sha256": sha}

    def _policy(self, policy_id: str, version: int) -> tuple[RetirementPolicy, str]:
        row = self.connection.execute(
            "SELECT canonical_json,content_sha256 FROM policy_versions WHERE policy_id=? AND version=?",
            (policy_id, version),
        ).fetchone()
        if row is None:
            raise NotFound("政策版本不存在")
        return RetirementPolicy.from_dict(json.loads(row["canonical_json"])), row["content_sha256"]

    # -------------------------------------------------------------- 资产配置

    def register_component(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "component.write")
        try:
            component = Component.from_dict(raw)
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        sha = content_digest(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO components(component_id,station_id,model_name,chemistry,rated_capacity_kwh,"
                    "acquisition_cost_cny,baseline_resistance_milliohm,commissioned_at,config_sha256,"
                    "registered_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        component.component_id,
                        component.station_id,
                        component.model_name,
                        component.chemistry,
                        format(component.rated_capacity_kwh, "f"),
                        format(component.acquisition_cost_cny, "f"),
                        None
                        if component.baseline_resistance_milliohm is None
                        else format(component.baseline_resistance_milliohm, "f"),
                        component.commissioned_at,
                        sha,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit(
                    "component", component.component_id, "component.registered", actor_id, {"sha256": sha}
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"组件已存在: {component.component_id}") from exc
        return {"component_id": component.component_id, "config_sha256": sha}

    def _component_row(self, component_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM components WHERE component_id=?", (component_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"组件不存在: {component_id}")
        return row

    def _component_from_row(self, row: sqlite3.Row) -> Component:
        return Component(
            component_id=row["component_id"],
            station_id=row["station_id"],
            model_name=row["model_name"],
            chemistry=row["chemistry"],
            rated_capacity_kwh=Decimal(row["rated_capacity_kwh"]),
            acquisition_cost_cny=Decimal(row["acquisition_cost_cny"]),
            baseline_resistance_milliohm=None
            if row["baseline_resistance_milliohm"] is None
            else Decimal(row["baseline_resistance_milliohm"]),
            commissioned_at=row["commissioned_at"],
        )

    # -------------------------------------------------------------- 测量台账

    def record_measurements(
        self, actor_id: str, raw_rows: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        """登记容量/内阻/维修/安全记录。晚到证据只进台账，绝不回写既有结论。"""

        self._require(actor_id, "measurement.write")
        if not raw_rows:
            raise ValidationFailed("测量记录数组不能为空")
        parsed: list[MeasurementRecord] = []
        for index, raw in enumerate(raw_rows):
            try:
                parsed.append(MeasurementRecord.from_dict(raw))
            except ValueError as exc:
                raise ValidationFailed(f"第 {index + 1} 条记录: {exc}") from exc
        component_ids = {item.component_id for item in parsed}
        for component_id in component_ids:
            self._component_row(component_id)
        inserted: list[dict[str, Any]] = []
        with transaction(self.connection, immediate=True):
            for item, raw in zip(parsed, raw_rows):
                sha = content_digest(raw)
                try:
                    cursor = self.connection.execute(
                        "INSERT INTO measurement_records(component_id,kind,measured_at,source_batch,source_row,"
                        "value,severity,status,evidence_ref,content_sha256,recorded_by,recorded_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            item.component_id,
                            item.kind,
                            item.measured_at,
                            item.source_batch,
                            item.source_row,
                            None if item.value is None else format(item.value, "f"),
                            item.severity,
                            item.status,
                            item.evidence_ref,
                            sha,
                            actor_id,
                            self._now(),
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise Conflict(
                        f"记录来源重复: {item.component_id}/{item.source_batch}/{item.source_row}"
                    ) from exc
                inserted.append({"record_id": cursor.lastrowid, "kind": item.kind})
            self._audit(
                "component",
                sorted(component_ids)[0] if len(component_ids) == 1 else "*",
                "measurements.recorded",
                actor_id,
                {"components": sorted(component_ids), "count": len(parsed)},
            )
        return {"inserted": len(inserted), "records": inserted}

    def _ledger_records(self, component_id: str) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                "SELECT * FROM measurement_records WHERE component_id=? ORDER BY record_id",
                (component_id,),
            ).fetchall()
        )

    # ------------------------------------------------------------------ 评估

    def _build_assessment(
        self,
        component_row: sqlite3.Row,
        policy: RetirementPolicy,
        policy_sha: str,
        windows: AssessmentWindows,
    ) -> tuple[dict[str, Any], str, dict[str, Any]]:
        component = self._component_from_row(component_row)
        rows = self._ledger_records(component.component_id)
        records = [_row_to_record(row) for row in rows]
        snapshot_records = [
            {
                "record_id": row["record_id"],
                "content_sha256": row["content_sha256"],
                "payload": _record_payload(item),
            }
            for row, item in zip(rows, records)
        ]
        result = evaluate_component(component, records, windows, policy)
        windows_value = {
            "as_of": windows.as_of,
            "capacity_window": {
                "starts_at": windows.capacity_window.starts_at,
                "ends_at": windows.capacity_window.ends_at,
            },
            "resistance_window": {
                "starts_at": windows.resistance_window.starts_at,
                "ends_at": windows.resistance_window.ends_at,
            },
            "events_after": windows.events_after,
        }
        frozen_snapshot = {
            "component_config": {
                "component_id": component.component_id,
                "station_id": component.station_id,
                "model_name": component.model_name,
                "chemistry": component.chemistry,
                "rated_capacity_kwh": format(component.rated_capacity_kwh, "f"),
                "acquisition_cost_cny": format(component.acquisition_cost_cny, "f"),
                "baseline_resistance_milliohm": None
                if component.baseline_resistance_milliohm is None
                else format(component.baseline_resistance_milliohm, "f"),
                "commissioned_at": component.commissioned_at,
            },
            "config_sha256": component_row["config_sha256"],
            "policy_id": policy.policy_id,
            "policy_version": policy.version,
            "policy_sha256": policy_sha,
            "policy_engine_version": POLICY_ENGINE_VERSION,
            "windows": windows_value,
            "records": snapshot_records,
        }
        input_sha = content_digest(frozen_snapshot)
        return result.as_dict(), input_sha, frozen_snapshot

    def prepare_assessment(
        self,
        actor_id: str,
        assessment_id: str,
        component_id: str,
        policy_id: str,
        policy_version: int,
        windows_raw: Mapping[str, Any],
        source_review_id: int | None = None,
    ) -> dict[str, Any]:
        """冻结配置/窗口/证据/政策版本并产出解释性结论（提交态，尚不生效）。"""

        self._require(actor_id, "assessment.prepare")
        component_row = self._component_row(component_id)
        if self.connection.execute(
            "SELECT 1 FROM disposition_confirmations WHERE component_id=?", (component_id,)
        ).fetchone():
            raise InvalidState("组件已有最终去向确认，不能再产生新评估版本")
        try:
            windows = AssessmentWindows.from_dict(windows_raw)
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        policy, policy_sha = self._policy(policy_id, policy_version)

        review_row = None
        if source_review_id is not None:
            review_row = self.connection.execute(
                "SELECT * FROM review_requests WHERE review_id=?", (source_review_id,)
            ).fetchone()
            if review_row is None:
                raise NotFound("复核申请不存在")
            if review_row["component_id"] != component_id:
                raise ValidationFailed("复核申请与组件不匹配")
            if review_row["status"] != "accepted":
                raise InvalidState("只有已受理的复核申请可以派生新版本")
            if review_row["spawned_assessment_id"] is not None:
                raise InvalidState("该复核申请已派生过评估版本")
            requested_windows = review_row["requested_windows_json"]
            if requested_windows is not None:
                requested = json.loads(requested_windows)
                if canonical_json(requested) != canonical_json(windows_raw):
                    raise InvalidState("新版本窗口必须与复核申请受理的窗口一致")

        result, input_sha, frozen_snapshot = self._build_assessment(
            component_row, policy, policy_sha, windows
        )

        # 相同输入必然得到相同结论：直接返回既有版本，保证复算幂等。
        existing = self.connection.execute(
            "SELECT assessment_id FROM assessments WHERE component_id=? AND input_sha256=?",
            (component_id, input_sha),
        ).fetchone()
        if existing is not None:
            return {**self.assessment(existing["assessment_id"]), "replayed": True}

        with transaction(self.connection, immediate=True):
            latest = self.connection.execute(
                "SELECT coalesce(max(version),0) AS v FROM assessments WHERE component_id=?",
                (component_id,),
            ).fetchone()["v"]
            version = latest + 1
            try:
                self.connection.execute(
                    "INSERT INTO assessments(assessment_id,component_id,version,policy_id,policy_version,"
                    "policy_engine_version,windows_json,frozen_snapshot_json,input_sha256,recommendation,"
                    "recommendation_label,explanations_json,evidence_gaps_json,metrics_json,"
                    "residual_value_cny,state,source_review_id,prepared_by,prepared_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        assessment_id,
                        component_id,
                        version,
                        policy_id,
                        policy_version,
                        POLICY_ENGINE_VERSION,
                        canonical_json(result["basis"]["windows"]),
                        canonical_json(frozen_snapshot),
                        input_sha,
                        result["recommendation"],
                        result["recommendation_label"],
                        canonical_json(result["explanations"]),
                        canonical_json(result["evidence_gaps"]),
                        canonical_json(result["metrics"]),
                        result["residual_value_cny"],
                        "submitted",
                        source_review_id,
                        actor_id,
                        self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("评估编号、版本或输入摘要冲突") from exc
            if review_row is not None:
                self.connection.execute(
                    "UPDATE review_requests SET spawned_assessment_id=? WHERE review_id=? AND status='accepted'",
                    (assessment_id, source_review_id),
                )
            self._audit(
                "assessment",
                assessment_id,
                "assessment.prepared",
                actor_id,
                {
                    "component_id": component_id,
                    "version": version,
                    "recommendation": result["recommendation"],
                    "input_sha256": input_sha,
                    "source_review_id": source_review_id,
                },
            )
        return {**self.assessment(assessment_id), "replayed": False}

    def assessment(self, assessment_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM assessments WHERE assessment_id=?", (assessment_id,)
        ).fetchone()
        if row is None:
            raise NotFound("评估结论不存在")
        snapshot = json.loads(row["frozen_snapshot_json"])
        return {
            "assessment_id": row["assessment_id"],
            "component_id": row["component_id"],
            "version": row["version"],
            "state": row["state"],
            "policy": {
                "policy_id": row["policy_id"],
                "version": row["policy_version"],
                "engine_version": row["policy_engine_version"],
                "content_sha256": snapshot.get("policy_sha256"),
            },
            "windows": json.loads(row["windows_json"]),
            "recommendation": row["recommendation"],
            "recommendation_label": row["recommendation_label"],
            "explanations": json.loads(row["explanations_json"]),
            "evidence_gaps": json.loads(row["evidence_gaps_json"]),
            "metrics": json.loads(row["metrics_json"]),
            "residual_value_cny": row["residual_value_cny"],
            "frozen_basis": snapshot,
            "input_sha256": row["input_sha256"],
            "prepared_by": row["prepared_by"],
            "prepared_at": row["prepared_at"],
            "decided_by": row["decided_by"],
            "decided_at": row["decided_at"],
            "decision_note": row["decision_note"],
            "source_review_id": row["source_review_id"],
        }

    def list_versions(self, component_id: str) -> dict[str, Any]:
        self._component_row(component_id)
        rows = self.connection.execute(
            "SELECT assessment_id,version,state,recommendation,prepared_at,decided_at "
            "FROM assessments WHERE component_id=? ORDER BY version",
            (component_id,),
        ).fetchall()
        return {"component_id": component_id, "versions": [dict(row) for row in rows]}

    def effective_assessment(self, component_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT assessment_id FROM assessments WHERE component_id=? AND state='approved'",
            (component_id,),
        ).fetchone()
        return None if row is None else self.assessment(row["assessment_id"])

    # ------------------------------------------------------------------ 审批

    def decide_assessment(
        self, actor_id: str, assessment_id: str, approve: bool, note: str
    ) -> dict[str, Any]:
        """独立审批后结论才生效；批准新版本会把旧生效版本标记为 superseded。"""

        self._require(actor_id, "assessment.approve")
        if not note.strip():
            raise ValidationFailed("审批意见不能为空")
        row = self.connection.execute(
            "SELECT * FROM assessments WHERE assessment_id=?", (assessment_id,)
        ).fetchone()
        if row is None:
            raise NotFound("评估结论不存在")
        if row["state"] != "submitted":
            raise InvalidState("只有提交态结论可以审批")
        if row["prepared_by"] == actor_id:
            raise Forbidden("审批人不能批准自己准备的评估结论")
        if approve:
            active_item = self.connection.execute(
                "SELECT batch_id FROM cascade_batch_items WHERE component_id=? AND item_state='active'",
                (row["component_id"],),
            ).fetchone()
            if active_item is not None:
                raise InvalidState("组件仍在进行中的梯次候选批次内，不能以新版本替换生效结论")
        decision = "approved" if approve else "rejected"
        with transaction(self.connection, immediate=True):
            if approve:
                self.connection.execute(
                    "UPDATE assessments SET state='superseded' WHERE component_id=? AND state='approved'",
                    (row["component_id"],),
                )
            cursor = self.connection.execute(
                "UPDATE assessments SET state=?,decided_by=?,decided_at=?,decision_note=? "
                "WHERE assessment_id=? AND state='submitted'",
                (decision, actor_id, self._now(), note, assessment_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("评估结论状态已变化")
            self.connection.execute(
                "INSERT INTO assessment_approvals(assessment_id,component_id,version,decision,note,"
                "approved_by,approved_at) VALUES(?,?,?,?,?,?,?)",
                (
                    assessment_id,
                    row["component_id"],
                    row["version"],
                    decision,
                    note,
                    actor_id,
                    self._now(),
                ),
            )
            self._audit(
                "assessment",
                assessment_id,
                f"assessment.{decision}",
                actor_id,
                {"component_id": row["component_id"], "version": row["version"], "note": note},
            )
        return self.assessment(assessment_id)

    # ------------------------------------------------------------------ 复核

    def request_review(
        self,
        actor_id: str,
        assessment_id: str,
        reason: str,
        new_windows: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """新证据不允许改写结论，只能对既有版本提出复核申请。"""

        self._require(actor_id, "review.request")
        if not reason.strip():
            raise ValidationFailed("复核理由不能为空")
        row = self.connection.execute(
            "SELECT * FROM assessments WHERE assessment_id=?", (assessment_id,)
        ).fetchone()
        if row is None:
            raise NotFound("评估结论不存在")
        if row["state"] not in {"approved", "rejected"}:
            raise InvalidState("只能对已批准或已驳回的结论申请复核")
        if self.connection.execute(
            "SELECT 1 FROM disposition_confirmations WHERE component_id=?",
            (row["component_id"],),
        ).fetchone():
            raise InvalidState("组件已有最终去向确认，处置流程已闭环，不能再申请复核")
        open_review = self.connection.execute(
            "SELECT 1 FROM review_requests WHERE assessment_id=? AND status='pending'",
            (assessment_id,),
        ).fetchone()
        if open_review is not None:
            raise Conflict("该结论已有待处理复核申请")
        windows_json = None
        if new_windows is not None:
            try:
                windows = AssessmentWindows.from_dict(new_windows)
            except ValueError as exc:
                raise ValidationFailed(str(exc)) from exc
            windows_json = canonical_json({
                "as_of": windows.as_of,
                "capacity_window": {
                    "starts_at": windows.capacity_window.starts_at,
                    "ends_at": windows.capacity_window.ends_at,
                },
                "resistance_window": {
                    "starts_at": windows.resistance_window.starts_at,
                    "ends_at": windows.resistance_window.ends_at,
                },
                "events_after": windows.events_after,
            })
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO review_requests(assessment_id,component_id,reason,requested_windows_json,"
                "status,requested_by,requested_at) VALUES(?,?,?,?, 'pending',?,?)",
                (assessment_id, row["component_id"], reason, windows_json, actor_id, self._now()),
            )
            review_id = cursor.lastrowid
            self._audit(
                "review",
                str(review_id),
                "review.requested",
                actor_id,
                {"assessment_id": assessment_id, "reason": reason},
            )
        return {"review_id": review_id, "status": "pending"}

    def decide_review(self, actor_id: str, review_id: int, accept: bool, note: str) -> dict[str, Any]:
        self._require(actor_id, "review.review")
        if not note.strip():
            raise ValidationFailed("复核意见不能为空")
        row = self.connection.execute(
            "SELECT * FROM review_requests WHERE review_id=?", (review_id,)
        ).fetchone()
        if row is None:
            raise NotFound("复核申请不存在")
        if row["status"] != "pending":
            raise InvalidState("复核申请已经处理")
        if row["requested_by"] == actor_id:
            raise Forbidden("不能复核自己发起的申请")
        status = "accepted" if accept else "rejected"
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE review_requests SET status=?,reviewed_by=?,reviewed_at=?,review_note=? "
                "WHERE review_id=? AND status='pending'",
                (status, actor_id, self._now(), note, review_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("复核状态已变化")
            self._audit(
                "review",
                str(review_id),
                f"review.{status}",
                actor_id,
                {"assessment_id": row["assessment_id"], "note": note},
            )
        return {"review_id": review_id, "status": status}

    # ------------------------------------------------------------- 梯次批次

    def create_cascade_batch(self, actor_id: str, batch_id: str, note: str = "") -> dict[str, Any]:
        self._require(actor_id, "batch.write")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO cascade_batches(batch_id,state,note,created_by,created_at) "
                    "VALUES(?, 'forming',?,?,?)",
                    (batch_id, note, actor_id, self._now()),
                )
                self._audit("cascade_batch", batch_id, "batch.created", actor_id, {"note": note})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"候选批次已存在: {batch_id}") from exc
        return self.cascade_batch(batch_id)

    def add_to_cascade_batch(self, actor_id: str, batch_id: str, component_id: str) -> dict[str, Any]:
        """把生效结论为梯次利用的组件加入带来源的候选批次。"""

        self._require(actor_id, "batch.write")
        batch = self.connection.execute(
            "SELECT * FROM cascade_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if batch is None:
            raise NotFound("候选批次不存在")
        if batch["state"] != "forming":
            raise InvalidState("只有组建中的批次可以加入组件")
        if self.connection.execute(
            "SELECT 1 FROM disposition_confirmations WHERE component_id=?", (component_id,)
        ).fetchone():
            raise InvalidState("组件已有最终去向确认")
        effective = self.connection.execute(
            "SELECT * FROM assessments WHERE component_id=? AND state='approved'",
            (component_id,),
        ).fetchone()
        if effective is None:
            raise InvalidState("组件没有生效评估结论，不能进入候选批次")
        if effective["recommendation"] != CASCADE:
            raise InvalidState(f"生效结论为 {effective['recommendation']}，不是梯次利用")
        soh = Decimal(json.loads(effective["metrics_json"])["soh_percent"])
        policy, _ = self._policy(effective["policy_id"], effective["policy_version"])
        rated = Decimal(self._component_row(component_id)["rated_capacity_kwh"])
        available = (rated * soh / Decimal("100")).quantize(_QUANT)
        unit_value = (policy.rules["reuse_value_cny_per_kwh"] * soh / Decimal("100")).quantize(_MONEY_QUANT)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO cascade_batch_items(batch_id,component_id,assessment_id,available_kwh,"
                    "unit_value_cny_per_kwh,item_state,added_by,added_at) "
                    "VALUES(?,?,?,?,?, 'active',?,?)",
                    (
                        batch_id,
                        component_id,
                        effective["assessment_id"],
                        format(available, "f"),
                        format(unit_value, "f"),
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit(
                    "cascade_batch",
                    batch_id,
                    "batch.item_added",
                    actor_id,
                    {
                        "component_id": component_id,
                        "assessment_id": effective["assessment_id"],
                        "available_kwh": format(available, "f"),
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("组件已在某个进行中的候选批次内") from exc
        return self.cascade_batch(batch_id)

    def seal_cascade_batch(self, actor_id: str, batch_id: str) -> dict[str, Any]:
        self._require(actor_id, "batch.write")
        with transaction(self.connection, immediate=True):
            count = self.connection.execute(
                "SELECT count(*) FROM cascade_batch_items WHERE batch_id=? AND item_state='active'",
                (batch_id,),
            ).fetchone()[0]
            if count == 0:
                raise InvalidState("候选批次没有在组组件，不能封存")
            cursor = self.connection.execute(
                "UPDATE cascade_batches SET state='sealed',sealed_at=? "
                "WHERE batch_id=? AND state='forming'",
                (self._now(), batch_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("批次不是组建中状态")
            self._audit("cascade_batch", batch_id, "batch.sealed", actor_id, {"items": count})
        return self.cascade_batch(batch_id)

    def cascade_batch(self, batch_id: str) -> dict[str, Any]:
        batch = self.connection.execute(
            "SELECT * FROM cascade_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if batch is None:
            raise NotFound("候选批次不存在")
        items = self.connection.execute(
            "SELECT i.*,a.version AS assessment_version,a.input_sha256 AS assessment_input_sha256 "
            "FROM cascade_batch_items i JOIN assessments a ON a.assessment_id=i.assessment_id "
            "WHERE i.batch_id=? ORDER BY i.added_at,i.component_id",
            (batch_id,),
        ).fetchall()
        held = Decimal(
            self.connection.execute(
                "SELECT coalesce(sum(CAST(h.held_kwh AS REAL)),0) FROM capacity_holds h "
                "JOIN cascade_projects p ON p.project_id=h.project_id "
                "WHERE h.batch_id=? AND h.state='held' AND p.state='reserved'",
                (batch_id,),
            ).fetchone()[0]
        )
        total_available = sum(
            (Decimal(item["available_kwh"]) for item in items if item["item_state"] == "active"),
            Decimal(0),
        )
        return {
            "batch_id": batch_id,
            "state": batch["state"],
            "note": batch["note"],
            "created_by": batch["created_by"],
            "created_at": batch["created_at"],
            "sealed_at": batch["sealed_at"],
            "closed_at": batch["closed_at"],
            "total_available_kwh": format(total_available.quantize(_QUANT), "f"),
            "held_kwh": format(held.quantize(_QUANT), "f"),
            "free_kwh": format((total_available - held).quantize(_QUANT), "f"),
            "items": [dict(row) for row in items],
        }

    def _close_batch(self, actor_id: str, batch_id: str, new_state: str, reason: str) -> None:
        projects = self.connection.execute(
            "SELECT project_id FROM cascade_projects WHERE batch_id=? AND state='reserved'",
            (batch_id,),
        ).fetchall()
        for project in projects:
            self._release_project(project["project_id"], reason, actor_id, require_actor=False)
        self.connection.execute(
            "UPDATE capacity_holds SET state='released',release_reason=?,released_at=? "
            "WHERE batch_id=? AND state='held'",
            (reason, self._now(), batch_id),
        )
        self.connection.execute(
            "UPDATE cascade_batch_items SET item_state='released',released_reason=?,released_at=? "
            "WHERE batch_id=? AND item_state='active'",
            (
                "batch_withdrawn" if reason == "withdrawn" else "batch_failed",
                self._now(),
                batch_id,
            ),
        )
        cursor = self.connection.execute(
            "UPDATE cascade_batches SET state=?,closed_at=? WHERE batch_id=? AND state IN ('forming','sealed')",
            (new_state, self._now(), batch_id),
        )
        if cursor.rowcount != 1:
            raise InvalidState("批次当前状态不允许关闭")

    def withdraw_cascade_batch(self, actor_id: str, batch_id: str, reason: str) -> dict[str, Any]:
        """撤回批次：安全释放全部预留与组件占用，历史行保留。"""

        self._require(actor_id, "batch.write")
        if not reason.strip():
            raise ValidationFailed("撤回原因不能为空")
        with transaction(self.connection, immediate=True):
            self._close_batch(actor_id, batch_id, "withdrawn", "withdrawn")
            self._audit("cascade_batch", batch_id, "batch.withdrawn", actor_id, {"reason": reason})
        return self.cascade_batch(batch_id)

    def fail_cascade_batch(self, actor_id: str, batch_id: str, reason: str) -> dict[str, Any]:
        """项目/批次失败：安全释放容量，组件可进入新批次。"""

        self._require(actor_id, "batch.write")
        if not reason.strip():
            raise ValidationFailed("失败原因不能为空")
        with transaction(self.connection, immediate=True):
            self._close_batch(actor_id, batch_id, "failed", "project_failed")
            self._audit("cascade_batch", batch_id, "batch.failed", actor_id, {"reason": reason})
        return self.cascade_batch(batch_id)

    # ------------------------------------------------------------- 项目与预留

    def reserve_project(
        self,
        actor_id: str,
        project_id: str,
        batch_id: str,
        requested_capacity_kwh: object,
        hold_days: int,
    ) -> dict[str, Any]:
        """在封存批次上按组件顺序占用容量；预留有期限且不可被两个项目重复占用。"""

        self._require(actor_id, "project.write")
        try:
            requested = Decimal(str(requested_capacity_kwh))
        except Exception as exc:  # noqa: BLE001
            raise ValidationFailed("requested_capacity_kwh 必须是十进制数值") from exc
        if requested <= 0:
            raise ValidationFailed("申请容量必须大于零")
        if not isinstance(hold_days, int) or isinstance(hold_days, bool) or hold_days <= 0:
            raise ValidationFailed("hold_days 必须是正整数")
        batch = self.connection.execute(
            "SELECT * FROM cascade_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if batch is None:
            raise NotFound("候选批次不存在")
        if batch["state"] != "sealed":
            raise InvalidState("只有已封存批次可以接受项目预留")
        items = self.connection.execute(
            "SELECT * FROM cascade_batch_items WHERE batch_id=? AND item_state='active' "
            "ORDER BY added_at,component_id",
            (batch_id,),
        ).fetchall()
        allocation: list[tuple[sqlite3.Row, Decimal]] = []
        remaining = requested
        free_total = Decimal(0)
        for item in items:
            held = Decimal(
                self.connection.execute(
                    "SELECT coalesce(sum(CAST(held_kwh AS REAL)),0) FROM capacity_holds "
                    "WHERE component_id=? AND state='held'",
                    (item["component_id"],),
                ).fetchone()[0]
            )
            free = (Decimal(item["available_kwh"]) - held).quantize(_QUANT)
            free_total += free
            if remaining > 0 and free > 0:
                take = min(free, remaining).quantize(_QUANT)
                allocation.append((item, take))
                remaining -= take
        if remaining > 0:
            raise InvalidState(
                f"批次空闲容量不足：申请 {requested} kWh，可用 {free_total.quantize(_QUANT)} kWh"
            )
        expires = isoformat(self.clock.now() + timedelta(days=hold_days))
        now = self._now()
        with transaction(self.connection, immediate=True):
            try:
                cursor = self.connection.execute(
                    "INSERT INTO cascade_projects(project_id,batch_id,requested_capacity_kwh,"
                    "reserved_capacity_kwh,state,hold_expires_at,created_by,created_at) "
                    "VALUES(?,?,?,?,'reserved',?,?,?)",
                    (
                        project_id,
                        batch_id,
                        format(requested.quantize(_QUANT), "f"),
                        format(requested.quantize(_QUANT), "f"),
                        expires,
                        actor_id,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict(f"项目已存在: {project_id}") from exc
            for item, take in allocation:
                try:
                    self.connection.execute(
                        "INSERT INTO capacity_holds(project_id,batch_id,component_id,held_kwh,state,"
                        "expires_at,created_at) VALUES(?,?,?,?, 'held',?,?)",
                        (project_id, batch_id, item["component_id"], format(take, "f"), expires, now),
                    )
                except sqlite3.IntegrityError as exc:
                    raise Conflict("容量已被其他项目占用") from exc
            self._audit(
                "cascade_project",
                project_id,
                "project.reserved",
                actor_id,
                {
                    "batch_id": batch_id,
                    "requested_kwh": format(requested.quantize(_QUANT), "f"),
                    "expires_at": expires,
                    "components": [item["component_id"] for item, _ in allocation],
                },
            )
        return self.project(project_id)

    def _release_project(
        self, project_id: str, reason: str, actor_id: str, *, require_actor: bool
    ) -> None:
        project = self.connection.execute(
            "SELECT * FROM cascade_projects WHERE project_id=?", (project_id,)
        ).fetchone()
        if project is None:
            raise NotFound("梯次项目不存在")
        if project["state"] != "reserved":
            raise InvalidState("项目没有生效中的容量预留")
        self.connection.execute(
            "UPDATE capacity_holds SET state='released',release_reason=?,released_at=? "
            "WHERE project_id=? AND state='held'",
            (reason, self._now(), project_id),
        )
        self.connection.execute(
            "UPDATE cascade_projects SET state='released',release_reason=?,released_at=? "
            "WHERE project_id=? AND state='reserved'",
            (reason, self._now(), project_id),
        )
        if require_actor:
            self._audit(
                "cascade_project",
                project_id,
                "project.released",
                actor_id,
                {"reason": reason, "batch_id": project["batch_id"]},
            )

    def withdraw_project(self, actor_id: str, project_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "project.write")
        if not reason.strip():
            raise ValidationFailed("撤回原因不能为空")
        with transaction(self.connection, immediate=True):
            self._release_project(project_id, "withdrawn", actor_id, require_actor=True)
        return self.project(project_id)

    def mark_project_failed(self, actor_id: str, project_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "project.write")
        if not reason.strip():
            raise ValidationFailed("失败原因不能为空")
        with transaction(self.connection, immediate=True):
            self._release_project(project_id, "project_failed", actor_id, require_actor=True)
        return self.project(project_id)

    def confirm_project(self, actor_id: str, project_id: str, reference: str) -> dict[str, Any]:
        """项目落地：释放预留、占用组件为最终梯次去向，并确认每个组件唯一去向。"""

        self._require(actor_id, "project.write")
        if not reference.strip():
            raise ValidationFailed("项目落地凭证不能为空")
        project = self.connection.execute(
            "SELECT * FROM cascade_projects WHERE project_id=?", (project_id,)
        ).fetchone()
        if project is None:
            raise NotFound("梯次项目不存在")
        if project["state"] != "reserved":
            raise InvalidState("项目没有生效中的容量预留")
        if project["hold_expires_at"] <= self._now():
            raise InvalidState("容量预留已过期，请重新申请")
        holds = self.connection.execute(
            "SELECT * FROM capacity_holds WHERE project_id=? AND state='held' ORDER BY component_id",
            (project_id,),
        ).fetchall()
        now = self._now()
        with transaction(self.connection, immediate=True):
            for hold in holds:
                item = self.connection.execute(
                    "SELECT assessment_id FROM cascade_batch_items "
                    "WHERE batch_id=? AND component_id=?",
                    (project["batch_id"], hold["component_id"]),
                ).fetchone()
                self.connection.execute(
                    "INSERT INTO disposition_confirmations(component_id,assessment_id,final_destination,"
                    "reference,confirmed_by,confirmed_at) VALUES(?,?, 'cascade',?,?,?)",
                    (hold["component_id"], item["assessment_id"], reference, actor_id, now),
                )
            self.connection.execute(
                "UPDATE capacity_holds SET state='released',release_reason='project_confirmed',released_at=? "
                "WHERE project_id=? AND state='held'",
                (now, project_id),
            )
            self.connection.execute(
                "UPDATE cascade_batch_items SET item_state='released',released_reason='project_confirmed',"
                "released_at=? WHERE batch_id=? AND component_id IN "
                "(SELECT component_id FROM capacity_holds WHERE project_id=?) AND item_state='active'",
                (now, project["batch_id"], project_id),
            )
            self.connection.execute(
                "UPDATE cascade_projects SET state='confirmed',release_reason='project_confirmed',"
                "released_at=?,confirmed_at=? WHERE project_id=? AND state='reserved'",
                (now, now, project_id),
            )
            remaining = self.connection.execute(
                "SELECT count(*) FROM cascade_batch_items WHERE batch_id=? AND item_state='active'",
                (project["batch_id"],),
            ).fetchone()[0]
            if remaining == 0:
                self.connection.execute(
                    "UPDATE cascade_batches SET state='consumed',closed_at=? WHERE batch_id=? AND state='sealed'",
                    (now, project["batch_id"]),
                )
            self._audit(
                "cascade_project",
                project_id,
                "project.confirmed",
                actor_id,
                {"reference": reference, "components": [hold["component_id"] for hold in holds]},
            )
        return self.project(project_id)

    # ------------------------------------------------------------- 过期清扫

    def expire_holds(self, actor_id: str) -> dict[str, Any]:
        """清扫过期预留：安全释放容量供其他项目使用，历史保留。"""

        self._require(actor_id, "hold.expire")
        now = self._now()
        with transaction(self.connection, immediate=True):
            expired = self.connection.execute(
                "SELECT DISTINCT project_id FROM capacity_holds WHERE state='held' AND expires_at<=?",
                (now,),
            ).fetchall()
            for row in expired:
                self._release_project(row["project_id"], "expired", actor_id, require_actor=True)
        return {"expired_projects": [row["project_id"] for row in expired], "swept_at": now}

    def project(self, project_id: str) -> dict[str, Any]:
        project = self.connection.execute(
            "SELECT * FROM cascade_projects WHERE project_id=?", (project_id,)
        ).fetchone()
        if project is None:
            raise NotFound("梯次项目不存在")
        holds = self.connection.execute(
            "SELECT hold_id,component_id,held_kwh,state,release_reason,expires_at,released_at "
            "FROM capacity_holds WHERE project_id=? ORDER BY component_id",
            (project_id,),
        ).fetchall()
        return {**dict(project), "holds": [dict(row) for row in holds]}

    # ------------------------------------------------------------- 去向与复算

    def confirm_disposition(
        self, actor_id: str, component_id: str, final_destination: str, reference: str
    ) -> dict[str, Any]:
        """确认非梯次路径的最终去向；必须与生效结论一致，且组件未被批次占用。"""

        self._require(actor_id, "disposition.confirm")
        if final_destination not in {"continued", "derated", "recycled"}:
            raise ValidationFailed("该接口只确认 continued、derated 或 recycled 去向")
        if not reference.strip():
            raise ValidationFailed("去向凭证不能为空")
        effective = self.connection.execute(
            "SELECT * FROM assessments WHERE component_id=? AND state='approved'",
            (component_id,),
        ).fetchone()
        if effective is None:
            raise InvalidState("组件没有生效结论")
        expected = _DESTINATION_BY_RECOMMENDATION.get(effective["recommendation"])
        if expected is None:
            raise InvalidState("等待补证结论不能确认最终去向")
        if expected != final_destination:
            raise InvalidState(
                f"生效结论 {effective['recommendation']} 要求去向 {expected}，不能确认 {final_destination}"
            )
        active_item = self.connection.execute(
            "SELECT 1 FROM cascade_batch_items WHERE component_id=? AND item_state='active'",
            (component_id,),
        ).fetchone()
        if active_item is not None:
            raise InvalidState("组件仍在候选批次内，必须先退出批次")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO disposition_confirmations(component_id,assessment_id,final_destination,"
                    "reference,confirmed_by,confirmed_at) VALUES(?,?,?,?,?,?)",
                    (component_id, effective["assessment_id"], final_destination, reference, actor_id, self._now()),
                )
                self._audit(
                    "component",
                    component_id,
                    "disposition.confirmed",
                    actor_id,
                    {
                        "final_destination": final_destination,
                        "assessment_id": effective["assessment_id"],
                        "reference": reference,
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("组件已经确认过最终去向") from exc
        return self.component_disposition(component_id)

    def component_disposition(self, component_id: str) -> dict[str, Any]:
        self._component_row(component_id)
        effective = self.effective_assessment(component_id)
        confirmation = self.connection.execute(
            "SELECT * FROM disposition_confirmations WHERE component_id=?", (component_id,)
        ).fetchone()
        active_batch = self.connection.execute(
            "SELECT batch_id FROM cascade_batch_items WHERE component_id=? AND item_state='active'",
            (component_id,),
        ).fetchone()
        active_hold = self.connection.execute(
            "SELECT h.project_id,h.held_kwh,h.expires_at FROM capacity_holds h WHERE h.component_id=? AND h.state='held'",
            (component_id,),
        ).fetchone()
        return {
            "component_id": component_id,
            "effective_assessment_id": None if effective is None else effective["assessment_id"],
            "effective_recommendation": None if effective is None else effective["recommendation"],
            "final_destination": None if confirmation is None else confirmation["final_destination"],
            "confirmation_reference": None if confirmation is None else confirmation["reference"],
            "active_cascade_batch": None if active_batch is None else active_batch["batch_id"],
            "active_hold": None
            if active_hold is None
            else {
                "project_id": active_hold["project_id"],
                "held_kwh": active_hold["held_kwh"],
                "expires_at": active_hold["expires_at"],
            },
            "destinations_count": 0 if confirmation is None else 1,
        }

    def disposition_register(self, actor_id: str) -> dict[str, Any]:
        """委员会台账：列出每个组件的唯一有效去向并做完整性校验。"""

        self._require(actor_id, "report.read")
        rows = self.connection.execute(
            "SELECT c.component_id,"
            " (SELECT assessment_id FROM assessments WHERE component_id=c.component_id AND state='approved') "
            "   AS effective_assessment_id,"
            " (SELECT recommendation FROM assessments WHERE component_id=c.component_id AND state='approved') "
            "   AS effective_recommendation,"
            " (SELECT count(*) FROM assessments WHERE component_id=c.component_id AND state='approved') "
            "   AS effective_count,"
            " (SELECT final_destination FROM disposition_confirmations WHERE component_id=c.component_id) "
            "   AS final_destination"
            " FROM components c ORDER BY c.component_id"
        ).fetchall()
        violations: list[str] = []
        entries: list[dict[str, Any]] = []
        for row in rows:
            if row["effective_count"] > 1:
                violations.append(f"{row['component_id']}: 存在 {row['effective_count']} 个生效结论")
            if row["final_destination"] is not None and row["effective_recommendation"] is not None:
                expected = _DESTINATION_BY_RECOMMENDATION.get(row["effective_recommendation"])
                if expected is not None and expected != row["final_destination"]:
                    violations.append(
                        f"{row['component_id']}: 最终去向 {row['final_destination']} 与生效结论不一致"
                    )
            entries.append({
                "component_id": row["component_id"],
                "effective_assessment_id": row["effective_assessment_id"],
                "effective_recommendation": row["effective_recommendation"],
                "final_destination": row["final_destination"],
            })
        double_holds = self.connection.execute(
            "SELECT component_id,count(*) FROM capacity_holds WHERE state='held' GROUP BY component_id "
            "HAVING count(*) > 1"
        ).fetchall()
        for row in double_holds:
            violations.append(f"{row[0]}: 容量被 {row[1]} 个项目同时占用")
        return {
            "components": entries,
            "violations": violations,
            "consistent": not violations,
        }

    def recompute_assessment(self, actor_id: str, assessment_id: str) -> dict[str, Any]:
        """用冻结快照重放策略引擎，复算任一结论并核对输入摘要。"""

        self._require(actor_id, "report.read")
        stored = self.assessment(assessment_id)
        snapshot = stored["frozen_basis"]
        windows = AssessmentWindows.from_dict(snapshot["windows"])
        policy, _policy_sha = self._policy(snapshot["policy_id"], stored["policy"]["version"])
        cfg = snapshot["component_config"]
        frozen_component = Component(
            component_id=cfg["component_id"],
            station_id=cfg["station_id"],
            model_name=cfg["model_name"],
            chemistry=cfg["chemistry"],
            rated_capacity_kwh=Decimal(cfg["rated_capacity_kwh"]),
            acquisition_cost_cny=Decimal(cfg["acquisition_cost_cny"]),
            baseline_resistance_milliohm=None
            if cfg["baseline_resistance_milliohm"] is None
            else Decimal(cfg["baseline_resistance_milliohm"]),
            commissioned_at=cfg["commissioned_at"],
        )
        # 复算只采信冻结快照中的记录；当前台账仅用于防篡改比对。
        records: list[MeasurementRecord] = []
        tampered: list[str] = []
        for item in snapshot["records"]:
            try:
                records.append(MeasurementRecord.from_dict(item["payload"]))
            except ValueError as exc:
                tampered.append(f"record {item['record_id']} 快照载荷损坏: {exc}")
            row = self.connection.execute(
                "SELECT * FROM measurement_records WHERE record_id=?", (item["record_id"],)
            ).fetchone()
            if row is None:
                tampered.append(f"record {item['record_id']} 已从台账消失")
                continue
            if row["content_sha256"] != item["content_sha256"]:
                tampered.append(f"record {item['record_id']} 摘要变化")
            if canonical_json(_record_payload(_row_to_record(row))) != canonical_json(item["payload"]):
                tampered.append(f"record {item['record_id']} 内容与冻结快照不一致")
        current = self.connection.execute(
            "SELECT config_sha256 FROM components WHERE component_id=?", (stored["component_id"],)
        ).fetchone()
        if current is not None and current["config_sha256"] != snapshot["config_sha256"]:
            tampered.append("资产配置摘要与冻结快照不一致")
        result = evaluate_component(frozen_component, records, windows, policy).as_dict()
        recomputed_input = content_digest(snapshot)
        checks = {
            "input_sha256_match": recomputed_input == stored["input_sha256"],
            "recommendation_match": result["recommendation"] == stored["recommendation"],
            "metrics_match": canonical_json(result["metrics"]) == canonical_json(stored["metrics"]),
            "explanations_match": canonical_json(result["explanations"]) == canonical_json(stored["explanations"]),
            "residual_value_match": result["residual_value_cny"] == stored["residual_value_cny"],
            "evidence_tampered": tampered,
        }
        checks["matches"] = (
            checks["input_sha256_match"]
            and checks["recommendation_match"]
            and checks["metrics_match"]
            and checks["explanations_match"]
            and checks["residual_value_match"]
            and not tampered
        )
        return {
            "assessment_id": assessment_id,
            "checks": checks,
            "stored": {
                "recommendation": stored["recommendation"],
                "metrics": stored["metrics"],
                "residual_value_cny": stored["residual_value_cny"],
                "explanations": stored["explanations"],
                "evidence_gaps": stored["evidence_gaps"],
                "input_sha256": stored["input_sha256"],
            },
            "recomputed": result,
        }

    def preview(
        self,
        actor_id: str,
        component_id: str,
        policy_id: str,
        policy_version: int,
        windows_raw: Mapping[str, Any],
    ) -> dict[str, Any]:
        """用当前台账试算（不落库），用于查看证据缺口与剩余价值。"""

        self._require(actor_id, "report.read")
        component_row = self._component_row(component_id)
        try:
            windows = AssessmentWindows.from_dict(windows_raw)
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        policy, policy_sha = self._policy(policy_id, policy_version)
        result, input_sha, _ = self._build_assessment(component_row, policy, policy_sha, windows)
        return {"preview_input_sha256": input_sha, **result}

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute(
            "SELECT * FROM retirement_audit_events ORDER BY event_id"
        ).fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
