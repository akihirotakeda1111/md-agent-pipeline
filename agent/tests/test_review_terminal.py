from __future__ import annotations

from agent.config import load_config
from agent.review_terminal import (
    CodeRabbitTerminalKind,
    has_coderabbit_event_identity,
    is_terminal_wakeup_event,
    resolve_coderabbit_terminal,
)


def _cfg():
    return load_config().coderabbit


def test_success_check_on_current_head_is_completed() -> None:
    cfg = _cfg()
    terminal = resolve_coderabbit_terminal(
        [
            {
                "head_sha": "abc",
                "status": "completed",
                "conclusion": "success",
                "completed_at": "2026-08-20T00:00:00Z",
                "app": {"slug": cfg.check_app_slug},
            }
        ],
        [],
        head_sha="abc",
        cfg=cfg,
    )
    assert terminal.kind is CodeRabbitTerminalKind.COMPLETED
    assert terminal.source == "check_run"


def test_cancelled_check_is_skipped() -> None:
    cfg = _cfg()
    terminal = resolve_coderabbit_terminal(
        [
            {
                "head_sha": "abc",
                "status": "completed",
                "conclusion": "cancelled",
                "completed_at": "2026-08-20T00:00:00Z",
                "app": {"slug": cfg.check_app_slug},
            }
        ],
        [],
        head_sha="abc",
        cfg=cfg,
    )
    assert terminal.kind is CodeRabbitTerminalKind.SKIPPED
    assert terminal.is_escalating()


def test_failure_check_is_failed() -> None:
    cfg = _cfg()
    terminal = resolve_coderabbit_terminal(
        [
            {
                "head_sha": "abc",
                "status": "completed",
                "conclusion": "failure",
                "completed_at": "2026-08-20T00:00:00Z",
                "app": {"slug": cfg.check_app_slug},
            }
        ],
        [],
        head_sha="abc",
        cfg=cfg,
    )
    assert terminal.kind is CodeRabbitTerminalKind.FAILED
    assert terminal.escalation_code() == "CODERABBIT_REVIEW_FAILED"


def test_skipped_check_is_skipped() -> None:
    cfg = _cfg()
    terminal = resolve_coderabbit_terminal(
        [
            {
                "head_sha": "abc",
                "status": "completed",
                "conclusion": "skipped",
                "completed_at": "2026-08-20T00:00:00Z",
                "app": {"slug": cfg.check_app_slug},
            }
        ],
        [],
        head_sha="abc",
        cfg=cfg,
    )
    assert terminal.kind is CodeRabbitTerminalKind.SKIPPED
    assert terminal.escalation_code() == "CODERABBIT_SKIPPED"


def test_other_app_checks_are_ignored() -> None:
    cfg = _cfg()
    terminal = resolve_coderabbit_terminal(
        [
            {
                "head_sha": "abc",
                "status": "completed",
                "conclusion": "success",
                "app": {"slug": "github-actions"},
            }
        ],
        [],
        head_sha="abc",
        cfg=cfg,
    )
    assert terminal.kind is CodeRabbitTerminalKind.NONE


def test_old_sha_check_is_ignored() -> None:
    cfg = _cfg()
    terminal = resolve_coderabbit_terminal(
        [
            {
                "head_sha": "old",
                "status": "completed",
                "conclusion": "success",
                "app": {"slug": cfg.check_app_slug},
            }
        ],
        [],
        head_sha="new",
        cfg=cfg,
    )
    assert terminal.kind is CodeRabbitTerminalKind.NONE


def test_commit_status_review_completed_is_completed() -> None:
    cfg = _cfg()
    terminal = resolve_coderabbit_terminal(
        [],
        [
            {
                "state": "success",
                "context": "CodeRabbit",
                "description": "Review completed",
                "updated_at": "2026-08-20T00:00:00Z",
                "creator": {"login": cfg.actor},
            }
        ],
        head_sha="abc",
        cfg=cfg,
    )
    assert terminal.kind is CodeRabbitTerminalKind.COMPLETED
    assert terminal.source == "commit_status"
    assert terminal.description == "Review completed"


def test_commit_status_success_without_description_is_ambiguous() -> None:
    cfg = _cfg()
    terminal = resolve_coderabbit_terminal(
        [],
        [
            {
                "state": "success",
                "context": "CodeRabbit",
                "updated_at": "2026-08-20T00:00:00Z",
                "creator": {"login": cfg.actor},
            }
        ],
        head_sha="abc",
        cfg=cfg,
    )
    assert terminal.kind is CodeRabbitTerminalKind.AMBIGUOUS
    assert terminal.is_escalating()
    assert terminal.escalation_code() == "CODERABBIT_AMBIGUOUS"
    assert terminal.source == "commit_status"


