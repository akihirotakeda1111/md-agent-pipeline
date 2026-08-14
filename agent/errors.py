"""Orchestrator error model.

Categories are distinct so callers can branch without parsing message text.
Unknown exceptions are treated as InternalFailure (fail closed).
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCategory(StrEnum):
    INVALID_INPUT = "InvalidInput"
    ENVIRONMENT_FAILURE = "EnvironmentFailure"
    POLICY_VIOLATION = "PolicyViolation"
    ESCALATION_REQUIRED = "EscalationRequired"
    INTERNAL_FAILURE = "InternalFailure"


class AgentError(Exception):
    """Typed orchestrator failure."""

    def __init__(
        self,
        category: ErrorCategory,
        message: str,
        *,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.code = code

    @classmethod
    def invalid_input(cls, message: str, *, code: str | None = None) -> AgentError:
        return cls(ErrorCategory.INVALID_INPUT, message, code=code)

    @classmethod
    def invalid_spec(cls, message: str) -> AgentError:
        return cls(ErrorCategory.INVALID_INPUT, message, code="INVALID_SPEC")

    @classmethod
    def environment_failure(cls, message: str, *, code: str | None = None) -> AgentError:
        return cls(ErrorCategory.ENVIRONMENT_FAILURE, message, code=code)

    @classmethod
    def policy_violation(cls, message: str, *, code: str | None = None) -> AgentError:
        return cls(ErrorCategory.POLICY_VIOLATION, message, code=code)

    @classmethod
    def escalation_required(cls, message: str, *, code: str | None = None) -> AgentError:
        return cls(ErrorCategory.ESCALATION_REQUIRED, message, code=code)

    @classmethod
    def internal_failure(cls, message: str, *, code: str | None = None) -> AgentError:
        return cls(ErrorCategory.INTERNAL_FAILURE, message, code=code)

    def to_dict(self) -> dict[str, str]:
        payload = {
            "category": self.category.value,
            "message": str(self),
        }
        if self.code is not None:
            payload["code"] = self.code
        return payload


def error_category_of(error: BaseException) -> ErrorCategory:
    """Map an exception to a category. Unrecognized errors are InternalFailure."""
    if isinstance(error, AgentError):
        return error.category
    return ErrorCategory.INTERNAL_FAILURE
