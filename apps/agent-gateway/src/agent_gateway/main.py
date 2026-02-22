"""CLI entrypoint for running the agent gateway service."""

from __future__ import annotations

import os

import uvicorn

from agent_gateway.app import create_app



def main() -> None:
    host = os.getenv("HOST", "127.0.0.1")
    port_raw = os.getenv("PORT", "8081")

    try:
        port = int(port_raw)
    except ValueError:
        port = 8081

    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    main()
