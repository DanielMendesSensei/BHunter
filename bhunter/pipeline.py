"""
BHunter Pipeline Orchestrator

Main entry point that coordinates the full detect -> analyze -> fix -> PR pipeline.

Lifecycle:
  1. Start Sentry MCP + GitHub MCP servers (subprocess via npx)
  2. Inject MCPTools + CodebaseToolkit into the Agno Agent
  3. Run the agent with a high-level prompt
  4. Parse agent output into structured models
  5. Track run metrics and return results

The agent itself handles the full workflow autonomously -- the pipeline
just manages infrastructure (MCP servers, config, error handling, tracking).

All credentials are read from BHunterConfig -- no global settings.
"""

from __future__ import annotations

import fcntl
import json
import logging
import re
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from bhunter.agent import create_bhunter_agent
from bhunter.config import (
    AttemptStatus,
    BHunterConfig,
    BugSeverity,
    LLMProvider,
    RunStatus,
    RunTrigger,
)
from bhunter.models import (
    BugFixAttempt,
    BHunterRun,
    BHunterRunResponse,
)
from bhunter.tools.codebase_tool import CodebaseToolkit
from bhunter.tools.github_tool import get_github_mcp
from bhunter.tools.sentry_tool import get_sentry_mcp

logger = logging.getLogger("bhunter.pipeline")

# ---------------------------------------------------------------------------
# File-based run history (cross-worker safe via fcntl)
# ---------------------------------------------------------------------------
_MAX_STORED_RUNS = 50


def _get_store_paths(config: BHunterConfig) -> tuple[Path, Path]:
    """Return (store_dir, runs_file) based on config."""
    store_dir = Path(config.store_dir)
    return store_dir, store_dir / "runs.json"


def _ensure_store(config: BHunterConfig) -> None:
    """Create store directory if needed."""
    store_dir, runs_file = _get_store_paths(config)
    store_dir.mkdir(parents=True, exist_ok=True)
    if not runs_file.exists():
        runs_file.write_text("[]")