def test_commit_status_success_with_unknown_description_is_ambiguous() -> None:
    cfg = _cfg()
    terminal = resolve_coderabbit_terminal(
        [],
        [
            {
                "state": "success",
                "context": "CodeRabbit",
                "description": "CodeRabbit finished",
                "updated_at": "2026-08-20T00:00:00Z",
                "creator": {"login": cfg.actor},
            }
        ],
        head_sha="abc",
        cfg=cfg,
    )
    assert terminal.kind is CodeRabbitTerminalKind.AMBIGUOUS
    assert terminal.description == "CodeRabbit finished"


def test_commit_status_review_skipped_is_skipped() -> None:
    cfg = _cfg()
    terminal = resolve_coderabbit_terminal(
        [],
        [
            {
                "state": "success",
                "context": "CodeRabbit",
                "description": "Review skipped",
                "updated_at": "2026-08-20T00:00:00Z",
                "creator": {"login": cfg.actor},
            }
        ],
        head_sha="abc",
        cfg=cfg,
    )
    assert terminal.kind is CodeRabbitTerminalKind.SKIPPED
    assert terminal.escalation_code() == "CODERABBIT_SKIPPED"


def test_older_pending_does_not_override_later_completed() -> None:
    cfg = _cfg()
    terminal = resolve_coderabbit_terminal(
        [
            {
                "head_sha": "abc",
                "status": "completed",
                "conclusion": "success",
                "completed_at": "2026-08-20T00:00:00Z",
                "app": {"slug": cfg.check_app_slug},
            },
            {
                "head_sha": "abc",
                "status": "in_progress",
                "conclusion": None,
                "started_at": "2026-08-19T23:00:00Z",
                "app": {"slug": cfg.check_app_slug},
            },
        ],
        [],
        head_sha="abc",
        cfg=cfg,
    )
    assert terminal.kind is CodeRabbitTerminalKind.COMPLETED


def test_later_in_progress_wins_over_older_completed() -> None:
    cfg = _cfg()
    terminal = resolve_coderabbit_terminal(
        [
            {
                "head_sha": "abc",
                "status": "completed",
                "conclusion": "success",
                "completed_at": "2026-08-20T00:00:00Z",
                "app": {"slug": cfg.check_app_slug},
            },
            {
                "head_sha": "abc",
                "status": "in_progress",
                "conclusion": None,
                "started_at": "2026-08-20T01:00:00Z",
                "app": {"slug": cfg.check_app_slug},
            },
        ],
        [],
        head_sha="abc",
        cfg=cfg,
    )
    assert terminal.kind is CodeRabbitTerminalKind.IN_PROGRESS


def test_latest_completed_item_wins() -> None:
    cfg = _cfg()
    terminal = resolve_coderabbit_terminal(
        [
            {
                "head_sha": "abc",
                "status": "completed",
                "conclusion": "skipped",
                "completed_at": "2026-08-20T00:00:00Z",
                "app": {"slug": cfg.check_app_slug},
            },
            {
                "head_sha": "abc",
                "status": "completed",
                "conclusion": "success",
                "completed_at": "2026-08-20T01:00:00Z",
                "app": {"slug": cfg.check_app_slug},
            },
        ],
        [],
        head_sha="abc",
        cfg=cfg,
    )
    assert terminal.kind is CodeRabbitTerminalKind.COMPLETED


def test_pr19_status_history_latest_completed_ignores_past_skipped() -> None:
    cfg = _cfg()
    sha = "abc"
    terminal = resolve_coderabbit_terminal(
        [],
        [
            {
                "sha": sha,
                "state": "pending",
                "context": "CodeRabbit",
                "description": "Review in progress",
                "updated_at": "2026-08-20T00:00:00Z",
                "creator": {"login": cfg.actor},
            },
            {
                "sha": sha,
                "state": "success",
                "context": "CodeRabbit",
                "description": "Review skipped",
                "updated_at": "2026-08-20T00:01:00Z",
                "creator": {"login": cfg.actor},
            },
            {
                "sha": sha,
                "state": "pending",
                "context": "CodeRabbit",
                "description": "Review in progress",
                "updated_at": "2026-08-20T00:02:00Z",
                "creator": {"login": cfg.actor},
            },
            {
                "sha": sha,
                "state": "success",
                "context": "CodeRabbit",
                "description": "Review completed",
                "updated_at": "2026-08-20T00:03:00Z",
                "creator": {"login": cfg.actor},
            },
        ],
        head_sha=sha,
        cfg=cfg,
    )
    assert terminal.kind is CodeRabbitTerminalKind.COMPLETED
    assert terminal.description == "Review completed"


