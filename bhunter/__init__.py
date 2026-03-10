"""
BHunter - Standalone AI-Powered Bug Detection & Fix Agent

A pip-installable package that integrates Sentry + GitHub MCP tools
with an LLM agent to automatically detect, analyze, and fix bugs.

Can be injected into any Python project with a simple config object.

Quick start:
    from bhunter import BHunterConfig, BHunterPipeline

    config = BHunterConfig(
        sentry_token="...",
        github_token="...",
        github_repo_owner="myorg",
        github_repo_name="myrepo",
    )
    pipeline = BHunterPipeline(config)
    run = await pipeline.execute()

    # Optionally mount FastAPI router:
    from bhunter import create_router
    router = create_router(config, auth_dependency=my_auth)
    app.include_router(router, prefix="/api/admin")
"""

from bhunter.config import (
    AttemptStatus,
    BHunterConfig,
    BugSeverity,
    LLMProvider,
    RunStatus,
    RunTrigger,
)
from bhunter.models import (
    BugAnalysis,
    BugFixAttempt,
    BHunterRun,
    BHunterRunResponse,
    BHunterStatusResponse,
    BHunterTriggerRequest,
    FileChange,
    FixProposal,
    SentryEvent,
    SentryIssue,
    SentryStackFrame,
)
from bhunter.pipeline import BHunterPipeline, run_to_response

try:
    from bhunter.router import create_router
except ImportError:
    # FastAPI not installed -- router factory unavailable
    create_router = None  # type: ignore[assignment]

__version__ = "1.0.0"

__all__ = [
    # Config
    "BHunterConfig",
    "BugSeverity",
    "LLMProvider",
    "RunTrigger",
    "AttemptStatus",
    "RunStatus",
    # Core
    "BHunterPipeline",
    "run_to_response",
    "create_router",
    # Models
    "BugAnalysis",
    "BugFixAttempt",
    "BHunterRun",
    "BHunterRunResponse",
    "BHunterStatusResponse",
    "BHunterTriggerRequest",
    "FileChange",
    "FixProposal",
    "SentryEvent",
    "SentryIssue",
    "SentryStackFrame",
]
