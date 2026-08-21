"""Parse and schema-validate Markdown Task Specs. No LLM involved."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import jsonschema
import yaml
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from agent.errors import AgentError

SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "task-spec.schema.json"
SUPPORTED_SCHEMA_VERSION = 1

FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(.*?\r?\n)---[ \t]*(?:\r?\n(.*))?\Z",
    re.DOTALL,
)
HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$")
TASK_HEADING_RE = re.compile(r"^(?P<id>[A-Za-z0-9][A-Za-z0-9_-]*)(?::[ \t]*(?P<title>.+))?$")
DEPENDS_RE = re.compile(r"^depends_on:[ \t]*(.*)$", re.IGNORECASE)
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

REQUIRED_H1 = (
    "Objective",
    "Non-Goals",
    "Forbidden Actions",
    "Architecture Invariants",
    "Tasks",
    "Final Verification",
)
TASK_H3 = ("Requirement", "Acceptance Criteria", "Validation")
BODY_FIELD_KEYS = {
    "objective",
    "non_goals",
    "forbidden_actions",
    "architecture_invariants",
    "tasks",
    "final_verification",
}

_TASK_SPEC_SCHEMA: dict[str, Any] | None = None


@dataclass(frozen=True)
class SpecTask:
    id: str
    title: str
    depends_on: tuple[str, ...]
    requirement: str
    acceptance_criteria: str
    validation: str


@dataclass(frozen=True)
class TaskSpec:
    schema_version: int
    id: str
    title: str
    status: str
    base_branch: str
    target_branch: str
    allowed_paths: tuple[str, ...]
    forbidden_paths: tuple[str, ...]
    repair_attempt_limit: int
    review_attempt_limit: int
    objective: str
    non_goals: str
    forbidden_actions: str
    architecture_invariants: str
    tasks: tuple[SpecTask, ...]
    final_verification: str
    source_path: str | None = None
    spec_sha256: str = ""


@dataclass(frozen=True)
class WorkUnitIdentity:
    spec_id: str
    spec_path: str
    spec_sha256: str
    base_branch: str
    target_branch: str


@dataclass
class _HeadingBlock:
    level: int
    title: str
    body: str


def load_task_spec_schema() -> dict[str, Any]:
    global _TASK_SPEC_SCHEMA
    if _TASK_SPEC_SCHEMA is None:
        _TASK_SPEC_SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return _TASK_SPEC_SCHEMA


def parse_spec(path: Path | str) -> TaskSpec:
    spec_path = Path(path)
    try:
        text = spec_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise AgentError.environment_failure(f"spec file not found: {spec_path}") from exc
    except OSError as exc:
        raise AgentError.environment_failure(f"spec file could not be read: {spec_path}") from exc
    return parse_spec_text(text, source_path=str(spec_path))


def parse_spec_text(text: str, *, source_path: str | None = None) -> TaskSpec:
    digest = spec_source_sha256(text)
    instance = parse_spec_dict(text)
    validate_spec_dict(instance)
    return spec_from_dict(instance, source_path=source_path, spec_sha256=digest)


def spec_source_sha256(text: str) -> str:
    """SHA-256 of original Task Spec source text after newline normalization.

    This is the sole Production digest for Work Unit identity. Callers must not
    reimplement CRLF/CR → LF → UTF-8 → SHA-256 independently.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def canonicalize_spec_path(
    path: str,
    *,
    repo_root: Path | str,
    spec_directory: str,
) -> str:
    """Return a canonical repository-relative POSIX Task Spec path.

    Absolute and Windows paths under ``repo_root`` are converted. ``.`` / ``..``
    are collapsed, repository escape is rejected, and the result must stay under
    the Task Spec directory.
    """
    raw = path.strip()
    if not raw:
        raise AgentError.escalation_required(
            "spec_path is empty",
            code="SPEC_PATH_INVALID",
        )
    relative = _repo_relative_spec_path(raw, repo_root=repo_root)
    canonical = _collapse_path_segments(relative)
    if not canonical:
        raise AgentError.escalation_required(
            "canonical spec_path is empty",
            code="SPEC_PATH_INVALID",
        )
    _assert_under_spec_directory(canonical, spec_directory)
    return canonical


def is_canonical_spec_path(path: str, *, spec_directory: str) -> bool:
    if not path or path != path.strip() or "\\" in path:
        return False
    if path.startswith("/") or _is_windows_drive_path(path) or _is_unc_path(path):
        return False
    parts = path.split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return False
    try:
        _assert_under_spec_directory(path, spec_directory)
    except AgentError:
        return False
    return True


