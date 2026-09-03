"""
Keel Trader - A minimal, maintainable crypto trading framework.

Named after a ship's keel: the small structural core that everything else attaches to.
The design philosophy prioritizes:
- Small core with swappable adapters
- Honest naming (no marketing fluff)
- SQLite ledger over JSON file IPC
- Single scheduler owner
- Hard risk gates independent of LLM
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