def test_pr19_github_newest_first_order_is_still_completed() -> None:
    cfg = _cfg()
    sha = "abc"
    terminal = resolve_coderabbit_terminal(
        [],
        [
            {
                "id": 4,
                "sha": sha,
                "state": "success",
                "context": "CodeRabbit",
                "description": "Review completed",
                "updated_at": "2026-08-20T00:03:00Z",
                "creator": {"login": cfg.actor},
            },
            {
                "id": 3,
                "sha": sha,
                "state": "pending",
                "context": "CodeRabbit",
                "description": "Review in progress",
                "updated_at": "2026-08-20T00:02:00Z",
                "creator": {"login": cfg.actor},
            },
            {
                "id": 2,
                "sha": sha,
                "state": "success",
                "context": "CodeRabbit",
                "description": "Review skipped",
                "updated_at": "2026-08-20T00:01:00Z",
                "creator": {"login": cfg.actor},
            },
            {
                "id": 1,
                "sha": sha,
                "state": "pending",
                "context": "CodeRabbit",
                "description": "Review in progress",
                "updated_at": "2026-08-20T00:00:00Z",
                "creator": {"login": cfg.actor},
            },
        ],
        head_sha=sha,
        cfg=cfg,
    )
    assert terminal.kind is CodeRabbitTerminalKind.COMPLETED
    assert terminal.description == "Review completed"


def test_latest_review_skipped_is_skipped_not_completed() -> None:
    cfg = _cfg()
    terminal = resolve_coderabbit_terminal(
        [],
        [
            {
                "state": "success",
                "context": "CodeRabbit",
                "description": "Review completed",
                "updated_at": "2026-08-20T00:01:00Z",
                "creator": {"login": cfg.actor},
            },
            {
                "state": "success",
                "context": "CodeRabbit",
                "description": "Review skipped",
                "updated_at": "2026-08-20T00:02:00Z",
                "creator": {"login": cfg.actor},
            },
        ],
        head_sha="abc",
        cfg=cfg,
    )
    assert terminal.kind is CodeRabbitTerminalKind.SKIPPED


def test_review_in_progress_description_is_in_progress() -> None:
    cfg = _cfg()
    terminal = resolve_coderabbit_terminal(
        [],
        [
            {
                "state": "pending",
                "context": "CodeRabbit",
                "description": "Review in progress",
                "updated_at": "2026-08-20T00:00:00Z",
                "creator": {"login": cfg.actor},
            }
        ],
        head_sha="abc",
        cfg=cfg,
    )
    assert terminal.kind is CodeRabbitTerminalKind.IN_PROGRESS


def test_status_on_other_sha_is_ignored() -> None:
    cfg = _cfg()
    terminal = resolve_coderabbit_terminal(
        [],
        [
            {
                "sha": "old",
                "state": "success",
                "context": "CodeRabbit",
                "description": "Review completed",
                "updated_at": "2026-08-20T00:03:00Z",
                "creator": {"login": cfg.actor},
            }
        ],
        head_sha="new",
        cfg=cfg,
    )
    assert terminal.kind is CodeRabbitTerminalKind.NONE


def test_commit_status_failure_is_failed() -> None:
    cfg = _cfg()
    terminal = resolve_coderabbit_terminal(
        [],
        [
            {
                "state": "failure",
                "context": "CodeRabbit",
                "description": "Review failed",
                "updated_at": "2026-08-20T00:00:00Z",
                "creator": {"login": cfg.actor},
            }
        ],
        head_sha="abc",
        cfg=cfg,
    )
    assert terminal.kind is CodeRabbitTerminalKind.FAILED


def test_event_identity_accepts_check_app_without_actor() -> None:
    cfg = _cfg()
    assert has_coderabbit_event_identity(
        {
            "sender": {"login": "github-actions[bot]"},
            "check_run": {"app": {"slug": cfg.check_app_slug}},
        },
        cfg,
    )
    assert not has_coderabbit_event_identity(
        {"sender": {"login": "human"}, "check_run": {"app": {"slug": "other"}}},
        cfg,
    )


def test_terminal_wakeup_ignores_comment_payloads() -> None:
    assert not is_terminal_wakeup_event(
        {
            "sender": {"login": "coderabbitai[bot]"},
            "comment": {"body": "Full review finished."},
            "issue": {"number": 7, "pull_request": {}},
        }
    )
    assert not is_terminal_wakeup_event(
        {
            "sender": {"login": "human"},
            "comment": {"body": "@coderabbitai full review"},
            "issue": {"number": 7, "pull_request": {}},
        }
    )
    assert not is_terminal_wakeup_event(
        {"sender": {"login": "coderabbitai[bot]"}, "state": "pending", "context": "CodeRabbit"}
    )
    assert is_terminal_wakeup_event(
        {"check_run": {"status": "completed", "conclusion": "success"}}
    )
    assert is_terminal_wakeup_event({"state": "success", "context": "CodeRabbit"})
    assert is_terminal_wakeup_event({"state": "failure", "context": "CodeRabbit"})
    assert not is_terminal_wakeup_event(
        {"check_run": {"status": "in_progress", "conclusion": ""}}
    )
