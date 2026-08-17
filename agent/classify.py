"""Deterministic failure classification. Unknown cases escalate."""

from __future__ import annotations

import re
from enum import StrEnum

from agent.validation import ValidationRecord


class FailureClass(StrEnum):
    AGENT_REPAIRABLE = "AGENT_REPAIRABLE"
    ENVIRONMENT_FAILURE = "ENVIRONMENT_FAILURE"
    ESCALATION_REQUIRED = "ESCALATION_REQUIRED"


REPAIRABLE_BINARIES = frozenset(
    {
        "pytest",
        "python",
        "python3",
        "py",
        "ruff",
        "mypy",
        "pyright",
        "npm",
        "npx",
        "node",
        "cargo",
        "go",
    }
)

ENV_PHRASES = (
    "could not resolve host",
    "temporary failure in name resolution",
    "connection refused",
    "network is unreachable",
    "no space left on device",
    "failed to fetch",
    "registry.npmjs.org",
    "pypi.org",
    "ssl certificate",
    "certificate verify failed",
    "authentication failed",
    "no matching distribution found",
    "git executable not found",
    "codex cli not found",
    "validation executable not found",
    "timed out after",
    "getaddrinfo enotfound",
    "name or service not known",
    "nodename nor servname provided",
    "unknownhostexception",
)

# Errno tokens must be whole words. "enotfound" must not match FileNotFoundError.
ENV_ERRNO_RE = re.compile(r"\b(?:etimedout|econnrefused|enotfound)\b", re.IGNORECASE)

ESCALATION_MARKERS = (
    "iam policy",
    "terraform apply",
    "terraform destroy",
    "destructive migration",
    "scope_violation",
    "requires human",
    "architecture invariant",
    "forbidden command",
    "command not in allowlist",
)


def classify_validation(record: ValidationRecord) -> FailureClass | None:
    if record.passed:
        return None
    if record.denied:
        return FailureClass.ESCALATION_REQUIRED
    if record.timed_out:
        return FailureClass.ENVIRONMENT_FAILURE
    return classify_output(
        stdout=record.stdout,
        stderr=record.stderr,
        binary=record.argv[0] if record.argv else "",
        exit_code=record.exit_code,
    )


def classify_output(
    *,
    stdout: str,
    stderr: str,
    binary: str = "",
    exit_code: int | None = None,
) -> FailureClass:
    text = f"{stdout}\n{stderr}".lower()
    if any(marker in text for marker in ENV_PHRASES) or ENV_ERRNO_RE.search(text):
        return FailureClass.ENVIRONMENT_FAILURE
    if any(marker in text for marker in ESCALATION_MARKERS):
        return FailureClass.ESCALATION_REQUIRED
    name = binary.replace("\\", "/").split("/")[-1].lower()
    if name.endswith(".exe"):
        name = name[:-4]
    if name in REPAIRABLE_BINARIES and exit_code not in (None, 0):
        return FailureClass.AGENT_REPAIRABLE
    return FailureClass.ESCALATION_REQUIRED
