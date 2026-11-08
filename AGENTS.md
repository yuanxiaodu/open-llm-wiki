# Repository Guidelines

## Structure

- `app/` — FastAPI backend: `routers/api/`, `services/`, `agent/`, `wiki/`, `wiki_config.py`, `plugins.py`.
- `web/src/` — React + TypeScript client. `web/src/plugins.ts` discovers `plugins/<id>/web/index.tsx` at build time.
- `plugins/` — optional feature plugins. This repository ships none; see `docs/PLUGINS.md`.
- `tests/` — backend pytest and plugin contract tests.
- `migrations/` — SQLite schema migrations.
- `scripts/` — utilities; `deploy/` — optional deployment templates; `docs/` — architecture/config/plugin/deployment docs.
- `wiki.toml` — project-side wiki configuration (domain, tags, labels, plugin enablement).
- `data/` — ignored runtime data. `WIKI_DIR` (default `data/wiki`) is a **separate Git repository**; the project repo never tracks it.

## Commands

Python 3.11 and Node 22 (pnpm from `web/package.json`).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q

cd web
corepack pnpm install --frozen-lockfile
corepack pnpm test
corepack pnpm exec tsc --noEmit
corepack pnpm build
```

Generated API contract:

```bash
python scripts/export_openapi.py --out web/openapi.json
cd web && corepack pnpm gen:api
```

## Style

- Python: four spaces, `snake_case`.
- TypeScript: two spaces, semicolons, `camelCase`; PascalCase components.
- No formatter/linter is configured; follow surrounding files.
- Keep TypeScript strict checks passing.

## Tests

- Backend: `tests/test_*.py`; reuse `tests/conftest.py` fixtures (temporary `DATA_DIR`/`WIKI_DIR`, isolated identities). Mock model calls.
- Frontend: colocated `*.test.ts`; plugin frontend tests live under `plugins/<id>/web/` and are picked up by Vite's `test.include`.
- CI requires pytest, Vitest, type check, production build, and generated API consistency.

## Security

Never commit `.env`, credentials, runtime data, wiki content, uploads, or private deployment details. Keep Uvicorn at one worker: write locks and chat guards are process-local.
