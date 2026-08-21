from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from .models import EnvironmentBlocker, ProductionBug

REQUIRED_FILES = (
    ".agent/bootstrap.md",
    ".agent/contracts/security.md",
    ".agent/contracts/invariants.md",
    ".agent/contracts/architecture.md",
    ".agent/phases/06-git-pr-observability.md",
    ".agent/phases/07-coderabbit-review.md",
    ".github/workflows/agent-execute.yml",
    ".github/workflows/agent-review.yml",
    "agent/config.json",
    ".coderabbit.yaml",
    "README.md",
)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inspect_source_contracts(
    root: Path, *, e2e_base_branch: str | None = None, default_branch: str | None = None
) -> dict[str, Any]:
    missing = [relative for relative in REQUIRED_FILES if not (root / relative).is_file()]
    if missing:
        raise ProductionBug(f"required Phase 7 Production source file(s) missing: {missing}")
    config_path = root / "agent" / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionBug(f"cannot parse agent/config.json: {exc}") from exc
    actor = str((config.get("coderabbit") or {}).get("actor") or "").strip()
    check_app_slug = str(
        (config.get("coderabbit") or {}).get("check_app_slug") or "coderabbitai"
    ).strip()
    status_context = str(
        (config.get("coderabbit") or {}).get("status_context") or "CodeRabbit"
    ).strip()
    classifier_model = str((config.get("review") or {}).get("classifier_model") or "").strip()
    track_author = str((config.get("review") or {}).get("track_author") or "").strip()
    if not actor:
        raise ProductionBug("agent/config.json coderabbit.actor is not configured")
    if not check_app_slug:
        raise ProductionBug("agent/config.json coderabbit.check_app_slug is not configured")
    if not status_context:
        raise ProductionBug("agent/config.json coderabbit.status_context is not configured")
    if not classifier_model:
        raise ProductionBug("agent/config.json review.classifier_model is not configured")
    if not track_author:
        raise ProductionBug("agent/config.json review.track_author is not configured")
    coderabbit_path = root / ".coderabbit.yaml"
    try:
        coderabbit = yaml.safe_load(coderabbit_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ProductionBug(f"cannot parse .coderabbit.yaml: {exc}") from exc
    if not isinstance(coderabbit, dict):
        raise ProductionBug(".coderabbit.yaml must be a mapping")
    reviews = coderabbit.get("reviews") or {}
    auto_review = reviews.get("auto_review") or {}
    finishing = reviews.get("finishing_touches") or {}
    autofix = finishing.get("autofix") or {}
    if auto_review.get("enabled") is not True:
        raise EnvironmentBlocker("CodeRabbit automatic review is not enabled")
    if auto_review.get("auto_incremental_review") is not True:
        raise EnvironmentBlocker("CodeRabbit incremental review is not enabled")
    if autofix.get("enabled") is not False:
        raise ProductionBug("CodeRabbit Autofix must be explicitly disabled")
    if reviews.get("review_status") is not True:
        raise EnvironmentBlocker("CodeRabbit review_status is not enabled")
    if reviews.get("review_progress") is not True:
        raise EnvironmentBlocker("CodeRabbit review_progress is not enabled")
    if reviews.get("commit_status") is not True:
        raise EnvironmentBlocker("CodeRabbit commit_status is not enabled")
    base_branches = auto_review.get("base_branches")
    if not isinstance(base_branches, list):
        raise ProductionBug("CodeRabbit reviews.auto_review.base_branches must be a list")
    if (
        e2e_base_branch
        and default_branch
        and e2e_base_branch != default_branch
        and not base_branches
    ):
        raise EnvironmentBlocker(
            "CodeRabbit is configured for the default base branch only; "
            "the isolated Phase 7 E2E base branch needs an authorized base pattern",
            evidence={
                "e2e_base_branch": e2e_base_branch,
                "default_branch": default_branch,
                "configured_base_branches": base_branches,
            },
        )
    files = [root / relative for relative in REQUIRED_FILES]
    return {
        "files": [str(path.relative_to(root)).replace("\\", "/") for path in files],
        "sha256": {str(path.relative_to(root)).replace("\\", "/"): _digest(path) for path in files},
        "coderabbit_actor": actor,
        "coderabbit_check_app_slug": check_app_slug,
        "coderabbit_status_context": status_context,
        "classifier_model": classifier_model,
        "track_author": track_author,
        "coderabbit": {
            "auto_review": True,
            "incremental_review": True,
            "autofix": False,
            "review_status": True,
            "review_progress": True,
            "commit_status": True,
            "base_branches": base_branches,
        },
    }
