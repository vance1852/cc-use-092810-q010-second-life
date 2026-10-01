"""退役评估与梯次利用管理的领域用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from typing import Any, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .contracts import (
    ComponentConfig,
    EvidenceBundle,
    Measurement,
    Policy,
    QualityEvent,
    ValidationError,
)
from .engine import ENGINE_VERSION, evaluate
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json, digest
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "engineer": {
        "catalog.write", "evidence.import", "assessment.open",
        "assessment.submit", "review.request", "report.read",
    },
    "approver": {"assessment.approve", "review.review", "report.read"},
    "planner": {"cascade.write", "project.write", "reservation.write", "report.read"},
    "auditor": {"report.read", "audit.read"},
}
EVIDENCE_KIND_LABEL = {"measurement": "检测记录", "event": "质量事件"}


class RetirementService:
    """在单个 SQLite 连接上提供全部退役处置业务操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    # ── 基础辅助 ──────────────────────────────────────────────────────────

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT user_id, display_name, role, active FROM retirement_users WHERE user_id=?",
            (user_id,),
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
                entity_type, entity_id, event_type, actor_id,
                canonical_json(payload), previous_hash, event_hash, body["created_at"],
            ),
        )

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

    # ── 组件与配置版本 ─────────────────────────────────────────────────────

    def register_component(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        try:
            config = ComponentConfig.from_dict(raw, "component")
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        if config.revision != 1:
            raise ValidationFailed("新组件的首个配置版本必须是 revision=1")
        now = self._now()
        config_json = canonical_json(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO components(component_id,model_name,chemistry,nominal_capacity_kwh,"
                    "current_config_revision,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        config.component_id, config.model_name, config.chemistry,
                        format(config.nominal_capacity_kwh, "f"), 1, actor_id, now,
                    ),
                )
                self.connection.execute(
                    "INSERT INTO component_configs(component_id,revision,config_json,content_sha256,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (config.component_id, 1, config_json, digest(raw), actor_id, now),
                )
                self._audit("component", config.component_id, "component.registered", actor_id,
                            {"revision": 1, "model_name": config.model_name})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"组件已存在: {config.component_id}") from exc
        return {"component_id": config.component_id, "current_config_revision": 1}

    def add_config_revision(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        try:
            config = ComponentConfig.from_dict(raw, "component")
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        component = self.connection.execute(
            "SELECT current_config_revision FROM components WHERE component_id=?", (config.component_id,)
        ).fetchone()
        if component is None:
            raise NotFound("组件不存在，请先登记")
        expected = component["current_config_revision"] + 1
        if config.revision != expected:
            raise Conflict(f"配置版本必须连续递增，下一个版本为 {expected}")
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO component_configs(component_id,revision,config_json,content_sha256,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (config.component_id, config.revision, canonical_json(raw), digest(raw), actor_id, now),
                )
                self.connection.execute(
                    "UPDATE components SET current_config_revision=? WHERE component_id=?",
                    (config.revision, config.component_id),
                )
                self._audit("component", config.component_id, "config.revision_added", actor_id,
                            {"revision": config.revision})
        except sqlite3.IntegrityError as exc:
            raise Conflict("配置版本冲突") from exc
        return {"component_id": config.component_id, "current_config_revision": config.revision}

    def _load_config(self, component_id: str, revision: int) -> tuple[ComponentConfig, dict[str, Any], str]:
        row = self.connection.execute(
            "SELECT config_json, content_sha256 FROM component_configs WHERE component_id=? AND revision=?",
            (component_id, revision),
        ).fetchone()
        if row is None:
            raise NotFound(f"组件配置版本不存在: {component_id}@{revision}")
        raw = json.loads(row["config_json"])
        return ComponentConfig.from_dict(raw, "component"), raw, row["content_sha256"]

    # ── 政策版本 ───────────────────────────────────────────────────────────

    def publish_policy(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        try:
            policy = Policy.from_dict(raw)
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        text = canonical_json(raw)
        content_sha = digest(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO retirement_policies(policy_id,version,title,canonical_json,content_sha256,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (policy.policy_id, policy.version, policy.title, text, content_sha, actor_id, self._now()),
                )
                self._audit("policy", f"{policy.policy_id}@{policy.version}", "policy.published", actor_id,
                            {"sha256": content_sha})
        except sqlite3.IntegrityError as exc:
            raise Conflict("政策版本编号或内容摘要已经存在") from exc
        return {"policy_id": policy.policy_id, "version": policy.version, "sha256": content_sha}

    def _load_policy(self, policy_id: str, version: int) -> tuple[Policy, dict[str, Any], str]:
        row = self.connection.execute(
            "SELECT canonical_json, content_sha256 FROM retirement_policies WHERE policy_id=? AND version=?",
            (policy_id, version),
        ).fetchone()
        if row is None:
            raise NotFound(f"政策版本不存在: {policy_id}@{version}")
        raw = json.loads(row["canonical_json"])
        return Policy.from_dict(raw), raw, row["content_sha256"]

    # ── 不可变证据：检测记录与质量事件 ──────────────────────────────────────

    def import_measurements(self, actor_id: str, component_id: str, rows: list[Mapping[str, Any]]) -> dict[str, Any]:
        self._require(actor_id, "evidence.import")
        if self.connection.execute("SELECT 1 FROM components WHERE component_id=?", (component_id,)).fetchone() is None:
            raise NotFound("组件不存在")
        parsed: list[Measurement] = []
        for index, raw_row in enumerate(rows):
            try:
                item = Measurement.from_dict(raw_row, f"measurements[{index}]")
            except ValidationError as exc:
                raise ValidationFailed(str(exc)) from exc
            parsed.append(item)
        if not parsed:
            raise ValidationFailed("检测记录不能为空")
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                for item, raw_row in zip(parsed, rows):
                    self.connection.execute(
                        "INSERT INTO measurements(component_id,record_id,kind,value,source,measured_at,recorded_at,"
                        "content_sha256,imported_by,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            component_id, item.record_id, item.kind, format(item.value, "f"), item.source,
                            item.measured_at, item.recorded_at, digest(raw_row), actor_id, now,
                        ),
                    )
                self._audit("component", component_id, "measurements.imported", actor_id,
                            {"count": len(parsed)})
        except sqlite3.IntegrityError as exc:
            raise Conflict("检测记录编号重复或组件不存在；证据一经入库不可覆盖") from exc
        return {"component_id": component_id, "inserted": len(parsed)}

    def record_quality_event(self, actor_id: str, component_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "evidence.import")
        if self.connection.execute("SELECT 1 FROM components WHERE component_id=?", (component_id,)).fetchone() is None:
            raise NotFound("组件不存在")
        try:
            event = QualityEvent.from_dict(raw, "event")
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO quality_events(component_id,event_id,category,severity,source,occurred_at,"
                    "recorded_at,resolved,note,content_sha256,imported_by,imported_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        component_id, event.event_id, event.category, event.severity, event.source,
                        event.occurred_at, event.recorded_at, 1 if event.resolved else 0, event.note,
                        digest(raw), actor_id, self._now(),
                    ),
                )
                self._audit("component", component_id, "quality_event.recorded", actor_id,
                            {"event_id": event.event_id, "category": event.category, "severity": event.severity})
        except sqlite3.IntegrityError as exc:
            raise Conflict("质量事件编号重复；事件一经入库不可覆盖") from exc
        return {"component_id": component_id, "event_id": event.event_id}

    def _measurement_dicts(self, component_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT record_id,kind,value,source,measured_at,recorded_at FROM measurements "
            "WHERE component_id=? ORDER BY recorded_at, record_id",
            (component_id,),
        ).fetchall()
        return [
            {
                "record_id": row["record_id"], "kind": row["kind"], "value": row["value"],
                "source": row["source"], "measured_at": row["measured_at"], "recorded_at": row["recorded_at"],
            }
            for row in rows
        ]

    def _event_dicts(self, component_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT event_id,category,severity,source,occurred_at,recorded_at,resolved,note FROM quality_events "
            "WHERE component_id=? ORDER BY recorded_at, event_id",
            (component_id,),
        ).fetchall()
        return [
            {
                "event_id": row["event_id"], "category": row["category"], "severity": row["severity"],
                "source": row["source"], "occurred_at": row["occurred_at"], "recorded_at": row["recorded_at"],
                "resolved": bool(row["resolved"]), "note": row["note"],
            }
            for row in rows
        ]

    def _bundle(self, component_id: str) -> EvidenceBundle:
        return EvidenceBundle.from_dict({
            "measurements": self._measurement_dicts(component_id),
            "events": self._event_dicts(component_id),
        })

    # ── 评估版本：冻结、提交、独立审批 ─────────────────────────────────────

    def _next_serial(self, component_id: str) -> int:
        row = self.connection.execute(
            "SELECT max(serial) AS serial FROM assessments WHERE component_id=?", (component_id,)
        ).fetchone()
        return 1 if row["serial"] is None else int(row["serial"]) + 1

    @staticmethod
    def _frozen_input(
        config_raw: Mapping[str, Any],
        policy_raw: Mapping[str, Any],
        window_start: str,
        window_cutoff: str,
        measurements: list[Mapping[str, Any]],
        events: list[Mapping[str, Any]],
    ) -> dict[str, Any]:
        return {
            "config": config_raw,
            "policy": policy_raw,
            "window": {"start": window_start, "cutoff": window_cutoff},
            "measurements": sorted(measurements, key=lambda row: (row["recorded_at"], row["record_id"])),
            "events": sorted(events, key=lambda row: (row["recorded_at"], row["event_id"])),
        }

    def _persist_assessment(
        self,
        actor_id: str,
        assessment_id: str,
        component_id: str,
        config_revision: int,
        policy_id: str,
        policy_version: int,
        start_text: str,
        cutoff_text: str,
        supersedes_assessment_id: str | None = None,
    ) -> dict[str, Any]:
        """在已开启的事务内冻结证据并写入评估版本。"""

        config, config_raw, config_sha = self._load_config(component_id, config_revision)
        policy, policy_raw, policy_sha = self._load_policy(policy_id, policy_version)
        bundle = self._bundle(component_id)
        result = evaluate(config, policy, bundle, start_text, cutoff_text)
        admitted_measurements = [
            row for row in self._measurement_dicts(component_id)
            if start_text <= row["recorded_at"] <= cutoff_text
        ]
        admitted_events = [
            row for row in self._event_dicts(component_id)
            if start_text <= row["recorded_at"] <= cutoff_text
        ]
        frozen_input = self._frozen_input(
            config_raw, policy_raw, start_text, cutoff_text, admitted_measurements, admitted_events
        )
        input_sha = digest(frozen_input)
        serial = self._next_serial(component_id)
        self.connection.execute(
            "INSERT INTO assessments(assessment_id,component_id,serial,state,config_revision,policy_id,"
            "policy_version,window_start,window_cutoff,config_sha256,policy_sha256,input_sha256,result_json,"
            "conclusion,supersedes_assessment_id,opened_by,opened_at,effective) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (
                assessment_id, component_id, serial, "open", config_revision, policy_id, policy_version,
                start_text, cutoff_text, config_sha, policy_sha, input_sha, canonical_json(result),
                result["conclusion"], supersedes_assessment_id, actor_id, self._now(),
            ),
        )
        evidence_rows = self.connection.execute(
            "SELECT 'measurement' AS kind, record_id AS ref, recorded_at FROM measurements WHERE component_id=? "
            "UNION ALL SELECT 'event', event_id, recorded_at FROM quality_events WHERE component_id=?",
            (component_id, component_id),
        ).fetchall()
        for evidence in evidence_rows:
            admitted = 1 if start_text <= evidence["recorded_at"] <= cutoff_text else 0
            self.connection.execute(
                "INSERT INTO assessment_evidence(assessment_id,evidence_kind,evidence_ref,recorded_at,admitted) "
                "VALUES(?,?,?,?,?)",
                (assessment_id, evidence["kind"], evidence["ref"], evidence["recorded_at"], admitted),
            )
        self._audit("assessment", assessment_id, "assessment.opened", actor_id,
                    {"component_id": component_id, "serial": serial, "conclusion": result["conclusion"]})
        return {"serial": serial, "conclusion": result["conclusion"], "input_sha256": input_sha}

    def open_assessment(
        self,
        actor_id: str,
        assessment_id: str,
        component_id: str,
        config_revision: int,
        policy_id: str,
        policy_version: int,
        window_start: str,
        window_cutoff: str | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "assessment.open")
        try:
            start = parse_utc(window_start, "window_start")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        cutoff_text = self._now() if window_cutoff is None else window_cutoff
        try:
            cutoff = parse_utc(cutoff_text, "window_cutoff")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if start > cutoff:
            raise ValidationFailed("测量窗口起点不能晚于截止点")
        start_text, cutoff_text = utc_text(start), utc_text(cutoff)
        try:
            with transaction(self.connection, immediate=True):
                self._persist_assessment(
                    actor_id, assessment_id, component_id, config_revision, policy_id, policy_version,
                    start_text, cutoff_text,
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("评估编号冲突或引用的配置、政策版本不存在") from exc
        return self.get_assessment(assessment_id)

    def get_assessment(self, assessment_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM assessments WHERE assessment_id=?", (assessment_id,)
        ).fetchone()
        if row is None:
            raise NotFound("评估版本不存在")
        result = dict(row)
        result["result"] = json.loads(row["result_json"])
        return result

    def submit_assessment(self, actor_id: str, assessment_id: str) -> dict[str, Any]:
        self._require(actor_id, "assessment.submit")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT state, opened_by FROM assessments WHERE assessment_id=?", (assessment_id,)
            ).fetchone()
            if row is None:
                raise NotFound("评估版本不存在")
            if row["state"] != "open":
                raise InvalidState("只有草稿评估可以提交审批")
            self.connection.execute(
                "UPDATE assessments SET state='submitted',submitted_by=?,submitted_at=? WHERE assessment_id=?",
                (actor_id, self._now(), assessment_id),
            )
            self._audit("assessment", assessment_id, "assessment.submitted", actor_id, {})
        return self.get_assessment(assessment_id)

    def _active_effective(self, component_id: str, exclude_assessment_id: str | None = None) -> sqlite3.Row | None:
        sql = "SELECT * FROM assessments WHERE component_id=? AND effective=1"
        params: list[Any] = [component_id]
        if exclude_assessment_id is not None:
            sql += " AND assessment_id<>?"
            params.append(exclude_assessment_id)
        return self.connection.execute(sql, params).fetchone()

    def approve_assessment(self, actor_id: str, assessment_id: str, note: str) -> dict[str, Any]:
        self._require(actor_id, "assessment.approve")
        if not note.strip():
            raise ValidationFailed("审批意见不能为空")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT * FROM assessments WHERE assessment_id=?", (assessment_id,)
            ).fetchone()
            if row is None:
                raise NotFound("评估版本不存在")
            if row["state"] != "submitted":
                raise InvalidState("只有已提交待审批的评估可以批准")
            if row["opened_by"] == actor_id:
                raise Forbidden("提交人不能独立审批自己的评估版本")
            new_conclusion = json.loads(row["result_json"])["conclusion"]
            if new_conclusion == "pending_evidence":
                raise InvalidState("证据不足的结论不能批准生效，须补证后重开评估版本")
            if new_conclusion != "cascade":
                occupied = self.connection.execute(
                    "SELECT reservation_id, state FROM capacity_reservations WHERE component_id=? "
                    "AND state IN ('held','consumed') ORDER BY reservation_id LIMIT 1",
                    (row["component_id"],),
                ).fetchone()
                if occupied is not None:
                    raise InvalidState(
                        "组件容量已在梯次利用项目中占用，须先撤回或释放预留，不能改为其他去向"
                    )
            prior = self._active_effective(row["component_id"], assessment_id)
            if prior is not None:
                self.connection.execute(
                    "UPDATE assessments SET effective=0, state='superseded' WHERE assessment_id=?",
                    (prior["assessment_id"],),
                )
            self.connection.execute(
                "UPDATE assessments SET state='approved',approved_by=?,approved_at=?,approval_note=?,effective=1 "
                "WHERE assessment_id=?",
                (actor_id, self._now(), note.strip(), assessment_id),
            )
            self._audit("assessment", assessment_id, "assessment.approved", actor_id,
                        {"conclusion": new_conclusion, "note": note.strip()})
        return self.get_assessment(assessment_id)

    def reject_assessment(self, actor_id: str, assessment_id: str, note: str) -> dict[str, Any]:
        self._require(actor_id, "assessment.approve")
        if not note.strip():
            raise ValidationFailed("驳回意见不能为空")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT state, opened_by FROM assessments WHERE assessment_id=?", (assessment_id,)
            ).fetchone()
            if row is None:
                raise NotFound("评估版本不存在")
            if row["state"] != "submitted":
                raise InvalidState("只有待审批评估可以驳回")
            if row["opened_by"] == actor_id:
                raise Forbidden("提交人不能独立审批自己的评估版本")
            self.connection.execute(
                "UPDATE assessments SET state='rejected',approved_by=?,approved_at=?,approval_note=? "
                "WHERE assessment_id=?",
                (actor_id, self._now(), note.strip(), assessment_id),
            )
            self._audit("assessment", assessment_id, "assessment.rejected", actor_id, {"note": note.strip()})
        return self.get_assessment(assessment_id)

    # ── 复核申请：新证据不覆盖旧结论，只能触发新版本 ────────────────────────

    def request_review(
        self, actor_id: str, assessment_id: str, reason: str, new_evidence_refs: list[str]
    ) -> dict[str, Any]:
        self._require(actor_id, "review.request")
        if not reason.strip():
            raise ValidationFailed("复核原因不能为空")
        refs = list(dict.fromkeys(str(ref).strip() for ref in new_evidence_refs if str(ref).strip()))
        if not refs:
            raise ValidationFailed("至少提供一条新证据引用")
        row = self.connection.execute(
            "SELECT * FROM assessments WHERE assessment_id=?", (assessment_id,)
        ).fetchone()
        if row is None:
            raise NotFound("评估版本不存在")
        if row["state"] not in {"approved", "rejected", "superseded"}:
            raise InvalidState("只可对已经形成结论的评估版本申请复核")
        for ref in refs:
            evidence = self.connection.execute(
                "SELECT recorded_at FROM measurements WHERE component_id=? AND record_id=? "
                "UNION ALL SELECT recorded_at FROM quality_events WHERE component_id=? AND event_id=?",
                (row["component_id"], ref, row["component_id"], ref),
            ).fetchone()
            if evidence is None:
                raise NotFound(f"新证据不存在: {ref}")
            if evidence["recorded_at"] <= row["window_cutoff"]:
                raise ValidationFailed(f"证据 {ref} 在评估窗口内，不属于新证据")
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO review_requests(assessment_id,reason,new_evidence_refs,requested_by,requested_at,"
                    "status) VALUES(?,?,?,?,?, 'pending')",
                    (assessment_id, reason.strip(), canonical_json(refs), actor_id, self._now()),
                )
                review_id = int(cursor.lastrowid)
                self._audit("review", str(review_id), "review.requested", actor_id,
                            {"assessment_id": assessment_id, "refs": refs})
        except sqlite3.IntegrityError as exc:
            raise Conflict("该评估版本已有待处理复核申请") from exc
        return {"review_id": review_id, "assessment_id": assessment_id, "status": "pending"}

    def _open_successor(self, assessment: sqlite3.Row, actor_id: str, review_id: int) -> str:
        """在当前事务内，基于最新证据与当前配置开放仍需独立审批的新评估版本。"""

        component_id = assessment["component_id"]
        config_revision = self.connection.execute(
            "SELECT current_config_revision FROM components WHERE component_id=?", (component_id,)
        ).fetchone()["current_config_revision"]
        serial = self._next_serial(component_id)
        successor_id = f"{component_id}-rev-{serial}"
        self._persist_assessment(
            actor_id,
            successor_id,
            component_id,
            int(config_revision),
            assessment["policy_id"],
            assessment["policy_version"],
            assessment["window_start"],
            self._now(),
            supersedes_assessment_id=assessment["assessment_id"],
        )
        self._audit("assessment", successor_id, "assessment.successor_opened", actor_id,
                    {"from_assessment_id": assessment["assessment_id"], "review_id": review_id})
        return successor_id

    def decide_review(self, actor_id: str, review_id: int, accept: bool, note: str) -> dict[str, Any]:
        self._require(actor_id, "review.review")
        if not note.strip():
            raise ValidationFailed("复核意见不能为空")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT * FROM review_requests WHERE review_id=?", (review_id,)
            ).fetchone()
            if row is None:
                raise NotFound("复核申请不存在")
            if row["status"] != "pending":
                raise InvalidState("复核申请已经处理")
            if row["requested_by"] == actor_id:
                raise Forbidden("申请人不能审批自己的复核申请")
            new_assessment_id = None
            if accept:
                assessment = self.connection.execute(
                    "SELECT * FROM assessments WHERE assessment_id=?", (row["assessment_id"],)
                ).fetchone()
                new_assessment_id = self._open_successor(assessment, row["requested_by"], review_id)
            self.connection.execute(
                "UPDATE review_requests SET status=?,reviewed_by=?,reviewed_at=?,review_note=?,new_assessment_id=? "
                "WHERE review_id=? AND status='pending'",
                (
                    "accepted" if accept else "rejected", actor_id, self._now(), note.strip(),
                    new_assessment_id, review_id,
                ),
            )
            self._audit("review", str(review_id), "review.decided", actor_id,
                        {"accepted": accept, "new_assessment_id": new_assessment_id})
        return {
            "review_id": review_id,
            "status": "accepted" if accept else "rejected",
            "new_assessment_id": new_assessment_id,
        }

    # ── 委员会复算与缺口、剩余价值 ─────────────────────────────────────────

    def recompute(self, actor_id: str, assessment_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        row = self.connection.execute(
            "SELECT * FROM assessments WHERE assessment_id=?", (assessment_id,)
        ).fetchone()
        if row is None:
            raise NotFound("评估版本不存在")
        config, config_raw, _ = self._load_config(row["component_id"], row["config_revision"])
        policy, policy_raw, _ = self._load_policy(row["policy_id"], row["policy_version"])
        bundle = self._bundle(row["component_id"])
        recomputed = evaluate(config, policy, bundle, row["window_start"], row["window_cutoff"])
        admitted_measurements = [
            item for item in self._measurement_dicts(row["component_id"])
            if row["window_start"] <= item["recorded_at"] <= row["window_cutoff"]
        ]
        admitted_events = [
            item for item in self._event_dicts(row["component_id"])
            if row["window_start"] <= item["recorded_at"] <= row["window_cutoff"]
        ]
        frozen_input = self._frozen_input(
            config_raw, policy_raw, row["window_start"], row["window_cutoff"],
            admitted_measurements, admitted_events,
        )
        input_sha = digest(frozen_input)
        stored_result = json.loads(row["result_json"])
        return {
            "assessment_id": assessment_id,
            "engine_version": ENGINE_VERSION,
            "stored_input_sha256": row["input_sha256"],
            "recomputed_input_sha256": input_sha,
            "input_matches": input_sha == row["input_sha256"],
            "conclusion_matches": recomputed["conclusion"] == stored_result["conclusion"],
            "stored_conclusion": stored_result["conclusion"],
            "recomputed": recomputed,
        }

    def evidence_gaps(self, actor_id: str, assessment_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        assessment = self.get_assessment(assessment_id)
        result = assessment["result"]
        return {
            "assessment_id": assessment_id,
            "component_id": assessment["component_id"],
            "state": assessment["state"],
            "window": {"start": assessment["window_start"], "cutoff": assessment["window_cutoff"]},
            "evidence_gaps": result["evidence_gaps"],
            "late_evidence": result["late_evidence"],
        }

    def residual_value(self, actor_id: str, assessment_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        assessment = self.get_assessment(assessment_id)
        return {
            "assessment_id": assessment_id,
            "component_id": assessment["component_id"],
            "conclusion": assessment["conclusion"],
            "state": assessment["state"],
            "residual_value": assessment["result"]["residual_value"],
        }

    def disposition_report(self, actor_id: str, component_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        if self.connection.execute("SELECT 1 FROM components WHERE component_id=?", (component_id,)).fetchone() is None:
            raise NotFound("组件不存在")
        versions = self.connection.execute(
            "SELECT assessment_id,serial,state,conclusion,effective,opened_by,opened_at,approved_by,approved_at "
            "FROM assessments WHERE component_id=? ORDER BY serial",
            (component_id,),
        ).fetchall()
        effective = [row for row in versions if row["effective"]]
        active_reservation = self.connection.execute(
            "SELECT reservation_id,project_id,batch_id,capacity_kwh,state,holds_until FROM capacity_reservations "
            "WHERE component_id=? AND state='held' ORDER BY reservation_id",
            (component_id,),
        ).fetchone()
        consistent = len(effective) <= 1
        final = None
        if effective:
            final = {
                "assessment_id": effective[0]["assessment_id"],
                "conclusion": effective[0]["conclusion"],
                "approved_at": effective[0]["approved_at"],
            }
        return {
            "component_id": component_id,
            "effective_count": len(effective),
            "consistent_single_destination": consistent,
            "final_destination": final,
            "active_reservation": None if active_reservation is None else dict(active_reservation),
            "versions": [dict(row) for row in versions],
        }

    def reconciliation(self, actor_id: str) -> dict[str, Any]:
        """全量核对：每个退役组件最终至多一个有效去向。"""

        self._require(actor_id, "report.read")
        rows = self.connection.execute(
            "SELECT c.component_id, COUNT(a.assessment_id) AS effective_count, "
            "MAX(CASE WHEN a.effective=1 THEN a.conclusion END) AS conclusion "
            "FROM components c LEFT JOIN assessments a ON a.component_id=c.component_id AND a.effective=1 "
            "GROUP BY c.component_id ORDER BY c.component_id"
        ).fetchall()
        violations = [dict(row) for row in rows if row["effective_count"] > 1]
        return {
            "component_count": len(rows),
            "components_with_effective_destination": sum(1 for row in rows if row["effective_count"] >= 1),
            "violations": violations,
            "consistent": not violations,
        }

    # ── 梯次利用：带来源候选批次 ──────────────────────────────────────────

    def create_candidate_batch(self, actor_id: str, batch_id: str, note: str | None = None) -> dict[str, Any]:
        self._require(actor_id, "cascade.write")
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO candidate_batches(batch_id,state,content_sha256,note,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (batch_id, "forming", None, note, actor_id, now),
                )
                self._audit("candidate_batch", batch_id, "candidate_batch.created", actor_id, {"note": note})
        except sqlite3.IntegrityError as exc:
            raise Conflict("候选批次编号已存在") from exc
        return self.get_candidate_batch(batch_id)

    def get_candidate_batch(self, batch_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM candidate_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if row is None:
            raise NotFound("候选批次不存在")
        items = self.connection.execute(
            "SELECT component_id,source_assessment_id,capacity_kwh,added_at FROM candidate_batch_items "
            "WHERE batch_id=? ORDER BY component_id",
            (batch_id,),
        ).fetchall()
        result = dict(row)
        result["items"] = [dict(item) for item in items]
        return result

    def add_candidate_component(self, actor_id: str, batch_id: str, component_id: str) -> dict[str, Any]:
        self._require(actor_id, "cascade.write")
        batch = self.connection.execute(
            "SELECT state FROM candidate_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if batch is None:
            raise NotFound("候选批次不存在")
        if batch["state"] != "forming":
            raise InvalidState("只有组建中的候选批次可以加入组件")
        effective = self.connection.execute(
            "SELECT assessment_id,conclusion FROM assessments WHERE component_id=? AND effective=1",
            (component_id,),
        ).fetchone()
        if effective is None:
            raise InvalidState("组件没有生效评估结论，不能进入候选批次")
        if effective["conclusion"] != "cascade":
            raise Conflict("只有生效去向为梯次利用的组件可以加入候选批次")
        elsewhere = self.connection.execute(
            "SELECT b.batch_id FROM candidate_batch_items i JOIN candidate_batches b ON b.batch_id=i.batch_id "
            "WHERE i.component_id=? AND b.state IN ('forming','sealed')",
            (component_id,),
        ).fetchone()
        if elsewhere is not None:
            raise Conflict(f"组件已在未关闭的候选批次 {elsewhere['batch_id']} 中")
        config, _, _ = self._load_config(
            component_id,
            self.connection.execute(
                "SELECT config_revision FROM assessments WHERE assessment_id=?", (effective["assessment_id"],)
            ).fetchone()["config_revision"],
        )
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO candidate_batch_items(batch_id,component_id,source_assessment_id,capacity_kwh,"
                    "added_by,added_at) VALUES(?,?,?,?,?,?)",
                    (batch_id, component_id, effective["assessment_id"],
                     format(config.rated_capacity_kwh, "f"), actor_id, now),
                )
                self._audit("candidate_batch", batch_id, "candidate_batch.component_added", actor_id,
                            {"component_id": component_id, "source_assessment_id": effective["assessment_id"]})
        except sqlite3.IntegrityError as exc:
            raise Conflict("组件已在该候选批次中或来源评估不存在") from exc
        return self.get_candidate_batch(batch_id)

    def seal_candidate_batch(self, actor_id: str, batch_id: str) -> dict[str, Any]:
        self._require(actor_id, "cascade.write")
        with transaction(self.connection, immediate=True):
            batch = self.connection.execute(
                "SELECT state FROM candidate_batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise NotFound("候选批次不存在")
            if batch["state"] != "forming":
                raise InvalidState("只有组建中的候选批次可以封存")
            count = self.connection.execute(
                "SELECT count(*) FROM candidate_batch_items WHERE batch_id=?", (batch_id,)
            ).fetchone()[0]
            if count == 0:
                raise InvalidState("候选批次不能为空")
            items = self.connection.execute(
                "SELECT component_id,source_assessment_id,capacity_kwh FROM candidate_batch_items "
                "WHERE batch_id=? ORDER BY component_id",
                (batch_id,),
            ).fetchall()
            content_sha = digest([dict(item) for item in items])
            self.connection.execute(
                "UPDATE candidate_batches SET state='sealed',content_sha256=?,sealed_at=? WHERE batch_id=?",
                (content_sha, self._now(), batch_id),
            )
            self._audit("candidate_batch", batch_id, "candidate_batch.sealed", actor_id,
                        {"items": count, "sha256": content_sha})
        return self.get_candidate_batch(batch_id)

    # ── 梯次项目与容量预留（有期限、不重复占用、安全释放） ─────────────────

    def create_reuse_project(self, actor_id: str, project_id: str, name: str) -> dict[str, Any]:
        self._require(actor_id, "project.write")
        if not name.strip():
            raise ValidationFailed("项目名称不能为空")
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO reuse_projects(project_id,name,status,created_by,created_at) "
                    "VALUES(?,?,'active',?,?)",
                    (project_id, name.strip(), actor_id, now),
                )
                self._audit("reuse_project", project_id, "project.created", actor_id, {"name": name.strip()})
        except sqlite3.IntegrityError as exc:
            raise Conflict("梯次项目编号已存在") from exc
        return {"project_id": project_id, "name": name.strip(), "status": "active"}

    def reserve_capacity(
        self, actor_id: str, reservation_id: str, project_id: str, batch_id: str, component_id: str
    ) -> dict[str, Any]:
        self._require(actor_id, "reservation.write")
        project = self.connection.execute(
            "SELECT status FROM reuse_projects WHERE project_id=?", (project_id,)
        ).fetchone()
        if project is None:
            raise NotFound("梯次项目不存在")
        if project["status"] != "active":
            raise InvalidState("梯次项目不是活动状态，不能预留容量")
        batch = self.connection.execute(
            "SELECT state FROM candidate_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if batch is None:
            raise NotFound("候选批次不存在")
        if batch["state"] != "sealed":
            raise InvalidState("只有已封存候选批次的容量可以预留")
        item = self.connection.execute(
            "SELECT source_assessment_id,capacity_kwh FROM candidate_batch_items "
            "WHERE batch_id=? AND component_id=?",
            (batch_id, component_id),
        ).fetchone()
        if item is None:
            raise NotFound("组件不在该候选批次中")
        effective = self.connection.execute(
            "SELECT conclusion FROM assessments WHERE component_id=? AND effective=1",
            (component_id,),
        ).fetchone()
        if effective is None or effective["conclusion"] != "cascade":
            raise InvalidState("组件当前生效去向不是梯次利用，不能预留容量")
        assessment = self.connection.execute(
            "SELECT policy_id,policy_version FROM assessments WHERE assessment_id=?",
            (item["source_assessment_id"],),
        ).fetchone()
        policy, _, _ = self._load_policy(assessment["policy_id"], assessment["policy_version"])
        now = self.clock.now()
        holds_until = utc_text(now + timedelta(days=policy.reservation_hold_days))
        now_text = utc_text(now)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO capacity_reservations(reservation_id,project_id,batch_id,component_id,"
                    "source_assessment_id,capacity_kwh,state,hold_days,holds_until,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?, 'held',?,?,?,?)",
                    (
                        reservation_id, project_id, batch_id, component_id, item["source_assessment_id"],
                        item["capacity_kwh"], policy.reservation_hold_days, holds_until, actor_id, now_text,
                    ),
                )
                self._audit("reservation", reservation_id, "reservation.held", actor_id,
                            {"project_id": project_id, "component_id": component_id,
                             "capacity_kwh": item["capacity_kwh"], "holds_until": holds_until})
        except sqlite3.IntegrityError as exc:
            raise Conflict("容量预留编号冲突，或该组件容量已被其他项目占用") from exc
        return {
            "reservation_id": reservation_id, "project_id": project_id, "component_id": component_id,
            "capacity_kwh": item["capacity_kwh"], "state": "held", "holds_until": holds_until,
        }

    def _get_held_reservation(self, reservation_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM capacity_reservations WHERE reservation_id=?", (reservation_id,)
        ).fetchone()
        if row is None:
            raise NotFound("容量预留不存在")
        if row["state"] != "held":
            raise InvalidState("只有有效预留可以执行该操作")
        return row

    def expire_due_reservations(self, actor_id: str | None = None) -> dict[str, Any]:
        """释放所有到期预留；历史记录保留。"""

        if actor_id is not None:
            self._require(actor_id, "reservation.write")
        now_text = self._now()
        due = self.connection.execute(
            "SELECT reservation_id,component_id FROM capacity_reservations WHERE state='held' AND holds_until<=?",
            (now_text,),
        ).fetchall()
        with transaction(self.connection, immediate=True):
            for row in due:
                self.connection.execute(
                    "UPDATE capacity_reservations SET state='expired',released_at=?,release_reason=?,revision=revision+1 "
                    "WHERE reservation_id=? AND state='held'",
                    (now_text, "预留到期自动释放", row["reservation_id"]),
                )
                self._audit("reservation", row["reservation_id"], "reservation.expired",
                            actor_id or "system", {"component_id": row["component_id"]})
        return {"expired": len(due), "reservation_ids": [row["reservation_id"] for row in due]}

    def withdraw_reservation(self, actor_id: str, reservation_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "reservation.write")
        if not reason.strip():
            raise ValidationFailed("撤回原因不能为空")
        row = self._get_held_reservation(reservation_id)
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE capacity_reservations SET state='withdrawn',released_at=?,release_reason=?,revision=revision+1 "
                "WHERE reservation_id=? AND state='held'",
                (self._now(), reason.strip(), reservation_id),
            )
            self._audit("reservation", reservation_id, "reservation.withdrawn", actor_id,
                        {"component_id": row["component_id"], "reason": reason.strip()})
        return {"reservation_id": reservation_id, "state": "withdrawn"}

    def close_project(self, actor_id: str, project_id: str) -> dict[str, Any]:
        """项目成功落地：有效预留转为已消耗，容量被最终占用。"""

        self._require(actor_id, "project.write")
        project = self.connection.execute(
            "SELECT status FROM reuse_projects WHERE project_id=?", (project_id,)
        ).fetchone()
        if project is None:
            raise NotFound("梯次项目不存在")
        if project["status"] != "active":
            raise InvalidState("只有活动项目可以结项")
        now_text = self._now()
        with transaction(self.connection, immediate=True):
            held = self.connection.execute(
                "SELECT reservation_id FROM capacity_reservations WHERE project_id=? AND state='held'",
                (project_id,),
            ).fetchall()
            for row in held:
                self.connection.execute(
                    "UPDATE capacity_reservations SET state='consumed',revision=revision+1 WHERE reservation_id=?",
                    (row["reservation_id"],),
                )
            self.connection.execute(
                "UPDATE reuse_projects SET status='closed' WHERE project_id=? AND status='active'",
                (project_id,),
            )
            self._audit("reuse_project", project_id, "project.closed", actor_id,
                        {"consumed_reservations": len(held)})
        return {"project_id": project_id, "status": "closed", "consumed_reservations": len(held)}

    def fail_project(self, actor_id: str, project_id: str, reason: str) -> dict[str, Any]:
        """项目失败：安全释放其全部有效预留，容量回到可再分配状态，历史保留。"""

        self._require(actor_id, "project.write")
        if not reason.strip():
            raise ValidationFailed("项目失败原因不能为空")
        project = self.connection.execute(
            "SELECT status FROM reuse_projects WHERE project_id=?", (project_id,)
        ).fetchone()
        if project is None:
            raise NotFound("梯次项目不存在")
        if project["status"] != "active":
            raise InvalidState("只有活动项目可以标记失败")
        now_text = self._now()
        with transaction(self.connection, immediate=True):
            held = self.connection.execute(
                "SELECT reservation_id,component_id FROM capacity_reservations WHERE project_id=? AND state='held'",
                (project_id,),
            ).fetchall()
            for row in held:
                self.connection.execute(
                    "UPDATE capacity_reservations SET state='failed',released_at=?,release_reason=?,revision=revision+1 "
                    "WHERE reservation_id=? AND state='held'",
                    (now_text, f"项目失败：{reason.strip()}", row["reservation_id"]),
                )
                self._audit("reservation", row["reservation_id"], "reservation.released_project_failed", actor_id,
                            {"component_id": row["component_id"]})
            self.connection.execute(
                "UPDATE reuse_projects SET status='failed',failed_at=? WHERE project_id=? AND status='active'",
                (now_text, project_id),
            )
            self._audit("reuse_project", project_id, "project.failed", actor_id,
                        {"reason": reason.strip(), "released": len(held)})
        return {"project_id": project_id, "status": "failed", "released_reservations": len(held)}

    def reservation_history(self, actor_id: str, component_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        if self.connection.execute("SELECT 1 FROM components WHERE component_id=?", (component_id,)).fetchone() is None:
            raise NotFound("组件不存在")
        rows = self.connection.execute(
            "SELECT reservation_id,project_id,batch_id,state,capacity_kwh,hold_days,holds_until,created_at,"
            "released_at,revision FROM capacity_reservations WHERE component_id=? "
            "ORDER BY created_at,reservation_id",
            (component_id,),
        ).fetchall()
        return {"component_id": component_id, "reservations": [dict(row) for row in rows]}

    # ── 审计链 ────────────────────────────────────────────────────────────

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
