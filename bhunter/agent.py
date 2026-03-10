"""
BHunter Agent

Autonomous Agno Agent that:
1. Queries Sentry MCP for unresolved production errors
2. Reads affected source files via codebase toolkit
3. Analyzes root cause and severity
4. Generates minimal, safe fixes
5. Creates PRs (or Issues for complex bugs) via GitHub MCP

Supports two LLM backends:
- GitHub Models (Copilot Pro+) -- free with plan
- OpenRouter             -- existing infra, pay-per-use

All credentials are read from BHunterConfig -- no global settings.
"""

from __future__ import annotations

import logging

from agno.agent import Agent
from agno.models.openai import OpenAIChat
from agno.models.openrouter import OpenRouter

from bhunter.config import BHunterConfig, LLMProvider

logger = logging.getLogger("bhunter.agent")

# ---------------------------------------------------------------------------
# Agent instructions (system prompt)
# ---------------------------------------------------------------------------

BHUNTER_INSTRUCTIONS = """\
You are BHunter, an autonomous software engineer specialized in detecting
and fixing production bugs. You work surgically: small, safe, well-tested
changes with clear explanations.

## Your Workflow

### Phase 1 -- DETECT
Use the Sentry MCP tools to find unresolved issues in production:
1. Call `search_issues` for unresolved issues sorted by event count.
2. For each issue, call `get_issue_details` to get the full stacktrace,
   affected users, and frequency.
3. Skip issues with fewer than {min_occurrences} occurrences.
4. Prioritize by: user impact > frequency > recency.

### Phase 2 -- ANALYZE
For each selected issue:
1. Read the stacktrace to identify affected file(s) and line(s).
2. Use the codebase_analyzer tools to read those files and surrounding context.
3. Identify the root cause (null reference, type error, missing validation, etc.).
4. Classify the severity:
   - TRIVIAL: typo, missing null check, simple off-by-one, wrong status code
   - MEDIUM: logic error in single function, missing validation, incorrect condition
   - COMPLEX: multi-file issue, architectural problem, race condition, data model issue

### Phase 3 -- FIX (TRIVIAL and MEDIUM only)
Generate a minimal code fix:
1. Change the fewest lines possible.
2. Never modify more than {max_files_per_fix} files.
3. Never change more than {max_lines_changed} lines total.
4. Preserve existing code style and patterns.
5. Add brief inline comments explaining the fix.
6. NEVER modify blocked files (config, migrations, dockerfiles, etc.).

### Phase 4 -- PR / ISSUE
- For TRIVIAL/MEDIUM: create a branch and PR via GitHub MCP with:
  - Clear title: "fix: [concise description]"
  - Body with: Sentry issue link, root cause, what changed, testing notes
  - Labels: bugfix, auto-generated, bhunter
- For COMPLEX: create a GitHub Issue instead with full analysis and recommendations.

## Safety Rules (MANDATORY)
- NEVER modify database schemas, migrations, or seed data.
- NEVER change authentication, authorization, or security logic.
- NEVER modify environment variables or secrets.
- NEVER change API contracts (request/response schemas).
- NEVER modify Docker, CI/CD, or infrastructure files.
- NEVER add new dependencies.
- NEVER remove existing tests.
- If in doubt, create an Issue instead of a PR.
- Always explain your reasoning step by step.

## Output Format
After each bug analysis, output a structured summary:
```
ISSUE: [Sentry issue ID]
TITLE: [Brief description]
SEVERITY: [TRIVIAL | MEDIUM | COMPLEX]
ROOT_CAUSE: [Explanation]
FILES_AFFECTED: [List of files]
ACTION: [PR_CREATED | ISSUE_CREATED | SKIPPED]
REASON: [Why this action was taken]
```
"""


