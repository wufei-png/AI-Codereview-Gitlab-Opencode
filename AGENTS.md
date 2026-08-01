# Repository Guidelines

## Project Structure & Module Organization

`api.py` runs the Flask webhook API; `ui.py` runs the Streamlit dashboard. Application code lives under `biz/`: agent jobs in `biz/agent/`, routes in `biz/api/`, provider adapters in `biz/llm/`, and platform integrations in `biz/platforms/`. Tests are under `tests/`, with some handler tests colocated in `biz/platforms/`. Configuration belongs in `conf/`; OpenCode resources in `opencode/`; canonical review instructions in `skills/review-agent/`. Treat `data/` and `log/` as runtime output. Architecture records and plans live under `docs/`.

## Build, Test, and Development Commands

- `python -m venv .venv && source .venv/bin/activate`: create an isolated environment.
- `pip install -r requirements.txt`: install runtime and test dependencies.
- `cp conf/.env.dist conf/.env`: create local provider and platform configuration.
- `python api.py`: run the Flask service on port 5001.
- `streamlit run ui.py --server.port=5002`: run the dashboard.
- `python -m biz.agent.worker`: process queued external-agent review jobs.
- `pytest`: run the full suite configured by `pytest.ini`.
- `pytest tests/agent/ --cov=biz.agent --cov-report=term-missing`: check agent coverage.
- `docker compose up --build -d`: build and run the containerized services.

## Coding Style & Naming Conventions

Use four-space indentation and surrounding PEP 8-style Python. Use `snake_case` for modules, functions, and variables; `PascalCase` for classes; and `UPPER_SNAKE_CASE` for environment variables and constants. Type new public interfaces and group imports as standard library, third-party, then local. No formatter or linter is enforced, so avoid unrelated formatting churn.

## Testing Guidelines

Pytest discovers `test_*.py`, `Test*` classes, and `test_*` functions. Keep tests deterministic: mock LLM, platform, network, and CLI boundaries, and use `tmp_path` for repositories or job state. Add regression coverage with each bug fix. There is no CI coverage gate; agent design targets are 80% for `biz/agent`, 90% for `runner.py`, and 100% for `safety.py`.

## Commit & Pull Request Guidelines

History favors Conventional Commit subjects such as `fix: ...`, `feat(agent): ...`, `test(agent): ...`, and `docs: ...`; keep subjects imperative and focused. Pull requests should explain behavior and risk, link issues, list verification commands, and call out configuration or security changes. Include dashboard screenshots for UI changes and document manual webhook or external-CLI validation.

## Security & Configuration

Never commit `conf/.env`, API tokens, webhook secrets, or CLI credentials. Run external-agent workers with least-privilege accounts and disposable workspaces; changes to signature validation, command safety, or credential handling require focused security tests.
