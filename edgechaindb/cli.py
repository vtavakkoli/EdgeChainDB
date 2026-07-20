from __future__ import annotations

import argparse
import json

import uvicorn

from .ledger import EdgeChainLedger
from .store import Database


def verify_main() -> None:
    parser = argparse.ArgumentParser(description="Verify an EdgeChainDB database")
    parser.add_argument("database")
    args = parser.parse_args()
    result = EdgeChainLedger(Database(args.database)).verify_all()
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["valid"] else 1)


def api_main() -> None:
    parser = argparse.ArgumentParser(description="Run the EdgeChainDB API")
    parser.add_argument("--database", default="edgechain.db")
    parser.add_argument("--node-key", default="edgechain-node.key")
    parser.add_argument("--node-id", default="edge-gateway-1")
    parser.add_argument("--quorum", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    from .api import create_app

    app = create_app(
        database_path=args.database,
        node_key_path=args.node_key,
        node_id=args.node_id,
        quorum_threshold=args.quorum,
        batch_size=args.batch_size,
    )
    uvicorn.run(app, host=args.host, port=args.port)
