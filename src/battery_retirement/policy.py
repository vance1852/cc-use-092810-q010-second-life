"""退役结论的确定性策略引擎（纯函数，可独立复算）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping, Sequence

from .models import AssessmentWindows, Component, MeasurementRecord, RetirementPolicy


POLICY_ENGINE_VERSION = "retirement-engine-1.0.0"
RECENT_SAMPLE_SIZE = 3

CONTINUE_SERVICE = "continue_service"
DERATE = "derate"
CASCADE = "cascade_utilization"
RECYCLE = "recycle"
PENDING_EVIDENCE = "pending_evidence"

RECOMMENDATIONS = (CONTINUE_SERVICE, DERATE, CASCADE, RECYCLE, PENDING_EVIDENCE)

RECOMMENDATION_LABELS = {
    CONTINUE_SERVICE: "继续服役",
    DERATE: "降额使用",
    CASCADE: "进入梯次利用",
    RECYCLE: "拆解回收",
    PENDING_EVIDENCE: "等待补证",
}

_HUNDRED = Decimal("100")
_QUANTITY_QUANT = Decimal("0.001")
_MONEY_QUANT = Decimal("0.01")


def _q3(value: Decimal) -> Decimal:
    return value.quantize(_QUANTITY_QUANT, rounding=ROUND_HALF_UP)


def _money(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)


def _mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / Decimal(len(values))


def _within(value: str, start: str, end: str) -> bool:
    return start <= value <= end


@dataclass(frozen=True, slots=True)
class EvidenceSelection:
    """一次评估冻结窗口内实际采信的证据。"""

    capacity_records: tuple[MeasurementRecord, ...]
    resistance_records: tuple[MeasurementRecord, ...]
    repair_events: tuple[MeasurementRecord, ...]
    safety_events: tuple[MeasurementRecord, ...]
    gaps: tuple[str, ...]
    ledger_registered: Mapping[str, bool]

    def as_basis(self) -> dict[str, Any]:
        def identity(item: MeasurementRecord) -> str:
            return f"{item.source_batch}/{item.source_row}"

        return {
            "capacity_records": [identity(item) for item in self.capacity_records],
            "resistance_records": [identity(item) for item in self.resistance_records],
            "repair_events": [identity(item) for item in self.repair_events],
            "safety_events": [identity(item) for item in self.safety_events],
            "evidence_gaps": list(self.gaps),
            "ledger_registered": dict(self.ledger_registered),
        }


def select_evidence(
    records: Sequence[MeasurementRecord],
    windows: AssessmentWindows,
    policy: RetirementPolicy,
) -> EvidenceSelection:
    """按冻结窗口筛选记录；窗口外（含晚到）证据一律不采信。"""

    capacity = sorted(
        (
            item
            for item in records
            if item.kind == "capacity"
            and _within(item.measured_at, windows.capacity_window.starts_at, windows.capacity_window.ends_at)
        ),
        key=lambda item: (item.measured_at, item.source_batch, item.source_row),
    )[-RECENT_SAMPLE_SIZE:]
    resistance = sorted(
        (
            item
            for item in records
            if item.kind == "resistance"
            and _within(item.measured_at, windows.resistance_window.starts_at, windows.resistance_window.ends_at)
        ),
        key=lambda item: (item.measured_at, item.source_batch, item.source_row),
    )[-RECENT_SAMPLE_SIZE:]
    repairs = sorted(
        (
            item
            for item in records
            if item.kind == "repair"
            and _within(item.measured_at, windows.events_after, windows.as_of)
        ),
        key=lambda item: (item.measured_at, item.source_batch, item.source_row),
    )
    safety = sorted(
        (
            item
            for item in records
            if item.kind == "safety"
            and _within(item.measured_at, windows.events_after, windows.as_of)
        ),
        key=lambda item: (item.measured_at, item.source_batch, item.source_row),
    )
    ledger_registered = {
        "capacity": any(item.kind == "capacity" for item in records),
        "resistance": any(item.kind == "resistance" for item in records),
        "repairs": any(item.kind == "repair" for item in records),
        "safety": any(item.kind == "safety" for item in records),
    }
    gaps: list[str] = []
    if policy.required_evidence["capacity"] and not capacity:
        gaps.append("capacity: 容量窗口内没有可用容量检测记录")
    if policy.required_evidence["resistance"]:
        if not resistance:
            gaps.append("resistance: 内阻窗口内没有可用内阻检测记录")
    if policy.required_evidence["repairs"] and not ledger_registered["repairs"]:
        gaps.append("repairs: 未挂载维修台账记录")
    if policy.required_evidence["safety"] and not ledger_registered["safety"]:
        gaps.append("safety: 未挂载安全台账记录")
    return EvidenceSelection(
        tuple(capacity), tuple(resistance), tuple(repairs), tuple(safety), tuple(gaps), ledger_registered
    )


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    component_id: str
    recommendation: str
    recommendation_label: str
    explanations: tuple[str, ...]
    evidence_gaps: tuple[str, ...]
    metrics: Mapping[str, Any]
    residual_value_cny: Decimal | None
    basis: Mapping[str, Any]
    policy_engine_version: str = POLICY_ENGINE_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "recommendation": self.recommendation,
            "recommendation_label": self.recommendation_label,
            "explanations": list(self.explanations),
            "evidence_gaps": list(self.evidence_gaps),
            "metrics": self.metrics,
            "residual_value_cny": None
            if self.residual_value_cny is None
            else format(self.residual_value_cny, "f"),
            "basis": self.basis,
            "policy_engine_version": self.policy_engine_version,
        }


def evaluate_component(
    component: Component,
    records: Sequence[MeasurementRecord],
    windows: AssessmentWindows,
    policy: RetirementPolicy,
) -> EvaluationResult:
    """依据冻结的配置、窗口、证据和政策版本推导解释性结论。"""

    selection = select_evidence(records, windows, policy)
    rules = policy.rules
    explanations: list[str] = []

    soh: Decimal | None = None
    if selection.capacity_records:
        average_capacity = _mean([item.value for item in selection.capacity_records if item.value is not None])
        soh = _q3(average_capacity / component.rated_capacity_kwh * _HUNDRED)
        explanations.append(
            f"窗口内最近 {len(selection.capacity_records)} 次容量均值 {_q3(average_capacity)} kWh，"
            f"SOH={soh}%"
        )

    resistance_growth: Decimal | None = None
    resistance_gap: str | None = None
    if component.baseline_resistance_milliohm is None:
        resistance_gap = "resistance: 资产配置缺少基线内阻，无法计算劣化幅度"
    elif selection.resistance_records:
        average_resistance = _mean([item.value for item in selection.resistance_records if item.value is not None])
        resistance_growth = _q3(
            (average_resistance - component.baseline_resistance_milliohm)
            / component.baseline_resistance_milliohm
            * _HUNDRED
        )
        explanations.append(
            f"窗口内最近 {len(selection.resistance_records)} 次内阻均值 {_q3(average_resistance)} mΩ，"
            f"相对基线增长 {resistance_growth}%"
        )

    open_critical_safety = [
        item for item in selection.safety_events
        if item.status == "open" and item.severity == "critical"
    ]
    open_high_safety = [
        item for item in selection.safety_events
        if item.status == "open" and item.severity == "high"
    ]
    open_high_repairs = [
        item for item in selection.repair_events
        if item.status == "open" and item.severity == "high"
    ]
    open_medium_events = [
        item
        for item in (*selection.repair_events, *selection.safety_events)
        if item.status == "open" and item.severity == "medium"
    ]
    if open_critical_safety:
        explanations.append(f"存在 {len(open_critical_safety)} 起未闭环 critical 安全事件，禁止任何再利用路径")
    if open_high_safety:
        explanations.append(f"存在 {len(open_high_safety)} 起未闭环 high 安全事件，禁止继续服役")
    if open_high_repairs:
        explanations.append(f"存在 {len(open_high_repairs)} 起未闭环 high 维修事件，禁止继续服役")
    all_events = (*selection.repair_events, *selection.safety_events)
    if open_medium_events:
        explanations.append(f"存在 {len(open_medium_events)} 起未闭环 medium 质量事件，结论附带风险提示")
    closed_count = sum(1 for item in all_events if item.status == "closed")
    if closed_count > 0:
        explanations.append(f"另有 {closed_count} 起已闭环质量事件，不构成去向限制")

    gaps = list(selection.gaps)
    if resistance_gap is not None and policy.required_evidence["resistance"]:
        gaps.append(resistance_gap)

    metrics: dict[str, Any] = {
        "soh_percent": None if soh is None else format(soh, "f"),
        "resistance_growth_percent": None if resistance_growth is None else format(resistance_growth, "f"),
        "open_critical_safety": len(open_critical_safety),
        "open_high_safety": len(open_high_safety),
        "open_high_repairs": len(open_high_repairs),
        "open_medium_events": len(open_medium_events),
        "capacity_samples": len(selection.capacity_records),
        "resistance_samples": len(selection.resistance_records),
    }
    basis = {
        "policy_id": policy.policy_id,
        "policy_version": policy.version,
        "policy_engine_version": POLICY_ENGINE_VERSION,
        "windows": {
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
        },
        **selection.as_basis(),
    }

    def pending() -> EvaluationResult:
        explanations.insert(0, "关键证据缺失，结论暂为等待补证；补齐后需重新发起评估版本")
        return EvaluationResult(
            component.component_id,
            PENDING_EVIDENCE,
            RECOMMENDATION_LABELS[PENDING_EVIDENCE],
            tuple(explanations),
            tuple(gaps),
            metrics,
            None,
            basis,
        )

    # 安全优先：未闭环 critical 安全事件直接拆解回收，不再等待其他证据。
    if open_critical_safety:
        value = _money(component.rated_capacity_kwh * rules["recycle_value_cny_per_kwh"])
        explanations.append(f"剩余价值按回收单价 {rules['recycle_value_cny_per_kwh']} 元/kWh 估算为 {value} 元")
        return EvaluationResult(
            component.component_id,
            RECYCLE,
            RECOMMENDATION_LABELS[RECYCLE],
            tuple(explanations),
            tuple(gaps),
            metrics,
            value,
            basis,
        )

    if gaps:
        return pending()

    assert soh is not None and resistance_growth is not None  # gaps 已覆盖缺失情形

    blocked_by_high_event = bool(open_high_safety or open_high_repairs)
    recommendation: str
    if (
        soh >= rules["continue_service_min_soh"]
        and resistance_growth <= rules["resistance_warning_percent"]
        and not blocked_by_high_event
    ):
        recommendation = CONTINUE_SERVICE
        explanations.append(
            f"SOH>={rules['continue_service_min_soh']}%、内阻增幅<={rules['resistance_warning_percent']}%，"
            "满足继续服役条件"
        )
    elif (
        soh >= rules["derate_min_soh"]
        and resistance_growth <= rules["resistance_block_percent"]
        and not open_high_safety
    ):
        recommendation = DERATE
        explanations.append(
            f"SOH>={rules['derate_min_soh']}% 且内阻增幅<={rules['resistance_block_percent']}%，"
            "满足降额使用条件"
        )
    elif soh >= rules["reuse_min_soh"] and resistance_growth <= rules["resistance_block_percent"]:
        recommendation = CASCADE
        explanations.append(
            f"SOH>={rules['reuse_min_soh']}% 且内阻增幅<={rules['resistance_block_percent']}%，"
            "可进入梯次利用候选批次"
        )
    else:
        recommendation = RECYCLE
        explanations.append("SOH 或内阻不满足任何继续利用阈值，建议拆解回收")

    if recommendation == CONTINUE_SERVICE:
        value = _money(component.acquisition_cost_cny * soh / _HUNDRED)
        explanations.append(f"剩余价值按购置成本×SOH 估算为 {value} 元")
    elif recommendation == DERATE:
        value = _money(
            component.acquisition_cost_cny * soh / _HUNDRED * rules["derate_value_factor"]
        )
        explanations.append(
            f"剩余价值按购置成本×SOH×降额系数 {rules['derate_value_factor']} 估算为 {value} 元"
        )
    elif recommendation == CASCADE:
        value = _money(
            component.rated_capacity_kwh * rules["reuse_value_cny_per_kwh"] * soh / _HUNDRED
        )
        explanations.append(
            f"剩余价值按梯次利用单价 {rules['reuse_value_cny_per_kwh']} 元/kWh×可用容量估算为 {value} 元"
        )
    else:
        value = _money(component.rated_capacity_kwh * rules["recycle_value_cny_per_kwh"])
        explanations.append(f"剩余价值按回收单价 {rules['recycle_value_cny_per_kwh']} 元/kWh 估算为 {value} 元")

    return EvaluationResult(
        component.component_id,
        recommendation,
        RECOMMENDATION_LABELS[recommendation],
        tuple(explanations),
        tuple(gaps),
        metrics,
        value,
        basis,
    )
