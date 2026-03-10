"""
BHunter Configuration

Thresholds, filters, safety guards, credentials, and model selection
for the autonomous bug-fixing agent.

All configuration is injected via BHunterConfig -- no global
settings or environment variables are implicitly read.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger("bhunter")


class BugSeverity(str, Enum):
    """Bug complexity/severity classification."""

    TRIVIAL = "trivial"
    MEDIUM = "medium"
    COMPLEX = "complex"


class RunTrigger(str, Enum):
    """How the BHunter run was triggered."""

    CRON = "cron"
    WEBHOOK = "webhook"
    MANUAL = "manual"


class AttemptStatus(str, Enum):
    """Status of a single bug fix attempt."""

    ANALYZING = "analyzing"
    FIXING = "fixing"
    PR_CREATED = "pr_created"
    ISSUE_CREATED = "issue_created"
    FAILED = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"


class RunStatus(str, Enum):
    """Status of a BHunter run."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class LLMProvider(str, Enum):
    """Available LLM providers for BHunter."""

    GITHUB_MODELS = "github_models"
    OPENROUTER = "openrouter"


@dataclass
class BHunterConfig:
    """
    Complete configuration for BHunter.

    All credentials and settings are passed explicitly -- nothing is
    read from global settings or environment variables. This makes the
    package fully portable across projects.
    """

    # -- Credentials --
    sentry_token: str = ""
    sentry_host: str = ""               # Self-hosted Sentry URL (empty = sentry.io)
    sentry_org: str = ""                # Sentry organization slug
    github_token: str = ""              # GitHub PAT (also for GitHub Models API)
    openrouter_api_key: str = ""        # OpenRouter API key (fallback LLM)

    # -- LLM Provider --
    llm_provider: LLMProvider = LLMProvider.GITHUB_MODELS

    # GitHub Models (Copilot Pro+ / free tier)
    github_models_base_url: str = "https://models.github.ai/inference"
    github_models_model: str = "openai/gpt-5-mini"

    # OpenRouter (last-resort fallback, low-cost)
    openrouter_model: str = "minimax/minimax-m2.5"

    # Custom headers for OpenRouter
    openrouter_app_title: str = "BHunter"
    openrouter_app_url: str = ""

    # -- Project --
    project_root: str = ""              # Codebase root (auto-detected if empty)

    # -- Sentry Filters --
    min_occurrences: int = 1
    max_issue_age_hours: int = 168      # 7 days
    sentry_project_slugs: list[str] = field(default_factory=list)
    ignored_issue_ids: list[str] = field(default_factory=list)

    # -- Safety Guards --
    max_prs_per_run: int = 3
    max_prs_per_day: int = 5
    max_files_per_fix: int = 3
    max_lines_changed: int = 50
    dry_run: bool = False

    # -- File Blocklist (never modify these) --
    file_blocklist: list[str] = field(default_factory=lambda: [
        "config.py",
        "main.py",
        "settings.py",
        ".env",
        "alembic/",
        "migrations/",
        "docker-compose",
        "Dockerfile",
        "Makefile",
        "pyproject.toml",
        "package.json",
        "pnpm-lock.yaml",
    ])

    # -- GitHub --
    github_repo_owner: str = ""
    github_repo_name: str = ""
    github_base_branch: str = "main"
    github_reviewers: list[str] = field(default_factory=list)
    github_labels: list[str] = field(default_factory=lambda: [
        "bugfix",
        "auto-generated",
        "bhunter",
    ])

    # -- Complexity Gate --
    auto_fix_severities: list[BugSeverity] = field(default_factory=lambda: [
        BugSeverity.TRIVIAL,
        BugSeverity.MEDIUM,
    ])

    # -- Timeouts --
    mcp_timeout_seconds: int = 120
    analysis_timeout_seconds: int = 180
    fix_generation_timeout_seconds: int = 120

    # -- Storage --
    store_dir: str = "/tmp/bhunter"   # Directory for run history JSON

    # -- Feature Flag --
    enabled: bool = False

    def is_file_blocked(self, filepath: str) -> bool:
        """Check if a file is in the blocklist."""
        for blocked in self.file_blocklist:
            if blocked in filepath:
                return True
        return False

    def can_auto_fix(self, severity: BugSeverity) -> bool:
        """Check if a severity level allows auto-fixing."""
        return severity in self.auto_fix_severities

    @property
    def is_ready(self) -> bool:
        """Check if minimum credentials are configured."""
        return bool(self.enabled and self.github_token and self.sentry_token)
