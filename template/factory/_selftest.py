"""Installed consumer checks. No engine or provider is started."""
import ast
import contextlib
import io
import unittest
from pathlib import Path
import consumer

class InstalledTests(unittest.TestCase):
    def test_retired_decisions_are_inert(self):
        for action in ("accept", "level", "merge", "arm", "deploy"):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(consumer.refuse(action), 2)
        self.assertNotIn("tick", consumer.RETIRED)
    def test_all_installed_python_parses(self):
        root = Path(__file__).resolve().parent.parent
        for directory in (root / "factory", root / "harness"):
            for path in directory.rglob("*.py"):
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    def test_consumer_has_no_legacy_stage_api(self):
        for name in ("launch", "reconcile", "consume", "authorized", "validate_receipt"):
            self.assertFalse(hasattr(consumer, name))
    def test_codegraph_integration_is_refused_not_forwarded(self):
        self.assertNotIn("--codegraph", consumer.MANIFEST["capabilities"]["run"])
        with self.assertRaisesRegex(ValueError, "code_intelligence is no longer supported"):
            consumer.validate_settings({"code_intelligence": {"mode": "off"}})
        root = Path(__file__).resolve().parent.parent
        for flag in (["--codegraph", "required"], ["--codegraph=off"]):
            with self.assertRaisesRegex(ValueError, "--codegraph is not supported"):
                consumer.invoke(root, "status", flag)
        with self.assertRaisesRegex(ValueError, "Factory owns --cwd and --workflow-source"):
            consumer.invoke(root, "status", ["--cwd", "/tmp"])
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(consumer.invoke(root, "code-intelligence", ["enable", "--mode", "required"]), 2)
        self.assertIn("CodeGraph integration was removed", err.getvalue())

if __name__ == "__main__":
    import sys
    sys.argv = [sys.argv[0]]
    unittest.main(verbosity=2)
