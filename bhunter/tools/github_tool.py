"""
GitHub MCP Tool wrapper for BHunter.

Connects to @modelcontextprotocol/server-github via Agno MCPTools to:
  - create_or_update_file: Commit fix to a branch
  - create_branch: Create bugfix branch
  - create_pull_request: Open PR with analysis + fix
  - create_issue: Open issue for complex bugs
  - add_issue_comment: Add context to existing issues

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

logger = logging.getLogger("bhunter.tools.github")


def _get_github_env(config: BHunterConfig) -> dict[str, str]:
    """Build environment variables for the GitHub MCP server process."""
    return {
        "GITHUB_PERSONAL_ACCESS_TOKEN": config.github_token,
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
    }


@asynccontextmanager
async def get_github_mcp(config: BHunterConfig) -> AsyncGenerator[MCPTools, None]:
    """
    Context manager that starts and yields an MCPTools instance
    connected to the GitHub MCP server.

    Usage:
        async with get_github_mcp(config) as github:
            agent = Agent(tools=[github])
    """
    env = _get_github_env(config)

    logger.info(
        "Starting GitHub MCP server (timeout=%ds)",
        config.mcp_timeout_seconds,
    )

    mcp = MCPTools(
        command="npx @modelcontextprotocol/server-github",
        env=env,
        timeout_seconds=config.mcp_timeout_seconds,
    )

    try:
        async with mcp:
            logger.info("GitHub MCP server connected")
            yield mcp
    except Exception as exc:
        logger.error("GitHub MCP server failed: %s", str(exc), exc_info=True)
        raise
    finally:
        logger.info("GitHub MCP server stopped")
