# Phase 4 — Scope Enforcement, Validation & Repair

## Objective

Codexが行った変更をOrchestratorが機械検証し、
Scope内変更だけをValidationへ進め、
失敗時にbounded repair loopを実行できるようにする。

このPhase終了時点でローカルAutonomous Coreを完成させる。

---

## Depends On

Phase 3 — Codex CLI Runner

---

## Execution Flow

```text
Task Select
  ↓
BASE_SHA capture
  ↓
Codex Implement
  ↓
Git Diff
  ↓
Scope Check
  ↓
Validation
  ↓
PASS ──────────────> Task Complete
  │
 FAIL
  ↓
Failure Classification
  ↓
Repairable?
  ├─ no -> FAILED / ESCALATED
  └─ yes
       ↓
     Codex Repair
       ↓
     Scope Check
       ↓
     Re-run failed validation
```

---

## BASE_SHA

Codex実行直前のGit stateを保存する。

最低限:

```text
BASE_SHA
working tree precondition
```

開始時に既存のuncommitted changeがある場合のpolicyを明示する。
安全にagent由来差分と区別できない場合はfail closedとする。

---

## Scope Enforcement

Codex実行後、実際のGit差分から変更pathを取得する。

Task Specの:

```text
allowed_paths
forbidden_paths
```

と照合する。

### Policy

Allowed外またはForbidden内の変更があれば:

```text
SCOPE_VIOLATION
```

とする。

その場合:

- commitしない
- pushしない
- Task completedにしない
- violation pathsを記録する
- Escalation情報を作る

---

## Path Matching

path matchingは明確な実装を使う。

以下をtestする。

- exact file
- directory glob
- nested path
- overlapping allow/deny
- rename
- deleted file
- new untracked file

`git diff --name-only` だけでuntracked filesを取りこぼさないこと。

---

## Validation Runner

Task Spec内のValidation commandsをOrchestratorが実行する。

Requirement:

- command
- exit code
- stdout/stderr
- duration
- task id

を記録する。

Commandが失敗した時点で、
そのTaskのValidationはFAILとする。

---

## Command Safety

Task Specに書かれたcommandを無条件にtrusted codeとして実行しない。

少なくとも以下を検討する。

- permitted command families
- forbidden destructive substrings / structured command policy
- working directory restrictions
- timeout
- environment allowlist

初期版で完全なshell sandboxを作る必要はないが、
危険commandをそのまま実行する設計にしない。

---

## Failure Classification

最低限:

```text
AGENT_REPAIRABLE
ENVIRONMENT_FAILURE
ESCALATION_REQUIRED
```

### AGENT_REPAIRABLE

例:

- compile error
- test assertion failure
- lint error
- type error
- format error

### ENVIRONMENT_FAILURE

例:

- dependency registry unavailable
- network timeout
- GitHub unavailable
- credential missing
- external service unavailable

### ESCALATION_REQUIRED

例:

- IAM変更
- destructive migration
- Task Spec矛盾
- architecture ambiguity
- required scope violation
- security decision

分類できない場合は自動でrepairし続けずEscalation寄りに扱う。

---

## Repair Loop

1 Repair Attempt:

```text
Validation Failure
→ Failure context生成
→ Codex repair
→ Scope Check
→ failed command rerun
```

repair_attempt_limitを超えない。

上限到達:

```text
ESCALATED
```

---

## Repair Prompt

Codexには最低限:

- Current Task
- failed command
- exit code
- relevant error output
- current diff
- unchanged constraints

を渡す。

「テストを消す」「lintを無効化する」等で通すことを禁止する。

---

## Final Verification Interface

このPhaseでは、
全Task完了後にFinal Verification commandsを実行できる
共通runner/interfaceまで実装する。

Git/PR操作はまだ行わない。

---

## Tests

最低限:

- allowed file
- forbidden file
- nested glob
- untracked file
- deleted file
- renamed file
- scope violation
- validation success
- validation failure
- timeout
- repairable classification
- environment classification
- escalation classification
- retry increment
- retry limit
- final verification success/failure

---

## Definition of Done

ローカルfixture/mockを使い、

```text
Spec
→ State
→ Task Select
→ Mock/Real Codex Runner
→ Diff Check
→ Validation
→ Repair
```

のcontrol flowをtestできる。

- Scope違反をCommit前に検出できる
- ValidationをCodexではなくOrchestratorが実行する
- Repair回数がbounded
- Environment Failureをrepairへ送らない
- Final Verificationを実行できる
- tests PASS

---

## Stop Condition

DoD後、Phase 5へ進まず停止する。
