"""Merge naming and weak-reference cleanup without Blender."""

import itertools
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

from loopcut import merge, ownership  # noqa: E402


class FakeID:
    uids = itertools.count(1)

    def __init__(self, ids, name="data", id_type="FONT", readonly=False, weak_path=None):
        self.ids, self._name, self.id_type, self.readonly = ids, name, id_type, readonly
        self.session_uid = next(self.uids)
        self.library = None
        self.library_weak_reference = types.SimpleNamespace(filepath=weak_path) if weak_path else None
        self.filepath = "<builtin>" if id_type == "FONT" else ""
        self.users, self.use_fake_user = 1, False
        self.references, self.properties, self.name_writes = [], {}, []
        ids.append(self)

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        self.name_writes.append(value)
        if self.readonly:
            raise AttributeError('bpy_struct: attribute "name" from "VectorFont" is read-only')
        self._name = value

    def is_property_readonly(self, prop):
        assert prop == "name"
        return self.readonly

    def copy(self):
        return FakeID(self.ids, self.name + ".001", self.id_type, self.readonly)

    def user_remap(self, new):
        for holder, attr in self.references:
            setattr(holder, attr, new)
        new.references.extend(self.references)
        self.references.clear()

    def get(self, key, default=None):
        return self.properties.get(key, default)

    def __getitem__(self, key):
        return self.properties[key]

    def __delitem__(self, key):
        del self.properties[key]


class MergeReadonlyNamesTest(unittest.TestCase):
    def setUp(self):
        self.ids = []
        self.bpy = types.ModuleType("bpy")
        self.bpy.data = types.SimpleNamespace(
            all_ids=self.ids, batch_remove=mock.Mock(side_effect=self.remove),
            meshes={}, collections=types.SimpleNamespace(new=mock.Mock()))
        self.bpy.path = types.SimpleNamespace(abspath=os.path.abspath)
        patch = mock.patch.dict(sys.modules, bpy=self.bpy)
        patch.start()
        self.addCleanup(patch.stop)

    def remove(self, ids):
        for idb in ids:
            self.ids.remove(idb)

    def test_builtin_font_readonly_name_does_not_abort_weak_reference_cleanup(self):
        font = FakeID(self.ids, readonly=True, weak_path="/tmp/result.blend")
        text = types.SimpleNamespace(font=font)
        font.references.append((text, "font"))

        merge._drop_weak_references("/tmp/result.blend")

        self.assertIsNot(text.font, font)
        self.assertEqual(self.ids, [text.font])
        self.assertIsNone(text.font.library_weak_reference)
        self.assertEqual(text.font.filepath, "<builtin>")
        self.assertEqual(text.font.name, "data.001")
        self.assertEqual(text.font.name_writes, [])

    def test_editable_copy_keeps_name_and_unrelated_references_are_untouched(self):
        original = FakeID(self.ids, id_type="MATERIAL", weak_path="/tmp/result.blend")
        other = FakeID(self.ids, weak_path="/tmp/other.blend")
        local = FakeID(self.ids)
        holder = types.SimpleNamespace(data=original)
        original.references.append((holder, "data"))

        merge._drop_weak_references("/tmp/result.blend")

        self.assertIsNot(holder.data, original)
        self.assertEqual(holder.data.name, "data")
        self.assertIsNone(holder.data.library_weak_reference)
        self.assertEqual(self.ids, [other, local, holder.data])
        self.assertEqual(other.library_weak_reference.filepath, "/tmp/other.blend")

    def test_cleanup_propagates_unexpected_errors(self):
        for method in ("copy", "user_remap", "is_property_readonly", "name"):
            with self.subTest(method=method):
                self.ids.clear()
                FakeID(self.ids, weak_path="/tmp/result.blend")
                failure = mock.Mock(side_effect=AttributeError("unexpected failure"))
                replacement = property(FakeID.name.fget, failure) if method == "name" else failure
                with mock.patch.object(FakeID, method, replacement):
                    with self.assertRaisesRegex(AttributeError, "unexpected failure"):
                        merge._drop_weak_references("/tmp/result.blend")

    def test_mesh_naming_respects_readonly_names(self):
        for readonly in (True, False):
            with self.subTest(readonly=readonly):
                mesh = FakeID(self.ids, id_type="MESH", readonly=readonly)
                obj = types.SimpleNamespace(id_type="OBJECT", type="MESH", data=mesh,
                                            session_uid=0, name="object")
                merge._name_meshes([obj, mesh])
                self.assertEqual(mesh.name, "data" if readonly else "object")

    def test_owned_datablock_and_shape_key_names_respect_readonly(self):
        for readonly in (True, False):
            with self.subTest(readonly=readonly):
                self.ids.clear()
                live = FakeID(self.ids, name="live", id_type="CURVE")
                new = FakeID(self.ids, name="appended", id_type="CURVE", readonly=readonly)
                live.shape_keys = FakeID(self.ids, name="keys", id_type="KEY")
                new.shape_keys = FakeID(self.ids, name="keys.001", id_type="KEY", readonly=readonly)
                new.properties[ownership.STAMP] = "data"
                key = "CURVE:data"
                spawn = merge.Spawn("wave", Path("/tmp/spawn.blend"), {key: live.session_uid},
                                    {key: "before"}, {})
                report, moved = merge.MergeReport(), {}
                result = {"manifest": {"result": "/tmp/result.blend", "owned": [key]}}
                with mock.patch.object(merge.os.path, "isfile", return_value=True), \
                        mock.patch.object(ownership, "all_ids", side_effect=[[live], [live, new]]), \
                        mock.patch.object(ownership, "token", side_effect=lambda idb, _: "after" if idb is new else "before"), \
                        mock.patch.object(merge, "_append", return_value=[new.session_uid]), \
                        mock.patch.object(merge, "_replace", side_effect=lambda old, new, missing: [old]), \
                        mock.patch.object(merge, "_settle", return_value=[]), \
                        mock.patch.object(merge, "_worker_collections", return_value=(set(), set())), \
                        mock.patch.object(merge, "_link_created"):
                    merge._merge_worker(spawn, result, spawn.uid2spawn(), report, [], moved)

                self.assertEqual(moved, {key: new.session_uid})
                self.assertNotIn(live, self.ids)
                self.assertEqual(new.name, "appended" if readonly else "live")
                self.assertEqual(new.shape_keys.name, "keys.001" if readonly else "keys")
                self.assertEqual(report.errors,
                                 ["worker: could not restore the name live (got appended)"] if readonly else [])


if __name__ == "__main__":
    unittest.main()
