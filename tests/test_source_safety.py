"""Mistakes that only show up on Windows are checked from the source, so they are caught anywhere."""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class SourceSafetyTests(unittest.TestCase):
    def test_strftime_formats_are_plain_ascii(self):
        """datetime.strftime encodes its format with the Windows locale codec: an emoji or Hebrew letter in it
        raises UnicodeEncodeError (it crashed the player window). Put such text outside the format string."""
        bad = []
        for path in list((ROOT / "app").rglob("*.py")) + list((ROOT / "tools").rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ("strftime", "strptime") and node.args
                        and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
                        and not node.args[0].value.isascii()):
                    bad.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(bad, [])

    def test_the_check_would_catch_the_old_line(self):
        tree = ast.parse('x.strftime("\U0001f552 %Y")')
        call = tree.body[0].value
        self.assertFalse(call.args[0].value.isascii())


if __name__ == "__main__":
    unittest.main()
