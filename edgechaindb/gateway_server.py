from __future__ import annotations

import argparse
import asyncio
from contextlib import nullcontext
import os
import signal
from typing import Any

import uvicorn

from .api import create_app
from .observability import get_logger

log = get_logger("gateway-launcher")


def _config(app: Any, host: str, port: int, log_level: str) -> uvicorn.Config:
    return uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level=log_level.lower(),
        access_log=False,
        server_header=False,
        date_header=False,
    )


async def serve_dual_port(
    *,
    database_path: str,
    node_key_path: str,
    node_id: str,
    quorum_threshold: int,
    batch_size: int,
    authority_count: int,
    authority_key_dir: str,
    host: str,
    api_port: int,
    monitor_port: int,
    log_level: str,
) -> None:
    """Run one shared gateway application on API and monitor ports.

    Both listeners use the same in-process ledger, locks, Docker controller, and
    database object. This avoids the fragile host-port alias previously used for
    port 3030 and makes the monitor a real listener inside the gateway container.
    """

    app = create_app(
        database_path=database_path,
        node_key_path=node_key_path,
        node_id=node_id,
        quorum_threshold=quorum_threshold,
        batch_size=batch_size,
        authority_count=authority_count,
        authority_key_dir=authority_key_dir,
    )
    app.state.api_port = api_port
    app.state.monitor_port = monitor_port

    api_server = uvicorn.Server(_config(app, host, api_port, log_level))
    monitor_server = uvicorn.Server(_config(app, host, monitor_port, log_level))

    # Uvicorn normally installs one signal handler per server. Two concurrently
    # running servers would overwrite each other, so the launcher owns signals.
    api_server.capture_signals = lambda: nullcontext()  # type: ignore[method-assign]
    monitor_server.capture_signals = lambda: nullcontext()  # type: ignore[method-assign]

    loop = asyncio.get_running_loop()

    def request_shutdown() -> None:
        log.info("gateway_shutdown_requested")
        api_server.should_exit = True
        monitor_server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_shutdown)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, lambda *_: request_shutdown())

    log.info(
        "gateway_listeners_starting",
        host=host,
        api_port=api_port,
        monitor_port=monitor_port,
        database_path=database_path,
    )

    api_task = asyncio.create_task(api_server.serve(), name="edgechain-api-listener")
    monitor_task = asyncio.create_task(
        monitor_server.serve(), name="edgechain-monitor-listener"
    )
    tasks = {api_task, monitor_task}

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    unexpected: list[str] = []
    for task in done:
        try:
            result = task.result()
            unexpected.append(f"{task.get_name()} stopped: {result!r}")
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # pragma: no cover - defensive process boundary
            unexpected.append(f"{task.get_name()} failed: {type(exc).__name__}: {exc}")

    api_server.should_exit = True
    monitor_server.should_exit = True
    await asyncio.gather(*pending, return_exceptions=True)

    if unexpected and not (api_server.should_exit and monitor_server.should_exit):
        raise RuntimeError("; ".join(unexpected))
    if unexpected:
        log.info("gateway_listeners_stopped", details=unexpected)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run EdgeChainDB API and live monitor on separate ports"
    )
    parser.add_argument("--database", default="edgechain.db")
    parser.add_argument("--node-key", default="edgechain-node.key")
    parser.add_argument("--node-id", default="edge-gateway-1")
    parser.add_argument("--quorum", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--authorities", type=int, default=int(os.getenv("EDGECHAIN_AUTHORITIES", "1")))
    parser.add_argument("--authority-key-dir", default=os.getenv("EDGECHAIN_AUTHORITY_KEY_DIR", "/data/authorities"))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--api-port", type=int, default=8000)
    parser.add_argument("--monitor-port", type=int, default=3030)
    parser.add_argument(
        "--log-level", default=os.getenv("EDGECHAIN_LOG_LEVEL", "INFO")
    )
    args = parser.parse_args()

    if args.api_port == args.monitor_port:
        parser.error("--api-port and --monitor-port must be different")

    asyncio.run(
        serve_dual_port(
            database_path=args.database,
            node_key_path=args.node_key,
            node_id=args.node_id,
            quorum_threshold=args.quorum,
            batch_size=args.batch_size,
            authority_count=args.authorities,
            authority_key_dir=args.authority_key_dir,
            host=args.host,
            api_port=args.api_port,
            monitor_port=args.monitor_port,
            log_level=args.log_level,
        )
    )


if __name__ == "__main__":
    main()