def _get_model(config: BHunterConfig) -> OpenAIChat | OpenRouter:
    """
    Create the LLM model instance based on configured provider.

    Priority chain:
    1. GitHub Models (free with Copilot plan)
       Requires fine-grained PAT with Account Permission: user_models=read
    2. OpenRouter fallback -- low-cost (MiniMax-M2.5)

    Returns:
        Agno-compatible model
    """
    if config.llm_provider == LLMProvider.GITHUB_MODELS:
        token = config.github_token or ""
        if not token:
            logger.warning(
                "No GitHub token configured, falling back to OpenRouter"
            )
            return _get_openrouter_model(config)

        # Verify token has model access with a lightweight pre-flight check
        try:
            from openai import OpenAI
            test_client = OpenAI(
                base_url=config.github_models_base_url,
                api_key=token,
            )
            test_client.chat.completions.create(
                model=config.github_models_model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
            )
            logger.info(
                "GitHub Models pre-flight OK (model=%s)",
                config.github_models_model,
            )
        except Exception as verify_err:
            err_str = str(verify_err).lower()
            if "no_access" in err_str or "403" in err_str or "unknown_model" in err_str:
                logger.error(
                    "GitHub Models token lacks model access (403/no_access). "
                    "Your fine-grained PAT needs the 'Models' Account Permission "
                    "(user_models=read). This is under ACCOUNT permissions, NOT "
                    "repository permissions. Create one at: "
                    "https://github.com/settings/personal-access-tokens/new"
                    "?user_models=read "
                    "-- Falling back to OpenRouter (%s)",
                    config.openrouter_model,
                )
                return _get_openrouter_model(config)
            # Other errors (network, rate limit, etc.) -- still try with GitHub Models
            logger.warning(
                "GitHub Models pre-flight warning: %s. Proceeding anyway.",
                str(verify_err)[:120],
            )

        return OpenAIChat(
            id=config.github_models_model,
            api_key=token,
            base_url=config.github_models_base_url,
            timeout=config.analysis_timeout_seconds,
            retries=2,
            delay_between_retries=3,
            exponential_backoff=True,
        )

    return _get_openrouter_model(config)


def _get_openrouter_model(config: BHunterConfig) -> OpenRouter:
    """Create OpenRouter model. Uses config fields for credentials and branding."""
    return OpenRouter(
        id=config.openrouter_model,
        api_key=config.openrouter_api_key,
        timeout=180,
        retries=2,
        delay_between_retries=3,
        exponential_backoff=True,
        default_headers={
            "X-Title": config.openrouter_app_title,
            "HTTP-Referer": config.openrouter_app_url,
        },
    )


def create_bhunter_agent(
    config: BHunterConfig,
    tools: list | None = None,
) -> Agent:
    """
    Create the BHunter Agno Agent.

    Args:
        config: BHunter configuration with thresholds and provider settings.
        tools: List of Agno tools/toolkits (Sentry MCP, GitHub MCP, Codebase).
               These are injected by the pipeline orchestrator.

    Returns:
        Configured Agno Agent ready for autonomous bug hunting.
    """
    model = _get_model(config)

    # Format instructions with config values
    instructions = BHUNTER_INSTRUCTIONS.format(
        min_occurrences=config.min_occurrences,
        max_files_per_fix=config.max_files_per_fix,
        max_lines_changed=config.max_lines_changed,
    )

    # Add dry-run notice
    if config.dry_run:
        instructions += (
            "\n\n## DRY RUN MODE\n"
            "Do NOT create any PRs or Issues. Instead, output the full analysis "
            "and proposed fix as text. Prefix output with [DRY RUN].\n"
        )

    agent = Agent(
        name="BHunter",
        model=model,
        tools=tools or [],
        instructions=[instructions],
        markdown=True,
        debug_mode=False,
    )

    logger.info(
        "Agent created with provider=%s model=%s dry_run=%s",
        config.llm_provider.value,
        model.id if hasattr(model, "id") else "unknown",
        config.dry_run,
    )

    return agent


__all__ = ["create_bhunter_agent", "BHUNTER_INSTRUCTIONS"]
