"""退役结论的纯函数评估引擎。

引擎不读写数据库、不依赖当前时间以外的副作用：给定冻结的资产配置、政策
版本、测量窗口截止点和候选证据，就一定产生相同的结论、规则轨迹、证据缺口
和剩余价值。服务层据此开放评估版本，委员会也可随时用同一输入复算。
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .contracts import (
    ComponentConfig,
    EvidenceBundle,
    Measurement,
    Policy,
    QualityEvent,
)


ENGINE_VERSION = "battery-retirement-engine/1"

MONEY = Decimal("0.01")
ENERGY = Decimal("0.001")
HUNDRED = Decimal("100")

# 信号劣化分级，worst() 取其中最严重者。
LEVELS = ("good", "mild", "moderate", "bad")
LEVEL_RANK = {name: index for index, name in enumerate(LEVELS)}

# 信号分级到处置结论的映射。
LEVEL_CONCLUSION = {
    "good": "continue_service",
    "mild": "derating",
    "moderate": "cascade",
    "bad": "recycle",
}

CONCLUSION_LABELS = {
    "continue_service": "继续服役",
    "derating": "降额使用",
    "cascade": "进入梯次利用",
    "recycle": "拆解回收",
    "pending_evidence": "等待补证",
}


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def _energy(value: Decimal) -> Decimal:
    return value.quantize(ENERGY, rounding=ROUND_HALF_UP)


def _text(value: Decimal) -> str:
    return format(value, "f")


def admit_evidence(
    bundle: EvidenceBundle, window_start: str, cutoff: str
) -> tuple[
    tuple[Measurement, ...], tuple[QualityEvent, ...], list[dict[str, str]], list[dict[str, str]]
]:
    """按测量窗口 [window_start, cutoff] 冻结证据。

    入库晚于截止点的记录被隔离为 late（只能触发新版本或复核），早于窗口
    起点的记录归入 before。两者都不删除，只是不进入本次结论。
    """

    measurements: list[Measurement] = []
    events: list[QualityEvent] = []
    late: list[dict[str, str]] = []
    before: list[dict[str, str]] = []

    def classify(recorded_at: str, kind: str, ref: str) -> str:
        row = {"kind": kind, "record_id": ref, "recorded_at": recorded_at}
        if recorded_at > cutoff:
            late.append(row)
            return "late"
        if recorded_at < window_start:
            before.append(row)
            return "before"
        return "in"

    for item in bundle.measurements:
        if classify(item.recorded_at, "measurement", item.record_id) == "in":
            measurements.append(item)
    for item in bundle.events:
        if classify(item.recorded_at, "event", item.event_id) == "in":
            events.append(item)
    late.sort(key=lambda row: (row["recorded_at"], row["kind"], row["record_id"]))
    before.sort(key=lambda row: (row["recorded_at"], row["kind"], row["record_id"]))
    return tuple(measurements), tuple(events), late, before


def _latest_measurement(measurements: tuple[Measurement, ...], kind: str) -> Measurement | None:
    candidates = [item for item in measurements if item.kind == kind]
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item.measured_at, item.recorded_at, item.record_id))


def _capacity_level(capacity: Decimal, policy: Policy) -> tuple[str, Decimal, Decimal]:
    thresholds = policy.thresholds
    mild_floor = (thresholds.capacity_continue_percent + thresholds.capacity_cascade_percent) / 2
    if capacity >= thresholds.capacity_continue_percent:
        return "good", thresholds.capacity_continue_percent, HUNDRED
    if capacity >= mild_floor:
        return "mild", mild_floor, thresholds.capacity_continue_percent
    if capacity >= thresholds.capacity_cascade_percent:
        return "moderate", thresholds.capacity_cascade_percent, mild_floor
    return "bad", Decimal(0), thresholds.capacity_cascade_percent


def _resistance_level(resistance: Decimal, policy: Policy) -> tuple[str, Decimal, Decimal]:
    thresholds = policy.thresholds
    mild_ceiling = (thresholds.resistance_good_percent + thresholds.resistance_cascade_percent) / 2
    if resistance <= thresholds.resistance_good_percent:
        return "good", Decimal(0), thresholds.resistance_good_percent
    if resistance <= mild_ceiling:
        return "mild", thresholds.resistance_good_percent, mild_ceiling
    if resistance <= thresholds.resistance_cascade_percent:
        return "moderate", mild_ceiling, thresholds.resistance_cascade_percent
    return "bad", thresholds.resistance_cascade_percent, Decimal("100000")


def _event_level(events: tuple[QualityEvent, ...], policy: Policy) -> tuple[str, dict[str, int]]:
    open_critical_safety = 0
    open_major_safety = 0
    open_major_maintenance = 0
    for item in events:
        if item.resolved:
            continue
        if item.category == "safety" and item.severity == "critical":
            open_critical_safety += 1
        elif item.category == "safety" and item.severity == "major":
            open_major_safety += 1
        elif item.category == "maintenance" and item.severity in {"major", "critical"}:
            open_major_maintenance += 1
    counts = {
        "open_critical_safety": open_critical_safety,
        "open_major_safety": open_major_safety,
        "open_major_maintenance": open_major_maintenance,
    }
    if open_critical_safety:
        return "bad", counts
    if open_major_safety:
        return "moderate", counts
    if open_major_maintenance >= policy.thresholds.maintenance_major_limit:
        return "mild", counts
    return "good", counts


def _worst(levels: list[str]) -> str:
    return max(levels, key=lambda name: LEVEL_RANK[name])


def residual_value(
    config: ComponentConfig, policy: Policy, conclusion: str
) -> dict[str, Any]:
    """按处置结论估算可解释的剩余价值。"""

    valuation = policy.valuation
    usable = _energy(config.rated_capacity_kwh)
    provisional = False
    if conclusion == "continue_service":
        value = usable * valuation.reference_unit_value_cny_per_kwh * valuation.continue_factor
        basis = "当前可用容量 × 基准单价 × 继续服役系数"
    elif conclusion == "derating":
        value = usable * valuation.reference_unit_value_cny_per_kwh * valuation.derating_factor
        basis = "当前可用容量 × 基准单价 × 降额系数"
    elif conclusion == "cascade":
        value = usable * valuation.reference_unit_value_cny_per_kwh * valuation.cascade_factor
        basis = "当前可用容量 × 基准单价 × 梯次利用系数"
    elif conclusion == "recycle":
        value = usable * valuation.recycle_unit_value_cny_per_kwh
        basis = "当前可用容量 × 拆解回收单位价值"
    else:  # pending_evidence：证据不足时只保证回收底价，其余待补证后重估。
        value = usable * valuation.recycle_unit_value_cny_per_kwh
        basis = "证据未齐：先按拆解回收底价保底，补证后重新计量"
        provisional = True
    return {
        "usable_capacity_kwh": _text(usable),
        "residual_value_cny": _text(_money(value)),
        "value_basis": basis,
        "provisional": provisional,
    }


def evaluate(
    config: ComponentConfig,
    policy: Policy,
    bundle: EvidenceBundle,
    window_start: str,
    window_cutoff: str,
) -> dict[str, Any]:
    """复算一次退役结论，返回结论、分级轨迹、证据缺口与剩余价值。"""

    if window_start > window_cutoff:
        raise ValueError("测量窗口起点不能晚于截止点")

    measurements, events, late, before = admit_evidence(bundle, window_start, window_cutoff)

    latest_capacity = _latest_measurement(measurements, "capacity_retention_percent")
    latest_resistance = _latest_measurement(measurements, "internal_resistance_percent")

    gaps: list[dict[str, str]] = []
    if latest_capacity is None:
        gaps.append({
            "signal": "capacity_retention_percent",
            "reason": "测量窗口内没有已入库的容量保持率记录",
        })
    if latest_resistance is None:
        gaps.append({
            "signal": "internal_resistance_percent",
            "reason": "测量窗口内没有已入库的内阻增幅记录",
        })

    event_level, event_counts = _event_level(events, policy)

    signal_traces: list[dict[str, Any]] = []
    hard_recycle = event_level == "bad"
    levels: list[str] = []

    if latest_capacity is not None:
        cap_level, lower, upper = _capacity_level(latest_capacity.value, policy)
        levels.append(cap_level)
        if cap_level == "bad":
            hard_recycle = True
        signal_traces.append({
            "signal": "capacity_retention_percent",
            "actual": _text(latest_capacity.value),
            "record_id": latest_capacity.record_id,
            "measured_at": latest_capacity.measured_at,
            "level": cap_level,
            "band": {"lower_percent": _text(lower), "upper_percent": _text(upper)},
        })
    if latest_resistance is not None:
        res_level, lower, upper = _resistance_level(latest_resistance.value, policy)
        levels.append(res_level)
        if res_level == "bad":
            hard_recycle = True
        signal_traces.append({
            "signal": "internal_resistance_percent",
            "actual": _text(latest_resistance.value),
            "record_id": latest_resistance.record_id,
            "measured_at": latest_resistance.measured_at,
            "level": res_level,
            "band": {"lower_percent": _text(lower), "upper_percent": _text(upper)},
        })
    signal_traces.append({
        "signal": "quality_events",
        "level": event_level,
        "counts": event_counts,
    })
    levels.append(event_level)

    if hard_recycle:
        conclusion = "recycle"
        rationale = "存在不可接受的安全或性能劣化信号，组件不得继续服役或梯次利用"
    elif gaps:
        conclusion = "pending_evidence"
        rationale = "关键检测证据在测量窗口内缺失，需补证后重开评估"
    else:
        worst = _worst(levels)
        conclusion = LEVEL_CONCLUSION[worst]
        rationale = f"容量、内阻与质量事件的最差分级为 {worst}"

    valuation = residual_value(config, policy, conclusion)
    return {
        "engine_version": ENGINE_VERSION,
        "conclusion": conclusion,
        "conclusion_label": CONCLUSION_LABELS[conclusion],
        "rationale": rationale,
        "window_start": window_start,
        "window_cutoff": window_cutoff,
        "signals": signal_traces,
        "evidence_gaps": gaps,
        "admitted": {
            "measurement_count": len(measurements),
            "event_count": len(events),
        },
        "before_window_evidence": before,
        "late_evidence": late,
        "residual_value": valuation,
    }
