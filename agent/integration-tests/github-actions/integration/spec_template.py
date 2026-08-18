from __future__ import annotations


def plan_branches(
    case_id: str,
    *,
    base_branch_mode: str,
    default_ref: str,
    suffix: str,
) -> tuple[str, str, str, str]:
    """Return task_id, source_branch, target_branch, spec base_branch.

    Case 01 (self) uses an ephemeral source/base for intake and a different
    target so Deliver can open a PR. Skip cases keep a single ephemeral branch.
    """
    case_key = case_id.split("-", 1)[0]
    task_id = f"p5it{case_key}{suffix}"
    source_branch = f"agent/p5it-{case_key}-{suffix}"
    if base_branch_mode == "self":
        target_branch = f"{source_branch}-pr"
        base_branch = source_branch
        if target_branch == source_branch:
            raise ValueError("target_branch must differ from source/base for self mode")
    elif base_branch_mode == "default":
        target_branch = source_branch
        base_branch = default_ref
    else:
        raise ValueError(f"unknown base_branch_mode: {base_branch_mode!r}")
    return task_id, source_branch, target_branch, base_branch


def render_spec(
    template: str,
    *,
    task_id: str,
    base_branch: str,
    target_branch: str,
) -> str:
    return (
        template.replace("__TASK_ID__", task_id)
        .replace("__BASE_BRANCH__", base_branch)
        .replace("__TARGET_BRANCH__", target_branch)
    )
