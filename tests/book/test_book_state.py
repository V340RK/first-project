"""_BookState: apply_diff, sequence check, warmup replay rules."""

from __future__ import annotations

import pytest

from scalper.book.book_state import SequenceGapError, _BookState
from scalper.gateway.types import DepthSnapshot, RawDepthDiff


def _snap(last_id: int, bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> DepthSnapshot:
    return DepthSnapshot(
        symbol="BTCUSDT", last_update_id=last_id, bids=bids, asks=asks, timestamp_ms=1000,
    )


def _diff(U: int, u: int, bids=(), asks=()) -> RawDepthDiff:
    return RawDepthDiff(symbol="BTCUSDT", first_update_id=U, final_update_id=u,
                         bids=list(bids), asks=list(asks))


def test_load_snapshot_seeds_book() -> None:
    book = _BookState(symbol="BTCUSDT")
    book.load_snapshot(_snap(100, [(100.0, 1.0), (99.0, 2.0)], [(101.0, 0.5), (102.0, 1.0)]))
    assert book.bids == {100.0: 1.0, 99.0: 2.0}
    assert book.asks == {101.0: 0.5, 102.0: 1.0}
    assert book.last_update_id == 100
    assert book.initialized is False  # стане True після replay


def test_snapshot_skips_zero_qty() -> None:
    book = _BookState(symbol="X")
    book.load_snapshot(_snap(1, [(10.0, 0.0), (9.0, 1.0)], [(11.0, 2.0)]))
    assert 10.0 not in book.bids
    assert book.bids[9.0] == 1.0


def test_apply_diff_upsert_and_remove() -> None:
    book = _BookState(symbol="X")
    book.load_snapshot(_snap(100, [(100.0, 1.0)], [(101.0, 1.0)]))
    book.initialized = True
    book.apply_diff(_diff(101, 105, bids=[(100.0, 0.0), (99.0, 3.0)], asks=[(101.0, 2.0)]))
    assert 100.0 not in book.bids
    assert book.bids[99.0] == 3.0
    assert book.asks[101.0] == 2.0
    assert book.last_update_id == 105


def test_apply_diff_detects_gap() -> None:
    book = _BookState(symbol="X")
    book.load_snapshot(_snap(100, [], []))
    book.initialized = True
    # Очікуємо U=101, а прийшов U=103 → gap
    with pytest.raises(SequenceGapError):
        book.apply_diff(_diff(103, 105, bids=[(50.0, 1.0)]))


def test_apply_warmup_skips_older_than_snap() -> None:
    book = _BookState(symbol="X")
    book.load_snapshot(_snap(100, [], []))
    applied = book.apply_warmup_diff(_diff(80, 95), snap_last_id=100)
    assert applied is False
    assert book.last_update_id == 100  # нічого не змінилось


def test_apply_warmup_first_valid_accepted() -> None:
    book = _BookState(symbol="X")
    book.load_snapshot(_snap(100, [], []))
    # U=99, u=105 → містить 101 → перший валідний
    applied = book.apply_warmup_diff(_diff(99, 105, bids=[(50.0, 1.0)]), snap_last_id=100)
    assert applied is True
    assert book.last_update_id == 105


def test_apply_warmup_first_rejected_if_too_new() -> None:
    book = _BookState(symbol="X")
    book.load_snapshot(_snap(100, [], []))
    # U=150 > 101 → snapshot занадто старий
    with pytest.raises(SequenceGapError):
        book.apply_warmup_diff(_diff(150, 160), snap_last_id=100)


def test_warmup_sequence_enforced_after_first() -> None:
    book = _BookState(symbol="X")
    book.load_snapshot(_snap(100, [], []))
    book.apply_warmup_diff(_diff(99, 105), snap_last_id=100)
    # Очікуємо U=106, а прийшов U=108
    with pytest.raises(SequenceGapError):
        book.apply_warmup_diff(_diff(108, 110), snap_last_id=100)


def test_top_snapshot_sorted_and_truncated() -> None:
    book = _BookState(symbol="X")
    book.load_snapshot(_snap(1,
        bids=[(100.0, 1), (99.0, 2), (98.0, 3), (97.0, 4)],
        asks=[(101.0, 1), (102.0, 2), (103.0, 3)],
    ))
    book.initialized = True
    snap = book.top_snapshot(depth=2, timestamp_ms=5000)
    assert [lv.price for lv in snap.bids] == [100.0, 99.0]
    assert [lv.price for lv in snap.asks] == [101.0, 102.0]
    assert snap.is_synced is True
    assert snap.best_bid == 100.0 and snap.best_ask == 101.0
    assert snap.spread == pytest.approx(1.0)
    assert snap.mid_price == pytest.approx(100.5)
