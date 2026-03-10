"""
BHunter Data Models

Pydantic schemas for bug reports, fix proposals, and run tracking.
No external dependencies beyond pydantic.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from bhunter.config import AttemptStatus, BugSeverity, RunStatus, RunTrigger


# ---------------------------------------------------------------------------
# Sentry Data
# ---------------------------------------------------------------------------


class SentryIssue(BaseModel):
    """Parsed Sentry issue data."""

    issue_id: str
    title: str
    culprit: str = ""
    level: str = "error"
    status: str = "unresolved"
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    count: int = 0
    user_count: int = 0
    project_slug: str = ""
    platform: str = ""
    url: str = ""


class SentryStackFrame(BaseModel):
    """Single frame from a Sentry stacktrace."""

    filename: str
    function: str = ""
    lineno: int | None = None
    colno: int | None = None
    context_line: str = ""
    pre_context: list[str] = Field(default_factory=list)
    post_context: list[str] = Field(default_factory=list)
    in_app: bool = True


class SentryEvent(BaseModel):
    """Parsed Sentry event (error occurrence)."""

    event_id: str = ""
    message: str = ""
    exception_type: str = ""
    exception_value: str = ""
    stacktrace: list[SentryStackFrame] = Field(default_factory=list)
    tags: dict[str, str] = Field(default_factory=dict)
    breadcrumbs: list[dict] = Field(default_factory=list)
    raw_json: dict | None = None


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


class BugAnalysis(BaseModel):
    """Result of analyzing a bug from Sentry data + codebase context."""

    issue: SentryIssue
    event: SentryEvent | None = None
    severity: BugSeverity = BugSeverity.COMPLEX
    root_cause: str = ""
    affected_files: list[str] = Field(default_factory=list)
    explanation: str = ""
    can_auto_fix: bool = False
    skip_reason: str = ""


# ---------------------------------------------------------------------------
# Fix Proposal
# ---------------------------------------------------------------------------


class FileChange(BaseModel):
    """A single file modification in a fix."""

    filepath: str
    original_content: str = ""
    new_content: str = ""
    diff_summary: str = ""
    lines_changed: int = 0


class FixProposal(BaseModel):
    """Generated fix for a bug."""

    analysis: BugAnalysis
    changes: list[FileChange] = Field(default_factory=list)
    commit_message: str = ""
    pr_title: str = ""
    pr_body: str = ""
    validation_passed: bool = False
    validation_errors: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Run Tracking
# ---------------------------------------------------------------------------


class BugFixAttempt(BaseModel):
    """Record of a single bug fix attempt."""

    id: UUID = Field(default_factory=uuid4)
    sentry_issue_id: str
    sentry_issue_url: str = ""
    severity: BugSeverity = BugSeverity.COMPLEX
    status: AttemptStatus = AttemptStatus.ANALYZING
    affected_files: list[str] = Field(default_factory=list)
    fix_description: str = ""
    pr_url: str = ""
    github_issue_url: str = ""
    tokens_used: int = 0
    error_message: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: datetime | None = None


class BHunterRun(BaseModel):
    """Record of a complete BHunter pipeline execution."""

    id: UUID = Field(default_factory=uuid4)
    trigger: RunTrigger = RunTrigger.MANUAL
    status: RunStatus = RunStatus.RUNNING
    started_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: datetime | None = None
    issues_found: int = 0
    prs_created: int = 0
    issues_created: int = 0
    skipped: int = 0
    failed: int = 0
    attempts: list[BugFixAttempt] = Field(default_factory=list)
    error_log: str = ""
    config_snapshot: dict = Field(default_factory=dict)

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at and self.started_at:
            return (self.finished_at - self.started_at).total_seconds()
        return None

    @property
    def total_tokens(self) -> int:
        return sum(a.tokens_used for a in self.attempts)


# ---------------------------------------------------------------------------
# API Schemas
# ---------------------------------------------------------------------------


class BHunterTriggerRequest(BaseModel):
    """Request to manually trigger BHunter."""

    dry_run: bool = False
    max_issues: int = Field(default=5, ge=1, le=20)
    project_slugs: list[str] = Field(default_factory=list)
    min_occurrences: int = Field(default=1, ge=1)
    llm_provider: str = "github_models"
    model: str | None = None


class BHunterRunResponse(BaseModel):
    """Response with BHunter run details."""

    id: UUID
    trigger: RunTrigger
    status: RunStatus
    started_at: datetime
    finished_at: datetime | None
    issues_found: int
    prs_created: int
    issues_created: int
    skipped: int
    failed: int
    duration_seconds: float | None
    total_tokens: int
    attempts: list[BugFixAttempt]
    error_log: str = ""
    config_snapshot: dict = Field(default_factory=dict)


class BHunterStatusResponse(BaseModel):
    """Current status of BHunter service."""

    enabled: bool
    last_run: BHunterRunResponse | None = None
    prs_created_today: int = 0
    daily_limit: int = 5
    sentry_connected: bool = False
    github_connected: bool = False
