"""FAILED vs ESCALATED policy for Git / PR / GitHub reconciliation outcomes."""

from __future__ import annotations

from agent.classify import FailureClass
from agent.errors import AgentError, ErrorCategory, error_category_of

RETRYABLE_CODES = frozenset(
    {
        "GITHUB_API_FAILURE",
        "GITHUB_API_TIMEOUT",
        "GITHUB_API_NETWORK",
        "GITHUB_API_PERMISSION",
        "GITHUB_API_VALIDATION",
        "MISSING_GITHUB_TOKEN",
        "MISSING_AGENT_PR_PAT",
        "MISSING_GITHUB_REPOSITORY",
        "GIT_FAILED",
        "GIT_NOT_FOUND",
        "GIT_PUSH_FAILED",
        "MISSING_CLASSIFIER_API_KEY",
        "CLASSIFIER_API_FAILURE",
        "CLASSIFIER_API_TIMEOUT",
        "CLASSIFIER_API_NETWORK",
        "CLASSIFIER_API_RATE_LIMIT",
    }
)

ESCALATE_CODES = frozenset(
    {
        "STATE_GIT_MISMATCH",
        "STATE_BRANCH_MISMATCH",
        "STATE_PR_MISMATCH",
        "WORK_UNIT_PR_MISMATCH",
        "STATE_COMMIT_MISMATCH",
        "UNSAFE_RECONCILE",
        "SCOPE_VIOLATION",
        "COMMIT_BEFORE_VALIDATION",
        "COMMIT_SCOPE_VIOLATION",
        "PR_BEFORE_FINAL_VERIFICATION",
        "PATCH_APPLY_FAILED",
        "PATCH_DIGEST_MISMATCH",
        "PATCH_MANIFEST_MISMATCH",
        "REPORT_SPEC_MISMATCH",
        "REPORT_BRANCH_MISMATCH",
        "BASE_SHA_MISMATCH",
        "PULL_HEAD_MISMATCH",
        "BASE_SHA_MISSING",
        "DIRTY_WORKTREE",
        "NON_FAST_FORWARD",
        "EXECUTION_GUARD",
        "INVALID_TRANSITION",
        "REVIEW_ATTEMPT_LIMIT",
        "INVALID_CLASSIFIER_JSON",
        "CLASSIFIER_REFUSAL",
        "CLASSIFIER_INCOMPLETE",
        "REVIEW_SCOPE_VIOLATION",
        "REVIEW_FIX_VALIDATION_FAILED",
        "REVIEW_POLICY_ESCALATED",
        "CODERABBIT_SKIPPED",
        "CODERABBIT_REVIEW_FAILED",
        "CODERABBIT_AMBIGUOUS",
        "SPEC_NOT_FOUND",
        "DUPLICATE_SPEC_ID",
        "UNSAFE_REVIEW_TRACK",
        "SPEC_IDENTITY_MISMATCH",
        "SPEC_PATH_INVALID",
        "SPEC_PATH_ESCAPE",
        "SPEC_PATH_OUT_OF_DIR",
    }
)


def classify_control_plane_error(error: BaseException) -> FailureClass:
    """Map control-plane failures. GitHub/git outages are FAILED; inconsistency escalates."""
    if isinstance(error, AgentError):
        if error.code in ESCALATE_CODES:
            return FailureClass.ESCALATION_REQUIRED
        if error.code in RETRYABLE_CODES:
            return FailureClass.ENVIRONMENT_FAILURE
        if error.category is ErrorCategory.ENVIRONMENT_FAILURE:
            return FailureClass.ENVIRONMENT_FAILURE
        if error.category is ErrorCategory.ESCALATION_REQUIRED:
            return FailureClass.ESCALATION_REQUIRED
        if error.category is ErrorCategory.POLICY_VIOLATION:
            return FailureClass.ESCALATION_REQUIRED
    category = error_category_of(error)
    if category is ErrorCategory.ENVIRONMENT_FAILURE:
        return FailureClass.ENVIRONMENT_FAILURE
    return FailureClass.ESCALATION_REQUIRED


def is_failed(classification: FailureClass) -> bool:
    return classification is FailureClass.ENVIRONMENT_FAILURE
