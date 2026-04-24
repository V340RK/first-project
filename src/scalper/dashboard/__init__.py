"""Dashboard — FastAPI веб-інтерфейс для спостереження за ботом у реальному часі.

Джерело правди — journal JSONL файли. Дашборд їх тейлить і стрімить у браузер через WebSocket.
Жодної прямої залежності від pipeline-модулів: дашборд вмикається після них як звичайний підписник.
"""

from scalper.dashboard.config import DashboardConfig
from scalper.dashboard.server import DashboardServer, create_app
from scalper.dashboard.tailer import JournalTailer

__all__ = ["DashboardConfig", "DashboardServer", "JournalTailer", "create_app"]
