"""Every Loopcut operator says what it does: Blender shows "(undocumented operator)." on hover for one
with neither bl_description nor a docstring, which a first-run tester read as an error (0.2.16 and 0.2.17
gate runs, the credits screen's Continue next to See plans)."""

import ast
import unittest
from pathlib import Path

ADDON = Path(__file__).resolve().parents[2] / "scripts" / "addons_core" / "loopcut"


def undescribed():
    found = []
    for path in sorted(ADDON.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ClassDef):
                continue
            names = {t.id for b in node.body if isinstance(b, ast.Assign) for t in b.targets if isinstance(t, ast.Name)}
            described = "bl_description" in names or ast.get_docstring(node) or any(
                isinstance(b, ast.FunctionDef) and b.name == "description" for b in node.body)
            bases = {getattr(b, "attr", getattr(b, "id", "")) for b in node.bases}
            if "bl_idname" in names and "Operator" in bases and not described:
                found.append(f"{path.relative_to(ADDON)}:{node.lineno} {node.name}")
    return found


class OperatorDescriptionTest(unittest.TestCase):
    def test_every_operator_has_a_description(self):
        self.assertEqual(undescribed(), [])


if __name__ == "__main__":
    unittest.main()
