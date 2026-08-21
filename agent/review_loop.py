"""Asynchronous CodeRabbit review loop. GitHub durable tracking, no `.agent/state` resume."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.classify import FailureClass, classify_validation
from agent.codex_runner import (
    Executor,
    attach_codex_api_key,
    detach_codex_api_key,
    resolve_task,
    run_codex,
)
from agent.config import AgentConfig, load_config
from agent.cycle import run_final_verification
from agent.errors import AgentError, ErrorCategory
from agent.events import (
    READY_FOR_HUMAN,
    REVIEW_CLASSIFIED,
    REVIEW_COLLECTED,
    REVIEW_ESCALATED,
    REVIEW_FILTERED,
    REVIEW_FIX_STARTED,
    REVIEW_FIX_VALIDATION_FAILED,
    REVIEW_FIX_VALIDATION_PASSED,
    REVIEW_POLICY_APPLIED,
    REVIEW_RECEIVED,
    emit,
)
from agent.github_api import GitHubClient, github_client_from_env
from agent.gitutil import (
    assert_clean_worktree,
    capture_snapshot,
    change_path_list,
    collect_changes,
    run_git,
)
from agent.gitwrite import commit_paths, head_sha, push_branch
from agent.labels import apply_status_label, current_terminal_status_label, ensure_review_labels
from agent.notify import EscalationNotice, mention_from_config
from agent.policy import classify_control_plane_error, is_failed
from agent.pr import parse_work_unit_marker
from agent.review_classify import classify_review_comment
from agent.review_collect import collect_review_feedback, head_sha_from_pull
from agent.review_filter import applies_to_current_head, prefilter_reason
from agent.review_policy import (
    AUTO_REPAIR_DEFERRED_HUMAN_ACTION,
    AUTO_REPAIR_DEFERRED_REASON,
    decide_review_policy,
)
from agent.review_prepare import find_spec_by_id
from agent.review_prompt import build_review_repair_prompt
from agent.review_terminal import (
    CodeRabbitTerminal,
    CodeRabbitTerminalKind,
    collect_coderabbit_terminal,
)
from agent.review_track import (
    REVIEW_STATE_START,
    ReviewTrack,
    empty_review_track,
    parse_review_track,
    render_review_track,
    with_processed,
)
from agent.review_types import (
    ClassificationResult,
    PolicyDecision,
    ReviewFeedback,
    ReviewPolicyAction,
)
from agent.scope import check_scope, validate_spec_scope_policy
from agent.spec import TaskSpec, parse_spec
from agent.validation import run_validation_text

ClassifierFn = Callable[[ReviewFeedback, TaskSpec], ClassificationResult]
CLASSIFIER_FAIL_CLOSED = frozenset(
    {
        "INVALID_CLASSIFIER_JSON",
        "CLASSIFIER_REFUSAL",
        "CLASSIFIER_INCOMPLETE",
    }
)
STICKY_LABEL_OUTCOMES = {
    "agent:ready": "READY_FOR_HUMAN",
    "agent:escalated": "ESCALATED",
    "agent:failed": "FAILED",
}


@dataclass
class ReviewResult:
    outcome: str
    spec_id: str | None
    pull_number: int
    message: str
    code: str | None = None
    processed: tuple[str, ...] = ()
    review_attempts: int = 0
    commit_sha: str | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "spec_id": self.spec_id,
            "pull_number": self.pull_number,
            "message": self.message,
            "code": self.code,
            "processed": list(self.processed),
            "review_attempts": self.review_attempts,
            "commit_sha": self.commit_sha,
        }


def review_attempt_limit(spec: TaskSpec, config: AgentConfig) -> int:
    return min(spec.review_attempt_limit, config.retry.review_attempt_limit)


def run_review(
    *,
    repo_root: Path | str,
    pull_number: int,
    head_sha_expected: str,
    spec_path: str | None = None,
    config: AgentConfig | None = None,
    github: GitHubClient | None = None,
    classifier: ClassifierFn | None = None,
    executor: Executor | None = None,
    env: Mapping[str, str] | None = None,
) -> ReviewResult:
    cfg = config or load_config()
    root = Path(repo_root)
    client = github or github_client_from_env()
    rest_env, codex_key = detach_codex_api_key(env, api_key_env=cfg.codex.api_key_env)
    classifier_env, classifier_key = detach_codex_api_key(
        rest_env, api_key_env=cfg.review.api_key_env
    )
    emit(
        REVIEW_RECEIVED,
        f"review event for pull #{pull_number}",
        phase="review",
        extra={"pull_number": pull_number},
    )
    try:
        return _run_review(
            root=root,
            pull_number=pull_number,
            head_sha_expected=head_sha_expected,
            spec_path=spec_path,
            cfg=cfg,
            client=client,
            classifier=classifier,
            classifier_key=classifier_key,
            executor=executor,
            env=classifier_env,
            codex_key=codex_key,
        )
    except Exception as exc:
        return _control_plane_failure(client, pull_number, spec_path, root, cfg, exc)


def _run_review(
    *,
    root: Path,
    pull_number: int,
    head_sha_expected: str,
    spec_path: str | None,
    cfg: AgentConfig,
    client: GitHubClient,
    classifier: ClassifierFn | None,
    classifier_key: str | None,
    executor: Executor | None,
    env: Mapping[str, str] | None,
    codex_key: str | None,
) -> ReviewResult:
    actual_head = head_sha(root)
    pull = client.get_pull(pull_number)
    api_head = head_sha_from_pull(pull)
    if not api_head:
        raise AgentError.environment_failure(
            "pull request head sha is missing",
            code="GITHUB_API_FAILURE",
        )
    if api_head != head_sha_expected:
        raise AgentError.escalation_required(
            f"API pull.head.sha {api_head} does not match expected workspace {head_sha_expected}",
            code="PULL_HEAD_MISMATCH",
        )
    if actual_head != head_sha_expected:
        raise AgentError.escalation_required(
            f"HEAD {actual_head} does not match pull head {head_sha_expected}",
            code="BASE_SHA_MISMATCH",
        )
    spec = parse_spec(root / spec_path) if spec_path else None
    if spec is None:
        marker = parse_work_unit_marker(str(pull.get("body") or ""))
        if marker is None:
            raise AgentError.escalation_required(
                "pull request is not an orchestrator work unit",
                code="WORK_UNIT_PR_MISMATCH",
            )
        spec = parse_spec(find_spec_by_id(root, marker["spec_id"], config=cfg))
    validate_spec_scope_policy(spec, cfg.runtime_edit_policy)
    ensure_review_labels(client)
    track_id, track = load_review_track(
        client, pull_number, spec, track_author=cfg.review.track_author
    )
    terminal = collect_coderabbit_terminal(client, head_sha_expected, cfg.coderabbit)
    sticky = _sticky_terminal_result(client, spec, pull_number, track, head_sha_expected, terminal)
    if sticky is not None:
        return sticky
    if terminal.is_escalating():
        return _escalate(
            client,
            spec,
            pull_number,
            track,
            f"CodeRabbit terminal is {terminal.kind.value}",
            terminal.escalation_code(),
            track_id=track_id,
            head_sha=head_sha_expected,
        )
    if not terminal.is_completed():
        apply_status_label(client, pull_number, "agent:review")
        return _in_review(spec, pull_number, track, _waiting_for_coderabbit_message(terminal))
    apply_status_label(client, pull_number, "agent:review")
    items = collect_review_feedback(client, pull_number, actor=cfg.coderabbit.actor)
    emit(
        REVIEW_COLLECTED,
        f"collected {len(items)} CodeRabbit comments",
        task_id=spec.id,
        phase="review",
        extra={"count": len(items), "terminal": terminal.to_json_dict()},
    )
    processed = track.processed_set()
    skipped: list[str] = []
    forbidden: list[ReviewFeedback] = []
    candidates: list[ReviewFeedback] = []
    for item in items:
        reason = prefilter_reason(
            item,
            spec=spec,
            runtime_policy=cfg.runtime_edit_policy,
            actor=cfg.coderabbit.actor,
            head_sha=head_sha_expected,
            processed=processed,
            repo_root=root,
            track_head_sha=track.head_sha,
        )
        emit(
            REVIEW_FILTERED,
            f"{item.identity}: {reason or 'accepted'}",
            task_id=spec.id,
            phase="review",
            extra={"identity": item.identity, "reason": reason},
        )
        if reason in {"processed", "non-configured-actor"}:
            continue
        if reason in {"outdated-head", "missing-path"}:
            skipped.append(item.identity)
            continue
        if reason == "forbidden-path":
            forbidden.append(item)
            continue
        if reason is not None:
            skipped.append(item.identity)
            continue
        candidates.append(item)

    if forbidden:
        updated = with_processed(
            track, tuple(skipped + [item.identity for item in forbidden]), increment=False
        )
        return _escalate(
            client,
            spec,
            pull_number,
            updated,
            "review references a forbidden path",
            "REVIEW_POLICY_ESCALATED",
            track_id=track_id,
            head_sha=head_sha_expected,
        )

    cap = cfg.review.max_comments_per_run
    batch = candidates[:cap] if cap is not None and cap >= 0 else candidates
    if not batch:
        updated = with_processed(track, tuple(skipped), increment=False)
        track_id = persist_review_track(client, pull_number, track_id, updated)
        return _convergence_result(
            client,
            spec,
            pull_number,
            items,
            updated,
            head_sha_expected,
            track_id,
            terminal,
            "no unprocessed CodeRabbit feedback on the current HEAD",
        )

    classified: list[tuple[ReviewFeedback, ClassificationResult, PolicyDecision]] = []
    for item in batch:
        try:
            result = _classify(
                item,
                spec,
                cfg=cfg,
                classifier=classifier,
                api_key=classifier_key,
            )
        except AgentError as exc:
            if exc.code not in CLASSIFIER_FAIL_CLOSED:
                raise
            updated = with_processed(track, tuple([*skipped, item.identity]), increment=False)
            return _escalate(
                client,
                spec,
                pull_number,
                updated,
                str(exc),
                exc.code or "INVALID_CLASSIFIER_JSON",
                track_id=track_id,
                head_sha=head_sha_expected,
            )
        emit(
            REVIEW_CLASSIFIED,
            f"{item.identity}: {result.classification.value}",
            task_id=spec.id,
            phase="review",
            extra=result.to_json_dict(),
        )
        decision = decide_review_policy(
            result,
            spec,
            runtime_policy=cfg.runtime_edit_policy,
            confidence_threshold=cfg.review.confidence_threshold,
            auto_repair_enabled=cfg.review.auto_repair_enabled,
        )
        emit(
            REVIEW_POLICY_APPLIED,
            f"{item.identity}: {decision.action.value}",
            task_id=spec.id,
            phase="review",
            extra={"action": decision.action.value, "reason": decision.reason},
        )
        classified.append((item, result, decision))

    identities = tuple(skipped + [item.identity for item, _, _ in classified])
    if any(decision.action is ReviewPolicyAction.ESCALATE for _, _, decision in classified):
        reasons = [
            decision.reason
            for _, _, decision in classified
            if decision.action is ReviewPolicyAction.ESCALATE
        ]
        updated = with_processed(track, identities, increment=False)
        deferred = AUTO_REPAIR_DEFERRED_REASON in reasons
        return _escalate(
            client,
            spec,
            pull_number,
            updated,
            reasons[0],
            "REVIEW_POLICY_ESCALATED",
            track_id=track_id,
            head_sha=head_sha_expected,
            required_human_action=AUTO_REPAIR_DEFERRED_HUMAN_ACTION if deferred else None,
        )

    accepted = [
        (item, result)
        for item, result, decision in classified
        if decision.action is ReviewPolicyAction.FIX
    ]
    if not accepted:
        updated = with_processed(track, identities, increment=False)
        track_id = persist_review_track(client, pull_number, track_id, updated)
        leftover = candidates[len(batch) :]
        if leftover:
            return _in_review(
                spec,
                pull_number,
                updated,
                "non-actionable reviews processed; more comments remain",
            )
        return _convergence_result(
            client,
            spec,
            pull_number,
            items,
            updated,
            head_sha_expected,
            track_id,
            terminal,
            "no actionable CodeRabbit feedback on the current HEAD",
        )

    limit = review_attempt_limit(spec, cfg)
    if track.review_attempts >= limit:
        updated = with_processed(track, identities, increment=False)
        return _escalate(
            client,
            spec,
            pull_number,
            updated,
            "review_attempt_limit reached",
            "REVIEW_ATTEMPT_LIMIT",
            track_id=track_id,
            head_sha=head_sha_expected,
        )

    emit(REVIEW_FIX_STARTED, "codex review repair started", task_id=spec.id, phase="review")
    updated = with_processed(track, identities, increment=True, head_sha=head_sha_expected)
    persist_review_track(client, pull_number, track_id, updated)
    commit_sha = _apply_review_fix(
        spec,
        root,
        cfg,
        accepted,
        executor=executor,
        env=env,
        codex_key=codex_key,
        attempt=updated.review_attempts,
    )
    apply_status_label(client, pull_number, "agent:review")
    return ReviewResult(
        outcome="REVIEW_FIX_PUSHED",
        spec_id=spec.id,
        pull_number=pull_number,
        message="review repair committed and pushed",
        processed=updated.processed,
        review_attempts=updated.review_attempts,
        commit_sha=commit_sha,
    )


def _classify(
    item: ReviewFeedback,
    spec: TaskSpec,
    *,
    cfg: AgentConfig,
    classifier: ClassifierFn | None,
    api_key: str | None,
) -> ClassificationResult:
    if classifier is not None:
        return classifier(item, spec)
    if (cfg.review.provider or "").strip() != "openai":
        raise AgentError.environment_failure(
            f"unsupported review classifier provider: {cfg.review.provider!r}",
            code="CLASSIFIER_API_FAILURE",
        )
    return classify_review_comment(item, spec, config=cfg, api_key=api_key)


def _apply_review_fix(
    spec: TaskSpec,
    root: Path,
    cfg: AgentConfig,
    accepted: tuple[tuple[ReviewFeedback, ClassificationResult], ...]
    | list[tuple[ReviewFeedback, ClassificationResult]],
    *,
    executor: Executor | None,
    env: Mapping[str, str] | None,
    codex_key: str | None,
    attempt: int,
) -> str:
    snapshot = capture_snapshot(root)
    if cfg.validation.require_clean_worktree:
        assert_clean_worktree(snapshot)
    current_task = spec.tasks[-1] if spec.tasks else None
    prompt = build_review_repair_prompt(
        spec,
        repo_root=root,
        base_sha=_merge_base_sha(root, spec),
        accepted=tuple(accepted),
        current_task=current_task,
        runtime_policy=cfg.runtime_edit_policy,
    )
    task = current_task or resolve_task(spec, spec.tasks[0].id)
    run_codex(
        spec,
        task,
        repo_root=root,
        config=cfg,
        env=attach_codex_api_key(env, codex_key, api_key_env=cfg.codex.api_key_env),
        executor=executor,
        prompt=prompt,
        stage="repair",
        attempt=attempt,
    )
    changes = collect_changes(root, snapshot.base_sha)
    scope = check_scope(spec, changes, cfg.runtime_edit_policy)
    if not scope.allowed:
        emit(
            REVIEW_FIX_VALIDATION_FAILED,
            f"SCOPE_VIOLATION: {', '.join(scope.violation_paths)}",
            task_id=spec.id,
            phase="review",
        )
        raise AgentError.policy_violation(
            f"review repair scope violation: {', '.join(scope.violation_paths)}",
            code="REVIEW_SCOPE_VIOLATION",
        )
    paths = list(change_path_list(changes))
    if not paths:
        emit(
            REVIEW_FIX_VALIDATION_FAILED,
            "review repair produced no file changes",
            task_id=spec.id,
            phase="review",
        )
        raise AgentError.escalation_required(
            "review repair produced no file changes",
            code="REVIEW_FIX_VALIDATION_FAILED",
        )
    for item in spec.tasks:
        records = run_validation_text(
            item.validation,
            repo_root=root,
            task_id=item.id,
            timeout_seconds=cfg.validation.timeout_seconds,
            env=env,
        )
        failed = next((record for record in records if not record.passed), None)
        if failed is not None:
            emit(
                REVIEW_FIX_VALIDATION_FAILED,
                f"validation failed: {failed.command}",
                task_id=spec.id,
                phase="review",
            )
            classification = classify_validation(failed) or FailureClass.ESCALATION_REQUIRED
            if classification is FailureClass.ENVIRONMENT_FAILURE:
                raise AgentError.environment_failure(
                    "review repair validation failed",
                    code="REVIEW_FIX_VALIDATION_FAILED",
                )
            raise AgentError.escalation_required(
                "review repair validation failed",
                code="REVIEW_FIX_VALIDATION_FAILED",
            )
    records = run_final_verification(spec, repo_root=root, config=cfg, env=env)
    failed = next((record for record in records if not record.passed), None)
    if failed is not None:
        emit(
            REVIEW_FIX_VALIDATION_FAILED,
            "final verification failed after review repair",
            task_id=spec.id,
            phase="review",
        )
        classification = classify_validation(failed) or FailureClass.ESCALATION_REQUIRED
        if classification is FailureClass.ENVIRONMENT_FAILURE:
            raise AgentError.environment_failure(
                "review repair final verification failed",
                code="REVIEW_FIX_VALIDATION_FAILED",
            )
        raise AgentError.escalation_required(
            "review repair final verification failed",
            code="REVIEW_FIX_VALIDATION_FAILED",
        )
    emit(
        REVIEW_FIX_VALIDATION_PASSED,
        "review repair validation passed",
        task_id=spec.id,
        phase="review",
    )
    commit_sha = commit_paths(
        root,
        paths,
        f"fix({spec.id}): apply accepted review feedback",
    )
    if commit_sha is None:
        raise AgentError.environment_failure("git commit produced no sha", code="GIT_FAILED")
    push_branch(root, spec.target_branch)
    return commit_sha


def _merge_base_sha(repo_root: Path, spec: TaskSpec) -> str:
    for ref in (f"origin/{spec.base_branch}", spec.base_branch):
        completed = run_git(repo_root, "merge-base", ref, "HEAD")
        if completed.returncode == 0 and completed.stdout.strip():
            return completed.stdout.strip()
    raise AgentError.escalation_required(
        f"could not resolve merge-base with {spec.base_branch}",
        code="BASE_SHA_MISSING",
    )


def load_review_track(
    client: GitHubClient,
    pull_number: int,
    spec: TaskSpec,
    *,
    track_author: str,
) -> tuple[int | None, ReviewTrack]:
    found: list[tuple[int, ReviewTrack]] = []
    for comment in client.list_issue_comments(pull_number):
        body = str(comment.get("body") or "")
        if REVIEW_STATE_START not in body:
            continue
        author = _comment_login(comment)
        if author != track_author:
            raise AgentError.escalation_required(
                "review tracking comment author is not the orchestrator",
                code="UNSAFE_REVIEW_TRACK",
            )
        parsed = parse_review_track(body)
        if parsed is None or parsed.schema_version != 1:
            raise AgentError.escalation_required(
                "review tracking comment schema is invalid",
                code="UNSAFE_REVIEW_TRACK",
            )
        if not parsed.matches_work_unit(spec):
            raise AgentError.escalation_required(
                "review tracking comment does not match the work unit",
                code="UNSAFE_REVIEW_TRACK",
            )
        comment_id = comment.get("id")
        if not isinstance(comment_id, int) or isinstance(comment_id, bool):
            raise AgentError.environment_failure(
                "review tracking comment is missing an id",
                code="GITHUB_API_FAILURE",
            )
        found.append((comment_id, parsed))
    if len(found) > 1:
        raise AgentError.escalation_required(
            "multiple review tracking comments exist on the pull request",
            code="UNSAFE_REVIEW_TRACK",
        )
    if not found:
        return None, empty_review_track(spec)
    return found[0]


def persist_review_track(
    client: GitHubClient, pull_number: int, comment_id: int | None, track: ReviewTrack
) -> int:
    """Create the tracking comment once; later calls must pass the returned id."""
    body = render_review_track(track)
    if comment_id is None:
        created = client.create_issue_comment(pull_number, body)
        created_id = created.get("id")
        if not isinstance(created_id, int) or isinstance(created_id, bool):
            raise AgentError.environment_failure(
                "GitHub did not return a tracking comment id",
                code="GITHUB_API_FAILURE",
            )
        return created_id
    client.update_issue_comment(comment_id, body)
    return comment_id


def _has_escalation_comment(client: GitHubClient, pull_number: int, reason: str) -> bool:
    needle = f"- Reason: {reason}"
    for comment in client.list_issue_comments(pull_number):
        if needle in str(comment.get("body") or ""):
            return True
    return False


def _comment_login(comment: dict[str, Any]) -> str:
    user = comment.get("user")
    if isinstance(user, dict):
        login = user.get("login")
        if isinstance(login, str):
            return login
    return ""


def _convergence_result(
    client: GitHubClient,
    spec: TaskSpec,
    pull_number: int,
    items: tuple[ReviewFeedback, ...],
    track: ReviewTrack,
    head_sha_expected: str,
    track_id: int | None,
    terminal: CodeRabbitTerminal,
    message: str,
) -> ReviewResult:
    current = [
        item for item in items if applies_to_current_head(item, head_sha_expected, track.head_sha)
    ]
    processed = track.processed_set()
    unprocessed = [item for item in current if item.identity not in processed]
    if unprocessed:
        return _in_review(
            spec,
            pull_number,
            track,
            "unprocessed review comments remain on the current HEAD",
        )
    if not terminal.is_completed():
        waiting = _waiting_for_coderabbit_message(terminal)
        return _in_review(spec, pull_number, track, waiting)
    bound = with_processed(track, (), increment=False, head_sha=head_sha_expected)
    persist_review_track(client, pull_number, track_id, bound)
    return _ready(client, spec, pull_number, bound, message)


def _waiting_for_coderabbit_message(terminal: CodeRabbitTerminal) -> str:
    if terminal.kind is CodeRabbitTerminalKind.IN_PROGRESS:
        return "CodeRabbit review is still in progress on the current HEAD"
    return "no CodeRabbit terminal evidence on the current HEAD yet"


def _sticky_terminal_result(
    client: GitHubClient,
    spec: TaskSpec,
    pull_number: int,
    track: ReviewTrack,
    head_sha_expected: str,
    terminal: CodeRabbitTerminal,
) -> ReviewResult | None:
    if track.head_sha != head_sha_expected:
        return None
    label = current_terminal_status_label(client, pull_number)
    if label is None:
        return None
    outcome = STICKY_LABEL_OUTCOMES[label]
    code = None
    if outcome == "ESCALATED" and terminal.is_escalating():
        code = terminal.escalation_code()
    return ReviewResult(
        outcome=outcome,
        spec_id=spec.id,
        pull_number=pull_number,
        message=f"keeping {outcome} for unchanged HEAD",
        code=code,
        processed=track.processed,
        review_attempts=track.review_attempts,
    )


def _in_review(
    spec: TaskSpec,
    pull_number: int,
    track: ReviewTrack,
    message: str,
) -> ReviewResult:
    return ReviewResult(
        outcome="IN_REVIEW",
        spec_id=spec.id,
        pull_number=pull_number,
        message=message,
        processed=track.processed,
        review_attempts=track.review_attempts,
    )


def _ready(
    client: GitHubClient,
    spec: TaskSpec,
    pull_number: int,
    track: ReviewTrack,
    message: str,
) -> ReviewResult:
    apply_status_label(client, pull_number, "agent:ready")
    emit(READY_FOR_HUMAN, message, task_id=spec.id, phase="review", state="READY_FOR_HUMAN")
    return ReviewResult(
        outcome="READY_FOR_HUMAN",
        spec_id=spec.id,
        pull_number=pull_number,
        message=message,
        processed=track.processed,
        review_attempts=track.review_attempts,
    )


def _escalate(
    client: GitHubClient,
    spec: TaskSpec,
    pull_number: int,
    track: ReviewTrack,
    message: str,
    code: str,
    *,
    track_id: int | None,
    head_sha: str,
    required_human_action: str | None = None,
) -> ReviewResult:
    bound = with_processed(track, (), increment=False, head_sha=head_sha)
    persist_review_track(client, pull_number, track_id, bound)
    apply_status_label(client, pull_number, "agent:escalated")
    notice = EscalationNotice(
        task_id=spec.id,
        current_task=spec.tasks[-1].id if spec.tasks else None,
        reason=message,
        last_validation=None,
        repair_attempts=track.review_attempts,
        required_human_action=required_human_action
        or (
            "Inspect the review classification and decide whether to change "
            "the Task Spec or the implementation."
        ),
        mention=mention_from_config(),
    )
    body = notice.to_markdown()
    if not _has_escalation_comment(client, pull_number, notice.reason):
        client.create_issue_comment(pull_number, body)
    emit(REVIEW_ESCALATED, message, task_id=spec.id, phase="review", extra={"code": code})
    return ReviewResult(
        outcome="ESCALATED",
        spec_id=spec.id,
        pull_number=pull_number,
        message=message,
        code=code,
        processed=bound.processed,
        review_attempts=bound.review_attempts,
    )


def _control_plane_failure(
    client: GitHubClient,
    pull_number: int,
    spec_path: str | None,
    repo_root: Path,
    cfg: AgentConfig,
    error: BaseException,
) -> ReviewResult:
    spec_id = None
    if spec_path:
        try:
            spec_id = parse_spec(repo_root / spec_path).id
        except AgentError:
            spec_id = None
    classification = classify_control_plane_error(error)
    code = error.code if isinstance(error, AgentError) else "INTERNAL_FAILURE"
    message = str(error)
    if is_failed(classification):
        apply_status_label(client, pull_number, "agent:failed")
        outcome = "FAILED"
    else:
        apply_status_label(client, pull_number, "agent:escalated")
        outcome = "ESCALATED"
        emit(REVIEW_ESCALATED, message, task_id=spec_id, phase="review", extra={"code": code})
    notice = EscalationNotice(
        task_id=spec_id or f"pull-{pull_number}",
        current_task=None,
        reason=message,
        last_validation=None,
        repair_attempts=0,
        required_human_action=(
            "Inspect the failed review workflow and re-run after fixing the "
            "environment or policy issue."
        ),
        mention=mention_from_config(cfg),
    )
    try:
        client.create_issue_comment(pull_number, notice.to_markdown())
    except AgentError:
        pass
    if isinstance(error, AgentError) and error.category is ErrorCategory.ENVIRONMENT_FAILURE:
        return ReviewResult(
            outcome=outcome,
            spec_id=spec_id,
            pull_number=pull_number,
            message=message,
            code=code,
        )
    if isinstance(error, AgentError):
        return ReviewResult(
            outcome=outcome,
            spec_id=spec_id,
            pull_number=pull_number,
            message=message,
            code=code,
        )
    return ReviewResult(
        outcome="ESCALATED",
        spec_id=spec_id,
        pull_number=pull_number,
        message=message,
        code=code,
    )
