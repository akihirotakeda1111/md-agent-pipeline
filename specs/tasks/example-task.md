---
schema_version: 1
id: phase2-step2
title: Idempotent Worker
status: PENDING
base_branch: main
target_branch: feature/phase2-worker

allowed_paths:
  - worker/**
  - infra/terraform/**

forbidden_paths:
  - specs/**
  - .agent/**
  - agent/**
  - .github/**

repair_attempt_limit: 3
review_attempt_limit: 3
---

# Objective

Build an idempotent worker that acquires a job lease and emits a heartbeat until the job completes.

# Non-Goals

- Do not implement GitHub Actions orchestration.
- Do not add a generic agent framework.
- Do not change Task Spec or Execution State files.

# Forbidden Actions

- Do not force-push or rewrite git history.
- Do not merge pull requests.
- Do not run `terraform apply` or `terraform destroy`.
- Do not edit files outside `allowed_paths`.

# Architecture Invariants

- Lease acquisition must be idempotent for the same job id.
- Heartbeats must not extend an expired lease.
- Runtime state belongs in worker storage, not in this Task Spec.

# Tasks

## task-1: Lease repository

### Requirement

Implement a job lease repository that stores owner, expiry, and version for each job id.

### Acceptance Criteria

- Acquiring a lease for a new job id succeeds.
- Re-acquiring with the same owner and job id is idempotent.
- A different owner cannot steal an unexpired lease.

### Validation

```text
pytest worker/tests/test_lease_repository.py
```

## task-2: Heartbeat loop

depends_on: task-1

### Requirement

Implement a heartbeat loop that refreshes an owned lease until the job finishes or the lease is lost.

### Acceptance Criteria

- Heartbeat refreshes expiry while the caller still owns the lease.
- Heartbeat fails closed after the lease expires or is owned by someone else.
- The loop stops after a terminal job status.

### Validation

```text
pytest worker/tests/test_heartbeat_loop.py
```

# Final Verification

```text
pytest worker/tests
```
