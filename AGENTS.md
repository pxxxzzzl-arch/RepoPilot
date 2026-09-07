# RepoPilot agent guide

RepoPilot is a safety-first coding agent. `issue2patch` is the Python package
and CLI. It turns a failing issue into a tested diff while leaving the source
repository unchanged.

## Verify changes

```bash
python -m pip install -e ".[dev]"
pytest -q
python -m build
```

Root `pytest` must collect only `tests/`. Broken fixtures under `evals/tasks/`
and `examples/broken_calculator/` are run only when explicitly targeted.

## Structure

- `src/issue2patch/`: CLI, model clients, orchestrator, safe tools, patching,
  Docker sandbox, traces, and evaluation runner.
- `tests/`: deterministic unit and integration tests.
- `evals/`: fixed broken repositories and the evaluation manifest.
- `eval-results/` and `live-evidence/`: generated evidence from an explicitly
  approved credentialed workflow.
- `docker/`: the locked-down test image.
- `docs/`: architecture, decisions, and release notes.

## Non-negotiable boundaries

- Never add a model-provided shell action.
- Untrusted tests run in `DockerSandboxRunner`; local execution is trusted-only.
- Agent tools operate on a temporary copy, never the source repository.
- Preserve repository-relative path, symlink, sensitive-file, size, timeout,
  SHA-256 precondition, full-preflight, and atomic-write checks.
- Traces must omit source bodies, diffs, environment variables, API keys, and
  hidden reasoning.
- API keys come only from provider-specific environment variables.
- Normal CI remains deterministic and must not run paid API tests.

## Current scope

The v0.1 benchmark covers small Python fixes. GitHub Issue import, branch push,
and Pull Request creation are intentionally not implemented.