def bind_spec_identity(
    spec: TaskSpec,
    *,
    repo_root: Path | str,
    spec_directory: str,
) -> TaskSpec:
    """Fix canonical spec_path on a parsed spec. spec_sha256 comes from source text."""
    if not spec.spec_sha256:
        raise AgentError.escalation_required(
            "spec_sha256 is missing",
            code="SPEC_IDENTITY_MISMATCH",
        )
    if not spec.source_path:
        raise AgentError.escalation_required(
            "spec_path is missing",
            code="SPEC_IDENTITY_MISMATCH",
        )
    canonical = canonicalize_spec_path(
        spec.source_path,
        repo_root=repo_root,
        spec_directory=spec_directory,
    )
    if spec.source_path == canonical:
        return spec
    return replace(spec, source_path=canonical)


def work_unit_identity(spec: TaskSpec) -> WorkUnitIdentity:
    if not spec.source_path or not spec.spec_sha256:
        raise AgentError.escalation_required(
            "Task Spec identity is not bound",
            code="SPEC_IDENTITY_MISMATCH",
        )
    return WorkUnitIdentity(
        spec_id=spec.id,
        spec_path=spec.source_path,
        spec_sha256=spec.spec_sha256,
        base_branch=spec.base_branch,
        target_branch=spec.target_branch,
    )


def _repo_relative_spec_path(path: str, *, repo_root: Path | str) -> str:
    posix = path.replace("\\", "/")
    root = Path(repo_root)
    candidate = Path(path)
    absolute = (
        candidate.is_absolute() or _is_windows_drive_path(path) or _is_windows_drive_path(posix)
    )
    if absolute:
        if candidate.exists() and root.exists():
            try:
                return candidate.resolve().relative_to(root.resolve()).as_posix()
            except ValueError as exc:
                raise AgentError.escalation_required(
                    f"spec path escapes repository: {path}",
                    code="SPEC_PATH_ESCAPE",
                ) from exc
        stripped = _strip_repo_prefix(posix, _posix_root(root))
        if stripped is None:
            raise AgentError.escalation_required(
                f"spec path escapes repository: {path}",
                code="SPEC_PATH_ESCAPE",
            )
        return stripped
    return posix


def _posix_root(repo_root: Path) -> str:
    try:
        if repo_root.exists():
            return repo_root.resolve().as_posix().rstrip("/")
    except OSError:
        pass
    return str(repo_root).replace("\\", "/").rstrip("/")


def _strip_repo_prefix(posix_path: str, root_posix: str) -> str | None:
    path = posix_path.rstrip("/")
    root = root_posix.rstrip("/")
    if not root:
        return None
    if _is_windows_drive_path(path) or _is_windows_drive_path(root):
        path_cmp, root_cmp = path.lower(), root.lower()
    else:
        path_cmp, root_cmp = path, root
    if path_cmp == root_cmp:
        return ""
    prefix = root_cmp + "/"
    if not path_cmp.startswith(prefix):
        return None
    return path[len(root) + 1 :]


def _collapse_path_segments(relative: str) -> str:
    parts: list[str] = []
    for part in relative.replace("\\", "/").split("/"):
        if part == "" or part == ".":
            continue
        if part == "..":
            if not parts:
                raise AgentError.escalation_required(
                    "spec path escapes repository",
                    code="SPEC_PATH_ESCAPE",
                )
            parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def _assert_under_spec_directory(canonical: str, spec_directory: str) -> None:
    directory = spec_directory.replace("\\", "/").strip("/")
    if not directory:
        raise AgentError.escalation_required(
            "task spec directory is empty",
            code="SPEC_PATH_INVALID",
        )
    prefix = directory + "/"
    if canonical == directory or not canonical.startswith(prefix):
        raise AgentError.escalation_required(
            f"spec path must be under {prefix}: {canonical}",
            code="SPEC_PATH_OUT_OF_DIR",
        )


def _is_windows_drive_path(value: str) -> bool:
    return len(value) >= 2 and value[0].isalpha() and value[1] == ":"


def _is_unc_path(value: str) -> bool:
    return value.startswith("\\\\") or value.startswith("//")


def parse_spec_dict(text: str) -> dict[str, Any]:
    if text.startswith("\ufeff"):
        text = text[1:]
    frontmatter_raw, body = _split_frontmatter(text)
    metadata = _load_frontmatter(frontmatter_raw)
    overlap = BODY_FIELD_KEYS.intersection(metadata)
    if overlap:
        raise AgentError.invalid_spec(
            f"frontmatter must not include body field: {sorted(overlap)[0]}"
        )
    body_fields = _parse_markdown_body(body)
    return {**metadata, **body_fields}


