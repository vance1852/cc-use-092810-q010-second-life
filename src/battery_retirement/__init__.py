"""储能电池退役评估与梯次利用管理能力。

将评估所依据的资产配置、测量窗口、质量事件与政策版本冻结为不可变版本，
产出可解释、可复算的退役结论，经独立审批后生效；进入梯次利用的组件组成
带来源的候选批次，容量预留有期限且不被重复占用，释放过程保留完整历史。
"""

from .contracts import (
    ComponentConfig,
    EvidenceBundle,
    Measurement,
    Policy,
    QualityEvent,
    ValidationError,
)
from .engine import ENGINE_VERSION, evaluate, residual_value
from .service import RetirementService

__all__ = [
    "ComponentConfig",
    "EvidenceBundle",
    "Measurement",
    "Policy",
    "QualityEvent",
    "ValidationError",
    "ENGINE_VERSION",
    "RetirementService",
    "evaluate",
    "residual_value",
]

__version__ = "0.1.0"
