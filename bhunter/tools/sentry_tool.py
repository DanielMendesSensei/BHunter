"""
Sentry MCP Tool wrapper for BHunter.

Connects to @sentry/mcp-server via Agno MCPTools to:
  - search_issues: Find unresolved production errors
  - get_issue_details: Fetch stacktrace, breadcrumbs, user impact
  - find_organizations / find_projects: Discovery

The MCP server is started as a subprocess (npx) and communicates
via stdio transport.

All credentials are read from BHunterConfig -- no global settings.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from agno.tools.mcp import MCPTools

from bhunter.config import BHunterConfig

logger = logging.getLogger("bhunter.tools.sentry")


def _get_sentry_env(config: BHunterConfig) -> dict[str, str]:
    """Build environment variables for the Sentry MCP server process.

    NOTE: The Sentry MCP server has an embedded AI agent (Seer) that
    requires an OpenAI-compatible API key. We always use OpenRouter
    (OpenAI-compatible) for Seer when available.
    """
    env = {
        "SENTRY_ACCESS_TOKEN": config.sentry_token,
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
    }

    # LLM provider for Sentry's embedded Seer agent.
    if config.openrouter_api_key:
        env["EMBEDDED_AGENT_PROVIDER"] = "openai"
        env["OPENAI_API_KEY"] = config.openrouter_api_key
        env["OPENAI_BASE_URL"] = "https://openrouter.ai/api/v1"
        logger.debug("Sentry Seer agent configured with OpenRouter")
    else:
        logger.warning(
            "No OpenRouter API key available for Sentry Seer agent. "
            "Embedded agent analysis will likely fail."
        )

    # Self-hosted Sentry support
    if config.sentry_host:
        env["SENTRY_HOST"] = config.sentry_host

    # Sentry organization slug
    if config.sentry_org:
        env["SENTRY_ORG"] = config.sentry_org

    return env


@asynccontextmanager
async def get_sentry_mcp(config: BHunterConfig) -> AsyncGenerator[MCPTools, None]:
    """
    Context manager that starts and yields an MCPTools instance
    connected to the Sentry MCP server.

    Usage:
        async with get_sentry_mcp(config) as sentry:
            agent = Agent(tools=[sentry])
    """
    env = _get_sentry_env(config)

    logger.info(
        "Starting Sentry MCP server (timeout=%ds)",
        config.mcp_timeout_seconds,
    )

    mcp = MCPTools(
        command="npx @sentry/mcp-server@latest",
        env=env,
        timeout_seconds=config.mcp_timeout_seconds,
    )

    try:
        async with mcp:
            logger.info("Sentry MCP server connected")
            yield mcp
    except Exception as exc:
        logger.error("Sentry MCP server failed: %s", str(exc), exc_info=True)
        raise
    finally:
        logger.info("Sentry MCP server stopped")
