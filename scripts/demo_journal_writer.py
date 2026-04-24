"""Демо-генератор подій у журнал — щоб побачити живий дашборд без бота.

Запуск:
  python scripts/demo_journal_writer.py --journal-dir ./tmp_journal --rate 5

Спробуй відкривши паралельно в іншому терміналі:
  python -m scalper.dashboard --journal-dir ./tmp_journal
"""

from __future__ import annotations

import argparse
import asyncio
import random
from pathlib import Path

from scalper.common.time import now_ms
from scalper.journal.config import JournalConfig
from scalper.journal.logger import JournalLogger
from scalper.journal.types import EventKind, JournalEvent

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
KIND_WEIGHTS = [
    (EventKind.HEARTBEAT, 30),
    (EventKind.SETUP_CANDIDATE_GENERATED, 20),
    (EventKind.DECISION_REJECTED, 15),
    (EventKind.DECISION_ACCEPTED, 5),
    (EventKind.POSITION_OPENED, 4),
    (EventKind.FILL, 4),
    (EventKind.POSITION_CLOSED, 4),
    (EventKind.TRADE_OUTCOME, 3),
    (EventKind.WARNING, 3),
    (EventKind.REGIME_CHANGED, 2),
    (EventKind.ERROR, 1),
]


def pick_kind() -> EventKind:
    kinds, weights = zip(*KIND_WEIGHTS, strict=True)
    return random.choices(kinds, weights=weights, k=1)[0]


def make_payload(kind: EventKind) -> dict[str, object]:
    if kind == EventKind.DECISION_REJECTED:
        return {"reason": random.choice(["min_score", "regime_blocked", "budget"]), "score": round(random.uniform(0.3, 0.9), 2)}
    if kind == EventKind.DECISION_ACCEPTED:
        return {"setup_type": random.choice(["absorption_reversal", "trap"]), "direction": random.choice(["LONG", "SHORT"]), "score": round(random.uniform(0.9, 1.8), 2)}
    if kind == EventKind.TRADE_OUTCOME:
        return {"realized_r": round(random.uniform(-1.0, 3.0), 2), "setup_type": "absorption_reversal"}
    if kind == EventKind.WARNING:
        return {"msg": random.choice(["WS reconnect", "late tick", "rate limit 80%"])}
    if kind == EventKind.ERROR:
        return {"msg": "parse failed", "code": "PARSE_ERR"}
    return {"lag_ms": random.randint(1, 50)}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--journal-dir", type=Path, required=True)
    parser.add_argument("--rate", type=float, default=2.0, help="подій на секунду")
    parser.add_argument("--duration", type=float, default=0.0, help="0 = нескінченно")
    args = parser.parse_args()

    args.journal_dir.mkdir(parents=True, exist_ok=True)
    config = JournalConfig(journal_dir=args.journal_dir, batch_max=20, flush_interval_ms=100)
    log = JournalLogger(config)
    await log.start()

    interval = 1.0 / max(args.rate, 0.01)
    start = asyncio.get_event_loop().time()
    try:
        while True:
            kind = pick_kind()
            trade_id = None
            if kind in {EventKind.DECISION_ACCEPTED, EventKind.POSITION_OPENED,
                         EventKind.POSITION_CLOSED, EventKind.TRADE_OUTCOME}:
                trade_id = f"t-{random.randint(1000, 9999)}"
            log.log(JournalEvent(
                seq=0,
                timestamp_ms=now_ms(),
                kind=kind,
                trade_id=trade_id,
                symbol=random.choice(SYMBOLS),
                payload=make_payload(kind),
            ))
            await asyncio.sleep(interval)
            if args.duration and (asyncio.get_event_loop().time() - start) > args.duration:
                break
    finally:
        await log.stop()


if __name__ == "__main__":
    asyncio.run(main())
