"""退役评估领域输入契约与严格校验。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .clock import parse_utc


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
MEASUREMENT_KINDS = {"capacity", "resistance", "repair", "safety"}
SEVERITIES = {"low", "medium", "high", "critical"}
RECORD_STATUSES = {"open", "closed"}


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValueError(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValueError(f"{field} 格式不正确")
    return result


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValueError(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValueError(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValueError(f"{field} 不能大于 {maximum}")
    return result


def timestamp(value: object, field: str) -> str:
    text = required_text(value, field, 40)
    return parse_utc(text, field).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class Component:
    """退役评估对象的资产配置（评估时被冻结）。"""

    component_id: str
    station_id: str
    model_name: str
    chemistry: str
    rated_capacity_kwh: Decimal
    acquisition_cost_cny: Decimal
    baseline_resistance_milliohm: Decimal | None
    commissioned_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Component":
        baseline = raw.get("baseline_resistance_milliohm")
        return cls(
            component_id=identifier(raw.get("component_id"), "component_id"),
            station_id=identifier(raw.get("station_id"), "station_id"),
            model_name=required_text(raw.get("model_name"), "model_name"),
            chemistry=required_text(raw.get("chemistry"), "chemistry", 32),
            rated_capacity_kwh=decimal_value(
                raw.get("rated_capacity_kwh"), "rated_capacity_kwh", minimum=Decimal("0.001")
            ),
            acquisition_cost_cny=decimal_value(
                raw.get("acquisition_cost_cny", 0), "acquisition_cost_cny", minimum=Decimal("0")
            ),
            baseline_resistance_milliohm=None
            if baseline in (None, "")
            else decimal_value(
                baseline, "baseline_resistance_milliohm", minimum=Decimal("0")
            ),
            commissioned_at=timestamp(raw.get("commissioned_at"), "commissioned_at"),
        )


@dataclass(frozen=True, slots=True)
class MeasurementRecord:
    """容量、内阻、维修或安全类别的一条不可变来源记录。"""

    component_id: str
    kind: str
    measured_at: str
    source_batch: str
    source_row: str
    value: Decimal | None
    severity: str | None
    status: str | None
    evidence_ref: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "MeasurementRecord":
        kind = required_text(raw.get("kind"), "kind", 16).lower()
        if kind not in MEASUREMENT_KINDS:
            raise ValueError("kind 必须是 capacity、resistance、repair 或 safety")
        severity = raw.get("severity")
        if severity is not None:
            severity = required_text(severity, "severity", 16).lower()
            if severity not in SEVERITIES:
                raise ValueError("severity 必须是 low、medium、high 或 critical")
        status = raw.get("status")
        if status is not None:
            status = required_text(status, "status", 16).lower()
            if status not in RECORD_STATUSES:
                raise ValueError("status 必须是 open 或 closed")
        if kind in {"repair", "safety"}:
            if severity is None:
                raise ValueError(f"{kind} 记录必须给出 severity")
            if status is None:
                raise ValueError(f"{kind} 记录必须给出 open/closed 状态")
            value = None
            if raw.get("value") is not None:
                value = decimal_value(raw.get("value"), "value", minimum=Decimal("0"))
        else:
            value = decimal_value(raw.get("value"), "value", minimum=Decimal("0"))
        return cls(
            component_id=identifier(raw.get("component_id"), "component_id"),
            kind=kind,
            measured_at=timestamp(raw.get("measured_at"), "measured_at"),
            source_batch=identifier(raw.get("source_batch"), "source_batch"),
            source_row=required_text(raw.get("source_row"), "source_row", 128),
            value=value,
            severity=severity,
            status=status,
            evidence_ref=required_text(raw.get("evidence_ref"), "evidence_ref", 128),
        )


@dataclass(frozen=True, slots=True)
class TimeWindow:
    starts_at: str
    ends_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None, field: str) -> "TimeWindow | None":
        if raw is None:
            return None
        start = timestamp(raw.get("starts_at"), f"{field}.starts_at")
        end = timestamp(raw.get("ends_at"), f"{field}.ends_at")
        if end <= start:
            raise ValueError(f"{field}.ends_at 必须晚于 starts_at")
        return cls(start, end)


@dataclass(frozen=True, slots=True)
class AssessmentWindows:
    """评估冻结的测量窗口：容量窗口、内阻窗口与质量事件回溯起点。"""

    as_of: str
    capacity_window: TimeWindow
    resistance_window: TimeWindow
    events_after: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AssessmentWindows":
        as_of = timestamp(raw.get("as_of"), "as_of")
        capacity = TimeWindow.from_dict(raw.get("capacity_window"), "capacity_window")
        resistance = TimeWindow.from_dict(raw.get("resistance_window"), "resistance_window")
        if capacity is None or resistance is None:
            raise ValueError("容量与内阻测量窗口都必须提供")
        if capacity.ends_at > as_of or resistance.ends_at > as_of:
            raise ValueError("测量窗口结束时间不能晚于 as_of")
        events_after = timestamp(raw.get("events_after", raw.get("as_of")), "events_after")
        if events_after > as_of:
            raise ValueError("events_after 不能晚于 as_of")
        return cls(as_of, capacity, resistance, events_after)


@dataclass(frozen=True, slots=True)
class RetirementPolicy:
    """退役判定政策的不可变版本。"""

    policy_id: str
    version: int
    title: str
    rules: Mapping[str, Decimal]
    required_evidence: Mapping[str, bool]

    RULE_FIELDS = (
        "continue_service_min_soh",
        "derate_min_soh",
        "reuse_min_soh",
        "resistance_warning_percent",
        "resistance_block_percent",
        "derate_value_factor",
        "reuse_value_cny_per_kwh",
        "recycle_value_cny_per_kwh",
    )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RetirementPolicy":
        version = raw.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
            raise ValueError("policy.version 必须是正整数")
        rules_raw = raw.get("rules")
        if not isinstance(rules_raw, Mapping):
            raise ValueError("policy.rules 必须是对象")
        missing = sorted(set(cls.RULE_FIELDS) - set(rules_raw))
        if missing:
            raise ValueError(f"政策规则缺少: {missing}")
        rules = {
            key: decimal_value(rules_raw.get(key), f"rules.{key}", minimum=Decimal("0"))
            for key in cls.RULE_FIELDS
        }
        if not Decimal("0") <= rules["reuse_min_soh"] <= rules["derate_min_soh"] <= rules["continue_service_min_soh"] <= Decimal("100"):
            raise ValueError("SOH 阈值必须满足 0 <= 梯次 <= 降额 <= 继续服役 <= 100")
        if not Decimal("0") <= rules["resistance_warning_percent"] <= rules["resistance_block_percent"]:
            raise ValueError("内阻告警比例必须不大于阻断比例")
        if not Decimal("0") <= rules["derate_value_factor"] <= Decimal("1"):
            raise ValueError("derate_value_factor 必须在 0 到 1 之间")
        evidence_raw = raw.get("required_evidence", {})
        if not isinstance(evidence_raw, Mapping):
            raise ValueError("required_evidence 必须是对象")
        required_evidence = {}
        for key in ("capacity", "resistance"):
            required_evidence[key] = bool(evidence_raw.get(key, True))
        for key in ("repairs", "safety"):
            required_evidence[key] = bool(evidence_raw.get(key, True))
        return cls(
            policy_id=identifier(raw.get("policy_id"), "policy_id"),
            version=version,
            title=required_text(raw.get("title"), "title"),
            rules=rules,
            required_evidence=required_evidence,
        )
