"""CLI: `python -m scalper.dashboard --journal-dir <path> [--host ...] [--port ...]`.

Запускає uvicorn з FastAPI додатком. Окремий процес від бота — дашборд читає файли журналу.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import uvicorn

from scalper.dashboard.config import DashboardConfig
from scalper.dashboard.server import create_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="scalper.dashboard")
    parser.add_argument("--journal-dir", type=Path, required=True, help="Каталог із JSONL журналами")
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--poll-interval-ms", type=int, default=150)
    parser.add_argument("--backfill-lines", type=int, default=200)
    parser.add_argument("--log-level", type=str, default="info")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = DashboardConfig(
        journal_dir=args.journal_dir,
        host=args.host,
        port=args.port,
        poll_interval_ms=args.poll_interval_ms,
        backfill_lines=args.backfill_lines,
    )
    config.journal_dir.mkdir(parents=True, exist_ok=True)

    app = create_app(config)
    uvicorn.run(app, host=config.host, port=config.port, log_level=args.log_level.lower())


if __name__ == "__main__":
    main()
