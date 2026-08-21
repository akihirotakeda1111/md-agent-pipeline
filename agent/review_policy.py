"""Deterministic review policy. Classifier output is not execution authority."""

from __future__ import annotations

from agent.config import RuntimeEditPolicy
from agent.review_types import (
    ClassificationResult,
    PolicyDecision,
    ReviewClassification,
    ReviewPolicyAction,
)
from agent.scope import path_is_in_scope
from agent.spec import TaskSpec

AUTO_REPAIR_DEFERRED_REASON = "automatic review repair is deferred"
AUTO_REPAIR_DEFERRED_HUMAN_ACTION = (
    "Inspect CodeRabbit findings and decide whether to apply a fix. "
    "Automatic review repair is deferred."
)


def decide_review_policy(
    result: ClassificationResult,
    spec: TaskSpec,
    *,
    runtime_policy: RuntimeEditPolicy,
    confidence_threshold: float,
    auto_repair_enabled: bool = False,
) -> PolicyDecision:
    if result.classification is ReviewClassification.NON_ACTIONABLE:
        return PolicyDecision(
            ReviewPolicyAction.IGNORE,
            "non-actionable review",
            result.classification,
        )
    if result.classification is ReviewClassification.OUT_OF_SCOPE:
        return PolicyDecision(
            ReviewPolicyAction.ESCALATE,
            "review is out of scope",
            result.classification,
        )
    if result.classification is ReviewClassification.CONFLICTS_WITH_SPEC:
        return PolicyDecision(
            ReviewPolicyAction.ESCALATE,
            "review conflicts with the Task Spec",
            result.classification,
        )
    if result.classification is ReviewClassification.UNCERTAIN:
        return PolicyDecision(
            ReviewPolicyAction.ESCALATE,
            "review classification is uncertain",
            result.classification,
        )
    if result.classification is not ReviewClassification.ACTIONABLE:
        return PolicyDecision(
            ReviewPolicyAction.ESCALATE,
            f"unsupported classification {result.classification.value}",
            result.classification,
        )
    if not auto_repair_enabled:
        return PolicyDecision(
            ReviewPolicyAction.ESCALATE,
            AUTO_REPAIR_DEFERRED_REASON,
            result.classification,
        )
    if result.confidence < confidence_threshold:
        return PolicyDecision(
            ReviewPolicyAction.ESCALATE,
            "actionable review is below confidence threshold",
            result.classification,
        )
    if not result.referenced_paths:
        return PolicyDecision(
            ReviewPolicyAction.ESCALATE,
            "actionable review has no referenced paths",
            result.classification,
        )
    if any(not path_is_in_scope(path, spec, runtime_policy) for path in result.referenced_paths):
        return PolicyDecision(
            ReviewPolicyAction.ESCALATE,
            "actionable referenced paths are not a subset of allowed_paths",
            result.classification,
        )
    return PolicyDecision(
        ReviewPolicyAction.FIX,
        "actionable review is in scope",
        result.classification,
    )
