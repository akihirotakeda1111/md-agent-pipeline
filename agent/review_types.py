"""Review feedback and classification types. No GitHub or Codex I/O."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ReviewOutcome(StrEnum):
    IN_REVIEW = "IN_REVIEW"
    REVIEW_FIX_PUSHED = "REVIEW_FIX_PUSHED"
    READY_FOR_HUMAN = "READY_FOR_HUMAN"
    FAILED = "FAILED"
    ESCALATED = "ESCALATED"


class ReviewClassification(StrEnum):
    ACTIONABLE = "ACTIONABLE"
    NON_ACTIONABLE = "NON_ACTIONABLE"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    CONFLICTS_WITH_SPEC = "CONFLICTS_WITH_SPEC"
    UNCERTAIN = "UNCERTAIN"


class ReviewPolicyAction(StrEnum):
    FIX = "FIX"
    IGNORE = "IGNORE"
    ESCALATE = "ESCALATE"


@dataclass(frozen=True)
class ReviewFeedback:
    kind: str
    identity: str
    source_id: int
    updated_at: str
    author: str
    body: str
    path: str | None
    commit_sha: str | None
    html_url: str | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "identity": self.identity,
            "source_id": self.source_id,
            "updated_at": self.updated_at,
            "author": self.author,
            "body": self.body,
            "path": self.path,
            "commit_sha": self.commit_sha,
            "html_url": self.html_url,
        }


@dataclass(frozen=True)
class ClassificationResult:
    classification: ReviewClassification
    confidence: float
    reason: str
    referenced_paths: tuple[str, ...]

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification.value,
            "confidence": self.confidence,
            "reason": self.reason,
            "referencedPaths": list(self.referenced_paths),
        }


@dataclass(frozen=True)
class PolicyDecision:
    action: ReviewPolicyAction
    reason: str
    classification: ReviewClassification | None = None
