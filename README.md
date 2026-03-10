# BHunter -- Autonomous Bug-Fixing Agent

Standalone Python package that detects production bugs from **Sentry**, analyzes root cause using LLMs, generates minimal fixes, and creates **GitHub PRs/Issues** automatically.

## Features

- **Sentry integration** via MCP Server (subprocess)
- **GitHub integration** via MCP Server (subprocess)
- **Local codebase analysis** via custom Agno toolkit
- **Safety validators** prevent dangerous auto-generated changes
- **Dry-run mode** for testing without creating PRs
- **FastAPI router factory** for easy integration into any app

## Quick Start

```bash
pip install bhunter
# or with FastAPI support:
pip install bhunter[api]
```

### Minimal Usage

```python
from bhunter import BHunterConfig, BHunterPipeline

config = BHunterConfig(
    sentry_token="sntrys_...",
    github_token="ghp_...",
    github_repo_owner="your-org",
    github_repo_name="your-repo",
    openrouter_api_key="sk-or-...",  # for LLM fallback
)

pipeline = BHunterPipeline(config)
run = await pipeline.execute(max_issues=5)
print(f"PRs created: {run.prs_created}")
```

### FastAPI Integration

```python
from fastapi import FastAPI
from bhunter import create_router, BHunterConfig

app = FastAPI()
config = BHunterConfig(
    sentry_token="sntrys_...",
    github_token="ghp_...",
    github_repo_owner="your-org",
    github_repo_name="your-repo",
    openrouter_api_key="sk-or-...",
)

# No auth (development)
app.include_router(create_router(config), prefix="/bhunter")

# With auth dependency
from fastapi import Depends, Security
app.include_router(
    create_router(config, auth_dependency=Depends(require_admin)),
    prefix="/bhunter",
)
```

## Configuration

All config is injected via `BHunterConfig` -- no global settings or environment variables are implicitly read.

| Parameter | Default | Description |
|---|---|---|
| `sentry_token` | `""` | Sentry API token |
| `github_token` | `""` | GitHub PAT (also for GitHub Models API) |
| `openrouter_api_key` | `""` | OpenRouter API key (LLM fallback) |
| `github_repo_owner` | `""` | GitHub repo owner |
| `github_repo_name` | `""` | GitHub repo name |
| `project_root` | `""` | Codebase root path (auto-detected if empty) |
| `llm_provider` | `github_models` | `github_models` or `openrouter` |
| `dry_run` | `false` | Analyze but don't create PRs |
| `max_prs_per_run` | `3` | Max PRs per execution |
| `max_prs_per_day` | `5` | Max PRs in 24h |
| `max_files_per_fix` | `3` | Max files per fix |
| `max_lines_changed` | `50` | Max lines per fix |
| `min_occurrences` | `1` | Min Sentry events to consider |

## Architecture

```
bhunter/
    __init__.py           # Public API
    config.py             # Enums + BHunterConfig dataclass
    models.py             # Pydantic schemas
    agent.py              # Agno Agent factory
    pipeline.py           # Pipeline orchestrator
    router.py             # FastAPI router factory
    tools/
        sentry_tool.py    # Sentry MCP wrapper
        github_tool.py    # GitHub MCP wrapper
        codebase_tool.py  # Local codebase toolkit
    validators/
        safety.py         # Pre-PR safety checks
```

## License

MIT
