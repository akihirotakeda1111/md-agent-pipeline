"""Parse and serialize durable review tracking stored on the Pull Request.

GitHub PR comments are the durable source. `.agent/state` is not used.
Identity of processed feedback includes id, timestamp, and content digest so
edited comments are reprocessed. Author, schema version, and work-unit fields
are validated before the comment is trusted.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.spec import TaskSpec, work_unit_identity

REVIEW_STATE_START = "<!-- md-agent-review-state"
REVIEW_STATE_END = "-->"
REVIEW_TRACK_SCHEMA_VERSION = 2
_KNOWN_KEYS = frozenset(
    {
        "schema_version",
        "spec_id",
        "spec_path",
        "spec_sha256",
        "base_branch",
        "target_branch",
        "head_sha",
        "review_attempts",
    }
)


@dataclass(frozen=True)
class ReviewTrack:
    spec_id: str
    spec_path: str
    spec_sha256: str
    base_branch: str
    target_branch: str
    review_attempts: int
    processed: tuple[str, ...]
    head_sha: str = ""
    schema_version: int = REVIEW_TRACK_SCHEMA_VERSION

    def processed_set(self) -> set[str]:
        return set(self.processed)

    def matches_work_unit(self, spec: TaskSpec) -> bool:
        identity = work_unit_identity(spec)
        return (
            self.spec_id == identity.spec_id
            and self.spec_path == identity.spec_path
            and self.spec_sha256 == identity.spec_sha256
            and self.base_branch == identity.base_branch
            and self.target_branch == identity.target_branch
        )


def empty_review_track(spec: TaskSpec) -> ReviewTrack:
    identity = work_unit_identity(spec)
    return ReviewTrack(
        spec_id=identity.spec_id,
        spec_path=identity.spec_path,
        spec_sha256=identity.spec_sha256,
        base_branch=identity.base_branch,
        target_branch=identity.target_branch,
        review_attempts=0,
        processed=(),
        head_sha="",
        schema_version=REVIEW_TRACK_SCHEMA_VERSION,
    )


def parse_review_track(body: str | None) -> ReviewTrack | None:
    if not body:
        return None
    start = body.find(REVIEW_STATE_START)
    if start < 0:
        return None
    end = body.find(REVIEW_STATE_END, start + len(REVIEW_STATE_START))
    if end < 0:
        return None
    block = body[start + len(REVIEW_STATE_START) : end]
    fields: dict[str, str] = {}
    processed: list[str] = []
    in_processed = False
    for raw in block.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line == "processed:":
            in_processed = True
            continue
        if line.startswith("- ") and in_processed:
            identity = line[2:].strip()
            if identity:
                processed.append(identity)
            continue
        in_processed = False
        if ":" not in line:
            return None
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key not in _KNOWN_KEYS:
            return None
        if key in fields:
            return None
        fields[key] = value
    if fields.get("schema_version") != str(REVIEW_TRACK_SCHEMA_VERSION):
        return None
    spec_id = fields.get("spec_id", "")
    spec_path = fields.get("spec_path", "")
    spec_sha256 = fields.get("spec_sha256", "")
    base_branch = fields.get("base_branch", "")
    target_branch = fields.get("target_branch", "")
    if not spec_id or not spec_path or not spec_sha256 or not base_branch or not target_branch:
        return None
    try:
        attempts = int(fields.get("review_attempts", "0"))
    except ValueError:
        return None
    if attempts < 0:
        return None
    return ReviewTrack(
        spec_id=spec_id,
        spec_path=spec_path,
        spec_sha256=spec_sha256,
        base_branch=base_branch,
        target_branch=target_branch,
        review_attempts=attempts,
        processed=tuple(processed),
        head_sha=fields.get("head_sha", ""),
        schema_version=REVIEW_TRACK_SCHEMA_VERSION,
    )


def render_review_track(track: ReviewTrack) -> str:
    lines = [
        REVIEW_STATE_START,
        f"schema_version: {track.schema_version}",
        f"spec_id: {track.spec_id}",
        f"spec_path: {track.spec_path}",
        f"spec_sha256: {track.spec_sha256}",
        f"base_branch: {track.base_branch}",
        f"target_branch: {track.target_branch}",
        f"head_sha: {track.head_sha}",
        f"review_attempts: {track.review_attempts}",
        "processed:",
    ]
    for identity in track.processed:
        lines.append(f"- {identity}")
    lines.append(REVIEW_STATE_END)
    lines.append("")
    lines.append("Orchestrator review tracking. Do not edit.")
    return "\n".join(lines)


def with_processed(
    track: ReviewTrack,
    identities: tuple[str, ...],
    *,
    increment: bool,
    head_sha: str | None = None,
) -> ReviewTrack:
    merged = list(track.processed)
    for identity in identities:
        if identity not in merged:
            merged.append(identity)
    attempts = track.review_attempts + 1 if increment else track.review_attempts
    return ReviewTrack(
        spec_id=track.spec_id,
        spec_path=track.spec_path,
        spec_sha256=track.spec_sha256,
        base_branch=track.base_branch,
        target_branch=track.target_branch,
        review_attempts=attempts,
        processed=tuple(merged),
        head_sha=track.head_sha if head_sha is None else head_sha,
        schema_version=track.schema_version,
    )
