from __future__ import annotations


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