def validate_spec_dict(instance: dict[str, Any]) -> None:
    try:
        jsonschema.validate(instance=instance, schema=load_task_spec_schema())
    except JsonSchemaValidationError as exc:
        raise AgentError.invalid_spec(_format_schema_error(exc)) from exc
    _assert_task_graph(instance["tasks"])


def spec_to_dict(spec: TaskSpec) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": spec.schema_version,
        "id": spec.id,
        "title": spec.title,
        "status": spec.status,
        "base_branch": spec.base_branch,
        "target_branch": spec.target_branch,
        "allowed_paths": list(spec.allowed_paths),
        "forbidden_paths": list(spec.forbidden_paths),
        "repair_attempt_limit": spec.repair_attempt_limit,
        "review_attempt_limit": spec.review_attempt_limit,
        "objective": spec.objective,
        "non_goals": spec.non_goals,
        "forbidden_actions": spec.forbidden_actions,
        "architecture_invariants": spec.architecture_invariants,
        "tasks": [
            {
                "id": task.id,
                "title": task.title,
                "depends_on": list(task.depends_on),
                "requirement": task.requirement,
                "acceptance_criteria": task.acceptance_criteria,
                "validation": task.validation,
            }
            for task in spec.tasks
        ],
        "final_verification": spec.final_verification,
    }
    return payload


def spec_from_dict(
    instance: dict[str, Any],
    *,
    source_path: str | None = None,
    spec_sha256: str = "",
) -> TaskSpec:
    return TaskSpec(
        schema_version=int(instance["schema_version"]),
        id=instance["id"],
        title=instance["title"],
        status=instance["status"],
        base_branch=instance["base_branch"],
        target_branch=instance["target_branch"],
        allowed_paths=tuple(instance["allowed_paths"]),
        forbidden_paths=tuple(instance.get("forbidden_paths", [])),
        repair_attempt_limit=int(instance["repair_attempt_limit"]),
        review_attempt_limit=int(instance["review_attempt_limit"]),
        objective=instance["objective"],
        non_goals=instance["non_goals"],
        forbidden_actions=instance["forbidden_actions"],
        architecture_invariants=instance["architecture_invariants"],
        tasks=tuple(
            SpecTask(
                id=task["id"],
                title=task["title"],
                depends_on=tuple(task["depends_on"]),
                requirement=task["requirement"],
                acceptance_criteria=task["acceptance_criteria"],
                validation=task["validation"],
            )
            for task in instance["tasks"]
        ),
        final_verification=instance["final_verification"],
        source_path=source_path,
        spec_sha256=spec_sha256,
    )


def _split_frontmatter(text: str) -> tuple[str, str]:
    match = FRONTMATTER_RE.match(text)
    if match is None:
        raise AgentError.invalid_spec("missing frontmatter")
    return match.group(1), match.group(2) or ""


