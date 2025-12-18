"""Main entry point for KnowCode server."""

import uvicorn
from fastapi import FastAPI

from knowcode.server import api
from knowcode.service import KnowCodeService

def create_app(store_path: str = ".") -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="KnowCode API",
        description="Local intelligence server for Codebase Knowledge Graph",
        version="2.0.0",
    )

    # Initialize service
    api._service = KnowCodeService(store_path=store_path)

    app.include_router(api.router)

    return app

def start_server(host: str = "127.0.0.1", port: int = 8000, store_path: str = "."):
    """Start the uvicorn server."""
    app = create_app(store_path=store_path)
    uvicorn.run(app, host=host, port=port)
