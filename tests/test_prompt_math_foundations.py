"""Regression tests for self-improvement prompt contracts (brain trader retired)."""
from __future__ import annotations
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import self_improvement_engine


class PromptMathFoundationsTests(unittest.TestCase):
    def test_evolution_prompt_forbids_unobserved_math_attribution(self):
        prompt = self_improvement_engine.EVOLUTION_SYSTEM_PROMPT
        self.assertIn("数理快照不可观测", prompt)
        self.assertIn("NO_CHANGE", prompt)
        self.assertIn("不得编造", prompt)

    def test_no_change_preserves_existing_memory(self):
        status, lessons, preserved = self_improvement_engine.resolve_memory_update("NO_CHANGE", [], ["existing lesson"])
        self.assertEqual(status, "NO_CHANGE")
        self.assertEqual(lessons, ["existing lesson"])
        self.assertTrue(preserved)
        status, lessons, preserved = self_improvement_engine.resolve_memory_update("ADD", ["new lesson"], ["old"])
        self.assertEqual(lessons, ["new lesson"])
        self.assertFalse(preserved)


if __name__ == "__main__":
    unittest.main()