def _load_frontmatter(raw: str) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise AgentError.invalid_spec(f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(loaded, dict):
        raise AgentError.invalid_spec("frontmatter must be a YAML object")
    return loaded


def _parse_markdown_body(body: str) -> dict[str, Any]:
    blocks = _heading_blocks(body)
    sections: dict[str, Any] = {}
    index = 0
    while index < len(blocks):
        block = blocks[index]
        if block.level != 1:
            raise AgentError.invalid_spec(f"unexpected heading level {block.level}: {block.title}")
        if block.title in sections:
            raise AgentError.invalid_spec(f"duplicate section: {block.title}")
        if block.title not in REQUIRED_H1:
            raise AgentError.invalid_spec(f"unknown section: {block.title}")

        if block.title == "Tasks":
            if block.body:
                raise AgentError.invalid_spec("Tasks may not contain text before the first task")
            tasks, index = _parse_tasks(blocks, index + 1)
            sections["Tasks"] = tasks
            continue

        if index + 1 < len(blocks) and blocks[index + 1].level > 1:
            raise AgentError.invalid_spec(f"subheadings are not allowed under {block.title}")
        if not block.body:
            raise AgentError.invalid_spec(f"{block.title} is empty")
        sections[block.title] = block.body
        index += 1

    missing = [name for name in REQUIRED_H1 if name not in sections]
    if missing:
        raise AgentError.invalid_spec(f"missing section: {missing[0]}")

    return {
        "objective": sections["Objective"],
        "non_goals": sections["Non-Goals"],
        "forbidden_actions": sections["Forbidden Actions"],
        "architecture_invariants": sections["Architecture Invariants"],
        "tasks": sections["Tasks"],
        "final_verification": sections["Final Verification"],
    }


def _parse_tasks(blocks: list[_HeadingBlock], index: int) -> tuple[list[dict[str, Any]], int]:
    tasks: list[dict[str, Any]] = []
    while index < len(blocks) and blocks[index].level > 1:
        heading = blocks[index]
        if heading.level != 2:
            raise AgentError.invalid_spec(f"expected ## task heading, found {heading.title}")
        index += 1
        subsections: dict[str, _HeadingBlock] = {}
        while index < len(blocks) and blocks[index].level >= 3:
            sub = blocks[index]
            if sub.level != 3:
                raise AgentError.invalid_spec("headings deeper than ### are not allowed")
            if sub.title in subsections:
                raise AgentError.invalid_spec(f"duplicate subsection: {sub.title}")
            subsections[sub.title] = sub
            index += 1
        tasks.append(_parse_task(heading, subsections))
    if not tasks:
        raise AgentError.invalid_spec("Tasks must contain at least one task")
    return tasks, index


def _parse_task(heading: _HeadingBlock, subsections: dict[str, _HeadingBlock]) -> dict[str, Any]:
    match = TASK_HEADING_RE.match(heading.title)
    if match is None:
        raise AgentError.invalid_spec(f"invalid task heading: {heading.title}")
    task_id = match.group("id")
    title = (match.group("title") or task_id).strip()
    depends_on, leftover = _parse_depends(heading.body)
    if leftover:
        raise AgentError.invalid_spec(f"task {task_id}: unexpected text before Requirement")

    missing = [name for name in TASK_H3 if name not in subsections]
    if missing:
        raise AgentError.invalid_spec(f"task {task_id}: missing {missing[0]}")
    extra = [name for name in subsections if name not in TASK_H3]
    if extra:
        raise AgentError.invalid_spec(f"task {task_id}: unknown subsection {extra[0]}")
    for name in TASK_H3:
        if not subsections[name].body:
            raise AgentError.invalid_spec(f"task {task_id}: {name} is empty")

    return {
        "id": task_id,
        "title": title,
        "depends_on": depends_on,
        "requirement": subsections["Requirement"].body,
        "acceptance_criteria": subsections["Acceptance Criteria"].body,
        "validation": subsections["Validation"].body,
    }


def _parse_depends(body: str) -> tuple[list[str], str]:
    if not body:
        return [], ""
    first, _, rest = body.partition("\n")
    match = DEPENDS_RE.match(first.strip())
    if match is None:
        return [], body
    raw = match.group(1).strip()
    leftover = rest.strip()
    if not raw or raw == "[]":
        return [], leftover
    depends: list[str] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        if TASK_ID_RE.fullmatch(item) is None:
            raise AgentError.invalid_spec(f"invalid depends_on id: {item}")
        depends.append(item)
    return depends, leftover


def _heading_blocks(text: str) -> list[_HeadingBlock]:
    lines = text.splitlines()
    blocks: list[_HeadingBlock] = []
    current_level: int | None = None
    current_title = ""
    body_lines: list[str] = []
    preamble: list[str] = []

    def flush() -> None:
        if current_level is None:
            return
        blocks.append(
            _HeadingBlock(
                level=current_level,
                title=current_title,
                body="\n".join(body_lines).strip(),
            )
        )

    for line in lines:
        match = HEADING_RE.match(line)
        if match is None:
            if current_level is None:
                preamble.append(line)
            else:
                body_lines.append(line)
            continue
        flush()
        current_level = len(match.group(1))
        current_title = match.group(2).strip()
        body_lines = []
    flush()

    if any(part.strip() for part in preamble):
        raise AgentError.invalid_spec("content is not allowed before the first heading")
    if not blocks:
        raise AgentError.invalid_spec("markdown body has no headings")
    return blocks


def _assert_task_graph(tasks: list[dict[str, Any]]) -> None:
    ids = [task["id"] for task in tasks]
    if len(ids) != len(set(ids)):
        raise AgentError.invalid_spec("duplicate task id")
    known = set(ids)
    adjacency = {task["id"]: list(task["depends_on"]) for task in tasks}
    for task_id, deps in adjacency.items():
        for dep in deps:
            if dep not in known:
                raise AgentError.invalid_spec(f"task {task_id} depends on unknown task {dep}")

    visiting: set[str] = set()
    visited: set[str] = set()

    def dfs(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            raise AgentError.invalid_spec(f"dependency cycle involving {node}")
        visiting.add(node)
        for dep in adjacency[node]:
            dfs(dep)
        visiting.remove(node)
        visited.add(node)

    for task_id in ids:
        dfs(task_id)


def _format_schema_error(exc: JsonSchemaValidationError) -> str:
    path = ".".join(str(part) for part in exc.absolute_path)
    if not path:
        path = "(root)"
    if exc.validator == "const" and path == "schema_version":
        return f"unsupported schema_version: {exc.instance}"
    return f"{path}: {exc.message}"
