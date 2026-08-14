from __future__ import annotations

from agent.errors import AgentError, ErrorCategory, error_category_of


def test_error_categories_match_contract() -> None:
    assert ErrorCategory.INVALID_INPUT.value == "InvalidInput"
    assert ErrorCategory.ENVIRONMENT_FAILURE.value == "EnvironmentFailure"
    assert ErrorCategory.POLICY_VIOLATION.value == "PolicyViolation"
    assert ErrorCategory.ESCALATION_REQUIRED.value == "EscalationRequired"
    assert ErrorCategory.INTERNAL_FAILURE.value == "InternalFailure"
    assert len(set(ErrorCategory)) == 5


def test_factory_methods_set_distinct_categories() -> None:
    errors = [
        AgentError.invalid_input("bad spec"),
        AgentError.environment_failure("network down"),
        AgentError.policy_violation("SCOPE_VIOLATION"),
        AgentError.escalation_required("terraform apply"),
        AgentError.internal_failure("bug"),
    ]

    categories = [error.category for error in errors]
    assert categories == [
        ErrorCategory.INVALID_INPUT,
        ErrorCategory.ENVIRONMENT_FAILURE,
        ErrorCategory.POLICY_VIOLATION,
        ErrorCategory.ESCALATION_REQUIRED,
        ErrorCategory.INTERNAL_FAILURE,
    ]


def test_to_dict_exposes_category_and_message() -> None:
    error = AgentError.policy_violation("path outside allowed_paths")
    assert error.to_dict() == {
        "category": "PolicyViolation",
        "message": "path outside allowed_paths",
    }


def test_unknown_exception_is_internal_failure() -> None:
    assert error_category_of(RuntimeError("boom")) is ErrorCategory.INTERNAL_FAILURE
    assert error_category_of(AgentError.invalid_input("x")) is ErrorCategory.INVALID_INPUT
