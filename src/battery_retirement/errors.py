"""退役评估与梯次利用服务向 API 和 CLI 暴露的稳定错误。"""

from __future__ import annotations


class RetirementError(RuntimeError):
    code = "retirement_error"
    status = 400


class NotFound(RetirementError):
    code = "not_found"
    status = 404


class Conflict(RetirementError):
    code = "conflict"
    status = 409


class Forbidden(RetirementError):
    code = "forbidden"
    status = 403


class InvalidState(RetirementError):
    code = "invalid_state"
    status = 409


class ValidationFailed(RetirementError):
    code = "validation_failed"
    status = 422
