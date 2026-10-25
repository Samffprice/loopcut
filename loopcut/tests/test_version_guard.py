"""version_guard: a module whose file is from another Loopcut version than the running one is not loaded.
A throwaway package on disk stands in for the add-on; no Blender."""

import importlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

import loopcut  # noqa: E402
from loopcut import version_guard  # noqa: E402

INIT = 'bl_info = {\n    "name": "Stand-in",\n    "version": (%d, %d, %d),\n}\n'


class VersionGuardTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.package = f"guarded_{id(self)}"
        self.root = self.folder / self.package
        self.root.mkdir()
        self.stamp(0, 2, 18)
        for name in ("early", "lazy", "other"):
            (self.root / f"{name}.py").write_text(f"NAME = {name!r}\n", encoding="utf-8")
        sys.path.insert(0, str(self.folder))
        self.addCleanup(sys.path.remove, str(self.folder))
        importlib.import_module(self.package)
        self.started = version_guard._started
        version_guard._started = None
        self.addCleanup(setattr, version_guard, "_started", self.started)
        version_guard.register(self.package, self.root, (0, 2, 18))
        self.addCleanup(version_guard.unregister, self.package)
        self.addCleanup(self.forget)

    def stamp(self, *version):
        (self.root / "__init__.py").write_text(INIT % version, encoding="utf-8")

    def forget(self):
        for name in [n for n in sys.modules if n == self.package or n.startswith(self.package + ".")]:
            del sys.modules[name]

    def load(self, name):
        importlib.invalidate_caches()
        return importlib.import_module(f"{self.package}.{name}")

    def test_the_same_version_on_disk_loads_as_usual(self):
        self.assertEqual(self.load("lazy").NAME, "lazy")

    def test_a_module_first_loaded_after_an_update_is_refused_with_the_restart_message(self):
        early = self.load("early")
        self.stamp(0, 2, 19)  # The installer replaced the files under this Blender.
        with self.assertRaises(version_guard.RestartNeeded) as caught:
            self.load("lazy")
        self.assertEqual(str(caught.exception), "Loopcut was updated while Blender was open. "
                                                "Restart Blender to finish the update.")
        self.assertNotIn(f"{self.package}.lazy", sys.modules)
        self.assertIs(self.load("early"), early, "what is loaded already stays as it is")
        self.stamp(0, 2, 17)  # Or an older build put back: just as mixed.
        with self.assertRaises(version_guard.RestartNeeded):
            self.load("other")

    def test_from_import_raises_it_too_not_a_vaguer_import_error(self):
        self.stamp(0, 2, 19)
        with self.assertRaises(version_guard.RestartNeeded):
            exec(f"from {self.package} import lazy", {})

    def test_an_unreadable_version_lets_the_import_through(self):
        (self.root / "__init__.py").write_text("bl_info = {}\n", encoding="utf-8")  # Mid-copy, say.
        self.assertEqual(self.load("lazy").NAME, "lazy")
        (self.root / "__init__.py").unlink()
        self.assertEqual(self.load("other").NAME, "other")

    def test_a_reregister_after_a_reloaded_init_keeps_the_version_blender_started_with(self):
        # Blender reloads __init__.py when its file changed, not the modules it imported: the newer bl_info
        # must not make the old modules look current.
        self.stamp(0, 2, 19)
        version_guard.register(self.package, self.root, (0, 2, 19))
        with self.assertRaises(version_guard.RestartNeeded):
            self.load("lazy")
        self.assertEqual(sum(isinstance(f, version_guard._Finder) and f.package == self.package
                             for f in sys.meta_path), 1)

    def test_other_packages_are_not_checked(self):
        self.stamp(0, 2, 19)
        self.assertIsNotNone(importlib.import_module("json.decoder"))

    def test_the_real_init_states_the_version_the_guard_reads(self):
        root = Path(loopcut.__file__).parent
        self.assertEqual(version_guard.version_on_disk(root), tuple(loopcut.bl_info["version"]))


class SubmitTest(unittest.TestCase):
    def test_a_refused_import_while_sending_is_a_chat_card_and_the_input_stays(self):
        # ui.host._submit, taken from the source: agent.send imports a few modules on first use itself.
        import ast
        import types
        source = Path(loopcut.__file__).parent / "ui" / "host.py"
        nodes = [node for node in ast.parse(source.read_text(encoding="utf-8")).body
                 if isinstance(node, ast.FunctionDef) and node.name == "_submit"]
        cleared = []

        def send(text):
            raise version_guard.RestartNeeded(version_guard.MESSAGE, name="loopcut.video_attach")
        host = {"__package__": "loopcut.ui", "agent": types.SimpleNamespace(send=send),
                "state": types.SimpleNamespace(item_error=lambda text: {"kind": "error", "text": text},
                                               ui={"mentions": ["@Cube"]}),
                "textedit": types.SimpleNamespace(set_text=lambda session, text: cleared.append(text))}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), host)
        session = {"input": "Make it red", "items": []}
        host["_submit"](session)
        self.assertEqual(session["items"], [{"kind": "error", "text": version_guard.MESSAGE}])
        self.assertEqual((session["input"], cleared), ("Make it red", []))


if __name__ == "__main__":
    unittest.main()
