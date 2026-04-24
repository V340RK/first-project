"""JournalConfig — pydantic-конфіг для Journal Logger."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel


class JournalConfig(BaseModel):
    journal_dir: Path                                # куди писати *.jsonl
    batch_max: int = 100                             # подій за один write
    flush_interval_ms: int = 200                     # таймаут drain з черги
    fsync_interval_ms: int = 250
    fsync_bytes_threshold: int = 65536               # або після 64KB, що раніше
    queue_size: int = 10000
    gzip_old_files: bool = True
    retention_days: int = 90                         # gz старші — кандидати на видалення
