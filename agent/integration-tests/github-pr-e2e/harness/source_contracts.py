from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .models import ProductionGap

REQUIRED_FILES = (
    ".agent/bootstrap.md",
    ".agent/phases/05-github-actions.md",
    ".agent/phases/06-git-pr-observability.md",
    ".github/workflows/agent-execute.yml",
    "README.md",
)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inspect_source_contracts(root: Path) -> dict[str, Any]:
    missing = [relative for relative in REQUIRED_FILES if not (root / relative).is_file()]
    if missing:
        raise ProductionGap(
            "E2E_SOURCE_CONTRACT_MISSING",
            contract="Phase 5-6 E2E must inspect the documented Production contracts and workflow",
            observed=f"Required source file(s) missing: {missing}",
            impact="The harness cannot bind its scenario to the declared Production contract",
            required_change=(
                "Restore or identify the canonical Production contract files; "
                "do not duplicate them in the E2E suite"
            ),
        )
    contract_candidates = sorted(
        {
            path
            for pattern in (".agent/contracts/**/*.md", ".agent/**/*contract*.md")
            for path in root.glob(pattern)
            if path.is_file()
        }
    )
    if not contract_candidates:
        raise ProductionGap(
            "GLOBAL_CONTRACTS_NOT_FOUND",
            contract="Global Contracts are a required E2E source of truth",
            observed=(
                "No Markdown Global Contract was found under .agent/contracts "
                "or a *contract*.md path"
            ),
            impact="The harness cannot evidence which Global Contracts governed the run",
            required_change=(
                "Point the harness at the canonical Global Contract location "
                "or restore the documented files"
            ),
        )
    files = [root / relative for relative in REQUIRED_FILES] + contract_candidates
    return {
        "files": [str(path.relative_to(root)).replace("\\", "/") for path in files],
        "sha256": {str(path.relative_to(root)).replace("\\", "/"): _digest(path) for path in files},
    }