def _read_all_runs(config: BHunterConfig) -> list[BHunterRun]:
    """Read run history from disk (shared across workers)."""
    _ensure_store(config)
    _, runs_file = _get_store_paths(config)
    try:
        with open(runs_file, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            data = json.load(f)
            fcntl.flock(f, fcntl.LOCK_UN)
        return [BHunterRun.model_validate(item) for item in data]
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        logger.warning("Failed to read run store: %s", exc)
        return []


def _save_run(run: BHunterRun, config: BHunterConfig) -> None:
    """Append a run to the file store (atomic read-modify-write)."""
    _ensure_store(config)
    _, runs_file = _get_store_paths(config)
    try:
        with open(runs_file, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                data = json.load(f)
            except json.JSONDecodeError:
                data = []
            data.append(run.model_dump(mode="json"))
            data = data[-_MAX_STORED_RUNS:]
            f.seek(0)
            f.truncate()
            json.dump(data, f, default=str)
            fcntl.flock(f, fcntl.LOCK_UN)
    except OSError as exc:
        logger.error("Failed to save run: %s", exc)


def _today_key() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def get_prs_created_today(config: BHunterConfig) -> int:
    """Count PRs created today across all stored runs."""
    today = _today_key()
    total = 0
    for run in _read_all_runs(config):
        if (
            run.started_at
            and run.started_at.strftime("%Y-%m-%d") == today
            and run.prs_created > 0
        ):
            total += run.prs_created
    return total


# ---------------------------------------------------------------------------
# Agent prompt builder
# ---------------------------------------------------------------------------

def _build_run_prompt(
    config: BHunterConfig,
    max_issues: int = 5,
    project_slugs: list[str] | None = None,
) -> str:
    """
    Build the user-level prompt for a BHunter run.

    This tells the agent exactly what to do for this particular execution.
    """
    project_filter = ""
    if project_slugs:
        project_filter = (
            f"\nOnly search in these Sentry projects: {', '.join(project_slugs)}"
        )

    repo_info = ""
    if config.github_repo_owner and config.github_repo_name:
        repo_info = (
            f"\nGitHub repository: {config.github_repo_owner}/{config.github_repo_name}"
            f"\nBase branch: {config.github_base_branch}"
        )

    pr_budget = config.max_prs_per_run
    daily_remaining = config.max_prs_per_day - get_prs_created_today(config)
    effective_budget = min(pr_budget, daily_remaining)

    return f"""\
Execute a BHunter run now.

## Parameters
- Analyze up to {max_issues} unresolved Sentry issues.
- Minimum {config.min_occurrences} occurrences to consider.
- Maximum issue age: {config.max_issue_age_hours} hours.
- PR budget this run: {effective_budget} (daily remaining: {daily_remaining}).
{project_filter}
{repo_info}

## Steps
1. Search for unresolved errors in Sentry, sorted by frequency.
2. For each issue (up to {max_issues}):
   a. Get full details (stacktrace, user count).
   b. Read affected source files from the codebase.
   c. Analyze root cause and classify severity.
   d. If TRIVIAL or MEDIUM and within PR budget:
      - Generate a minimal fix.
      - Create a branch named `bhunter/{{sentry-issue-id}}`.
      - Commit the fix and open a PR.
   e. If COMPLEX:
      - Create a GitHub Issue with your full analysis.
   f. Output the structured summary for each issue.

3. After processing all issues, output a final summary:
```
=== BHUNTER RUN SUMMARY ===
ISSUES_ANALYZED: X
PRS_CREATED: X
ISSUES_CREATED: X
SKIPPED: X
```

Begin now.
"""


# ---------------------------------------------------------------------------
# Output parser
# ---------------------------------------------------------------------------

def _parse_attempts_from_output(output: str) -> list[BugFixAttempt]:
    """
    Parse structured attempt data from agent output.

    Looks for the ISSUE/TITLE/SEVERITY/ACTION blocks in the agent response.
    """
    attempts: list[BugFixAttempt] = []

    # Pattern: ISSUE: xxx ... ACTION: yyy
    blocks = re.split(r"(?=ISSUE:\s)", output)

    for block in blocks:
        if not block.strip().startswith("ISSUE:"):
            continue

        attempt = BugFixAttempt(sentry_issue_id="unknown")

        # Extract fields
        issue_match = re.search(r"ISSUE:\s*(.+)", block)
        if issue_match:
            attempt.sentry_issue_id = issue_match.group(1).strip()

        title_match = re.search(r"TITLE:\s*(.+)", block)
        if title_match:
            attempt.fix_description = title_match.group(1).strip()

        severity_match = re.search(r"SEVERITY:\s*(\w+)", block)
        if severity_match:
            sev = severity_match.group(1).strip().lower()
            if sev in ("trivial", "medium", "complex"):
                attempt.severity = BugSeverity(sev)

        action_match = re.search(r"ACTION:\s*(\w+)", block)
        if action_match:
            action = action_match.group(1).strip().upper()
            if "PR" in action:
                attempt.status = AttemptStatus.PR_CREATED
            elif "ISSUE" in action:
                attempt.status = AttemptStatus.ISSUE_CREATED
            elif "SKIP" in action:
                attempt.status = AttemptStatus.SKIPPED
            else:
                attempt.status = AttemptStatus.FAILED

        # Extract file list
        files_match = re.search(r"FILES_AFFECTED:\s*(.+)", block)
        if files_match:
            files_str = files_match.group(1).strip()
            attempt.affected_files = [
                f.strip() for f in files_str.split(",") if f.strip()
            ]

        attempt.finished_at = datetime.utcnow()
        attempts.append(attempt)

    return attempts


def _parse_summary(output: str) -> dict[str, int]:
    """Parse the final summary counters from agent output."""
    summary: dict[str, int] = {}
    for key in ("ISSUES_ANALYZED", "PRS_CREATED", "ISSUES_CREATED", "SKIPPED"):
        match = re.search(rf"{key}:\s*(\d+)", output)
        if match:
            summary[key] = int(match.group(1))
    return summary


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class BHunterPipeline:
    """
    Main pipeline that orchestrates the BHunter agent.

    Usage:
        config = BHunterConfig(sentry_token="...", github_token="...")
        pipeline = BHunterPipeline(config)
        run = await pipeline.execute(trigger=RunTrigger.MANUAL)
    """

    def __init__(self, config: BHunterConfig | None = None):
        self.config = config or BHunterConfig()

    async def execute(
        self,
        trigger: RunTrigger = RunTrigger.MANUAL,
        max_issues: int = 5,
        project_slugs: list[str] | None = None,
    ) -> BHunterRun:
        """
        Execute a full BHunter pipeline run.

        Args:
            trigger: How the run was initiated (MANUAL, CRON, WEBHOOK)
            max_issues: Maximum number of Sentry issues to analyze
            project_slugs: Optional list of Sentry project slugs to filter

        Returns:
            BHunterRun with full results and attempt details
        """
        run = BHunterRun(
            trigger=trigger,
            status=RunStatus.RUNNING,
            config_snapshot={
                "llm_provider": self.config.llm_provider.value,
                "dry_run": self.config.dry_run,
                "max_issues": max_issues,
                "min_occurrences": self.config.min_occurrences,
                "max_prs_per_run": self.config.max_prs_per_run,
            },
        )

        logger.info(
            "Pipeline started (run_id=%s trigger=%s dry_run=%s provider=%s)",
            str(run.id),
            trigger.value,
            self.config.dry_run,
            self.config.llm_provider.value,
        )

        try:
            # Check daily PR limit
            if get_prs_created_today(self.config) >= self.config.max_prs_per_day:
                logger.warning(
                    "Daily PR limit reached (%d/%d)",
                    get_prs_created_today(self.config),
                    self.config.max_prs_per_day,
                )
                run.status = RunStatus.COMPLETED
                run.error_log = "Daily PR limit reached"
                run.finished_at = datetime.utcnow()
                _save_run(run, self.config)
                return run

            # Execute agent with MCP tools
            output = await self._run_agent(max_issues, project_slugs)

            # Log raw output for debugging (truncate to 2000 chars)
            output_preview = output[:2000] if output else "(empty)"
            logger.info("Agent raw output (preview): %s", output_preview)
            # Store full output in error_log for debugging purposes
            run.error_log = f"AGENT_OUTPUT_LEN={len(output)}\n---\n{output[:5000]}"

            # Parse results
            attempts = _parse_attempts_from_output(output)
            summary = _parse_summary(output)

            logger.info(
                "Parsed %d attempts, summary=%s",
                len(attempts),
                summary,
            )

            run.attempts = attempts
            run.issues_found = summary.get("ISSUES_ANALYZED", len(attempts))
            run.prs_created = summary.get("PRS_CREATED", 0)
            run.issues_created = summary.get("ISSUES_CREATED", 0)
            run.skipped = summary.get("SKIPPED", 0)
            run.failed = sum(
                1 for a in attempts if a.status == AttemptStatus.FAILED
            )
            run.status = RunStatus.COMPLETED

            logger.info(
                "Pipeline completed (issues=%d prs=%d issues_created=%d skipped=%d failed=%d)",
                run.issues_found,
                run.prs_created,
                run.issues_created,
                run.skipped,
                run.failed,
            )

        except Exception as exc:
            run.status = RunStatus.FAILED
            run.error_log = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            logger.error("Pipeline failed: %s", str(exc), exc_info=True)

        finally:
            run.finished_at = datetime.utcnow()
            _save_run(run, self.config)

        return run

    async def _run_agent(
        self,
        max_issues: int,
        project_slugs: list[str] | None,
    ) -> str:
        """
        Start MCP servers, create agent, run it, and return its output.

        This is the core execution that manages the lifecycle of MCP
        server subprocesses and the agent invocation.
        """
        prompt = _build_run_prompt(
            self.config,
            max_issues=max_issues,
            project_slugs=project_slugs,
        )

        # Create codebase toolkit (always available, no subprocess)
        codebase = CodebaseToolkit(config=self.config)

        # Start both MCP servers and inject as tools
        async with get_sentry_mcp(self.config) as sentry_mcp:
            if self.config.dry_run:
                # Dry run: no GitHub MCP needed
                agent = create_bhunter_agent(
                    config=self.config,
                    tools=[sentry_mcp, codebase],
                )
                response = await agent.arun(prompt)
                return response.content if hasattr(response, "content") else str(response)

            async with get_github_mcp(self.config) as github_mcp:
                agent = create_bhunter_agent(
                    config=self.config,
                    tools=[sentry_mcp, github_mcp, codebase],
                )
                response = await agent.arun(prompt)
                return response.content if hasattr(response, "content") else str(response)

    # -----------------------------------------------------------------------
    # Query methods
    # -----------------------------------------------------------------------

    def get_last_run(self) -> BHunterRun | None:
        """Get the most recent pipeline run."""
        runs = _read_all_runs(self.config)
        return runs[-1] if runs else None

    def get_run_history(self, limit: int = 10) -> list[BHunterRun]:
        """Get recent pipeline runs."""
        runs = _read_all_runs(self.config)
        return list(reversed(runs[-limit:]))

    def get_run_by_id(self, run_id: str) -> BHunterRun | None:
        """Get a specific run by ID."""
        for run in _read_all_runs(self.config):
            if str(run.id) == run_id:
                return run
        return None


def run_to_response(run: BHunterRun) -> BHunterRunResponse:
    """Convert a BHunterRun to an API response schema."""
    return BHunterRunResponse(
        id=run.id,
        trigger=run.trigger,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        issues_found=run.issues_found,
        prs_created=run.prs_created,
        issues_created=run.issues_created,
        skipped=run.skipped,
        failed=run.failed,
        duration_seconds=run.duration_seconds,
        total_tokens=run.total_tokens,
        attempts=run.attempts,
        error_log=run.error_log,
        config_snapshot=run.config_snapshot,
    )


__all__ = ["BHunterPipeline", "run_to_response", "get_prs_created_today"]
