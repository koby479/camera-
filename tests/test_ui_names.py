"""A widget attribute assigned twice in one class (self.x = QPushButton(...) twice) silently throws the first
widget away: the button never appears. This once hid the 'play' button, so it is checked from the source."""
import ast
import unittest
from pathlib import Path

UI = Path(__file__).resolve().parent.parent / "app" / "ui"


class WidgetNameTests(unittest.TestCase):
    def test_no_widget_attribute_is_created_twice_in_a_class(self):
        problems = []
        for path in sorted(UI.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
                seen = {}
                for node in ast.walk(cls):
                    if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
                        continue
                    func = node.value.func
                    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                    if not name.startswith("Q"):
                        continue
                    for t in node.targets:
                        if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self":
                            if t.attr in seen:
                                problems.append(f"{path.name}: {cls.name}.{t.attr} lines {seen[t.attr]} and {node.lineno}")
                            seen[t.attr] = node.lineno
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
