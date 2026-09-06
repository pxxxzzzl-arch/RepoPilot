# Design decisions

This lightweight decision log records choices that define RepoPilot's safety and evaluation model.

## D001 — Return a diff, never modify the source repository

**Decision:** every Agent run operates on a fresh temporary copy and returns only a unified diff.

**Why:** review and approval remain explicit, failures cannot leave a half-edited checkout, and evaluation runs are isolated from one another.

## D002 — Model actions instead of model shell commands

**Decision:** the model selects from `SearchAction`, `ReadFileAction`, `PatchAction`, `RunTestsAction`, and `FinishAction`.

**Why:** capability is bounded in code, arguments can be validated, and an audit trail can explain every tool call.

## D003 — Optimistic concurrency for patches

**Decision:** reads return SHA-256 metadata and every patch supplies `expected_sha256`.

**Why:** stale model observations fail closed as `PATCH_CONFLICT` instead of overwriting newer content.

## D004 — Docker by default for untrusted tests

**Decision:** untrusted repositories use a network-disabled, non-root, read-only Docker sandbox. Local execution is retained only as an explicitly trusted runner.

**Why:** tests are executable code and require a stronger boundary than path validation alone.

## D005 — Deterministic CI, opt-in paid evaluation

**Decision:** CI uses scripted/mock models and never receives API credentials. Real API smoke tests and 10×3 evaluations require explicit operator approval.

**Why:** pull requests stay reproducible, free of accidental charges, and safe for forks.

## D006 — Strict evaluation success

**Decision:** a repair succeeds only when the Agent terminates successfully, tests pass, the source repository is unchanged, and the diff touches only the task allowlist.

**Why:** test pass rate alone can hide unrelated or unsafe modifications.

## D007 — Explicit provider and isolated credentials

**Decision:** the CLI selects `openai` or `deepseek` explicitly. Each client reads only its provider-specific environment variable and uses a fixed HTTPS endpoint.

**Why:** credentials cannot be mistaken for one another, API keys never become command-line arguments, and adding a provider does not weaken the shared structured-action validation.
