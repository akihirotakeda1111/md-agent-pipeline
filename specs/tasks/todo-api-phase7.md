---
schema_version: 1
id: todo-api-phase7
title: Implement the Go TODO REST API comparison workload
status: PENDING
base_branch: baseline/todo-api
target_branch: feature/todo-api-phase7

allowed_paths:
  - apps/todo-api/**

forbidden_paths:
  - specs/**
  - .agent/**
  - agent/**
  - .github/**

repair_attempt_limit: 3
review_attempt_limit: 3
---

# Objective

Complete a local-only TODO REST API in `apps/todo-api` using Go and primarily the standard library. Implement `GET /todos`, `POST /todos`, `PATCH /todos/{id}`, and `DELETE /todos/{id}` with deterministic HTTP behavior and automated tests.

# Non-Goals

- Do not add a frontend, Vue.js, Node.js, or browser build chain.
- Do not add a database, authentication, Docker, cloud services, deployment, or migrations.
- Do not add Phase 7 behavior, CodeRabbit integration, or experiment-specific branches in application code.
- Do not introduce production secrets or external runtime dependencies.

# Forbidden Actions

- Do not edit files outside `allowed_paths`, including `.agent/**`, `agent/**`, `.github/**`, and `specs/**`.
- Do not change the pipeline, workflows, state, infrastructure, Task Spec, or agent configuration.
- Do not perform destructive Git operations, rewrite history, force-push, merge pull requests, or run destructive infrastructure commands.
- Do not weaken, delete, or bypass tests or validation commands to make validation pass.

# Architecture Invariants

- The application is a single Go module rooted at `apps/todo-api` and uses in-memory storage only.
- The server and tests must not require network services, persistent storage, credentials, Docker, or cloud resources.
- Shared mutable state must be safe for concurrent HTTP requests.
- A Todo JSON object is `{"id": 1, "title": "buy milk", "completed": false}`: `id` is a positive integer, `title` is a string, and `completed` is a boolean.
- An error JSON object is `{"error": {"code": "validation_error", "message": "title is required"}}`; every error uses this envelope with non-empty string `code` and `message` fields.
- Responses with JSON bodies set `Content-Type: application/json`. A successful delete has no body.
- IDs increase monotonically from 1 within a process, and list responses are ordered by ascending ID.
- Prefer Go standard-library packages such as `net/http`, `encoding/json`, `sync`, `testing`, and `httptest`.

# Tasks

## task-1: TODO domain and in-memory repository

### Requirement

Define the TODO representation and implement concurrency-safe in-memory creation, listing, lookup/update, and deletion primitives needed by the HTTP layer.

### Acceptance Criteria

- A TODO has a positive integer identifier, a non-empty trimmed title, and a completed state that defaults to `false` when created.
- Created TODOs remain available for later operations during the process lifetime.
- Unknown identifiers are distinguishable from valid records.
- Repository list results are ordered by ascending ID.
- Repository behavior is deterministic and covered by unit tests.

### Validation

```text
go -C apps/todo-api test ./...
```

## task-2: HTTP CRUD handlers

depends_on: task-1

### Requirement

Implement the four required routes using the fixed Todo JSON contract. `POST /todos` accepts `{"title": "buy milk"}`. `PATCH /todos/{id}` accepts an object containing `title`, `completed`, or both and must distinguish an omitted field from an explicit `false` value.

### Acceptance Criteria

- `GET /todos` returns `200 OK` and a JSON array of Todo objects; an empty repository returns `[]` rather than `null`.
- `POST /todos` returns `201 Created` and the created Todo object with `completed: false`.
- `PATCH /todos/{id}` returns `200 OK` and the updated Todo object without overwriting omitted fields.
- `DELETE /todos/{id}` returns `204 No Content`, an empty body, and removes the TODO.
- Unsupported methods return `405 Method Not Allowed`; unmatched paths return `404 Not Found`.

### Validation

```text
go -C apps/todo-api test ./...
```

## task-3: Validation and error handling

depends_on: task-2

### Requirement

Make malformed requests and domain failures deterministic using the fixed error JSON contract, including malformed JSON, invalid titles, invalid identifiers, unknown TODO identifiers, unknown fields, and trailing JSON values.

### Acceptance Criteria

- Malformed JSON, trailing JSON values, unknown request fields, non-positive/non-decimal IDs, and an empty PATCH object return `400 Bad Request` with error code `invalid_request`.
- Empty or whitespace-only titles return `400 Bad Request` with error code `validation_error`; accepted titles are stored after trimming leading and trailing whitespace.
- Unknown positive TODO identifiers return `404 Not Found` with error code `not_found` for PATCH and DELETE.
- Unsupported methods return error code `method_not_allowed`; unmatched paths return error code `not_found`.
- Every error response has the documented JSON envelope and `Content-Type: application/json`.

### Validation

```text
go -C apps/todo-api test ./...
```

## task-4: Tests and final cleanup

depends_on: task-3

### Requirement

Add focused unit and `httptest` coverage for the fixed API contract, successful CRUD flow, partial updates, invalid inputs, unknown identifiers, and method/path handling; then clean up the implementation for final verification.

### Acceptance Criteria

- Tests cover all four endpoints, exact success statuses, Todo JSON shape, error JSON shape, and content types through the HTTP handler.
- Tests cover malformed and trailing JSON, unknown fields, empty titles, invalid and unknown identifiers, completed-state updates, empty PATCH, unsupported methods, and unmatched paths.
- Tests verify deterministic ascending list order and `[]` for an empty list.
- Tests are isolated and do not require a running server or external service.
- The placeholder `501 Not Implemented` baseline behavior is removed.
- Source files are gofmt-clean, and vetting, tests, and build all pass.

### Validation

```text
go -C apps/todo-api test ./...
```

# Final Verification

```text
go -C apps/todo-api vet ./...
go -C apps/todo-api test ./...
go -C apps/todo-api build ./...
```
