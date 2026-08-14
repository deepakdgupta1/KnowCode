"""Main entry point for the KnowCode FastAPI server."""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI

from . import api
from knowcode.api.lifecycle import DEFAULT_SHUTDOWN_TIMEOUT, ServerResources
from knowcode.service import KnowCodeService
from knowcode.utils.logger import get_logger

logger = get_logger(__name__)


def create_app(
    store_path: str = ".",
    watch: bool = False,
    *,
    shutdown_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT,
) -> FastAPI:
    """Create and configure the FastAPI application.

    The watch worker and file monitor are started by the ASGI lifespan rather
    than here, so an app that is built but never run owns no threads, and every
    app that *is* run is guaranteed a matching teardown (Step 17).

    Args:
        store_path: File or directory containing the knowledge store.
        watch: Whether to enable background file watching and re-indexing.
        shutdown_timeout: Upper bound on the whole shutdown sequence.

    Returns:
        Configured FastAPI application instance.
    """
    service = KnowCodeService(store_path=store_path, strict_config=True)
    watch_root = (
        Path(store_path).parent if Path(store_path).is_file() else Path(store_path)
    )
    resources = ServerResources(
        service,
        watch=watch,
        watch_root=watch_root,
        shutdown_timeout=shutdown_timeout,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Start owned resources, and release them in one documented order."""
        resources.startup()
        app.state.bg_indexer = resources.worker
        app.state.monitor = resources.monitor
        try:
            yield
        finally:
            report = resources.shutdown()
            app.state.shutdown_report = report
            app.state.monitor = resources.monitor
            if api._service is service:
                # A closed service must not stay reachable by a later request.
                api._service = None
            if not report.completed:
                logger.warning(
                    "Server did not shut down cleanly: %s", report.as_dict()
                )

    app = FastAPI(
        title="KnowCode API",
        description="Local intelligence server for Codebase Knowledge Graph",
        version="2.0.0",
        lifespan=lifespan,
    )

    api._service = service
    app.state.resources = resources
    app.state.bg_indexer = None
    app.state.monitor = None
    app.state.shutdown_report = None

    app.include_router(api.router)

    # Attach rate limiting middleware (must be after router inclusion)
    from knowcode.api.rate_limit import setup_rate_limiting
    setup_rate_limiting(app)

    return app


def start_server(host: str = "127.0.0.1", port: int = 8000, store_path: str = ".", watch: bool = False) -> None:
    """Start the uvicorn server with the configured app.

    Args:
        host: Bind address for the server.
        port: Port to listen on.
        store_path: Knowledge store location used by the API.
        watch: Whether to enable background file monitoring.
    """
    app = create_app(store_path=store_path, watch=watch)
    uvicorn.run(app, host=host, port=port)
