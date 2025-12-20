"""Main entry point for the KnowCode FastAPI server."""

from pathlib import Path

import uvicorn
from fastapi import FastAPI

from . import api
from knowcode.service import KnowCodeService

def create_app(store_path: str = ".", watch: bool = False) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        store_path: File or directory containing the knowledge store.
        watch: Whether to enable background file watching and re-indexing.

    Returns:
        Configured FastAPI application instance.
    """
    app = FastAPI(
        title="KnowCode API",
        description="Local intelligence server for Codebase Knowledge Graph",
        version="2.0.0",
    )

    # Initialize service
    service = KnowCodeService(store_path=store_path)
    api._service = service
    
    if watch:
        from knowcode.indexing.background_indexer import BackgroundIndexer
        from knowcode.indexing.monitor import FileMonitor
        
        indexer = service.get_indexer()
        bg_indexer = BackgroundIndexer(indexer)
        bg_indexer.start()
        
        # Determine watch root (directory where store is, or current dir)
        watch_root = Path(store_path).parent if Path(store_path).is_file() else Path(store_path)
        monitor = FileMonitor(watch_root, bg_indexer)
        monitor.start()
        
        # Store on app state to keep alive
        app.state.monitor = monitor
        app.state.bg_indexer = bg_indexer

    app.include_router(api.router)

    return app

def start_server(host: str = "127.0.0.1", port: int = 8000, store_path: str = ".", watch: bool = False):
    """Start the uvicorn server with the configured app.

    Args:
        host: Bind address for the server.
        port: Port to listen on.
        store_path: Knowledge store location used by the API.
        watch: Whether to enable background file monitoring.
    """
    app = create_app(store_path=store_path, watch=watch)
    uvicorn.run(app, host=host, port=port)
