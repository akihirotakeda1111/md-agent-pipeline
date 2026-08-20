from __future__ import annotations

from agent.config import load_config
from agent.review_terminal import (
    CodeRabbitTerminalKind,
    has_coderabbit_event_identity,
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


def test_commit_status_success_is_completed() -> None:
    cfg = _cfg()
    terminal = resolve_coderabbit_terminal(
        [],
        [
            {
                "state": "success",
                "context": "CodeRabbit Review",
                "updated_at": "2026-08-20T00:00:00Z",
                "creator": {"login": cfg.actor},
            }
        ],
        head_sha="abc",
        cfg=cfg,
    )
    assert terminal.kind is CodeRabbitTerminalKind.COMPLETED
    assert terminal.source == "commit_status"


def test_in_progress_blocks_older_success() -> None:
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
