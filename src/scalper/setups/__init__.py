"""06 Setup Detector — pure stateless перевірка фіч на 4-5 сетапів.

Див. DOCS/architecture/06-setup-detector.md.
"""

from scalper.setups.base import SetupRule
from scalper.setups.config import SetupConfig
from scalper.setups.detector import SetupDetector
from scalper.setups.rules import default_rules

__all__ = ["SetupConfig", "SetupDetector", "SetupRule", "default_rules"]
