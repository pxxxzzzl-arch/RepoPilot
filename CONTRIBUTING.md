# Contributing to RepoPilot

Thanks for helping improve RepoPilot. Keep changes small, auditable, and covered by tests.

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
pytest -q
```

Build the sandbox image before running Docker integration tests:

```bash
docker build -f docker/sandbox.Dockerfile -t issue2patch-sandbox:py311 .
pytest tests/test_sandbox_integration.py -q
```

## Pull requests

- Add tests for success, failure, timeout, and security-boundary behavior.
- Preserve the rule that untrusted repositories execute only in the Docker sandbox.
- Never commit API keys, `.env` files, source-bearing traces, or paid API output that contains private data.
- Keep real API tests opt-in; normal CI must remain deterministic and free of API charges.
- Explain user-visible behavior and any new trust assumption in the pull request.

By contributing, you agree that your contribution is licensed under the MIT License.
