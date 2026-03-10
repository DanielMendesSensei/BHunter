"""
BHunter Validators

Pre-flight safety checks applied before creating PRs:
  - File blocklist enforcement
  - Max files/lines changed
  - No schema/migration changes
  - No security-sensitive file modifications
  - Basic syntax validation (Python)

These validators act as safety nets on top of the agent's instructions.
Even if the agent ignores a rule, validators will catch it.

All imports are from the standalone bhunter package -- no global settings.
"""

from __future__ import annotations

import ast
import logging

from bhunter.config import BHunterConfig
from bhunter.models import FileChange, FixProposal

logger = logging.getLogger("bhunter.validators.safety")


def validate_fix(proposal: FixProposal, config: BHunterConfig) -> FixProposal:
    """
    Run all safety validators on a fix proposal.

    Populates proposal.validation_passed and proposal.validation_errors.

    Returns:
        The same proposal with validation results filled in.
    """
    errors: list[str] = []

    # 1. Check number of files
    if len(proposal.changes) > config.max_files_per_fix:
        errors.append(
            f"Too many files changed: {len(proposal.changes)} "
            f"(max {config.max_files_per_fix})"
        )

    # 2. Check total lines changed
    total_lines = sum(c.lines_changed for c in proposal.changes)
    if total_lines > config.max_lines_changed:
        errors.append(
            f"Too many lines changed: {total_lines} "
            f"(max {config.max_lines_changed})"
        )

    # 3. Check blocklist
    for change in proposal.changes:
        if config.is_file_blocked(change.filepath):
            errors.append(f"Blocked file modified: {change.filepath}")

    # 4. Check for dangerous patterns
    for change in proposal.changes:
        errs = _check_dangerous_patterns(change)
        errors.extend(errs)

    # 5. Python syntax check
    for change in proposal.changes:
        if change.filepath.endswith(".py") and change.new_content:
            err = _check_python_syntax(change.filepath, change.new_content)
            if err:
                errors.append(err)

    proposal.validation_errors = errors
    proposal.validation_passed = len(errors) == 0

    if errors:
        logger.warning("Fix validation failed: %s", "; ".join(errors))
    else:
        logger.info("Fix validation passed")

    return proposal


def _check_dangerous_patterns(change: FileChange) -> list[str]:
    """Check for patterns that should never appear in auto-generated fixes."""
    errors: list[str] = []
    content = change.new_content.lower()

    # Security-sensitive patterns
    dangerous = [
        ("os.system(", "Direct system command execution"),
        ("subprocess.call(", "Subprocess call without safety"),
        ("eval(", "Use of eval()"),
        ("exec(", "Use of exec()"),
        ("__import__(", "Dynamic import"),
        ("password", "Possible password/credential modification"),
        ("secret_key", "Possible secret key modification"),
        ("api_key", "Possible API key modification"),
        ("drop table", "SQL DROP TABLE statement"),
        ("truncate", "SQL TRUNCATE statement"),
        ("rm -rf", "Dangerous file system operation"),
    ]

    for pattern, description in dangerous:
        if pattern in content and pattern not in change.original_content.lower():
            # Only flag if the pattern is NEW (not already in original)
            errors.append(
                f"Dangerous pattern in {change.filepath}: {description}"
            )

    return errors


def _check_python_syntax(filepath: str, content: str) -> str | None:
    """Validate Python syntax. Returns error string or None."""
    try:
        ast.parse(content, filename=filepath)
        return None
    except SyntaxError as exc:
        return (
            f"Python syntax error in {filepath} "
            f"line {exc.lineno}: {exc.msg}"
        )


__all__ = ["validate_fix"]
