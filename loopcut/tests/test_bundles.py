"""Portable manifests and checkpoint handoff with fake Blender data and fake HTTP."""

import copy
import hashlib
import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "addons_core"))
sys.path.insert(0, str(REPO / "loopcut" / "harness"))
sys.modules.setdefault("bpy", types.ModuleType("bpy"))

from loopcut import blobs, bundles, checkpoints  # noqa: E402
from fake_blobs import FakeBlobs  # noqa: E402

INFO = {"blender": "5.2.0", "scene": {"name": "Scène", "camera": "Camera", "frame_start": 1,
                                        "frame_end": 120, "fps": 24}, "missing": [], "unsupported": [], "system": []}
SOURCE = {"kind": "checkpoint", "session": "ab" * 16, "checkpoint": "cd" * 16}


class PathMap(list):
    """bpy.data.file_path_map's result, as (ID, raw paths) pairs: fake IDs are not hashable."""

    def items(self):
        return list(self)


class Image(types.SimpleNamespace):
    @property
    def filepath_raw(self):
        return self.filepath

    @filepath_raw.setter
    def filepath_raw(self, value):
        self.filepath = value



def scripted(path, index=0, simple=True, mute=False, kind="SCRIPTED"):
    """A driver F-curve as the packer reads it."""
    return types.SimpleNamespace(data_path=path, array_index=index, mute=mute,
                                 driver=types.SimpleNamespace(type=kind, is_simple_expression=simple))

class BundlesTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "scene.blend").write_bytes(b"blend stand-in")
        (self.root / "textures").mkdir()
        (self.root / "textures" / "wood.png").write_bytes(b"texture stand-in")

    def test_manifest_is_the_contract_and_lists_every_file_with_correct_digest(self):
        value = bundles.manifest(self.root, INFO, SOURCE)
        self.assertEqual(value["schema"], "loopcut.bundle/0.1")
        self.assertEqual(value["entry"], "scene.blend")
        self.assertEqual(value["blender"], "5.2.0")
        self.assertEqual(value["scene"], INFO["scene"])
        self.assertEqual(value["source"], SOURCE)
        self.assertEqual([f["path"] for f in value["files"]], ["scene.blend", "textures/wood.png"])
        for item in value["files"]:
            data = (self.root / item["path"]).read_bytes()
            self.assertEqual(item["sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(item["size"], len(data))
        self.assertNotIn("missing", value)
        self.assertNotIn("unsupported", value)
        self.assertNotIn("system", value)

    def test_manifest_rejects_extra_keys_and_scene_values_outside_the_contract(self):
        first = bundles.manifest(self.root, INFO, SOURCE)
        for location in ((), ("scene",), ("source",), ("files", 0)):
            value = copy.deepcopy(first)
            target = value
            for key in location:
                target = target[key]
            target["extra"] = True
            with self.subTest(location=location), self.assertRaises(bundles.BundleError):
                bundles.canonical_json(value)
        for key, invalid in (("name", ""), ("camera", ""), ("fps", 0.5)):
            value = copy.deepcopy(first)
            value["scene"][key] = invalid
            with self.subTest(key=key), self.assertRaises(bundles.BundleError):
                bundles.canonical_json(value)

    def test_canonical_json_is_utf8_compact_sorted_and_stable_across_runs_and_key_order(self):
        first = bundles.manifest(self.root, INFO, SOURCE)
        second = bundles.manifest(self.root, INFO, SOURCE)

        def reverse_keys(value):
            if isinstance(value, dict):
                return {k: reverse_keys(v) for k, v in reversed(list(value.items()))}
            if isinstance(value, list):
                return [reverse_keys(v) for v in value]
            return value

        body = bundles.canonical_json(first)
        self.assertEqual(body, bundles.canonical_json(second))
        self.assertEqual(body, bundles.canonical_json(reverse_keys(first)))
        self.assertEqual(json.loads(body), first)
        self.assertIn("Scène".encode(), body)
        self.assertEqual(body, json.dumps(first, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())

    def test_canonical_json_writes_whole_floats_as_javascript_does(self):
        # Blender's fps is 24.0 (fps / fps_base). The service re-serializes the manifest in
        # JavaScript, which writes 24, and checks that hash: "fps":24.0 got every bundle refused as
        # "Upload a valid bundle manifest…" (live test 2026-10-17).
        value = bundles.manifest(self.root, {**INFO, "scene": {**INFO["scene"], "fps": 24.0}}, SOURCE)
        body = bundles.canonical_json(value)
        self.assertIn(b'"fps":24,', body)
        self.assertNotIn(b"24.0", body)
        fractional = bundles.manifest(self.root, {**INFO, "scene": {**INFO["scene"], "fps": 23.976}}, SOURCE)
        self.assertIn(b'"fps":23.976,', bundles.canonical_json(fractional))

    def test_explicit_checkpoint_creation_time_is_used(self):
        value = bundles.manifest(self.root, {**INFO, "created_at": "2026-10-03T18:00:00Z"}, SOURCE)
        self.assertEqual(value["created_at"], "2026-10-03T18:00:00Z")

    def test_paths_cannot_escape_or_have_duplicates_or_noncanonical_separators(self):
        first = bundles.manifest(self.root, INFO, SOURCE)
        for path in ("../evil", "/etc/passwd", "C:/evil", "C:evil", "textures\\evil", "a/../b", "./scene.blend",
                     "textures//wood.png", "", "file\0bad"):
            value = copy.deepcopy(first)
            value["files"][1]["path"] = path
            with self.subTest(path=path), self.assertRaises(bundles.BundleError):
                bundles.canonical_json(value)
        value = copy.deepcopy(first)
        value["files"].append(dict(value["files"][0]))
        with self.assertRaises(bundles.BundleError):
            bundles.canonical_json(value)

    def test_symlink_files_and_directories_are_rejected(self):
        for name, target in (("linked", self.root / "scene.blend"), ("linked-dir", self.root / "textures")):
            link = self.root / name
            link.symlink_to(target, target_is_directory=target.is_dir())
            with self.subTest(name=name), self.assertRaises(bundles.BundleError):
                bundles.manifest(self.root, INFO, SOURCE)
            link.unlink()

    def test_entry_is_required(self):
        (self.root / "scene.blend").unlink()
        with self.assertRaises(bundles.BundleError):
            bundles.manifest(self.root, INFO, SOURCE)

    def test_upload_files_then_manifest_twice_without_putting_existing_content(self):
        value = bundles.manifest(self.root, INFO, SOURCE)
        service = FakeBlobs()
        digest = bundles.upload(self.root, value, client=service.client())
        self.assertEqual(len(service.puts), 3)
        self.assertEqual(service.puts[-1], digest)
        self.assertEqual(digest, hashlib.sha256(bundles.canonical_json(value)).hexdigest())
        self.assertEqual(service.stored[digest], bundles.canonical_json(value))
        self.assertEqual(service.pending[digest]["mime"], "application/json")
        self.assertEqual(bundles.upload(self.root, value, client=service.client()), digest)
        self.assertEqual(len(service.puts), 3)
        self.assertEqual(bundles.manifest(self.root, INFO, SOURCE), value)

    def test_a_large_scene_goes_in_parts_when_the_server_offers_multipart(self):
        value = bundles.manifest(self.root, INFO, SOURCE)
        scene = next(item for item in value["files"] if item["path"] == "scene.blend")
        service = FakeBlobs(part_size=max(1, scene["size"] // 2))
        digest = bundles.upload(self.root, value, client=service.client(), multipart={"min_size": scene["size"]})
        self.assertEqual(service.stored[scene["sha256"]], (self.root / "scene.blend").read_bytes())
        self.assertGreaterEqual(len(service.part_puts), 2)
        self.assertNotIn(scene["sha256"], service.puts)
        self.assertEqual(service.puts[-1], digest)  # The manifest itself stays one small PUT.

    def test_changed_or_added_staging_file_is_not_published(self):
        value = bundles.manifest(self.root, INFO, SOURCE)
        service = FakeBlobs()
        (self.root / "scene.blend").write_bytes(b"changed")
        with self.assertRaises(ValueError):
            bundles.upload(self.root, value, client=service.client())
        self.assertEqual(service.calls, [])
        (self.root / "extra").write_bytes(b"extra")
        with self.assertRaises(bundles.BundleError):
            bundles.upload(self.root, value, client=service.client())
        self.assertEqual(service.calls, [])


class CollectionTest(unittest.TestCase):
    def item(self, **kwargs):
        item = Image(is_dirty=False, **kwargs) if "source" in kwargs else types.SimpleNamespace(**kwargs)
        item.as_pointer = lambda: id(item)
        item.id_data = item
        return item

    def scene(self):
        return self.item(name="Scene", camera=None, frame_start=1, frame_end=2,
                         render=types.SimpleNamespace(fps=24, fps_base=1), rigidbody_world=None, sequence_editor=None)

    def blender(self, data, scene, stage, paths):
        def save(**kwargs):
            self.assertEqual(kwargs["filepath"], str(stage / "scene.blend"))
            self.assertFalse(kwargs["relative_remap"])
            self.assertEqual(fake.context.preferences.filepaths.save_modified_images, "NEVER_SAVE")
            Path(kwargs["filepath"]).write_bytes(b"bundled scene")
            return {"FINISHED"}

        fake = types.SimpleNamespace(data=data, app=types.SimpleNamespace(version=(5, 2, 0)),
                                     context=types.SimpleNamespace(scene=scene, preferences=types.SimpleNamespace(
                                         filepaths=types.SimpleNamespace(save_version=1))),
                                     ops=types.SimpleNamespace(wm=types.SimpleNamespace(save_as_mainfile=save)),
                                     utils=types.SimpleNamespace(blend_paths=paths, resource_path=lambda kind: "",
                                                                 system_resource=lambda kind: ""))
        return fake

    def test_system_path_classification_uses_components_and_explicit_roots_without_io(self):
        roots = ("/opt/Blender/5.2", "/opt/Blender/local", "")
        for path, expected in (("/opt/Blender/5.2/datafiles/assets/brushes/essentials.blend", True),
                               ("/opt/Blender/local/datafiles/assets/brushes/essentials.blend", True),
                               ("/opt/Blender/5.2/missing.png", True),
                               ("/opt/Blender/5.2-custom/texture.png", False),
                               ("/opt/Blender/5.2/../user/texture.png", False),
                               ("/home/user/assets/brushes/essentials.blend", False)):
            with self.subTest(path=path), patch.object(Path, "resolve", side_effect=AssertionError("filesystem")):
                self.assertEqual(bundles._is_system_path(path, roots), expected)
                self.assertFalse(bundles._is_system_path(path, ("",)))

    def test_system_inputs_stay_unchanged_and_do_not_make_user_libraries_complex(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            snapshot, stage = root / "snapshot.blend", root / "bundle"
            snapshot.write_bytes(b"snapshot")
            stage.mkdir()
            system, local, datafiles = root / "SYSTEM", root / "LOCAL", root / "datafiles"
            brush = local / "datafiles/assets/brushes/essentials_brushes-gp_draw.blend"
            preview = brush.parent / "preview.png"
            image_path = system / "textures/image.png"
            unhandled = datafiles / "assets/essentials.blend"
            user_lib = root / "user.blend"
            external = root / "SYSTEM-custom/unknown.bin"
            for path in (brush, preview, image_path, unhandled, user_lib, external):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"input")
            system_lib = self.item(name="brushes", filepath=str(brush), parent=None, is_missing=False)
            user_lib = self.item(name="user", filepath=str(user_lib), parent=None, is_missing=False)
            system_lib.parent = user_lib
            image = self.item(name="system image", filepath=str(image_path), source="FILE", library=None)
            linked = self.item(name="preview", filepath="//preview.png", source="FILE", library=system_lib)
            scene = self.scene()
            data = types.SimpleNamespace(images=[image, linked], objects=[], scenes=[scene],
                                         libraries=[system_lib, user_lib])
            paths = [str(path) for path in (brush, image_path, unhandled, external)]
            fake = self.blender(data, scene, stage,
                                lambda **kw: paths if kw["local"] else [*paths, str(preview)])
            fake.utils.resource_path = lambda kind: str({"SYSTEM": system, "LOCAL": local}[kind])
            fake.utils.system_resource = lambda kind: str(datafiles) if kind == "DATAFILES" else ""
            with patch.dict(sys.modules, {"bpy": fake}):
                info = bundles._collect_opened(snapshot, stage)
            self.assertEqual(info["missing"], [])
            self.assertEqual(info["unsupported"], [{"type": "external", "path": str(external),
                                                    "reason": "Unhandled input path"}])
            self.assertEqual(info["system"], sorted(map(str, (brush, preview, image_path, unhandled))))
            self.assertEqual(system_lib.filepath, str(brush))
            self.assertEqual(image.filepath, str(image_path))
            self.assertEqual(linked.filepath, "//preview.png")
            self.assertEqual(len(list(stage.rglob("*.blend"))), 2)
            self.assertEqual(len(list(stage.rglob("*.png"))), 0)
            self.assertTrue(user_lib.filepath.startswith("//libraries/"))

    def test_original_relative_paths_packed_missing_sequences_and_caches_in_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, snapshot, stage = root / "project", root / "store" / "snapshot.blend", root / "bundle"
            project.mkdir()
            snapshot.parent.mkdir()
            stage.mkdir()
            snapshot.write_bytes(b"checkpoint unchanged")
            (project / "scene.blend").write_bytes(b"user unchanged")
            for name, data in (("wood.png", b"wood"), ("frame001.png", b"one"), ("frame002.png", b"two")):
                (project / name).write_bytes(data)
            (project / "cache").mkdir()
            (project / "cache" / "data.vdb").write_bytes(b"cache")
            wood = self.item(name="wood", filepath="//wood.png", source="FILE", library=None)
            packed = self.item(name="packed", filepath="//absent-packed.png", source="FILE", library=None,
                               packed_file=object())
            painted = self.item(name="painted", filepath="//painted.png", source="FILE", library=None)
            (project / "painted.png").write_bytes(b"before painting")
            painted.is_dirty = True

            def save_image(*, filepath, save_copy):
                self.assertTrue(save_copy)
                self.assertTrue(Path(filepath).is_relative_to(stage))
                Path(filepath).write_bytes(b"checkpoint pixels")

            painted.save = save_image
            absent = self.item(name="missing", filepath="//absent.png", source="FILE", library=None)
            sequence = self.item(name="sequence", filepath="//frame001.png", source="SEQUENCE", library=None)
            domain = self.item(name="fluid", cache_directory="//cache", library=None)
            modifier = self.item(type="FLUID", domain_settings=domain)
            obj = self.item(name="cube", modifiers=[modifier], particle_systems=[])
            scene = self.scene()
            data = types.SimpleNamespace(images=[wood, painted, packed, absent, sequence], sounds=[], movieclips=[],
                                         fonts=[], volumes=[], cache_files=[], objects=[obj], scenes=[scene], libraries=[])

            fake = self.blender(data, scene, stage, lambda **kwargs: [wood.filepath, absent.filepath,
                                                                    sequence.filepath, domain.cache_directory])
            with patch.dict(sys.modules, {"bpy": fake}):
                info = bundles._collect_opened(snapshot, stage, str(project / "scene.blend"))
            self.assertEqual(info["unsupported"], [])
            self.assertEqual([item["name"] for item in info["missing"]], ["missing"])
            self.assertEqual(packed.filepath, "//absent-packed.png")
            for item in (wood, sequence):
                self.assertTrue(item.filepath.startswith("//textures/"))
                self.assertTrue((stage / item.filepath[2:]).is_file())
            self.assertEqual(len(list(stage.rglob("frame*.png"))), 2)
            self.assertEqual(len(list(stage.rglob("data.vdb"))), 1)
            self.assertEqual(snapshot.read_bytes(), b"checkpoint unchanged")
            self.assertEqual((project / "scene.blend").read_bytes(), b"user unchanged")
            self.assertEqual((stage / painted.filepath[2:]).read_bytes(), b"checkpoint pixels")
            self.assertEqual((project / "painted.png").read_bytes(), b"before painting")

    def test_simple_library_is_copied_complex_library_reported_and_packed_library_stays_packed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original, snapshot, stage = root / "project.blend", root / "snapshot.blend", root / "bundle"
            snapshot.write_bytes(b"snapshot")
            stage.mkdir()
            (root / "libraries").mkdir()
            libs = []
            for name in ("simple", "complex", "packed"):
                path = root / "libraries" / (name + ".blend")
                path.write_bytes(b"library bytes")
                libs.append(self.item(name=name, filepath="//libraries/" + path.name, parent=None,
                                      packed_file=object() if name == "packed" else None, is_missing=False))
            texture = root / "libraries" / "external.png"
            texture.write_bytes(b"external")
            linked = self.item(name="linked texture", filepath="//external.png", source="FILE", library=libs[1])
            scene = self.scene()
            data = types.SimpleNamespace(images=[linked], objects=[], scenes=[scene], libraries=libs)
            paths = [str(root / lib.filepath[2:]) for lib in libs]
            fake = self.blender(data, scene, stage,
                                lambda **kw: paths if kw["local"] else [*paths, str(texture)])
            with patch.dict(sys.modules, {"bpy": fake}):
                info = bundles._collect_opened(snapshot, stage, str(original))
            self.assertEqual([item["name"] for item in info["unsupported"]], ["complex"])
            self.assertEqual(info["missing"], [])
            self.assertTrue(libs[0].filepath.startswith("//libraries/"))
            self.assertEqual((stage / libs[0].filepath[2:]).read_bytes(), b"library bytes")
            self.assertEqual(libs[2].filepath, "//libraries/packed.blend")
            self.assertEqual(len(list(stage.rglob("*.blend"))), 2)

    def test_appended_from_notes_and_empty_paths_are_not_inputs(self):
        # blend_paths lists ID.library_weak_reference for every appended ID, packed or not (a
        # BlenderKit Glock: live test 2026-10-17), and "" for one the model cleared, which once
        # resolved to the scene's whole folder. A linked ID's note must not make its library complex.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project, snapshot, stage = root / "project", root / "snapshot.blend", root / "bundle"
            snapshot.write_bytes(b"snapshot")
            stage.mkdir()
            glock = root / "data/blenderkit/d72fd57d/resolution_2K.blend"  # the eval folder is gone
            concrete = root / "data/blenderkit/ac2ff40c/resolution_2K.blend"
            library_path = project / "libraries/rocks.blend"
            for path in (concrete, library_path):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"blend")

            def weak(path):
                return types.SimpleNamespace(filepath=str(path))

            library = self.item(name="rocks", filepath="//libraries/rocks.blend", parent=None, is_missing=False,
                                library_weak_reference=None)
            image = self.item(name="Glock_Base_color.jpg", filepath="//textures_2k/Glock_Base_color.jpg",
                              source="FILE", library=None, packed_file=object(), library_weak_reference=weak(glock))
            mesh = self.item(name="Glock", library=None, library_weak_reference=weak(glock))
            material = self.item(name="Rough Cast Concrete Wall", library=None, library_weak_reference=weak(concrete))
            cleared = self.item(name="Reload", library=None, library_weak_reference=weak(""))
            rock = self.item(name="rock", library=library, library_weak_reference=types.SimpleNamespace(
                filepath="//assets/rock_source.blend"))
            scene = self.scene()
            data = types.SimpleNamespace(images=[image], meshes=[mesh, rock], materials=[material],
                                         node_groups=[cleared], objects=[], scenes=[scene], libraries=[library])
            local = [str(glock), str(glock), str(concrete), "", str(library_path)]
            fake = self.blender(data, scene, stage, lambda **kw: local if kw["local"] else
                                [*local, str(library_path.parent / "assets/rock_source.blend")])
            with patch.dict(sys.modules, {"bpy": fake}):
                info = bundles._collect_opened(snapshot, stage, str(project / "scene.blend"))
            self.assertEqual(info["unsupported"], [])
            self.assertEqual(info["missing"], [])
            self.assertTrue(library.filepath.startswith("//libraries/"))
            self.assertEqual(image.filepath, "//textures_2k/Glock_Base_color.jpg")
            self.assertEqual(sorted(p.name for p in stage.rglob("*") if p.is_file()), ["rocks.blend", "scene.blend"])

    def test_sound_strip_path_is_its_sounds_packed_or_copied(self):
        # A sound strip keeps its own copy of its sound's path, listed even when the sound is packed.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project, snapshot, stage = root / "project", root / "snapshot.blend", root / "bundle"
            project.mkdir()
            snapshot.write_bytes(b"snapshot")
            stage.mkdir()
            (project / "music.wav").write_bytes(b"music")
            voice_path = root / "T/job_1/0000-7b64aa41a6d0.wav"  # a temporary file, long gone
            voice = self.item(name="VO_Reloading", filepath=str(voice_path), library=None, packed_file=object())
            music = self.item(name="music", filepath="//music.wav", library=None, packed_file=None)
            strips = [types.SimpleNamespace(type="SOUND", name="VO", sound=voice),
                      types.SimpleNamespace(type="SOUND", name="Music", sound=music)]
            scene = self.scene()
            scene.library = None
            scene.sequence_editor = types.SimpleNamespace(strips_all=strips)
            data = types.SimpleNamespace(images=[], sounds=[voice, music], objects=[], scenes=[scene], libraries=[],
                                         file_path_map=lambda subset: PathMap([(scene, {str(voice_path), "//music.wav"})]))
            paths = [str(voice_path), str(project / "music.wav"), str(project / "music.wav")]
            fake = self.blender(data, scene, stage, lambda **kw: paths)
            with patch.dict(sys.modules, {"bpy": fake}):
                info = bundles._collect_opened(snapshot, stage, str(project / "scene.blend"))
            self.assertEqual(info["unsupported"], [])
            self.assertEqual(info["missing"], [])
            self.assertEqual(voice.filepath, str(voice_path))
            self.assertTrue(music.filepath.startswith("//sounds/"))
            self.assertEqual((stage / music.filepath[2:]).read_bytes(), b"music")

    def test_generated_sound_strips_keep_stale_temp_paths_that_are_not_inputs(self):
        # A strip keeps the temp path it was made from (cloud_jobs._add_audio); its sound was then packed,
        # unpacked or relinked. Only the Sound is played: packed passes, unpacked is copied or missing.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project, snapshot, stage = root / "project", root / "snapshot.blend", root / "bundle"
            (project / "sounds").mkdir(parents=True)
            snapshot.write_bytes(b"snapshot")
            stage.mkdir()
            download = root / "T/job_1-x"
            download.mkdir(parents=True)
            stale = {name: download / f"000{n}-7b64aa41a6d{n}.wav" for n, name in enumerate(("sfx", "vo", "gone"))}
            for path in stale.values():
                path.write_bytes(b"temporary download")  # Still there: an existing stale path was refused.
            (project / "sounds/vo.wav").write_bytes(b"unpacked voice")
            (project / "clip.mp4").write_bytes(b"clip")
            sfx = self.item(name="sfx", filepath="//sounds/sfx.wav", library=None, packed_file=object())
            vo = self.item(name="vo", filepath="//sounds/vo.wav", library=None, packed_file=None)
            gone = self.item(name="gone", filepath="//sounds/gone.wav", library=None, packed_file=None)
            movie = self.item(type="MOVIE", name="Clip", filepath="//clip.mp4", library=None)
            strips = [types.SimpleNamespace(type="SOUND", name=f"SFX • {sound.name}", sound=sound)
                      for sound in (sfx, vo, gone)] + [movie]
            scene = self.scene()
            scene.library = None
            scene.sequence_editor = types.SimpleNamespace(strips_all=strips)
            listed = {str(path) for path in stale.values()} | {"//clip.mp4"}
            data = types.SimpleNamespace(images=[], sounds=[sfx, vo, gone], objects=[], scenes=[scene], libraries=[],
                                         file_path_map=lambda subset: PathMap([(scene, set(listed))] if scene in subset else []))
            # blend_paths: each strip's own path, the unpacked sounds' paths and the movie; never a packed sound's.
            paths = [str(path) for path in stale.values()] + [str(project / "sounds/vo.wav"),
                                                              str(project / "sounds/gone.wav"), str(project / "clip.mp4")]
            fake = self.blender(data, scene, stage, lambda **kw: paths)
            with patch.dict(sys.modules, {"bpy": fake}):
                info = bundles._collect_opened(snapshot, stage, str(project / "scene.blend"))
            self.assertEqual(info["unsupported"], [])
            self.assertEqual(info["missing"], [{"type": "sounds", "name": "gone", "path": "//sounds/gone.wav"}])
            self.assertEqual(sfx.filepath, "//sounds/sfx.wav")  # Packed: left alone.
            self.assertEqual((stage / vo.filepath[2:]).read_bytes(), b"unpacked voice")
            self.assertEqual((stage / movie.filepath[2:]).read_bytes(), b"clip")
            self.assertEqual(sorted(p.name for p in stage.rglob("*") if p.is_file()), ["clip.mp4", "scene.blend", "vo.wav"])
            for path in stale.values():  # Gone from the temp folder: still not inputs, not missing.
                path.unlink()
            for item, raw in ((vo, "//sounds/vo.wav"), (gone, "//sounds/gone.wav"), (movie, "//clip.mp4")):
                item.filepath = raw
            shutil.rmtree(stage)
            stage.mkdir()
            with patch.dict(sys.modules, {"bpy": fake}):
                info = bundles._collect_opened(snapshot, stage, str(project / "scene.blend"))
            self.assertEqual(info["unsupported"], [])
            self.assertEqual(info["missing"], [{"type": "sounds", "name": "gone", "path": "//sounds/gone.wav"}])

    def test_sound_strip_copy_left_behind_by_a_repointed_sound_is_not_an_input(self):
        # A generated sound effect lands in the OS temp folder; the model packed it and pointed its
        # Sound at the workspace, but the strip's own copy (Python can't read it) still named the
        # temp file, which still existed, so cloud render refused it (live test 2026-10-17).
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project, snapshot, stage = root / "project", root / "snapshot.blend", root / "bundle"
            (project / "plates").mkdir(parents=True)
            snapshot.write_bytes(b"snapshot")
            stage.mkdir()
            temp = root / "T/job_w02c/0000-8d592b630cf0.wav"
            temp.parent.mkdir(parents=True)
            temp.write_bytes(b"click")
            plate = project / "plates/plate.mov"
            plate.write_bytes(b"movie")
            click = self.item(name="mag_release_generated.wav", library=None, packed_file=object(),
                              filepath=str(project / "audio/mag_release_generated.wav"))
            strips = [types.SimpleNamespace(type="SOUND", name="SFX", sound=click),
                      self.item(type="MOVIE", name="Plate", filepath="//plates/plate.mov")]
            scene = self.scene()
            scene.library = None
            scene.sequence_editor = types.SimpleNamespace(strips_all=strips)

            def path_map(subset):
                self.assertEqual(subset, [scene])
                return PathMap([(scene, {str(temp), "//plates/plate.mov"})])

            data = types.SimpleNamespace(images=[], sounds=[click], movieclips=[], objects=[], scenes=[scene],
                                         libraries=[], file_path_map=path_map)
            # blend_paths lists the packed Sound's own path only with packed=True, which packing never asks for.
            paths = [str(temp), str(plate)]
            fake = self.blender(data, scene, stage, lambda **kw: paths + (
                [str(project / "audio/mag_release_generated.wav")] if kw.get("packed") else []))
            with patch.dict(sys.modules, {"bpy": fake}):
                info = bundles._collect_opened(snapshot, stage, str(project / "scene.blend"))
            # The temp copy is left out; the movie strip's file is still an input, copied as before.
            self.assertEqual(info["unsupported"], [])
            self.assertEqual(info["missing"], [])
            self.assertEqual(click.filepath, str(project / "audio/mag_release_generated.wav"))  # Packed: left alone.
            self.assertTrue(strips[1].filepath.startswith("//clips/"))
            self.assertEqual((stage / strips[1].filepath[2:]).read_bytes(), b"movie")

    def test_real_external_inputs_are_still_copied_refused_or_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project, snapshot, stage = root / "project", root / "snapshot.blend", root / "bundle"
            project.mkdir()
            snapshot.write_bytes(b"snapshot")
            stage.mkdir()
            texture, script = root / "elsewhere/wood.png", root / "elsewhere/shader.osl"
            texture.parent.mkdir()
            texture.write_bytes(b"wood")
            script.write_bytes(b"shader")
            gone, model = root / "gone.png", root / "import/model.obj"
            wood = self.item(name="wood", filepath=str(texture), source="FILE", library=None)
            absent = self.item(name="gone", filepath=str(gone), source="FILE", library=None)
            scene = self.scene()
            data = types.SimpleNamespace(images=[wood, absent], objects=[], scenes=[scene], libraries=[])
            # An OSL script and an imported file are inputs the packer has no handler for.
            paths = [str(texture), str(gone), str(script), str(model)]
            fake = self.blender(data, scene, stage, lambda **kw: paths)
            with patch.dict(sys.modules, {"bpy": fake}):
                info = bundles._collect_opened(snapshot, stage, str(project / "scene.blend"))
            self.assertTrue(wood.filepath.startswith("//textures/"))
            self.assertEqual((stage / wood.filepath[2:]).read_bytes(), b"wood")
            self.assertEqual(info["unsupported"], [{"type": "external", "path": str(script),
                                                    "reason": "Unhandled input path"}])
            self.assertEqual(info["missing"], [{"type": "textures", "name": "gone", "path": str(gone)},
                                               {"type": "external", "path": str(model)}])

    def test_driver_rule_is_the_render_services(self):
        # Shared with loopcut-web workers/render/render_job.py: muted, not scripted, or a simple expression.
        for curve, acceptable in ((scripted("x"), True), (scripted("x", simple=False, mute=True), True),
                                  (scripted("x", simple=False, kind="AVERAGE"), True),
                                  (scripted("x", simple=False), False)):
            with self.subTest(curve=curve):
                self.assertIs(bundles.driver_acceptable(curve), acceptable)

    def driven(self, kind, name, drivers, values=None, **kwargs):
        """A data-block of an RNA type carrying drivers; values are what its data paths resolve to."""
        def path_resolve(path):
            if path not in (values or {}):
                raise ValueError(f"Path not found: {path}")
            return values[path]
        animation = types.SimpleNamespace(drivers=list(drivers), action=None)
        return self.item(name=name, bl_rna=types.SimpleNamespace(name=kind), animation_data=animation,
                         path_resolve=path_resolve, **kwargs)

    def test_only_drivers_that_need_python_are_refused_by_name_and_none_is_changed(self):
        # A free BlenderKit rigged character carries 100 scripted drivers ("var", "1-var" IK/FK switches), all simple
        # expressions: its cloud render failed on the worker (live 2026-10-21). The service now renders simple ones,
        # so they pack untouched; one that needs Python is refused here, before any upload, by the same rule.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            snapshot, stage = root / "snapshot.blend", root / "bundle"
            snapshot.write_bytes(b"snapshot")
            stage.mkdir()
            scene = self.scene()
            influence = 'pose.bones["mixamorig:LeftUpLeg"].constraints["FK_follow"].influence'
            rig_drivers = [scripted(influence), scripted("location", 2, simple=False),
                           scripted('["sway"]', simple=False), scripted("rotation_euler", simple=False),
                           scripted("scale", simple=False, mute=True), scripted("hide_viewport", kind="AVERAGE")]
            rig = self.driven("Object", "Armature", rig_drivers, {'["sway"]': 0.5, "rotation_euler": (0, 0, 0)},
                              library=None, modifiers=[], particle_systems=[])
            roughness = 'nodes["Principled BSDF"].inputs[2].default_value'
            tree = self.driven("Shader Node Tree", "Shader Nodetree", [scripted(roughness, simple=False)], library=None)
            material = self.item(name="Skin", bl_rna=types.SimpleNamespace(name="Material"), library=None,
                                 node_tree=tree)
            library = types.SimpleNamespace(name="hero.blend")
            hero = self.driven("Object", "Hero", [scripted('["blink"]')], library=library, modifiers=[],
                               particle_systems=[])
            prop = self.driven("Object", "Prop", [scripted("location", 1, simple=False)], library=library,
                               modifiers=[], particle_systems=[])
            data = types.SimpleNamespace(objects=[rig, hero, prop], materials=[material], scenes=[scene],
                                         libraries=[], all_ids=[rig, hero, prop, material])  # Every ID again.
            before = [list(block.animation_data.drivers) for block in (rig, tree, hero, prop)]
            fake = self.blender(data, scene, stage, lambda **kw: [])
            with patch.dict(sys.modules, {"bpy": fake}):
                info = bundles._collect_opened(snapshot, stage, None)
            self.assertEqual(info["missing"], [])
            self.assertEqual(info["unsupported"], [
                {"type": "driver", "name": 'Object "Armature"', "path": "location[2]", "reason": bundles.PYTHON_DRIVER},
                {"type": "driver", "name": 'Object "Armature"', "path": '["sway"]', "reason": bundles.PYTHON_DRIVER},
                {"type": "driver", "name": 'Object "Armature"', "path": "rotation_euler[0]",
                 "reason": bundles.PYTHON_DRIVER},
                {"type": "driver", "name": 'Object "Prop"', "path": "location[1]",
                 "reason": bundles.LINKED_PYTHON_DRIVER},
                {"type": "driver", "name": 'Material "Skin"', "path": "node_tree." + roughness,
                 "reason": bundles.PYTHON_DRIVER}])
            for text in ("simple expression", "bake"):
                self.assertIn(text, bundles.PYTHON_DRIVER)
            self.assertIn("making the data local", bundles.LINKED_PYTHON_DRIVER)
            # Nothing is dropped, baked or rewritten: the bundle is the scene as the user has it.
            self.assertEqual([list(block.animation_data.drivers) for block in (rig, tree, hero, prop)], before)
            self.assertEqual([block.animation_data.action for block in (rig, tree, hero, prop)], [None] * 4)

    def test_second_driver_rule_is_the_render_services(self):
        # Shared with render_job.py animates_drivers: any F-curve whose data path writes into animation data.
        for path, refused in (("animation_data.drivers[0].mute", True), ("animation_data.drivers[3].driver.type", True),
                              ("location", False), ('pose.bones["hand"]["ik_fk"]', False),
                              ('key_blocks["Smile"].value', False), ('nodes["Mix"].inputs[0].default_value', False)):
            with self.subTest(path=path):
                self.assertIs(bundles.animates_drivers(types.SimpleNamespace(data_path=path)), refused)

    def test_animation_that_changes_drivers_is_refused_by_name_and_ordinary_animation_packs(self):
        # Measured on the worker (Blender 5.2.2, 2026-10-21): an action keyframing animation_data.drivers[0].mute turned
        # a muted Python driver on mid-render. The service refuses keyframes in any action and drivers on any data-block
        # whose path enters animation data (render_job.py secure_scene, driver_animated), so they are refused here too.
        def action(name, paths, library=None):
            bag = types.SimpleNamespace(fcurves=[types.SimpleNamespace(data_path=path, array_index=index)
                                                 for path, index in paths])
            layer = types.SimpleNamespace(strips=[types.SimpleNamespace(channelbags=[bag])])
            return self.item(name=name, bl_rna=types.SimpleNamespace(name="Action"), library=library, layers=[layer])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            snapshot, stage = root / "snapshot.blend", root / "bundle"
            snapshot.write_bytes(b"snapshot")
            stage.mkdir()
            scene = self.scene()
            drivers = [scripted("location", 2, simple=False, mute=True), scripted('["switch"]'),
                       scripted("rotation_euler", 1, simple=False),
                       scripted("animation_data.drivers[0].driver.type", kind="AVERAGE")]
            cube = self.driven("Object", "Cube", drivers, library=None, modifiers=[], particle_systems=[])
            sneaky = action("Sneaky", [("location", 0), ("animation_data.drivers[0].mute", 0)])
            walk = action("Walk", [("location", 1), ('pose.bones["hand"]["ik_fk"]', 0), ('["switch"]', 0)])
            borrowed = action("Rig", [("animation_data.drivers[1].mute", 0)], library=types.SimpleNamespace(name="r"))
            legacy = self.item(name="Old", bl_rna=types.SimpleNamespace(name="Action"), library=None)  # No layers.
            data = types.SimpleNamespace(objects=[cube], actions=[sneaky, walk, borrowed, legacy], scenes=[scene],
                                         libraries=[], all_ids=[cube, sneaky, walk, borrowed, legacy])
            fake = self.blender(data, scene, stage, lambda **kw: [])
            with patch.dict(sys.modules, {"bpy": fake}):
                info = bundles._collect_opened(snapshot, stage, None)
            self.assertEqual(info["missing"], [])
            # The worker refuses Python drivers first, so their rows come first.
            self.assertEqual(info["unsupported"], [
                {"type": "driver", "name": 'Object "Cube"', "path": "rotation_euler[1]",
                 "reason": bundles.PYTHON_DRIVER},
                {"type": "driver", "name": 'Object "Cube"', "path": "animation_data.drivers[0].driver.type",
                 "reason": bundles.DRIVER_ANIMATED},
                {"type": "keyframes", "name": 'Action "Sneaky"', "path": "animation_data.drivers[0].mute",
                 "reason": bundles.DRIVER_ANIMATED},
                {"type": "keyframes", "name": 'Action "Rig"', "path": "animation_data.drivers[1].mute",
                 "reason": bundles.LINKED_DRIVER_ANIMATED}])
            self.assertIn("Remove the keyframes or drivers on driver settings", bundles.DRIVER_ANIMATED)
            self.assertIn("making the data local", bundles.LINKED_DRIVER_ANIMATED)
            # Ordinary keyframes and drivers, a rig's switches included, are not refused.
            cube.animation_data.drivers[1:] = cube.animation_data.drivers[1:2]
            data.actions[:] = [walk, legacy]
            stage = root / "again"
            stage.mkdir()
            fake = self.blender(data, scene, stage, lambda **kw: [])
            with patch.dict(sys.modules, {"bpy": fake}):
                self.assertEqual(bundles._collect_opened(snapshot, stage, None)["unsupported"], [])

    def test_collect_launches_only_an_isolated_child_and_rejects_reusing_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot, stage = root / "snapshot.blend", root / "bundle"
            snapshot.write_bytes(b"snapshot")
            fake = types.SimpleNamespace(app=types.SimpleNamespace(binary_path="/fake/blender"))

            def child(command, **kwargs):
                self.assertEqual(command[0], "/fake/blender")
                self.assertIn("--factory-startup", command)
                self.assertIn("--disable-autoexec", command)
                self.assertIn("--python-exit-code", command)
                self.assertNotIn("LOOPCUT_API_KEY", kwargs["env"])
                self.assertEqual(kwargs["env"]["LOOPCUT_WORKER"], "1")
                control = Path(command[-2]).parent
                self.assertEqual(kwargs["env"]["LOOPCUT_DATA_DIR"], str(control / "data"))
                self.assertEqual(kwargs["env"]["LOOPCUT_CONFIG_DIR"], str(control / "config"))
                self.assertEqual(kwargs["env"]["BLENDER_USER_CONFIG"], str(control / "blender-config"))
                self.assertTrue((control / "blender-config").is_dir())
                spec = json.loads(Path(command[-2]).read_text())
                self.assertEqual(spec["blend"], str(snapshot.resolve()))
                self.assertEqual(spec["original"], "/original/project.blend")
                (Path(spec["staging"]) / "scene.blend").write_bytes(b"bundle")
                Path(command[-1]).write_text(json.dumps(INFO))
                return types.SimpleNamespace(returncode=0)

            with patch.dict(sys.modules, {"bpy": fake}), patch.object(bundles.subprocess, "run", side_effect=child) as run:
                self.assertEqual(bundles.collect(snapshot, stage, "/original/project.blend"), INFO)
                with self.assertRaises(bundles.BundleError):
                    bundles.collect(snapshot, stage, "/original/project.blend")
                self.assertEqual(run.call_count, 1)
            self.assertEqual(snapshot.read_bytes(), b"snapshot")

    def test_child_opens_recovery_files_as_recovery_and_stock_copies_normally_without_scripts(self):
        with tempfile.TemporaryDirectory() as temporary:
            request, reply = Path(temporary) / "request.json", Path(temporary) / "reply.json"
            calls = []

            def opened(kind, **kwargs):
                calls.append((kind, kwargs))
                return {"FINISHED"}

            fake = types.SimpleNamespace(ops=types.SimpleNamespace(wm=types.SimpleNamespace(
                recover_auto_save=lambda **kw: opened("recovery", **kw),
                open_mainfile=lambda **kw: opened("copy", **kw))))
            with patch.dict(sys.modules, {"bpy": fake}), \
                    patch.object(sys, "argv", ["blender", "--", str(request), str(reply)]), \
                    patch.object(bundles, "_collect_opened", return_value=INFO) as collect:
                for original in ("/original/project.blend", None):
                    request.write_text(json.dumps({"blend": "/store/snapshot.blend", "staging": "/stage",
                                                   "original": original}))
                    bundles._worker_main()
                    self.assertEqual(json.loads(reply.read_text()), INFO)
                    self.assertEqual(collect.call_args.args, ("/store/snapshot.blend", "/stage", original))
            self.assertEqual([call[0] for call in calls], ["recovery", "copy"])
            self.assertTrue(all(call[1]["use_scripts"] is False for call in calls))

    def test_sequence_regex_does_not_expand_punctuation_and_udim_collects_tiles(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("tile.1001.png", "tile.1002.png", "tileX1003.png", "frame[1]001.png", "frame[1]002.png"):
                (root / name).write_bytes(b"x")
            self.assertEqual([p.name for p in bundles._members(root / "tile.<UDIM>.png", True)],
                             ["tile.1001.png", "tile.1002.png"])
            self.assertEqual(len(bundles._members(root / "frame[1]001.png", True)), 2)


class CheckpointBundleTest(unittest.TestCase):
    def test_index_selects_recovery_vs_copy_save_base_and_preserves_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = checkpoints.Store(Path(temporary), SOURCE["session"])
            store.folder.mkdir(parents=True)
            store.snapshot_path(SOURCE["checkpoint"]).write_bytes(b"snapshot")
            entry = {"id": SOURCE["checkpoint"], "expired": False, "source": "/original/project.blend",
                     "in_place": True, "created": 1791043200}
            store.entries = [entry]
            stage = Path(temporary) / "bundle"

            def collect(snapshot, staging, original_path=None):
                self.assertEqual(snapshot, store.snapshot_path(SOURCE["checkpoint"]))
                staging.mkdir(exist_ok=True)
                (staging / "scene.blend").write_bytes(b"bundled")
                return {**copy.deepcopy(INFO), "missing": [{"path": "missing.png"}], "system": ["/SYSTEM/brush.blend"]}

            with patch.object(checkpoints, "_store", return_value=store), \
                    patch.object(bundles, "collect", side_effect=collect) as called:
                result = checkpoints.bundle_from_checkpoint({"id": SOURCE["session"]}, SOURCE["checkpoint"], stage)
                self.assertEqual(called.call_args.kwargs["original_path"], entry["source"])
                self.assertEqual(result["missing"], [{"path": "missing.png"}])
                self.assertEqual(result["system"], ["/SYSTEM/brush.blend"])
                self.assertEqual(result["manifest"]["source"], SOURCE)
                entry["in_place"] = False
                checkpoints.bundle_from_checkpoint({"id": SOURCE["session"]}, SOURCE["checkpoint"], stage)
                self.assertIsNone(called.call_args.kwargs["original_path"])
                entry["expired"] = True
                with self.assertRaises(checkpoints.CheckpointError):
                    checkpoints.bundle_from_checkpoint({"id": SOURCE["session"]}, SOURCE["checkpoint"], stage)
                self.assertEqual(called.call_count, 2)


if __name__ == "__main__":
    unittest.main()
