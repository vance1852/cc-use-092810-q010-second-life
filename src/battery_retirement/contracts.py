"""退役评估输入的严格数据契约。

评估的四类依据在这里被冻结并校验：资产配置版本、测量窗口内的容量与内阻
记录、维修与安全质量事件、退役政策版本。所有对象不可变，便于按内容摘要
复算与长期保存。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from .clock import parse_utc


class ValidationError(ValueError):
    """输入不能满足退役领域契约。"""


MEASUREMENT_KINDS = frozenset({"capacity_retention_percent", "internal_resistance_percent"})
EVENT_CATEGORIES = frozenset({"maintenance", "safety"})
EVENT_SEVERITIES = frozenset({"minor", "major", "critical"})

CONCLUSIONS = frozenset(
    {"continue_service", "derating", "cascade", "recycle", "pending_evidence"}
)


def _mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{path} 必须是对象")
    return value


def _sequence(value: object, path: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValidationError(f"{path} 必须是数组")
    return value


def _text(value: object, path: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{path} 必须是非空字符串")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationError(f"{path} 不能超过 {maximum} 个字符")
    return result


def _optional_text(value: object, path: str) -> str | None:
    if value is None:
        return None
    return _text(value, path)


def _decimal(value: object, path: str) -> Decimal:
    if isinstance(value, bool):
        raise ValidationError(f"{path} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"{path} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationError(f"{path} 必须是有限数值")
    return result


def _decimal_between(value: object, path: str, lower: Decimal, upper: Decimal) -> Decimal:
    result = _decimal(value, path)
    if result < lower or result > upper:
        raise ValidationError(f"{path} 必须在 {lower} 到 {upper} 之间")
    return result


def _timestamp(value: object, path: str) -> str:
    text = _text(value, path, 40)
    try:
        return parse_utc(text, path).isoformat().replace("+00:00", "Z")
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class ComponentConfig:
    """评估时刻冻结的组件资产配置版本。"""

    component_id: str
    revision: int
    model_name: str
    chemistry: str
    nominal_capacity_kwh: Decimal
    rated_capacity_kwh: Decimal
    commissioned_at: str
    replaced_parts: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: object, path: str = "component_config") -> "ComponentConfig":
        data = _mapping(raw, path)
        revision = data.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
            raise ValidationError(f"{path}.revision 必须是正整数")
        nominal = _decimal_between(
            data.get("nominal_capacity_kwh"), f"{path}.nominal_capacity_kwh", Decimal("0"), Decimal("1e9")
        )
        rated = _decimal_between(
            data.get("rated_capacity_kwh"), f"{path}.rated_capacity_kwh", Decimal("0"), Decimal("1e9")
        )
        if rated > nominal:
            raise ValidationError(f"{path}.rated_capacity_kwh 不能大于额定铭牌容量")
        replaced = tuple(
            _text(item, f"{path}.replaced_parts[{index}]", 128)
            for index, item in enumerate(_sequence(data.get("replaced_parts", ()), f"{path}.replaced_parts"))
        )
        return cls(
            component_id=_text(data.get("component_id"), f"{path}.component_id", 64),
            revision=revision,
            model_name=_text(data.get("model_name"), f"{path}.model_name"),
            chemistry=_text(data.get("chemistry"), f"{path}.chemistry", 32),
            nominal_capacity_kwh=nominal,
            rated_capacity_kwh=rated,
            commissioned_at=_timestamp(data.get("commissioned_at"), f"{path}.commissioned_at"),
            replaced_parts=replaced,
        )


@dataclass(frozen=True, slots=True)
class Measurement:
    """一条容量或内阻检测记录。recorded_at 是结果入库时间。"""

    record_id: str
    kind: str
    value: Decimal
    source: str
    measured_at: str
    recorded_at: str

    @classmethod
    def from_dict(cls, raw: object, path: str) -> "Measurement":
        data = _mapping(raw, path)
        kind = _text(data.get("kind"), f"{path}.kind", 48)
        if kind not in MEASUREMENT_KINDS:
            raise ValidationError(f"{path}.kind 必须是容量或内阻检测")
        value = _decimal_between(data.get("value"), f"{path}.value", Decimal("0"), Decimal("100000"))
        measured_at = _timestamp(data.get("measured_at"), f"{path}.measured_at")
        recorded_at = _timestamp(data.get("recorded_at"), f"{path}.recorded_at")
        if recorded_at < measured_at:
            raise ValidationError(f"{path}.recorded_at 不能早于 measured_at")
        return cls(
            record_id=_text(data.get("record_id"), f"{path}.record_id", 64),
            kind=kind,
            value=value,
            source=_text(data.get("source"), f"{path}.source", 128),
            measured_at=measured_at,
            recorded_at=recorded_at,
        )


@dataclass(frozen=True, slots=True)
class QualityEvent:
    """一条维修或安全质量事件。resolved 表示事件是否已闭环。"""

    event_id: str
    category: str
    severity: str
    source: str
    occurred_at: str
    recorded_at: str
    resolved: bool
    note: str | None

    @classmethod
    def from_dict(cls, raw: object, path: str) -> "QualityEvent":
        data = _mapping(raw, path)
        category = _text(data.get("category"), f"{path}.category", 24)
        if category not in EVENT_CATEGORIES:
            raise ValidationError(f"{path}.category 必须是 maintenance 或 safety")
        severity = _text(data.get("severity"), f"{path}.severity", 16)
        if severity not in EVENT_SEVERITIES:
            raise ValidationError(f"{path}.severity 不受支持")
        resolved = data.get("resolved", True)
        if not isinstance(resolved, bool):
            raise ValidationError(f"{path}.resolved 必须是布尔值")
        occurred_at = _timestamp(data.get("occurred_at"), f"{path}.occurred_at")
        recorded_at = _timestamp(data.get("recorded_at"), f"{path}.recorded_at")
        return cls(
            event_id=_text(data.get("event_id"), f"{path}.event_id", 64),
            category=category,
            severity=severity,
            source=_text(data.get("source"), f"{path}.source", 128),
            occurred_at=occurred_at,
            recorded_at=recorded_at,
            resolved=resolved,
            note=_optional_text(data.get("note"), f"{path}.note"),
        )


@dataclass(frozen=True, slots=True)
class PolicyThresholds:
    capacity_continue_percent: Decimal
    capacity_cascade_percent: Decimal
    resistance_good_percent: Decimal
    resistance_cascade_percent: Decimal
    maintenance_major_limit: int

    @classmethod
    def from_dict(cls, raw: object, path: str) -> "PolicyThresholds":
        data = _mapping(raw, path)
        cap_continue = _decimal_between(
            data.get("capacity_continue_percent"), f"{path}.capacity_continue_percent",
            Decimal("0"), Decimal("100"),
        )
        cap_cascade = _decimal_between(
            data.get("capacity_cascade_percent"), f"{path}.capacity_cascade_percent",
            Decimal("0"), Decimal("100"),
        )
        if cap_cascade >= cap_continue:
            raise ValidationError(f"{path}.capacity_cascade_percent 必须低于继续服役阈值")
        res_good = _decimal_between(
            data.get("resistance_good_percent"), f"{path}.resistance_good_percent",
            Decimal("100"), Decimal("1000"),
        )
        res_cascade = _decimal_between(
            data.get("resistance_cascade_percent"), f"{path}.resistance_cascade_percent",
            Decimal("100"), Decimal("1000"),
        )
        if res_cascade < res_good:
            raise ValidationError(f"{path}.resistance_cascade_percent 不能低于良好内阻阈值")
        limit = data.get("maintenance_major_limit")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValidationError(f"{path}.maintenance_major_limit 必须是正整数")
        return cls(
            capacity_continue_percent=cap_continue,
            capacity_cascade_percent=cap_cascade,
            resistance_good_percent=res_good,
            resistance_cascade_percent=res_cascade,
            maintenance_major_limit=limit,
        )


@dataclass(frozen=True, slots=True)
class PolicyValuation:
    reference_unit_value_cny_per_kwh: Decimal
    continue_factor: Decimal
    derating_factor: Decimal
    cascade_factor: Decimal
    recycle_unit_value_cny_per_kwh: Decimal

    @classmethod
    def from_dict(cls, raw: object, path: str) -> "PolicyValuation":
        data = _mapping(raw, path)
        return cls(
            reference_unit_value_cny_per_kwh=_decimal_between(
                data.get("reference_unit_value_cny_per_kwh"),
                f"{path}.reference_unit_value_cny_per_kwh", Decimal("0"), Decimal("1e9"),
            ),
            continue_factor=_decimal_between(
                data.get("continue_factor"), f"{path}.continue_factor", Decimal("0"), Decimal("10"),
            ),
            derating_factor=_decimal_between(
                data.get("derating_factor"), f"{path}.derating_factor", Decimal("0"), Decimal("10"),
            ),
            cascade_factor=_decimal_between(
                data.get("cascade_factor"), f"{path}.cascade_factor", Decimal("0"), Decimal("10"),
            ),
            recycle_unit_value_cny_per_kwh=_decimal_between(
                data.get("recycle_unit_value_cny_per_kwh"),
                f"{path}.recycle_unit_value_cny_per_kwh", Decimal("0"), Decimal("1e9"),
            ),
        )


@dataclass(frozen=True, slots=True)
class Policy:
    """退役评估与剩余价值计量所依据的不可歧义政策版本。"""

    policy_id: str
    version: int
    title: str
    thresholds: PolicyThresholds
    valuation: PolicyValuation
    reservation_hold_days: int

    @classmethod
    def from_dict(cls, raw: object) -> "Policy":
        data = _mapping(raw, "policy")
        version = data.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
            raise ValidationError("policy.version 必须是正整数")
        hold_days = data.get("reservation_hold_days")
        if isinstance(hold_days, bool) or not isinstance(hold_days, int) or hold_days <= 0:
            raise ValidationError("policy.reservation_hold_days 必须是正整数")
        return cls(
            policy_id=_text(data.get("policy_id"), "policy.policy_id", 64),
            version=version,
            title=_text(data.get("title"), "policy.title"),
            thresholds=PolicyThresholds.from_dict(data.get("thresholds"), "policy.thresholds"),
            valuation=PolicyValuation.from_dict(data.get("valuation"), "policy.valuation"),
            reservation_hold_days=hold_days,
        )


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    """一次评估开放时面对的全部候选证据（含晚到记录，由引擎按窗口筛选）。"""

    measurements: tuple[Measurement, ...] = field(default_factory=tuple)
    events: tuple[QualityEvent, ...] = field(default_factory=tuple)

    @classmethod
    def from_dict(cls, raw: object) -> "EvidenceBundle":
        data = _mapping(raw, "evidence")
        measurements = tuple(
            Measurement.from_dict(item, f"evidence.measurements[{index}]")
            for index, item in enumerate(_sequence(data.get("measurements", ()), "evidence.measurements"))
        )
        events = tuple(
            QualityEvent.from_dict(item, f"evidence.events[{index}]")
            for index, item in enumerate(_sequence(data.get("events", ()), "evidence.events"))
        )
        measurement_ids = [item.record_id for item in measurements]
        event_ids = [item.event_id for item in events]
        if len(set(measurement_ids)) != len(measurement_ids):
            raise ValidationError("检测记录 record_id 不能重复")
        if len(set(event_ids)) != len(event_ids):
            raise ValidationError("质量事件 event_id 不能重复")
        return cls(measurements=measurements, events=events)
