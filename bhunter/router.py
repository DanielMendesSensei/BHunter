"""
BHunter FastAPI Router Factory

Creates a ready-to-mount FastAPI APIRouter with admin endpoints for
managing the BHunter agent. The auth dependency is injectable,
so any host application can provide its own authentication.

Usage:
    from bhunter import create_router, BHunterConfig

    config = BHunterConfig(sentry_token="...", github_token="...")
    router = create_router(config, auth_dependency=get_current_admin)
    app.include_router(router, prefix="/api/bhunter")
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any, Callable

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status

from bhunter.config import BHunterConfig, LLMProvider, RunTrigger
from bhunter.models import (
    BHunterRunResponse,
    BHunterStatusResponse,
    BHunterTriggerRequest,
)
from bhunter.pipeline import BHunterPipeline, get_prs_created_today, run_to_response

logger = logging.getLogger("bhunter.router")


def _build_config_from_request(
    base_config: BHunterConfig,
    request: BHunterTriggerRequest | None = None,
) -> BHunterConfig:
    """Build a run-specific BHunterConfig from base config + request overrides."""
    provider = base_config.llm_provider
    if request and request.llm_provider == "openrouter":
        provider = LLMProvider.OPENROUTER

    config = BHunterConfig(
        # Credentials from base config
        sentry_token=base_config.sentry_token,
        sentry_host=base_config.sentry_host,
        sentry_org=base_config.sentry_org,
        github_token=base_config.github_token,
        openrouter_api_key=base_config.openrouter_api_key,
        openrouter_app_title=base_config.openrouter_app_title,
        openrouter_app_url=base_config.openrouter_app_url,
        project_root=base_config.project_root,
        store_dir=base_config.store_dir,
        enabled=base_config.enabled,
        # Run-specific overrides
        llm_provider=provider,
        dry_run=request.dry_run if request else False,
        min_occurrences=request.min_occurrences if request else base_config.min_occurrences,
        github_repo_owner=base_config.github_repo_owner,
        github_repo_name=base_config.github_repo_name,
        sentry_project_slugs=request.project_slugs if request else [],
    )

    # Model override
    if request and request.model:
        if provider == LLMProvider.OPENROUTER:
            config.openrouter_model = request.model
        elif provider == LLMProvider.GITHUB_MODELS:
            config.github_models_model = request.model

    return config


# Module-level active task tracker
_active_run_task: asyncio.Task | None = None


async def _run_pipeline_bg(
    config: BHunterConfig,
    max_issues: int,
    project_slugs: list[str],
) -> None:
    """Run BHunter pipeline as a background task."""
    global _active_run_task
    try:
        pipeline = BHunterPipeline(config)
        await pipeline.execute(
            trigger=RunTrigger.MANUAL,
            max_issues=max_issues,
            project_slugs=project_slugs or None,
        )
    except Exception as exc:
        logger.error("Background run failed: %s", str(exc), exc_info=True)
    finally:
        _active_run_task = None


def _no_auth() -> None:
    """Default no-op auth dependency (no authentication)."""
    return None


def create_router(
    config: BHunterConfig,
    auth_dependency: Callable[..., Any] | None = None,
    prefix: str = "/bhunter",
    tags: list[str] | None = None,
) -> APIRouter:
    """
    Create a FastAPI APIRouter with BHunter admin endpoints.

    Args:
        config: Base BHunterConfig with credentials and defaults.
        auth_dependency: Optional FastAPI dependency for authentication.
                        If None, endpoints are unprotected.
        prefix: URL prefix for the router (default: "/bhunter").
        tags: OpenAPI tags for the endpoints.

    Returns:
        FastAPI APIRouter ready to be included in any app.
    """
    router = APIRouter(
        prefix=prefix,
        tags=tags or ["BHunter"],
    )

    # Use provided auth or no-op
    auth = auth_dependency or _no_auth
    pipeline = BHunterPipeline(config)

    @router.post(
        "/trigger",
        response_model=dict,
        status_code=status.HTTP_202_ACCEPTED,
        summary="Trigger BHunter run",
    )
    async def trigger_bhunter(
        request: BHunterTriggerRequest,
        background_tasks: BackgroundTasks,
        _user: Any = Depends(auth),
    ) -> dict:
        """Manually trigger a BHunter pipeline run (background)."""
        if not config.enabled:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="BHunter is not enabled.",
            )

        global _active_run_task
        if _active_run_task and not _active_run_task.done():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A BHunter run is already in progress.",
            )

        daily_limit = config.max_prs_per_day
        if get_prs_created_today(config) >= daily_limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Daily PR limit reached ({daily_limit}). Try again tomorrow.",
            )

        run_config = _build_config_from_request(config, request)

        logger.info(
            "Manual trigger (dry_run=%s, provider=%s)",
            request.dry_run,
            request.llm_provider,
        )

        _active_run_task = asyncio.create_task(
            _run_pipeline_bg(
                config=run_config,
                max_issues=request.max_issues,
                project_slugs=request.project_slugs,
            )
        )

        return {
            "message": "BHunter run started",
            "dry_run": request.dry_run,
            "max_issues": request.max_issues,
            "provider": request.llm_provider,
        }

    @router.get(
        "/status",
        response_model=BHunterStatusResponse,
        summary="Get BHunter status",
    )
    async def get_bhunter_status(
        _user: Any = Depends(auth),
    ) -> BHunterStatusResponse:
        """Get current BHunter service status including last run details."""
        last_run = pipeline.get_last_run()

        return BHunterStatusResponse(
            enabled=config.enabled,
            last_run=run_to_response(last_run) if last_run else None,
            prs_created_today=get_prs_created_today(config),
            daily_limit=config.max_prs_per_day,
            sentry_connected=bool(config.sentry_token),
            github_connected=bool(config.github_token),
        )

    @router.get(
        "/runs",
        response_model=list[BHunterRunResponse],
        summary="List BHunter runs",
    )
    async def list_bhunter_runs(
        limit: Annotated[int, Query(ge=1, le=50)] = 10,
        _user: Any = Depends(auth),
    ) -> list[BHunterRunResponse]:
        """List recent BHunter pipeline runs."""
        runs = pipeline.get_run_history(limit=limit)
        return [run_to_response(r) for r in runs]

    @router.get(
        "/runs/{run_id}",
        response_model=BHunterRunResponse,
        summary="Get BHunter run details",
    )
    async def get_bhunter_run(
        run_id: str,
        _user: Any = Depends(auth),
    ) -> BHunterRunResponse:
        """Get details of a specific BHunter run."""
        run = pipeline.get_run_by_id(run_id)
        if not run:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Run {run_id} not found",
            )
        return run_to_response(run)

    return router


__all__ = ["create_router"]
