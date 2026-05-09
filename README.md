# YanClaw

## Quick start / 快速开始

1. Install dependencies: `uv sync`
2. Set environment variables in `.env` (especially `YANCLAW_OPENAI_API_KEY`).
3. Run a crawl:
   `uv run yanclaw crawl --universities "北京航空航天大学"`
4. Check LLM connectivity:
   `uv run yanclaw llm-check`

## Project layout 

- `src/runtime/` shared runtime primitives
- `src/agents/crawler/` crawler agent and fetchers
- `docs/crawler/` design and requirements
- `data/` crawl output and local databases
- `logs/` runtime logs

## Notes

- Only public academic pages are targeted; login-protected pages are out of scope.
- 只面向公开学术页面，不处理需要登录的内容。
- For the full design, see `docs/crawler/design.md` and `docs/crawler/requirements.md`.
- 详细设计见 `docs/crawler/design.md` 与 `docs/crawler/requirements.md`.
