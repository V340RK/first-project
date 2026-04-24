"""03 Tape Flow Analyzer — стрічка трейдів, rolling delta/CVD, gap detection.

Див. DOCS/architecture/03-tape-flow-analyzer.md.
"""

from scalper.tape.analyzer import TapeAnalyzer, TapeFlowAnalyzer
from scalper.tape.config import TapeConfig, TapeGapConfig, TapeWindowsConfig
from scalper.tape.types import TapeWindow, TapeWindowsState

__all__ = [
    "TapeAnalyzer",
    "TapeConfig",
    "TapeFlowAnalyzer",
    "TapeGapConfig",
    "TapeWindow",
    "TapeWindowsConfig",
    "TapeWindowsState",
]
