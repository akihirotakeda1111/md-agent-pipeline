"""OpenAI Structured Output classifier for review comments.

Verified against:
- https://developers.openai.com/api/docs/guides/structured-outputs
- Chat Completions `response_format.json_schema` with `strict: true`
- Snapshot pin: gpt-5.4-nano-2026-03-17 (documented structured-output snapshot)

Classifier credentials are never passed to Codex. Codex credentials are never
used as a fallback for this client.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import jsonschema
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from agent.config import AgentConfig, load_config
from agent.errors import AgentError
from agent.review_context import format_review_task_context
from agent.review_types import ClassificationResult, ReviewClassification, ReviewFeedback
from agent.spec import TaskSpec

SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "review-classification.schema.json"
CLASSIFY_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "review-classify.md"
OPENAI_CHAT_COMPLETIONS = "https://api.openai.com/v1/chat/completions"

Classifier = Callable[[ReviewFeedback, TaskSpec], ClassificationResult]

_SCHEMA: dict[str, Any] | None = None


def load_classification_schema() -> dict[str, Any]:
    global _SCHEMA
    if _SCHEMA is None:
        _SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return _SCHEMA


def parse_classification_payload(payload: dict[str, Any]) -> ClassificationResult:
    try:
        jsonschema.validate(instance=payload, schema=load_classification_schema())
    except JsonSchemaValidationError as exc:
        raise AgentError.invalid_input(
            f"invalid classifier JSON: {exc.message}",
            code="INVALID_CLASSIFIER_JSON",
        ) from exc
    try:
        label = ReviewClassification(str(payload["classification"]))
    except ValueError as exc:
        raise AgentError.invalid_input(
            f"unknown classification: {payload.get('classification')!r}",
            code="INVALID_CLASSIFIER_JSON",
        ) from exc
    paths = tuple(
        str(path).replace("\\", "/") for path in payload["referencedPaths"] if str(path).strip()
    )
    return ClassificationResult(
        classification=label,
        confidence=float(payload["confidence"]),
        reason=str(payload["reason"]),
        referenced_paths=paths,
    )


def classify_review_comment(
    item: ReviewFeedback,
    spec: TaskSpec,
    *,
    config: AgentConfig | None = None,
    api_key: str | None = None,
    requester: Callable[[dict[str, Any], str], dict[str, Any]] | None = None,
) -> ClassificationResult:
    cfg = config or load_config()
    key = (api_key or "").strip()
    if not key:
        raise AgentError.environment_failure(
            f"{cfg.review.api_key_env} is required for review classification",
            code="MISSING_CLASSIFIER_API_KEY",
        )
    body = _request_body(item, spec, cfg.review.classifier_model)
    transport = requester or _openai_chat_completions
    raw = transport(body, key)
    return parse_classification_payload(_extract_message_json(raw))


def _request_body(item: ReviewFeedback, spec: TaskSpec, model: str) -> dict[str, Any]:
    instruction = CLASSIFY_PROMPT_PATH.read_text(encoding="utf-8").strip()
    user = "\n".join(
        [
            "# Classifier Responsibility",
            "Classify semantic alignment with the Task Spec only.",
            "Do not determine technical correctness, and do not decide whether to repair.",
            "",
            "# Task Scope",
            f"- allowed_paths: {', '.join(spec.allowed_paths) or '(none)'}",
            f"- forbidden_paths: {', '.join(spec.forbidden_paths) or '(none)'}",
            "- Unspecified paths = Default Deny.",
            "",
            format_review_task_context(spec),
            "",
            "# Review Comment",
            f"- identity: {item.identity}",
            f"- path: {item.path or '(none)'}",
            f"- commit: {item.commit_sha or '(none)'}",
            "",
            item.body.strip() or "(empty)",
        ]
    )
    schema = {
        key: value
        for key, value in load_classification_schema().items()
        if key not in {"$schema", "$id", "title"}
    }
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": instruction},
            {"role": "user", "content": user},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "review_classification",
                "strict": True,
                "schema": schema,
            },
        },
    }


def _extract_message_json(raw: dict[str, Any]) -> dict[str, Any]:
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AgentError.environment_failure(
            "classifier response has no choices",
            code="CLASSIFIER_INCOMPLETE",
        )
    choice = choices[0]
    if not isinstance(choice, dict):
        raise AgentError.environment_failure(
            "classifier response choice is not an object",
            code="CLASSIFIER_INCOMPLETE",
        )
    finish = choice.get("finish_reason")
    if isinstance(finish, str) and finish not in {"stop"}:
        if finish == "content_filter":
            raise AgentError.escalation_required(
                "classifier refused the request via content filter",
                code="CLASSIFIER_REFUSAL",
            )
        if finish == "length":
            raise AgentError.escalation_required(
                "classifier response was truncated",
                code="CLASSIFIER_INCOMPLETE",
            )
        raise AgentError.environment_failure(
            f"classifier finished with unexpected reason {finish!r}",
            code="CLASSIFIER_INCOMPLETE",
        )
    message = choice.get("message")
    if not isinstance(message, dict):
        raise AgentError.environment_failure(
            "classifier response has no message",
            code="CLASSIFIER_INCOMPLETE",
        )
    refusal = message.get("refusal")
    if isinstance(refusal, str) and refusal.strip():
        raise AgentError.escalation_required(
            "classifier refused the request",
            code="CLASSIFIER_REFUSAL",
        )
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise AgentError.escalation_required(
            "classifier returned empty content",
            code="CLASSIFIER_INCOMPLETE",
        )
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise AgentError.invalid_input(
            "classifier returned non-JSON content",
            code="INVALID_CLASSIFIER_JSON",
        ) from exc
    if not isinstance(payload, dict):
        raise AgentError.invalid_input(
            "classifier JSON root must be an object",
            code="INVALID_CLASSIFIER_JSON",
        )
    return payload


def classifier_error_for_http(status: int) -> AgentError:
    if status == 429:
        return AgentError.environment_failure(
            "classifier API rate limited (HTTP 429)",
            code="CLASSIFIER_API_RATE_LIMIT",
        )
    if status >= 500:
        return AgentError.environment_failure(
            f"classifier API HTTP {status}",
            code="CLASSIFIER_API_FAILURE",
        )
    return AgentError.environment_failure(
        f"classifier API HTTP {status}",
        code="CLASSIFIER_API_FAILURE",
    )


def _openai_chat_completions(body: dict[str, Any], api_key: str) -> dict[str, Any]:
    encoded = json.dumps(body).encode("utf-8")
    request = Request(
        OPENAI_CHAT_COMPLETIONS,
        data=encoded,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except TimeoutError as exc:
        raise AgentError.environment_failure(
            "classifier API timed out",
            code="CLASSIFIER_API_TIMEOUT",
        ) from exc
    except HTTPError as exc:
        raise classifier_error_for_http(exc.code) from exc
    except URLError as exc:
        raise AgentError.environment_failure(
            f"classifier API network error: {exc.reason}",
            code="CLASSIFIER_API_NETWORK",
        ) from exc
    if not isinstance(payload, dict):
        raise AgentError.environment_failure(
            "classifier API returned a non-object",
            code="CLASSIFIER_API_FAILURE",
        )
    return payload
