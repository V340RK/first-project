"""CLI: `python -m scalper.dashboard --journal-dir <path> [--host ...] [--port ...]`.

Запускає uvicorn з FastAPI додатком. Окремий процес від бота — дашборд читає файли журналу.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import uvicorn

from scalper.dashboard.account import BinanceAccountService
from scalper.dashboard.config import DashboardConfig
from scalper.dashboard.controller import BotRegistry
from scalper.dashboard.server import create_app
from scalper.dashboard.symbols import BinanceSymbolService


def main() -> None:
    parser = argparse.ArgumentParser(prog="scalper.dashboard")
    parser.add_argument("--journal-dir", type=Path, default=Path("journal"),
                        help="Каталог із JSONL журналами (за замовчанням ./journal)")
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--poll-interval-ms", type=int, default=150)
    parser.add_argument("--backfill-lines", type=int, default=200)
    parser.add_argument("--log-level", type=str, default="info")
    parser.add_argument("--runtime-configs-dir", type=Path, default=Path("configs"),
                        help="Папка для runtime_{SYMBOL}.yaml — по одному на пару")
    parser.add_argument("--no-controller", action="store_true",
                        help="Read-only режим — UI без кнопок старт/стоп")
    parser.add_argument("--binance-base-url", type=str, default=None,
                        help="Base URL для exchangeInfo. За замовчанням — з .env "
                             "(BINANCE_TESTNET=true → testnet, інакше prod)")
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

    registry = None
    if not args.no_controller:
        registry = BotRegistry(
            project_root=Path.cwd(),
            runtime_configs_dir=args.runtime_configs_dir,
        )

    # Зчитуємо .env, щоб знати testnet flag + API ключі для AccountService
    from dotenv import load_dotenv
    import os as _os
    load_dotenv(override=False)
    testnet = _os.environ.get("BINANCE_TESTNET", "true").strip().lower() in ("1", "true", "yes", "on")
    base_url = args.binance_base_url or (
        "https://testnet.binancefuture.com" if testnet
        else "https://fapi.binance.com"
    )
    symbol_service = BinanceSymbolService(base_url)

    # AccountService потребує API key/secret + спільний транспорт зі timing sync.
    account_service: BinanceAccountService | None = None
    api_key = _os.environ.get("BINANCE_API_KEY", "").strip()
    api_secret = _os.environ.get("BINANCE_API_SECRET", "").strip()
    if api_key and api_secret:
        from pydantic import SecretStr
        from scalper.gateway.config import GatewayConfig
        from scalper.gateway.transport import _RestTransport
        gw_cfg = GatewayConfig(
            testnet=testnet, base_url=base_url,
            ws_url=("wss://stream.binancefuture.com" if testnet else "wss://fstream.binance.com"),
            api_key=SecretStr(api_key), secret_key=SecretStr(api_secret),
        )
        rest = _RestTransport(gw_cfg)
        account_service = BinanceAccountService(rest)
    else:
        print("WARNING: BINANCE_API_KEY/SECRET відсутні — баланс не доступний")

    app = create_app(
        config, registry=registry, symbol_service=symbol_service,
        account_service=account_service,
    )
    uvicorn.run(app, host=config.host, port=config.port, log_level=args.log_level.lower())


if __name__ == "__main__":
    main()
