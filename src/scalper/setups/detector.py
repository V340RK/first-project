"""SetupDetector — обгортка над списком SetupRule. Pure stateless.

⚠ НЕ викликає Journal сам. Логування `SETUP_CANDIDATE_GENERATED` робить Orchestrator.
"""

from __future__ import annotations

from scalper.common.types import SetupCandidate
from scalper.features.types import Features
from scalper.setups.base import SetupRule


class SetupDetector:
    def __init__(self, rules: list[SetupRule]) -> None:
        self._rules = rules

    def detect(self, features: Features) -> list[SetupCandidate]:
        """Прогнати features через всі rule-и. Повертає всіх кандидатів (0..N).

        Порядок збережений (як rules передані в __init__), але DecisionEngine все одно
        вибирає найкращого по score, тому не покладайтеся на порядок.
        """
        out: list[SetupCandidate] = []
        for rule in self._rules:
            try:
                cand = rule.check(features)
            except Exception:
                # Один зламаний rule не повинен ронити весь pipeline
                continue
            if cand is not None:
                out.append(cand)
        return out
