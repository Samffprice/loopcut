"""Public snapshots must leave worktrees intact and fail closed on leaks.

All Git writes happen in throwaway repositories. Sensitive positive samples
are synthetic and assembled at runtime so this source stays scan-clean.
The real repository is never passed to the release script.
"""

import base64
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "public_release.py"
EXCLUDES = SCRIPT.with_name("public_release_exclude.txt")
spec = importlib.util.spec_from_file_location("public_release_c8", SCRIPT)
release = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = release
spec.loader.exec_module(release)


def opaque(length=48):
    alphabet = "aB3dE6gH9jK2mN5pQ8sT1vW4yZ7"
    return (alphabet * (length // len(alphabet) + 1))[:length]


def jwt(role):
    def segment(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return ".".join((segment({"alg": "HS256", "typ": "JWT"}),
                     segment({"role": role, "ref": "synthetic"}), opaque(43)))


def private_text():
    return " ".join(("Synthetic fixture", "instructions describe", "imaginary widgets",
                     "for this temporary test only."))


def api_lines():
    return (
        "bpy.context.scene.collection.objects.link(obj)",
        "scene.frame_start, scene.frame_end = 1, 120",
        'scene.render.image_settings.file_format = "FFMPEG"',
        'json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,',
    )


def withheld_modules():
    """Mirror production assignment shapes using only synthetic instructions."""
    modules = {
        "orchestrator.py": (39, 'PROMPT = """## Synthetic widgets\nSynthetic fixture instructions.\n"""\n',
                            "## Synthetic widgets\nSynthetic fixture instructions.\n"),
        "router.py": (40, 'PROMPT = """Synthetic routing instructions with \\\ncontinued physical lines for a fixture.\n"""\n',
                      "Synthetic routing instructions with continued physical lines for a fixture.\n"),
        "workers.py": (15, 'PROMPT = f"""Synthetic worker instructions with \\\ncontinued lines. {ownership.SYNTHETIC_RULE}\nSynthetic trailing instructions.\n"""\n',
                       "Synthetic worker instructions with continued lines. \n\nSynthetic trailing instructions.\n"),
        "ownership.py": (55, 'SYNTHETIC_RULE = ("Synthetic material instructions "\n'
                            '                  "continued by implicit concatenation.")\n',
                         "Synthetic material instructions continued by implicit concatenation."),
    }

    return {name: (line, code.replace("SYNTHETIC_RULE", release.WITHHELD["ownership.py"]), value)
            for name, (line, code, value) in modules.items()}


class SourceTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("cmake") and shutil.which("cpack"), "CMake/CPack unavailable")
    def test_cpack_staging_guard_strips_caches_before_archive_creation(self):
        with tempfile.TemporaryDirectory(prefix="loopcut-cpack-test-") as temp:
            root = Path(temp)
            payload = root / "payload"
            payload.mkdir()
            (payload / "public.py").write_text("# Synthetic package fixture\n")
            guard = release.REPO / "build_files/cmake/loopcut_release_hygiene.cmake"
            (root / "CMakeLists.txt").write_text(
                'cmake_minimum_required(VERSION 3.19)\nproject(Hygiene NONE)\n'
                'install(DIRECTORY payload/ DESTINATION .)\n'
                f'set(CPACK_PRE_BUILD_SCRIPTS "{guard.as_posix()}")\ninclude(CPack)\n')
            build = root / "build"
            subprocess.run(["cmake", "-S", str(root), "-B", str(build)], check=True, capture_output=True)
            for name in (None, "__pycache__/x.cpython-313.pyc", "nested/x.pyc", "nested/x.pyo"):
                with self.subTest(name=name):
                    if name:
                        path = payload / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text("Synthetic cache fixture")
                    for old in build.glob("*.zip"):
                        old.unlink()
                    result = subprocess.run(["cpack", "-G", "ZIP"], cwd=build, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("Release hygiene: removed", result.stdout + result.stderr)
                    [archive] = build.glob("*.zip")
                    with zipfile.ZipFile(archive) as packaged:
                        names = packaged.namelist()
                    self.assertTrue(any(n.endswith("public.py") for n in names), names)
                    self.assertFalse([n for n in names if "__pycache__" in n or n.endswith((".pyc", ".pyo"))], names)
                    if name:
                        path.unlink()

    @unittest.skipUnless(shutil.which("zip"), "zip unavailable")
    def test_package_archive_filters_caches_from_existing_install_tree(self):
        with tempfile.TemporaryDirectory(prefix="loopcut-package-test-") as temp:
            root = Path(temp)
            installed = root / "installed"
            (installed / "__pycache__").mkdir(parents=True)
            for name in ("public.py", "__pycache__/cached.pyc", "loose.pyc", "loose.pyo"):
                (installed / name).write_text("Synthetic package fixture")
            script = release.REPO / "build_files/package_spec/build_archive.py"
            subprocess.run([sys.executable, str(script), "fixture", "zip", "installed", "packages"],
                           cwd=root, check=True, capture_output=True)
            with zipfile.ZipFile(root / "packages/fixture.zip") as archive:
                self.assertIn("fixture/public.py", archive.namelist())
                self.assertFalse(any(release.bytecode_path(name) for name in archive.namelist()))
            self.assertTrue((installed / "loose.pyc").exists())

    def test_build_input_patterns_match_cmake_and_python_sources_exclude_bytecode(self):
        actual = release.cmake_build_patterns(release.REPO)
        self.assertEqual({name: set(patterns) for name, patterns in actual.items()},
                         {name: set(patterns) for name, patterns in release.BUILD_INPUT_PATTERNS.items()})
        # Our Python sources never ship bytecode; other inputs are scanned and block on any
        # bytecode their rules would not exclude, and CPack's hygiene hook strips the package.
        for name in ("scripts", "release/extensions", "release/darwin/Blender.app/Contents/Resources"):
            with self.subTest(source=name):
                self.assertTrue(set(release.BYTECODE_PATTERNS) <= set(actual[name]))

    def test_cmake_patterns_match_suffixes_and_ancestors(self):
        for path in ("scripts/addons_core/loopcut/__pycache__/x.bin", "scripts/nested/x.pyc",
                     "release/extensions/x.pyo", "__pycache__/empty"):
            self.assertTrue(release.bytecode_path(path), path)
        self.assertFalse(release.bytecode_path("scripts/cache.pyc.txt"))
        self.assertFalse(release.cmake_excluded("scripts/sitecustomize.py", ("site",)))
        self.assertTrue(release.cmake_excluded("scripts/addons_core/bl_pkg/tests/a.py", (
            "addons_core/bl_pkg/tests",)))

    def test_all_private_eval_tests_are_excluded_by_shipped_rules(self):
        patterns = release.exclusions(EXCLUDES)
        root = SCRIPT.parents[2]
        flagged = set()
        for path in sorted(Path(__file__).parent.glob("*.py")):
            name = path.relative_to(root).as_posix()
            scanner = release.Scanner([], {})
            scanner.private_import(path.read_text(encoding="utf-8"), name, "tree")
            if scanner.hits:
                flagged.add(path.name)
                self.assertTrue(release.excluded(name, patterns), name)
        # The guard also works in a public checkout, where these files are absent.
        expected = {"test_chatgpt_eval.py", "test_codex_answer.py", "test_eval_evidence.py",
                    "test_eval_quality.py", "test_eval_m4.py", "test_eval_m0.py", "test_eval_reports.py"}
        present = {path.name for path in Path(__file__).parent.glob("*.py")}
        self.assertTrue(expected & present <= flagged)

    def test_own_source_has_no_leak_hits(self):
        path = Path(__file__)
        text = path.read_text(encoding="utf-8")
        samples = [sample for category in ("private-text", "research-text", "cost-fingerprint")
                   for value in (private_text(), "Another " + private_text())
                   for sample in release.fingerprints(value, category, "fixture.txt")]
        self.assertEqual(len(samples), 6)
        for scope in ("tree", "history"):
            with self.subTest(scope=scope):
                scanner = release.Scanner(samples, {})
                scanner.filename(path.name, scope)
                scanner.text(text, path.name, scope)
                scanner.withheld(text, path.name, scope)
                self.assertEqual([(hit.category, hit.line) for hit in scanner.hits], [])


class PrivateImportTest(unittest.TestCase):
    def test_path_mutations_and_importlib_loaders(self):
        fixtures = (
            'sys.path.insert(0, str(ROOT / "harness" / "evals"))',
            'sys.path.append(os.path.join(ROOT, "harness", "evals"))',
            'sys.path = ["harness/evals"] + sys.path',
            'sys.path[:] = ["harness/evals"]',
            'sys.path += ["harness/evals"]',
            'sys.path: list = ["harness/evals"]',
            'EVALS = ROOT / "harness" / "evals"\nALIAS = EVALS\nsys.path.insert(0, str(ALIAS))',
            'import sys as system\nsystem.path.append("harness/evals")',
            'from sys import path as search_path\nsearch_path.append("harness/evals")',
            'importlib.util.spec_from_file_location("fixture", ROOT / "harness" / "evals" / "fixture.py")',
            'SOURCE = os.path.join(ROOT, "harness", "evals", "fixture.py")\n'
            'importlib.util.spec_from_file_location("fixture", location=SOURCE)',
            'from importlib.util import spec_from_file_location as load\n'
            'load("fixture", "harness/evals/fixture.py")',
            'importlib.machinery.SourceFileLoader("fixture", "harness/evals/fixture.py")',
            'importlib.import_module("harness.evals.fixture")',
        )
        for code in fixtures:
            with self.subTest(code=code):
                scanner = release.Scanner([], {})
                scanner.private_import(code, "loopcut/tests/test_fixture.py", "tree")
                self.assertEqual([hit.category for hit in scanner.hits], ["private_import"])
                self.assertEqual(scanner.hits[0].line, len(code.splitlines()))

    def test_docstrings_comments_and_unrelated_paths_are_not_imports(self):
        code = '''"""A run under out/evals/fixture and sys.path.insert(0, 'harness/evals')."""
# sys.path.append("harness/evals")
SAMPLE = 'sys.path.append("harness/evals")'
EVALS = ROOT / "harness" / "evals"
sys.path.insert(0, str(ROOT / "harness"))
sys.path.append("harness/evals_archive")
importlib.util.spec_from_file_location("evals", ROOT / "harness" / "fixture.py")
importlib.util.spec_from_file_location("fixture", ROOT / "out" / "evals" / "fixture.py")
'''
        scanner = release.Scanner([], {})
        scanner.private_import(code, "loopcut/tests/test_fixture.py", "tree")
        self.assertEqual(scanner.hits, [])

    def test_parse_failure_is_an_incomplete_check(self):
        scanner = release.Scanner([], {})
        scanner.check(scanner.private_import, "sys.path.append(", "loopcut/tests/test_fixture.py", "tree")
        self.assertTrue(scanner.incomplete)
        self.assertIn("Cannot parse public test", scanner.errors[0])


class LocalPathTest(unittest.TestCase):
    # Built from parts so this file holds none of the paths it checks for.
    TMP = "/".join(("", "private", "tmp"))

    def test_private_disk_checkouts_and_macos_temp_are_blocked_and_not_allowlistable(self):
        contract = (release.PRIVATE_HOME / "loopcut-web-platform" / "contract").as_posix()
        fixtures = [f'CONTRACT = Path("{contract}")',
                    f'tempfile.TemporaryDirectory(dir="{self.TMP}")',
                    *(f'ROOT = "/home/runner/work/{name}/skills"' for name in release.PRIVATE_REPOS)]
        for code in fixtures:
            with self.subTest(code=code):
                scanner = release.Scanner([], {})
                scanner.local_path("import os\n" + code + "\n", "loopcut/tests/test_fixture.py", "tree")
                self.assertEqual([(hit.category, hit.line) for hit in scanner.hits], [("local_path", 2)])
                hit = scanner.hits[0]
                allowlist = {hit.fingerprint_sha256: "reviewed", hit.line_sha256: "reviewed"}
                scanner = release.Scanner([], allowlist)
                scanner.local_path(code, "loopcut/tests/test_fixture.py", "tree")
                self.assertFalse(scanner.hits[0].allowed)

    def test_urls_relative_names_and_synthetic_paths_pass(self):
        code = "\n".join((
            'URL = "https://github.com/Samffprice/loopcut-web-platform"',
            'contract = folder / "loopcut-web-platform" / "contract"',
            'install_target("/Volumes/Loopcut/Loopcut.app/Contents/MacOS/Loopcut", "darwin", "/Users/me")',
            'env = {"TMPDIR": "/var/folders/x", "HOME": "/home/me/loopcut-web-platformer"}',
            f'other = "{self.TMP}x"'))
        scanner = release.Scanner([], {})
        scanner.local_path(code, "loopcut/tests/test_fixture.py", "tree")
        self.assertEqual(scanner.hits, [])

    def test_public_tests_name_no_local_paths(self):
        """The release rule, run here too, so a local test run catches what would fail in CI."""
        patterns = release.exclusions(EXCLUDES)
        root = SCRIPT.parents[2]
        for path in sorted(Path(__file__).parent.glob("*.py")):
            name = path.relative_to(root).as_posix()
            if release.excluded(name, patterns):
                continue
            scanner = release.Scanner([], {})
            scanner.local_path(path.read_text(encoding="utf-8"), name, "tree")
            self.assertEqual([hit.line for hit in scanner.hits], [], name)


class FingerprintTest(unittest.TestCase):
    def test_every_fifth_physical_line_and_first_line(self):
        lines = ["import unusually_long_module_name_that_is_only_boilerplate", "skip " + private_text(),
                 "short", "short", private_text(), "not sampled " + private_text(),
                 "short", "short", "short", "Another " + private_text()]
        samples = release.fingerprints("\n".join(lines), "private-text", "fixture")
        self.assertEqual([item.line for item in samples], [5, 10])
        self.assertTrue(all(len(item.text) == 40 for item in samples))
        self.assertEqual(len(release.fingerprints(private_text(), "private-text", "single-line")), 1)

    def test_normalizes_before_sizing_and_hashing(self):
        sample = release.fingerprints("   " + private_text().replace(" ", " \t "), "private-text", "fixture")[0]
        self.assertEqual(sample.text, private_text()[:40])
        self.assertEqual(release.digest(" hello \n world "), release.digest("hello world"))
        self.assertEqual(release.fingerprints(" " * 80 + "short", "private-text", "fixture"), [])
        self.assertEqual(len(release.fingerprints("合成测试文本" * 10, "cost-fingerprint", "fixture")), 1)

    def test_skips_boilerplate(self):
        for text in ("from enormous_module_name_used_only_for_imports import functions",
                     "import enormous_module_name_used_only_for_imports",
                     "export import enormous_module_name_used_only_for_imports",
                     "{}[](),;" * 20, '"abcdefghijklmnopqrstuvwx": "abcdefghijklmno",'):
            with self.subTest(text=text):
                self.assertEqual(release.fingerprints(text, "private-text", "fixture"), [])

    def test_wrapped_fingerprints_and_each_occurrence_have_locations(self):
        samples = release.fingerprints(private_text(), "private-text", "private/fixture")
        scanner = release.Scanner(samples, {})
        scanner.text("prefix\n" + private_text().replace("instructions ", "instructions\n") +
                     "\n" + private_text(), "public.md", "tree")
        self.assertEqual([hit.line for hit in scanner.hits], [2, 4])
        self.assertTrue(all(hit.source == "private/fixture" for hit in scanner.hits))

    def test_common_api_lines_never_produce_prompt_or_skill_hits(self):
        for category in ("private-text", "research-text"):
            for suffix in (".md", ".py", ".js", ".txt"):
                for line in api_lines():
                    with self.subTest(category=category, suffix=suffix, line=line):
                        samples = release.fingerprints(line, category, "fixture" + suffix)
                        scanner = release.Scanner(samples, {})
                        scanner.text(line, "public.py", "tree")
                        self.assertEqual(scanner.hits, [])

    def test_markdown_fences_skip_even_prose_and_keep_source_locations(self):
        for category in ("private-text", "research-text"):
            for fence in ("```", "~~~~"):
                text = fence + "python\n\n\n\n" + private_text() + "\n" + fence + "\n\n\n\n" + private_text()
                with self.subTest(category=category, fence=fence):
                    samples = release.fingerprints(text, category, "fake-private-skill.md")
                    self.assertEqual([sample.line for sample in samples], [10])
                    scanner = release.Scanner(samples, {})
                    scanner.text("    " + private_text().replace("instructions ", "instructions\n        "),
                                 "public.md", "tree")
                    self.assertEqual([hit.line for hit in scanner.hits], [1])
                    self.assertEqual(scanner.hits[0].category, category)

    def test_source_comments_and_strings_match_reflowed_prose(self):
        fixtures = (
            ("fixture.py", '# ' + private_text()),
            ("fixture.py", 'PROMPT = """' + private_text() + '"""'),
            ("fixture.py", 'PROMPT = f"""' + private_text() + ' {synthetic}"""'),
            ("fixture.py", 'PROMPT = r"""' + private_text() + '"""'),
            ("fixture.py", 'description = "' + private_text() + '"'),
            ("fixture.js", '// ' + private_text()),
            ("fixture.js", '/* ' + private_text() + ' */'),
            ("fixture.js", 'const prompt = `' + private_text() + '`;'),
            ("fixture.js", 'const description = "' + private_text() + '";'),
        )
        for category in ("private-text", "research-text"):
            for source, text in fixtures:
                with self.subTest(category=category, source=source, text=text):
                    samples = release.fingerprints(text, category, source)
                    self.assertEqual(len(samples), 1)
                    self.assertEqual(samples[0].text, private_text()[:40])
                    scanner = release.Scanner(samples, {})
                    scanner.text("prefix\n    " + private_text().replace(" ", "\n        "),
                                 "public.md", "tree")
                    self.assertEqual([hit.line for hit in scanner.hits], [2])
                    self.assertEqual(scanner.hits[0].source_line, 1)

    def test_prose_requires_eight_words_and_rejects_code_inside_strings(self):
        negatives = (
            "Synthetic instructions describe imaginary widgets in fixture.",
            "one two three four five six seven " + "{}[](),;" * 20,
            "widget.configure(one, two, three, four, five, six, seven, eight)",
            "value = one + two + three + four + five + six + seven + eight",
        )
        for category in ("private-text", "research-text"):
            for text in negatives:
                with self.subTest(category=category, text=text):
                    self.assertEqual(release.fingerprints(text, category, "fixture.txt"), [])
                    self.assertEqual(release.fingerprints('value = "' + text + '"', category, "fixture.py"), [])

    def test_economics_fingerprints_still_include_non_prose(self):
        samples = release.fingerprints(api_lines()[0], "cost-fingerprint", "fixture.py")
        self.assertEqual(len(samples), 1)


class PrivateSamplesTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="loopcut-private-samples-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.prompts = self.root / "prompts"
        self.skills = self.root / "skills"
        self.prompts.mkdir()
        self.skills.mkdir()
        self.args = release.parser(True).parse_args([
            "base..source", "--prompts-dir", str(self.prompts), "--skills-dir", str(self.skills),
            "--research-dir", str(self.root / "missing-research"),
            "--economics-file", str(self.root / "missing-economics")])

    def test_open_text_at_any_offset_filters_prompts_and_skills(self):
        opened, private = private_text(), "Restricted " + private_text()
        # Neither an unsampled line nor a code fence limits the public text corpus.
        (self.prompts / "base.md").write_text(
            "Open only " + private_text() + "\n```\nprefix " + opened.replace(" ", "\t\n") + "\n```\n")
        (self.prompts / "manifest.json").write_text(json.dumps({
            "open": ["base.md"], "prompts": {"single": {"parts": ["base.md"]}}}))
        for root in (self.prompts, self.skills):
            (root / "private.md").write_text(opened + "\n\n\n\n" + private)
        samples, coverage = release.private_samples(self.args)
        self.assertEqual([(sample.source, sample.line) for sample in samples],
                         [(str(root / "private.md"), 5) for root in (self.skills, self.prompts)])
        scanner = release.Scanner(samples, {})
        scanner.text(opened + "\n" + private, "public.md", "tree")
        self.assertEqual([hit.line for hit in scanner.hits], [2, 2])
        for item in coverage[:2]:
            self.assertEqual(item["files"], 1)
            self.assertEqual(item["fingerprints"], 1)
            self.assertEqual(item["open_fingerprints_dropped"], 1)

    def test_no_manifest_preserves_all_fingerprints(self):
        for name in ("base.md", "private.md"):
            (self.prompts / name).write_text(private_text())
        samples, coverage = release.private_samples(self.args)
        expected = [sample for path in sorted(self.prompts.iterdir())
                    for sample in release.fingerprints(path.read_text(), "private-text", path)]
        self.assertEqual(samples, expected)
        scanner = release.Scanner(samples, {})
        scanner.text(private_text(), "public.md", "tree")
        self.assertEqual(len(scanner.hits), 2)
        self.assertTrue(all(item["open_fingerprints_dropped"] == 0 for item in coverage))

    def test_malformed_manifests_fail_with_clear_errors(self):
        (self.prompts / "base.md").write_text(private_text())
        (self.prompts / "fixtures").mkdir()
        (self.prompts / "fixtures/private.md").write_text(private_text())
        (self.prompts / "README.md").write_text(private_text())
        invalid = ["{", "[]", '{"open": "base.md"}', '{"open": [1]}']
        invalid.extend(json.dumps({"open": [path]}) for path in (
            "", ".", "./base.md", "../base.md", "nested/../base.md", "/base.md",
            "nested//base.md", "missing.md", "fixtures", "fixtures/private.md", "README.md", "manifest.json"))
        invalid.extend(json.dumps({"prompts": value}) for value in (
            [], {"single": None}, {"single": {"parts": []}}, {"single": {"parts": "base.md"}},
            *({"single": {"parts": [part]}} for part in (None, "../base.md", "/base.md", "missing.md"))))
        for content in invalid:
            with self.subTest(content=content):
                (self.prompts / "manifest.json").write_text(content)
                with self.assertRaisesRegex(release.ReleaseError, "[Mm]anifest"):
                    release.private_samples(self.args)


class SecretTest(unittest.TestCase):
    def test_every_provider_positive_and_negative(self):
        hexadecimal = "0123456789abcdef" * 2
        uuid = "-".join(("01234567", "89ab", "cdef", "0123", "456789abcdef"))
        positives = {
            "openai": ["sk-" + opaque(), "sk-proj-" + opaque(80)],
            "loopcut": ["lc_" + opaque()],
            "anthropic": ["sk-ant-api03-" + opaque(80)],
            "google": ["AIza" + opaque(35)],
            "fal": [uuid + ":" + hexadecimal, hexadecimal + ":" + hexadecimal, uuid + ":" + uuid],
            "elevenlabs": ["xi-" + opaque(), "sk_" + opaque(),
                           '"xi-api-key": "' + hexadecimal + '"',
                           "ELEVENLABS_API_KEY=" + opaque()],
            "github": [prefix + opaque(40) for prefix in ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_")],
            "stripe": [prefix + opaque(24) for prefix in ("sk_live_", "sk_test_", "rk_live_", "rk_test_")],
            "aws": [prefix + "0123456789ABCDEF" for prefix in ("AKIA", "ASIA")],
            "supabase-service-role": [jwt("service_role")],
            "pem-private-key": [" ".join(("-----BEGIN", prefix + "PRIVATE", "KEY-----"))
                                for prefix in ("", "RSA ", "EC ", "DSA ", "OPENSSH ", "ENCRYPTED ")],
        }
        self.assertEqual(set(positives), set(release.SECRET_PATTERNS))
        for provider, values in positives.items():
            for value in values:
                with self.subTest(provider=provider, value_length=len(value)):
                    self.assertEqual({name for name, _ in release.secret_hits(value)}, {provider})
        negatives = {
            "openai": "sk-...",
            "loopcut": "lc_" + "x" * 48,
            "anthropic": "sk-ant-" + "x" * 48,
            "google": "AIza" + "x" * 35,
            "fal": "-".join("0" * length for length in (8, 4, 4, 4, 12)) + ":" + "0" * 32,
            "elevenlabs": "xi-api-key=" + "x" * 32,
            "github": "ghp_" + "x" * 36,
            "stripe": "sk_test_" + "x" * 24,
            "aws": "AKIA" + "0" * 16,
            "supabase-service-role": jwt("anon"),
            "pem-private-key": " ".join(("-----BEGIN", "PUBLIC", "KEY-----")),
        }
        self.assertEqual(set(negatives), set(release.SECRET_PATTERNS))
        for provider, value in negatives.items():
            with self.subTest(provider=provider):
                self.assertEqual(list(release.secret_hits(value)), [])

    def test_placeholders_short_tokens_and_invalid_jwts_are_ignored(self):
        for value in ("sk-your_api_key_here_" + opaque(), "sk-example_" + opaque(),
                      "sk-" + opaque(10), "lc_placeholder_" + opaque(), "OPENAI_API_KEY=",
                      ".".join(("eyJbroken", "eyJbroken", opaque()))):
            with self.subTest(length=len(value)):
                self.assertEqual(list(release.secret_hits(value)), [])

    def test_sensitive_files_and_populated_examples(self):
        for name in (".env", "config/.env.local", "config/.env.production", "credentials.json"):
            self.assertTrue(release.sensitive_filename(name), name)
        self.assertFalse(release.sensitive_filename("config/.env.example"))
        scanner = release.Scanner([], {})
        scanner.filename(".env.example", "tree")
        scanner.text("OPENAI_API_KEY=\nLOOPCUT_API_KEY=", ".env.example", "tree")
        self.assertEqual(scanner.hits, [])
        scanner.text("OPENAI_API_KEY=" + "sk-" + opaque(), ".env.example", "tree")
        self.assertEqual(scanner.hits[0].category, "secret:openai")
        scanner.filename("fixture/" + "lc_" + opaque() + ".txt", "zip")
        self.assertEqual(scanner.hits[-1].category, "secret:loopcut")

    def test_cost_terms_need_review(self):
        scanner = release.Scanner([], {})
        lines = (" ".join(("gross", "margin")), " ".join(("margin", "per", "job")),
                 " ".join(("margin", "per", "credit")), " ".join(("markup", "on")),
                 " ".join(("markup", "of")), " ".join(("cost", "per", "credit")),
                 "_".join(("cost", "per", "credit")), "-".join(("cost", "per", "credit")),
                 " ".join(("credits", "per", "dollar")), " ".join(("credits", "per", "USD")),
                 " ".join(("our", "cost")), " ".join(("unit", "economics")),
                 "_".join(("GROSS", "MARGIN")))
        scanner.text("\n".join(lines), "fixture", "tree")
        self.assertEqual([hit.line for hit in scanner.hits], list(range(1, len(lines) + 1)))
        self.assertTrue(all(hit.category == "cost-review" and not hit.allowed for hit in scanner.hits))

    def test_framing_uv_and_craft_terms_do_not_need_cost_review(self):
        scanner = release.Scanner([], {})
        scanner.text("\n".join(("margin", "markup", "camera framing margin", "UV island margins",
                                "5-10 % margin", "margin = 0.1", "markup text")), "fixture", "tree")
        self.assertEqual(scanner.hits, [])


class AllowlistTest(unittest.TestCase):
    def test_real_assignment_shapes_fingerprint_values_without_evaluation(self):
        scanner = release.Scanner([], {})
        for filename, (line, assignment, literal) in withheld_modules().items():
            with self.subTest(filename=filename):
                scanner.withheld("\n" * (line - 1) + assignment, filename, "tree")
                hit = scanner.hits[-1]
                self.assertEqual(hit.line, line)
                self.assertEqual(hit.category, "withheld-prompt")
                self.assertEqual(hit.fingerprint_sha256, release.digest(literal))
        self.assertEqual(len(scanner.hits), 4)

    def test_accepts_fingerprint_or_normalized_line_hash_with_reason(self):
        token = "sk-" + opaque()
        line = "sample = '" + token + "'"
        for hash_value in (release.digest(token), release.digest(line)):
            scanner = release.Scanner([], {hash_value: "generated test fixture"})
            scanner.text(line, "fixture.py", "tree")
            self.assertTrue(scanner.hits[0].allowed)
            self.assertEqual(scanner.hits[0].reason, "generated test fixture")
        samples = release.fingerprints(private_text(), "private-text", "fixture")
        scanner = release.Scanner(samples, {release.digest(samples[0].text): "reviewed synthetic overlap"})
        scanner.text(private_text(), "fixture.md", "tree")
        self.assertTrue(scanner.hits[0].allowed)

    def test_strict_hash_only_file_format(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "allowlist"
            hash_value = release.digest("synthetic")
            path.write_text("# explanation\n\n" + hash_value + " reviewed fixture\n")
            self.assertEqual(release.load_allowlist(path), {hash_value: "reviewed fixture"})
            for bad in (hash_value, "not-a-hash reason", hash_value + "   "):
                path.write_text(bad)
                with self.assertRaises(release.ReleaseError):
                    release.load_allowlist(path)

    def test_withheld_ast_constants_cannot_be_allowlisted_or_executed(self):
        text = "PROMPT = f'Synthetic worker text {ownership.SYNTHETIC_RULE}'"
        scanner = release.Scanner([], {})
        scanner.withheld(text, "loopcut/workers.py", "zip")
        hit = scanner.hits[0]
        scanner.allowlist = {hit.fingerprint_sha256: "must not suppress", hit.line_sha256: "must not suppress"}
        scanner.withheld(text, "loopcut/workers.py", "zip")
        self.assertFalse(scanner.hits[-1].allowed)
        self.assertIn("not yet moved", scanner.hits[-1].detail)
        for name, constant in release.WITHHELD.items():
            scanner.withheld(f"{constant}: str = 'Synthetic constant'", name, "zip")
        self.assertEqual(len(scanner.hits), 6)
        scanner.withheld("PROMPT = ''\n# PROMPT = 'comment only'", "router.py", "zip")
        self.assertEqual(len(scanner.hits), 6)


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="loopcut-release-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / "repository"
        self.repo.mkdir()
        self.git("init", "--initial-branch=base")
        self.git("config", "user.name", "Synthetic tester")
        self.git("config", "user.email", "fixture@localhost")
        self.git("config", "commit.gpgSign", "false")
        self.git("config", "gc.auto", "0")
        self.write("public.txt", "base public fixture\n")
        self.write("gone.txt", "deleted public fixture\n")
        self.write(".env.example", "OPENAI_API_KEY=\n")
        self.write("scripts/addons_core/loopcut/public.py", "# Synthetic public add-on fixture\n")
        self.write("loopcut/public.txt", "Synthetic unchanged Loopcut fixture\n")
        self.write("source/upstream.txt", "Synthetic unchanged upstream fixture\n")
        self.commit("Base fixture")
        self.base = self.git("rev-parse", "HEAD").strip().decode()
        self.git("checkout", "-b", "source")
        self.write("public.txt", "source public fixture\n")
        self.write("loopcut/track/internal.md", "Internal planning fixture\n")
        self.write("private/fixture.hidden", "Excluded glob fixture\n")
        self.write("names/a spaced file.txt", "Public named fixture\n")
        self.write("names/:(literal).txt", "Literal path fixture\n")
        (self.repo / "gone.txt").unlink()
        self.commit("Source fixture")
        self.source = self.git("rev-parse", "HEAD").strip().decode()
        self.excludes = self.root / "exclude.txt"
        self.excludes.write_text("# Private fixture planning\nloopcut/track/\n# Fixture glob\nprivate/*.hidden\n")
        self.allowlist = self.root / "allowlist.txt"
        self.allowlist.write_text("# Empty reviewed hashes\n")
        self.skills = self.root / "private-skills"
        self.skills.mkdir()
        self.patterns = release.exclusions(self.excludes)
        # Real install declarations, but no fixture scaffolding in snapshot/history.
        metadata = ("source/creator/CMakeLists.txt", "build_files/cmake/Modules/FindPythonLibsUnix.cmake")
        for name in metadata:
            self.write(name, (release.REPO / name).read_text())
        for name in release.BUILD_INPUT_PATTERNS:
            if name not in release.OPTIONAL_BUILD_INPUTS:
                (self.repo / name).mkdir(parents=True, exist_ok=True)
        for name in release.BUILD_INPUT_FILES:
            self.write(name, "# Synthetic explicit install fixture\n")
        (self.repo / ".git/info/exclude").write_text("\n".join((*metadata, *release.BUILD_INPUT_FILES)) + "\n")

    def git(self, *args, cwd=None):
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS="0")
        return subprocess.run(["git", "-C", str(cwd or self.repo), *args], env=env,
                              check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self, message):
        self.git("add", "--all")
        self.git("commit", "-m", message)

    def build(self):
        return release.build_snapshot(self.repo, "source", "base", self.patterns)

    def options(self, out):
        return ["--base", "base", "--out", str(out), "--allowlist", str(self.allowlist),
                "--skills-dir", str(self.skills), "--prompts-dir", str(self.root / "missing-prompts"),
                "--research-dir", str(self.root / "missing-research"),
                "--economics-file", str(self.root / "missing-economics")]

    def run_main(self, argv):
        if "--dry-run" in argv and "--leak-check" not in argv and "--no-leak-check" not in argv:
            argv = [*argv, "--no-leak-check"]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return release.main(argv, repo=self.repo)

    def test_build_inputs_exclude_ignored_caches_and_block_untracked_leaks(self):
        (self.skills / "fixture.md").write_text(private_text())
        self.write(".gitignore", "__pycache__/\n*.pyc\n*.pyo\n")
        names = [release.ADDON_PATH + "/__pycache__/x.cpython-314.pyc",
                 release.ADDON_PATH + "/loose.pyc", "release/extensions/loose.pyo"]
        for name in names:
            self.write(name, private_text())
        self.assertTrue(self.git("check-ignore", names[0]))
        for command in (["--dry-run", "--source", "source", "--exclude", str(self.excludes)],
                        ["leak-check", "HEAD"]):
            out = self.root / ("cache-" + command[0].lstrip("-"))
            self.assertEqual(self.run_main([*command, *self.options(out)]), 0)
            report = json.loads((out / "report.json").read_text())
            self.assertEqual(report["hits"], [])
            self.assertEqual({item["file"] for item in report["build_inputs"]["excluded"]}, set(names))
            self.assertTrue(all(len(item["sha256"]) == 64 for item in report["build_inputs"]["excluded"]))
            self.assertEqual(report["build_inputs"]["root"], str(self.repo))
            self.assertEqual(report["build_inputs"]["head"], self.source)
            self.assertNotIn(private_text(), (out / "report.json").read_text())
        self.write(release.ADDON_PATH + "/untracked.txt", private_text())
        out = self.root / "untracked-leak"
        self.assertEqual(self.run_main(["--dry-run", "--source", "source", "--exclude", str(self.excludes),
                                       *self.options(out)]), 1)
        report = json.loads((out / "report.json").read_text())
        self.assertEqual({hit["scope"] for hit in report["hits"]}, {"build-inputs"})
        self.assertNotIn(private_text(), (out / "report.md").read_text())

    def test_build_inputs_block_unexcluded_bytecode_even_when_allowlisted(self):
        # Simulate an install-rule regression in both lists to exercise the guard.
        path = self.repo / "source/creator/CMakeLists.txt"
        path.write_text(path.read_text().replace('PATTERN "*.pyc" EXCLUDE', '# omitted'))
        rules = {name: tuple(p for p in patterns if p != "*.pyc")
                 for name, patterns in release.BUILD_INPUT_PATTERNS.items()}
        name = release.ADDON_PATH + "/outside-cache.pyc"
        self.write(name, "Synthetic bytecode fixture")
        self.allowlist.write_text(release.digest(name) + " fixture review\n")
        out = self.root / "unexcluded-bytecode"
        with patch.object(release, "BUILD_INPUT_PATTERNS", rules):
            self.assertEqual(self.run_main(["leak-check", "HEAD", *self.options(out)]), 1)
        report = json.loads((out / "report.json").read_text())
        self.assertEqual([(hit["scope"], hit["category"], hit["allowed"]) for hit in report["hits"]],
                         [("build-inputs", "bytecode", False)])

    def test_build_input_rule_drift_missing_input_and_symlink_are_incomplete(self):
        target = self.repo / "scripts/addons_core/loopcut/link"
        target.symlink_to(self.root / "missing")
        out = self.root / "build-link"
        self.assertEqual(self.run_main(["leak-check", "HEAD", *self.options(out)]), 2)
        target.unlink()
        (self.repo / release.BUILD_INPUT_FILES[0]).unlink()
        out = self.root / "build-missing-file"
        self.assertEqual(self.run_main(["leak-check", "HEAD", *self.options(out)]), 2)
        cmake = self.repo / "source/creator/CMakeLists.txt"
        cmake.write_text(cmake.read_text().replace('PATTERN "*.pyc" EXCLUDE', '# omitted'))
        out = self.root / "build-rule-drift"
        self.assertEqual(self.run_main(["leak-check", "HEAD", *self.options(out)]), 2)
        self.assertIn("differ", (out / "report.md").read_text())

    def test_explicit_file_is_scanned_through_excluded_parent_link(self):
        site = self.repo / "scripts/site"
        (site / "sitecustomize.py").unlink()
        site.rmdir()
        target = self.root / "linked-site"
        target.mkdir()
        (target / "sitecustomize.py").write_text(private_text())
        site.symlink_to(target, target_is_directory=True)
        (self.skills / "fixture.md").write_text(private_text())
        out = self.root / "explicit-link"
        self.assertEqual(self.run_main(["leak-check", "HEAD", *self.options(out)]), 1)
        report = json.loads((out / "report.json").read_text())
        self.assertEqual([(hit["scope"], hit["file"]) for hit in report["hits"]],
                         [("build-inputs", release.BUILD_INPUT_FILES[0])])

    def test_explicit_build_root_head_mismatch_warns_and_site_file_is_scanned(self):
        other = self.root / "other-checkout"
        self.git("clone", "--quiet", str(self.repo), str(other))
        self.git("checkout", self.base, cwd=other)
        for name in ("source/creator/CMakeLists.txt", "build_files/cmake/Modules/FindPythonLibsUnix.cmake"):
            path = other / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((self.repo / name).read_bytes())
        for name in release.BUILD_INPUT_PATTERNS:
            (other / name).mkdir(parents=True, exist_ok=True)
        site = other / release.BUILD_INPUT_FILES[0]
        site.parent.mkdir(parents=True)
        site.write_text(private_text())
        (self.skills / "fixture.md").write_text(private_text())
        out = self.root / "other-build-root"
        self.assertEqual(self.run_main(["leak-check", "HEAD", "--build-root", str(other), *self.options(out)]), 1)
        report = json.loads((out / "report.json").read_text())
        self.assertEqual(report["build_inputs"]["head"], self.base)
        self.assertEqual(report["build_inputs"]["root"], str(other))
        self.assertTrue(report["build_inputs"]["warnings"])
        self.assertEqual({hit["file"] for hit in report["hits"]}, {release.BUILD_INPUT_FILES[0]})

    def test_tracked_bytecode_is_excluded_from_snapshot_export_and_addon_zip(self):
        names = [release.ADDON_PATH + "/__pycache__/x.cpython-314.pyc",
                 release.ADDON_PATH + "/outside.pyc", "release/extensions/x.pyo",
                 "__pycache__/tracked.txt"]
        for name in names:
            self.write(name, private_text())
        self.commit("Synthetic tracked cache fixture")
        _, _, snapshot, dropped = self.build()
        self.assertTrue(set(names) <= set(dropped))
        out = self.root / "bytecode-export"
        out.mkdir()
        release.export_tree(self.repo, snapshot, out)
        self.assertFalse(any(release.bytecode_path(p.relative_to(out / "tree").as_posix())
                             for p in (out / "tree").rglob("*")))
        archive = release.archive_addon(self.repo, snapshot, out)
        with zipfile.ZipFile(archive) as zipped:
            self.assertFalse(any(release.bytecode_path(name) for name in zipped.namelist()))
        with self.assertRaises(release.ReleaseError):
            release.archive_addon(self.repo, "HEAD", out)

    def test_zip_and_existing_tree_bytecode_block_without_text_hits(self):
        self.write(release.ADDON_PATH + "/existing.pyc", "Synthetic fixture")
        self.commit("Synthetic tracked bytecode")
        archive = self.root / "cache.zip"
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("addon/__pycache__/", "")
            zipped.writestr("addon/loose.pyo", "Synthetic fixture")
        out = self.root / "cache-tree-zip"
        self.assertEqual(self.run_main(["leak-check", "HEAD", "--zip", str(archive), *self.options(out)]), 1)
        report = json.loads((out / "report.json").read_text())
        self.assertEqual({hit["category"] for hit in report["hits"]}, {"bytecode"})
        self.assertEqual({hit["scope"] for hit in report["hits"]}, {"tree", "zip"})

    def platform_script(self, code=0):
        script = self.root / "leak-check.sh"
        script.write_text(f"#!{sys.executable}\n" + textwrap.dedent('''\
            import argparse
            import json
            from pathlib import Path
            import subprocess
            import sys
            import zipfile

            parser = argparse.ArgumentParser()
            parser.add_argument("--prompts-dir")
            parser.add_argument("--skills-dir")
            parser.add_argument("--zip", action="append", default=[])
            parser.add_argument("target", type=Path)
            parser.add_argument("revision", nargs="?")
            args = parser.parse_args()
            mode = "history" if args.revision else "zip" if args.zip else "tree"
            record = {"mode": mode, "target": str(args.target), "revision": args.revision,
                      "prompts": args.prompts_dir, "skills": args.skills_dir,
                      "files": sorted(p.relative_to(args.target).as_posix()
                                      for p in args.target.rglob("*")
                                      if p.is_file() and ".git" not in p.relative_to(args.target).parts),
                      "zips": args.zip, "members": []}
            for path in args.zip:
                with zipfile.ZipFile(path) as archive:
                    record["members"].append(archive.namelist())
            if args.revision:
                def git(*arguments):
                    return subprocess.check_output(["git", "-C", str(args.target), *arguments]).decode().strip()
                record["git_dir"] = git("rev-parse", "--absolute-git-dir")
                record["commits"] = git("rev-list", args.revision).splitlines()
                record["base"] = git("rev-parse", args.revision.split("..")[0])
                record["remotes"] = git("remote")
            with Path(__file__).with_suffix(".jsonl").open("a") as output:
                output.write(json.dumps(record) + "\\n")
        ''') + ("print('HIT tree fixture.txt:7 <- fixture.md:1')\n" if code == 1 else "") +
                          "print('Synthetic diagnostic omitted', file=sys.stderr)\n" +
                          f"sys.exit({code})\n")
        script.chmod(0o755)
        return script

    def test_platform_pass_scopes_isolation_and_both_zip_scanners(self):
        script = self.platform_script()
        linked = self.root / "linked-worktree"
        self.git("worktree", "add", "-b", "linked", str(linked), "base")
        self.write("public.txt", "Synthetic staged change\n")
        self.git("add", "public.txt")
        self.write("public.txt", "Synthetic unstaged change\n")
        refs = self.git("for-each-ref")
        worktrees = self.git("worktree", "list", "--porcelain")
        index = (self.repo / ".git/index").read_bytes()
        config = (self.repo / ".git/config").read_bytes()
        extra_zips = [self.root / "extra.zip", self.root / "another.zip"]
        zip_args = []
        for path in extra_zips:
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("extra.txt", "Synthetic public ZIP fixture")
            zip_args.extend(["--zip", str(path)])
        out = self.root / "platform-pass"
        with patch.object(release, "scan_zip", wraps=release.scan_zip) as scan_zip:
            self.assertEqual(self.run_main(["--dry-run", "--source", "source", "--exclude", str(self.excludes),
                                            "--leak-check", str(script), *zip_args, *self.options(out)]), 0)
        calls = [json.loads(line) for line in script.with_suffix(".jsonl").read_text().splitlines()]
        tree, history, zipped = calls
        report = json.loads((out / "report.json").read_text())
        self.assertEqual([call["mode"] for call in calls], ["tree", "history", "zip"])
        self.assertTrue(all(call["skills"] == str(self.skills.resolve()) for call in calls))
        self.assertTrue(all(call["prompts"] == str((self.root / "missing-prompts").resolve()) for call in calls))
        self.assertEqual(set(tree["files"]), {"public.txt", "names/a spaced file.txt", "names/:(literal).txt",
                                             "scripts/addons_core/loopcut/public.py", "loopcut/public.txt",
                                             ".env.example", "source/upstream.txt"})
        self.assertIn("source/upstream.txt", tree["files"])
        self.assertEqual(history["git_dir"], str(Path(history["target"]) / ".git"))
        self.assertNotEqual(Path(history["target"]), self.repo)
        self.assertEqual(history["base"], self.base)
        self.assertEqual(history["commits"], [report["snapshot"]])
        self.assertEqual(history["revision"], self.base + ".." + report["snapshot"])
        self.assertEqual(history["files"], [])
        self.assertEqual(history["remotes"], "")
        self.assertEqual(zipped["files"], [])
        self.assertEqual(zipped["zips"][1:], list(map(str, extra_zips)))
        self.assertIn("scripts/addons_core/loopcut/public.py", zipped["members"][0])
        self.assertEqual({str(call.args[0]) for call in scan_zip.call_args_list}, set(zipped["zips"]))
        self.assertEqual(report["shipped_addon_zip_files"], 1)
        self.assertEqual(report["zip_files"], 2)
        self.assertTrue(report["scan_complete"])
        self.assertEqual(report["status"], "clean")
        for run in report["platform_runs"]:
            self.assertEqual((run["status"], run["exit_code"], run["hit_count"]), ("clean", 0, 0))
            self.assertEqual(run["command"][0], str(script))
            self.assertNotIn(str(self.root), json.dumps(run["command"][1:]))
        new_refs = b"".join(line + b"\n" for line in self.git("for-each-ref").splitlines()
                            if not line.endswith(release.DRY_RUN_REF.encode()))
        self.assertEqual(new_refs, refs)
        self.assertEqual(self.git("worktree", "list", "--porcelain"), worktrees)
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)
        self.assertEqual((self.repo / ".git/config").read_bytes(), config)
        self.assertEqual((self.repo / "public.txt").read_text(), "Synthetic unstaged change\n")
        self.assertTrue(all(not Path(call["target"]).exists() for call in calls))

    def test_platform_hits_errors_and_missing_script_fail_closed(self):
        for label, code in (("hit", 1), ("error", 2), ("missing", None)):
            with self.subTest(label=label):
                script = self.platform_script(code) if code is not None else self.root / "missing-check.sh"
                out = self.root / ("platform-" + label)
                self.assertEqual(self.run_main(["--dry-run", "--source", "source", "--exclude", str(self.excludes),
                                                "--leak-check", str(script), *self.options(out)]), code or 2)
                report = json.loads((out / "report.json").read_text())
                self.assertEqual(report["hits"], [])
                self.assertEqual(report["scan_complete"], code == 1)
                self.assertEqual(report["status"], "blocked" if code == 1 else "error")
                self.assertEqual(len(report["platform_runs"]), 3)
                for run in report["platform_runs"]:
                    self.assertEqual(run["exit_code"], code)
                    self.assertEqual(run["hit_count"], 1 if code == 1 else 0 if code == 2 else None)
                    if code == 1:
                        self.assertEqual(run["locations"], ["HIT tree fixture.txt:7 <- fixture.md:1"])
                    if code is None:
                        self.assertEqual(run["status"], "not run")
                        self.assertIn("missing", run["reason"])
                for name in ("report.json", "report.md"):
                    content = (out / name).read_text()
                    self.assertNotIn("Synthetic diagnostic omitted", content)
                    if code == 1:
                        self.assertIn("HIT tree fixture.txt:7 <- fixture.md:1", content)
                    else:
                        self.assertIn("INCOMPLETE" if name.endswith("md") else '"scan_complete": false', content)

    def test_clean_platform_cannot_override_shipped_zip_finding(self):
        self.write("scripts/addons_core/loopcut/fixture.md", private_text())
        self.commit("Synthetic unchanged archive fixture")
        (self.skills / "fixture.md").write_text(private_text())
        script = self.platform_script()
        out = self.root / "platform-clean-own-blocked"
        options = self.options(out)
        options[1] = "source"
        self.assertEqual(self.run_main(["--dry-run", "--source", "source", "--exclude", str(self.excludes),
                                        "--leak-check", str(script), *options]), 1)
        report = json.loads((out / "report.json").read_text())
        self.assertEqual({hit["scope"] for hit in report["hits"]}, {"build-inputs", "zip"})
        self.assertTrue(all(run["exit_code"] == 0 for run in report["platform_runs"]))
        self.assertEqual(report["status"], "blocked")

    def matching_platform_script(self):
        """Importable runtime stand-in; independent blob enumeration and matching."""
        script = self.root / "matching-check.py"
        script.write_text(f"#!{sys.executable}\n" + textwrap.dedent('''\
            import argparse
            import bisect
            import io
            import json
            from pathlib import Path
            import subprocess
            import sys
            from types import SimpleNamespace
            import zipfile

            class CheckError(Exception):
                pass

            def normalize(text):
                return " ".join(text.split())

            def unescape(text):
                return text

            def display(value):
                return str(value).encode("unicode_escape").decode("ascii")

            def fingerprints(prompts, skills, allow):
                manifest = prompts / "manifest.json"
                opened = json.loads(manifest.read_text()).get("open", []) if manifest.exists() else []
                public = [normalize((prompts / name).read_text()) for name in opened]
                owners = {}
                for root in (prompts, skills):
                    for path in sorted(root.glob("*.md")):
                        if root == prompts and path.name in opened:
                            continue
                        lines = [(number, normalize(line)) for number, line in enumerate(path.read_text().splitlines(), 1)
                                 if line.strip()]
                        starts, position = [], 0
                        for number, line in lines:
                            starts.append(position)
                            position += len(line) + 1
                        whole = " ".join(line for _, line in lines)
                        candidates = [(line[offset:offset + 40], number) for number, line in lines if len(line) >= 40
                                      for offset in [*range(0, len(line) - 39, 20), len(line) - 40]]
                        if len(whole) >= 40:
                            candidates += [(whole[offset:offset + 40], lines[bisect.bisect_right(starts, offset) - 1][0])
                                           for offset in [*range(0, len(whole) - 39, 20), len(whole) - 40]]
                        for window, number in candidates:
                            if not any(window in text for text in public):
                                owners.setdefault(window.encode(), f"{display(path)}:{number}")
                ignored = set(allow.read_text().splitlines()) if allow else set()
                return {window: owner for window, owner in owners.items() if owner not in ignored}, len(owners), 0, 0

            def text_parts(stream):
                parts = stream.read().decode("utf-8").split("\\n")
                for number, part in enumerate(parts, 1):
                    end = number < len(parts)
                    yield number, part + ("\\n" if end else ""), end

            class Scanner:
                def __init__(self, owners):
                    self.windows = list(owners)
                    self.matcher = SimpleNamespace(owners=list(owners.values()))

                def emit(self, locations, number, indices):
                    for location in locations:
                        for index in sorted(indices):
                            print(f"HIT {location}:{number} <- {self.matcher.owners[index]}")

                def scan_stream(self, stream, locations):
                    carry = ""
                    for number, part, end in text_parts(stream):
                        text = normalize(carry + part)
                        raw = text.encode()
                        matches = {index for index, window in enumerate(self.windows) if window in raw}
                        self.emit(locations, number, matches)
                        carry = text[-39:] + (" " if end else "")

            def main():
                parser = argparse.ArgumentParser()
                parser.add_argument("--prompts-dir", type=Path)
                parser.add_argument("--skills-dir", type=Path)
                parser.add_argument("--allow", type=Path)
                parser.add_argument("--zip", action="append", default=[])
                parser.add_argument("target", type=Path)
                parser.add_argument("revision", nargs="?")
                args = parser.parse_args()
                mode = "history" if args.revision else "zip" if args.zip else "tree"
                allowed = args.allow.read_text().splitlines() if args.allow else []
                with Path(__file__).with_suffix(".jsonl").open("a") as output:
                    output.write(json.dumps({"mode": mode, "allowed": allowed,
                                             "allow_path": str(args.allow)}) + "\\n")
                owners, *_ = fingerprints(args.prompts_dir, args.skills_dir, args.allow)
                scanner = Scanner(owners)
                hits = []
                def emit(locations, number, indices):
                    for location in locations:
                        for index in sorted(indices):
                            hits.append(f"HIT {location}:{number} <- {scanner.matcher.owners[index]}")
                scanner.emit = emit
                def scan(raw, location):
                    scanner.scan_stream(io.BytesIO(raw), [location])
                def git(*arguments):
                    return subprocess.check_output(["git", "-C", str(args.target), *arguments])
                if args.revision:
                    assert git("rev-parse", "--show-toplevel").decode().strip() == str(args.target)
                    assert git("rev-parse", "--is-shallow-repository").strip() == b"false"
                    for row in git("rev-list", "--parents", args.revision).decode().splitlines():
                        commit, *parents = row.split()
                        scan(git("cat-file", "commit", commit), f"history {commit} <commit>")
                        names = git("diff-tree", "--root", "-r", "--no-commit-id", "--name-only", "-z",
                                    "--no-renames", commit).decode().split("\\0")
                        for name in filter(None, names):
                            for revision in [*parents, commit]:
                                entries = git("ls-tree", revision, "--", name)
                                if entries:
                                    oid = entries.split()[2].decode()
                                    scan(git("cat-file", "blob", oid), f"history {commit} {display(name)}")
                elif args.zip:
                    for path in args.zip:
                        with zipfile.ZipFile(path) as archive:
                            for member in archive.namelist():
                                scan(archive.read(member), f"zip {display(path)}!{display(member)}")
                else:
                    for path in sorted(args.target.rglob("*")):
                        if path.is_file() and ".git" not in path.relative_to(args.target).parts:
                            scan(path.read_bytes(), f"tree {display(path)}")
                control = Path(__file__).with_suffix(".exit")
                if args.allow and control.exists():
                    return int(control.read_text())
                print("\\n".join(hits))
                return 1 if hits else 0

            if __name__ == "__main__":
                sys.exit(main())
        '''))
        script.chmod(0o755)
        return script

    def platform_overlap_fixture(self, unexplained=False):
        # Only synthetic text; source line 2 avoids the independent scanner's sampling.
        public = " ".join(["Public", "fixture", *(["alpha"] * 9)])
        reviewed = " ".join(["Reviewed", "fixture", *(["beta"] * 11)])
        removed = " ".join(["Removed", "fixture", *(["gamma"] * 9)])
        shared, unseen = "S" * 40, "U" * 40
        self.write("loopcut/public.txt", public.replace(" ", "\t  ") + "\n" + shared + "\n")
        self.write("loopcut/removed.txt", removed + "\n")
        self.commit("Synthetic public baseline")
        self.git("branch", "-f", "base", "HEAD")
        (self.repo / "loopcut/removed.txt").unlink()
        # Changed line, but only its shared window is public; the full owner isn't.
        lines = [public + " edited suffix", shared + " edited suffix"]
        self.write("source/reviewed.txt", reviewed + "\n")
        if unexplained:
            lines.extend([unseen, shared + unseen])
        self.write("scripts/addons_core/loopcut/overlap.txt", "\n".join(lines) + "\n")
        self.commit("Synthetic release changes")
        for name, value in (("public", public), ("reviewed", reviewed), ("removed", removed),
                            ("mixed", shared + unseen)):
            (self.skills / (name + ".md")).write_text("fixture\n" + value + "\n")
        self.allowlist.write_text(release.digest(reviewed) + " reviewed synthetic documentation\n")
        return public, reviewed, removed, shared, unseen

    def test_platform_explained_hits_and_exact_owner_allow_files(self):
        self.assert_platform_classification(False)

    def test_platform_unexplained_hits_and_exact_owner_allow_files(self):
        self.assert_platform_classification(True)

    def assert_platform_classification(self, unexplained):
        values = self.platform_overlap_fixture(unexplained)
        script = self.matching_platform_script()
        script.with_suffix(".jsonl").unlink(missing_ok=True)
        out = self.root / ("classified-blocked" if unexplained else "classified-clean")
        self.assertEqual(self.run_main(["--dry-run", "--source", "source", "--exclude", str(self.excludes),
                                        "--leak-check", str(script), *self.options(out)]), int(unexplained))
        report = json.loads((out / "report.json").read_text())
        self.assertEqual(report["status"], "blocked" if unexplained else "clean")
        self.assertTrue(report["scan_complete"])
        calls = [json.loads(line) for line in script.with_suffix(".jsonl").read_text().splitlines()]
        self.assertEqual(len(calls), 6)
        for run in report["platform_runs"]:
            first, second = run["passes"]
            self.assertEqual(first["exit_code"], 1)
            self.assertEqual(second["exit_code"], int(unexplained))
            self.assertEqual(sum(hit["occurrences"] for hit in first["hits"]), first["hit_count"])
            self.assertEqual(sum(first["class_counts"].values()), first["hit_count"])
            self.assertGreater(first["class_counts"]["public-in-base"], 0)
            self.assertEqual(first["class_counts"]["allowlisted"] > 0, run["mode"] != "zip")
            self.assertEqual(bool(first["class_counts"]["unexplained"]), unexplained)
            expected = {str(self.skills / "public.md") + ":2"}
            if run["mode"] != "zip":
                expected.add(str(self.skills / "reviewed.md") + ":2")
            if not unexplained:
                expected.add(str(self.skills / "mixed.md") + ":2")
            if run["mode"] == "history":
                expected.add(str(self.skills / "removed.md") + ":2")
                removed = [hit for hit in first["hits"] if hit["side"] == "old"]
                self.assertTrue(removed)
                self.assertTrue(all(hit["class"] == "public-in-base" for hit in removed))
                added = [hit for hit in first["hits"] if hit["side"] == "new" and
                         hit["owner"].endswith("/public.md:2")]
                self.assertTrue(added)
                self.assertTrue(all(hit["class"] == "public-in-base" for hit in added))
            self.assertEqual({item["owner"] for item in run["allowed_owners"]}, expected)
            repeated = [call for call in calls if call["mode"] == run["mode"]][1]
            self.assertEqual(repeated["allowed"], sorted(expected))
            self.assertFalse(Path(repeated["allow_path"]).exists())
            for hit in first["hits"]:
                self.assertRegex(hit["line_sha256"], r"^[a-f0-9]{64}$")
                self.assertNotIn("could not recover", hit["reason"])
                if hit["class"] == "allowlisted":
                    self.assertEqual(hit["reason"], "reviewed synthetic documentation")
                    self.assertEqual(hit["line_sha256"], release.digest(values[1]))
        for name in ("report.json", "report.md"):
            content = (out / name).read_text()
            for value in values:
                self.assertNotIn(value, content)

    def test_platform_second_pass_must_exit_zero_and_cannot_hide_unexplained(self):
        self.platform_overlap_fixture(unexplained=True)
        script = self.matching_platform_script()
        for code in (0, 1, 2):
            with self.subTest(code=code):
                script.with_suffix(".exit").write_text(str(code))
                out = self.root / f"second-pass-{code}"
                self.assertEqual(self.run_main(["--dry-run", "--source", "source", "--exclude", str(self.excludes),
                                                "--leak-check", str(script), *self.options(out)]), 2 if code == 2 else 1)
                report = json.loads((out / "report.json").read_text())
                for run in report["platform_runs"]:
                    self.assertEqual(run["passes"][1]["exit_code"], code)
                    self.assertEqual(run["status"], "error" if code == 2 else "blocked")

    def test_platform_windows_use_character_offsets_lf_targets_and_cached_scoped_base(self):
        # 47 distinct Unicode characters exercise the terminal window at offset 7.
        source = "".join(chr(0x400 + number) for number in range(47))
        window = source[-40:]
        outside = "Outside fixture " + "X" * 40
        self.write("loopcut/window.txt", window + "\n")
        self.write("source/outside.txt", outside + "\n")
        self.commit("Synthetic window baseline")
        base = self.git("rev-parse", "HEAD").decode().strip()
        (self.skills / "unicode.md").write_text("fixture\n" + source + "\n")
        (self.skills / "outside.md").write_text("fixture\n" + outside + "\n")
        temporary = self.root / "targets"
        tree = temporary / "tree"
        tree.mkdir(parents=True)
        target = tree / "loopcut/fixture.txt"
        target.parent.mkdir()
        # CR, VT, NEL and a Unicode line separator must not advance LF line numbers.
        target.write_bytes("header\r\v\u0085\u2028\n".encode() + window.encode() + b"\n" + outside.encode())
        args = release.parser(False).parse_args(["--dry-run", "--leak-check", str(self.matching_platform_script()),
                                                  *self.options(self.root / "unused")])
        classifier = release.PlatformClassifier(self.repo, base, args, temporary, [], {})
        with patch.object(release, "git", wraps=release.git) as git_calls:
            hit = classifier.classify(f"HIT tree {target}:2 <- {self.skills / 'unicode.md'}:2")
            self.assertEqual(hit["class"], "public-in-base")
            self.assertEqual(hit["line_sha256"], release.digest(window))
            self.assertEqual(hit["fingerprint_sha256"], [release.digest(window)])
            reads = git_calls.call_count
            hit = classifier.classify(f"HIT tree {target}:3 <- {self.skills / 'outside.md'}:2")
            self.assertEqual(hit["class"], "unexplained")
            self.assertEqual(git_calls.call_count, reads)

    def test_current_protocol_and_redacted_locations(self):
        script = self.root / "protocol.py"
        locations = [
            "HIT tree /tmp/fixture.txt:7 <- /tmp/owner.md:2",
            "HIT history " + "a" * 40 + " source/fixture.txt:9 <- /tmp/owner.md:2",
            "HIT history " + "a" * 40 + " <commit>:5 <- /tmp/owner.md:2",
            "HIT history " + "a" * 40 + " source/fixture.txt <path>:1 <- /tmp/owner.md:2",
            "HIT <redacted location>:3 <- /tmp/owner.md:2",
            "HIT zip /tmp/fixture.zip!entry.txt:4 <- <redacted location>",
        ]
        script.write_text("print(" + repr("\n".join([*locations, "SKIP gitlink lib/fixture",
                                                    "SKIP lfs <redacted location>", "synthetic diagnostic"])) + ")\n")
        result = {}
        release.run_platform_check(result, [sys.executable, str(script)])
        self.assertEqual(result["locations"], locations)
        self.assertEqual(result["hit_count"], 6)
        self.assertEqual(result["skip_count"], 2)
        self.assertEqual(result["skip_counts"], {"gitlink": 1, "lfs": 1})
        self.assertEqual(result["status"], "blocked")
        args = release.parser(False).parse_args(["--dry-run", *self.options(self.root / "unused")])
        classifier = release.PlatformClassifier(self.repo, self.base, args, self.root, [], {})
        for location in locations[-2:]:
            self.assertEqual(classifier.classify(location)["class"], "unexplained")
        script.write_text("print('HIT unknown protocol')\n")
        release.run_platform_check(result, [sys.executable, str(script)])
        self.assertEqual(result["status"], "error")

    def test_skip_counts_are_reported_per_mode_and_not_hits(self):
        script = self.platform_script()
        script.write_text(script.read_text().replace("sys.exit(0)",
                          "print('SKIP lfs fixture.bin')\nprint('SKIP gitlink lib/fixture')\nsys.exit(0)"))
        out = self.root / "skips"
        self.assertEqual(self.run_main(["--dry-run", "--source", "source", "--exclude", str(self.excludes),
                                        "--leak-check", str(script), *self.options(out)]), 0)
        report = json.loads((out / "report.json").read_text())
        self.assertEqual(report["platform_hit_count"], 0)
        self.assertEqual(report["platform_skip_count"], 6)
        for run in report["platform_runs"]:
            self.assertFalse(run["strict"])
            self.assertEqual(run["skip_count"], 2)
            self.assertEqual(run["passes"][0]["skip_counts"], {"lfs": 1, "gitlink": 1})
        self.assertIn("non-strict", (out / "report.md").read_text())
        self.assertIn("unscanned", (out / "report.md").read_text())

    def test_history_same_location_unions_old_public_and_new_private_windows(self):
        public, unseen = "P" * 40, "Q" * 40
        self.write("loopcut/history.txt", public + "\n")
        self.commit("Synthetic base window")
        self.git("branch", "-f", "base", "HEAD")
        self.write("loopcut/history.txt", unseen + "\n")
        self.commit("Synthetic replacement window")
        (self.skills / "mixed.md").write_text("fixture\n" + public + unseen + "\n")
        script = self.matching_platform_script()
        out = self.root / "both-sides"
        self.assertEqual(self.run_main(["--dry-run", "--source", "source", "--exclude", str(self.excludes),
                                        "--leak-check", str(script), *self.options(out)]), 1)
        report = json.loads((out / "report.json").read_text())
        history = report["platform_runs"][1]
        hit, = history["passes"][0]["hits"]
        self.assertEqual(hit["class"], "unexplained")
        self.assertEqual(hit["occurrences"], 2)
        self.assertEqual(set(hit["side"].split(",")), {"old", "new"})
        self.assertEqual(set(hit["line_sha256s"]), {release.digest(public), release.digest(unseen)})
        self.assertEqual(set(hit["fingerprint_sha256"]), {release.digest(public), release.digest(unseen)})
        self.assertEqual(history["allowed_owners"], [])
        self.assertEqual(len(history["passes"]), 1)

    def test_unchanged_upstream_needs_exact_line_allowlist_not_public_base(self):
        shared = "W" * 40
        exact = "  # Synthetic API fixture: " + shared + " suffix\t"
        self.write("loopcut/public.txt", shared + "\n")
        self.write("source/upstream.txt", exact + "\n")
        self.commit("Synthetic upstream baseline")
        self.git("branch", "-f", "base", "HEAD")
        (self.skills / "shared.md").write_text("fixture\n" + shared + "\n")
        script = self.matching_platform_script()
        for label, expected in (("no-entry", 1), ("window-only", 1), ("exact-line", 0)):
            if label == "window-only":
                self.allowlist.write_text(release.digest(shared) + " synthetic window only\n")
            elif label == "exact-line":
                self.assertEqual(self.run_main(["allowlist-add", "--rev", "HEAD", "--path", "source/upstream.txt",
                                                "--line", "1", "--reason", "Synthetic API documentation",
                                                "--allowlist", str(self.allowlist)]), 0)
            out = self.root / label
            self.assertEqual(self.run_main(["--dry-run", "--source", "source", "--exclude", str(self.excludes),
                                            "--leak-check", str(script), *self.options(out)]), expected)
            report = json.loads((out / "report.json").read_text())
            hits = report["platform_runs"][0]["hits"]
            upstream = next(hit for hit in hits if hit["path"] == "source/upstream.txt")
            self.assertEqual(upstream["class"], "allowlisted" if expected == 0 else "unexplained")
            self.assertEqual((out / "tree/source/upstream.txt").read_text(), exact + "\n")
            self.assertNotIn("source/upstream.txt", report["changed_files"])
        self.assertNotIn(shared, self.allowlist.read_text())
        # Adding unrelated text changes the exact line and invalidates its allow entry.
        self.write("source/upstream.txt", exact + " modified\n")
        self.commit("Synthetic changed upstream line")
        out = self.root / "changed-exact-line"
        self.assertEqual(self.run_main(["--dry-run", "--source", "source", "--exclude", str(self.excludes),
                                        "--leak-check", str(script), *self.options(out)]), 1)
        history = json.loads((out / "report.json").read_text())["platform_runs"][1]
        self.assertGreater(history["class_counts"]["unexplained"], 0)

    def test_allowlist_add_refuses_addon_paths_empty_reason_and_never_copies_text(self):
        exact = "\t" + private_text() + "  "
        self.write("source/reviewed.txt", "header\n" + exact + "\n")
        self.commit("Synthetic review target")
        self.write("source/reviewed.txt", "uncommitted replacement\n")
        original = self.allowlist.read_bytes()
        for path, line, reason in (("loopcut/public.txt", "1", "review"),
                                   ("scripts/addons_core/loopcut/public.py", "1", "review"),
                                   ("loopcut", "1", "review"),
                                   ("source/reviewed.txt", "2", " \t "),
                                   ("source/reviewed.txt", "0", "review"),
                                   ("source/reviewed.txt", "4", "review"),
                                   ("source/../loopcut/public.txt", "1", "review")):
            with self.subTest(path=path, line=line):
                self.assertEqual(self.run_main(["allowlist-add", "--rev", "HEAD", "--path", path,
                                                "--line", line, "--reason", reason,
                                                "--allowlist", str(self.allowlist)]), 2)
                self.assertEqual(self.allowlist.read_bytes(), original)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = release.main(["allowlist-add", "--rev", "HEAD", "--path", "source/reviewed.txt",
                                   "--line", "2", "--reason", "Synthetic reviewed API text",
                                   "--allowlist", str(self.allowlist)], repo=self.repo)
        self.assertEqual(result, 0)
        content = self.allowlist.read_text()
        self.assertIn(release.digest(exact) + "  # source/reviewed.txt:2 Synthetic reviewed API text\n", content)
        self.assertNotIn(private_text(), content + output.getvalue())
        self.assertEqual((self.repo / "source/reviewed.txt").read_text(), "uncommitted replacement\n")

    def test_manifest_open_windows_are_not_reintroduced_by_classifier(self):
        public, opened = "A" * 40, "B" * 40
        self.write("loopcut/public.txt", public + "\n")
        self.commit("Synthetic public window")
        self.git("branch", "-f", "base", "HEAD")
        self.write("loopcut/combined.txt", public + opened + "\n")
        self.commit("Synthetic combined windows")
        (self.skills / "combined.md").write_text("fixture\n" + public + "\n" + opened + "\n")
        prompts = self.root / "missing-prompts"
        prompts.mkdir()
        (prompts / "open.md").write_text(opened)
        (prompts / "manifest.json").write_text(json.dumps({"open": ["open.md"]}))
        script = self.matching_platform_script()
        out = self.root / "open-filter"
        self.assertEqual(self.run_main(["--dry-run", "--source", "source", "--exclude", str(self.excludes),
                                        "--leak-check", str(script), *self.options(out)]), 0)
        report = json.loads((out / "report.json").read_text())
        self.assertGreater(report["platform_class_counts"]["public-in-base"], 0)
        self.assertEqual(report["platform_class_counts"]["unexplained"], 0)

    def test_exact_line_allowlist_cannot_hide_cross_line_insertion(self):
        first, second = "P" * 20, "Q" * 19
        (self.skills / "cross.md").write_text("fixture\n" + first + " " + second + "\n")
        temporary = self.root / "cross-targets"
        target = temporary / "tree/source/fixture.txt"
        target.parent.mkdir(parents=True)
        target.write_text(first + "\n" + second + "\n")
        args = release.parser(False).parse_args(["--dry-run", "--leak-check", str(self.matching_platform_script()),
                                                *self.options(self.root / "unused")])
        classifier = release.PlatformClassifier(self.repo, self.base, args, temporary, [],
                                                {release.digest(second): "synthetic reviewed ending line"})
        hit = classifier.classify(f"HIT tree {target}:2 <- {self.skills / 'cross.md'}:2")
        self.assertEqual(hit["class"], "unexplained")
        self.assertEqual(hit["line_sha256"], release.digest(second))
        self.assertEqual(hit["fingerprint_sha256"], [release.digest(first + " " + second)])

    def test_commit_and_path_hits_require_public_windows_and_ignore_allowlist(self):
        shared, unseen = "C" * 40, "D" * 40
        self.write("loopcut/public.txt", shared + "\n")
        self.commit("Synthetic public metadata window")
        base = self.git("rev-parse", "HEAD").decode().strip()
        name = "source/" + shared + ".txt"
        self.write(name, "synthetic body\n")
        self.commit(unseen)
        commit = self.git("rev-parse", "HEAD").decode().strip()
        (self.skills / "metadata.md").write_text("fixture\n" + shared + "\n" + unseen + "\n")
        script = self.matching_platform_script()
        args = release.parser(False).parse_args(["--dry-run", "--leak-check", str(script),
                                                *self.options(self.root / "unused")])
        classifier = release.PlatformClassifier(self.repo, base, args, self.root, [],
                                                {release.digest(unseen): "synthetic review"})
        path_hit = classifier.classify(f"HIT history {commit} {name} <path>:1 <- {self.skills / 'metadata.md'}:2")
        self.assertEqual(path_hit["class"], "public-in-base")
        raw = self.git("cat-file", "commit", commit).decode().split("\n")
        number = raw.index(unseen) + 1
        commit_hit = classifier.classify(f"HIT history {commit} <commit>:{number} <- {self.skills / 'metadata.md'}:3")
        self.assertEqual(commit_hit["class"], "unexplained")

    def test_platform_script_default_precedence_and_test_only_skip(self):
        first, second = self.root / "first.sh", self.root / "second.sh"
        second.touch()
        with patch.object(release, "LEAK_CHECK_PATHS", (first, second)), patch.dict(os.environ):
            os.environ.pop("LOOPCUT_RELEASE_LEAK_CHECK", None)
            self.assertEqual(release.parser(False).parse_args(["--dry-run"]).leak_check, second)
            first.touch()
            self.assertEqual(release.parser(False).parse_args(["--dry-run"]).leak_check, first)
            os.environ["LOOPCUT_RELEASE_LEAK_CHECK"] = str(self.root / "environment.sh")
            self.assertEqual(release.parser(False).parse_args(["--dry-run"]).leak_check,
                             self.root / "environment.sh")
            self.assertEqual(release.parser(False).parse_args(["--dry-run", "--leak-check", str(second)]).leak_check,
                             second)
        out = self.root / "platform-disabled"
        self.assertEqual(self.run_main(["--dry-run", "--source", "source", "--exclude", str(self.excludes),
                                        "--leak-check", str(self.root / "missing.sh"), "--no-leak-check",
                                        *self.options(out)]), 0)
        report = json.loads((out / "report.json").read_text())
        self.assertTrue(all(run["status"] == "not run" and not run["required"] and
                            "tests only" in run["reason"] for run in report["platform_runs"]))

    def test_snapshot_parent_tree_exclusions_and_every_worktree_are_preserved(self):
        linked = self.root / "linked-worktree"
        self.git("worktree", "add", "-b", "linked", str(linked), "base")
        self.git("remote", "add", "fixture", "https://invalid.example/fixture.git")
        self.write("public.txt", "staged local fixture\n")
        self.git("add", "public.txt")
        self.write("public.txt", "unstaged local fixture\n")
        (linked / "public.txt").write_text("linked staged fixture\n")
        self.git("add", "public.txt", cwd=linked)
        state = []
        for worktree in (self.repo, linked):
            index = Path(self.git("rev-parse", "--git-path", "index", cwd=worktree).decode().strip())
            if not index.is_absolute():
                index = worktree / index
            state.append((worktree, index, index.read_bytes(), self.git("rev-parse", "HEAD", cwd=worktree),
                          (worktree / "public.txt").read_bytes()))
        refs = self.git("for-each-ref", "refs/heads", "refs/remotes")
        config = (self.repo / ".git/config").read_bytes()
        # Even an inherited index override cannot redirect snapshot operations.
        with patch.dict(os.environ, {"GIT_INDEX_FILE": str(state[0][1]), "GIT_DIR": "/invalid"}):
            base, source, snapshot, dropped = self.build()
        self.assertEqual((base, source), (self.base, self.source))
        self.assertEqual(self.git("show", "-s", "--format=%P", snapshot).decode().strip(), self.base)
        self.assertEqual(self.git("rev-list", "--count", f"{base}..{snapshot}").strip(), b"1")
        self.assertEqual(set(dropped), {"loopcut/track/internal.md", "private/fixture.hidden"})
        names = release.tree_entries(self.repo, snapshot)
        self.assertNotIn("loopcut/track/internal.md", names)
        self.assertNotIn("private/fixture.hidden", names)
        self.assertNotIn("gone.txt", names)
        self.assertEqual(self.git("show", snapshot + ":public.txt"), b"source public fixture\n")
        self.assertEqual(self.git("rev-parse", release.DRY_RUN_REF).decode().strip(), snapshot)
        self.assertEqual(self.git("for-each-ref", "refs/heads", "refs/remotes"), refs)
        self.assertEqual((self.repo / ".git/config").read_bytes(), config)
        for worktree, index, old_index, head, public in state:
            self.assertEqual(index.read_bytes(), old_index)
            self.assertEqual(self.git("rev-parse", "HEAD", cwd=worktree), head)
            self.assertEqual((worktree / "public.txt").read_bytes(), public)

    def test_symbolic_private_ref_does_not_move_branch(self):
        self.git("symbolic-ref", release.DRY_RUN_REF, "refs/heads/base")
        _, _, snapshot, _ = self.build()
        self.assertEqual(self.git("rev-parse", "base").decode().strip(), self.base)
        self.assertEqual(self.git("rev-parse", release.DRY_RUN_REF).decode().strip(), snapshot)

    def test_dry_run_reports_and_whole_tree_export(self):
        (self.repo / "names/link").symlink_to("../../external-fixture")
        self.commit("Symlink fixture")
        out = self.root / "review"
        result = self.run_main(["--dry-run", "--source", "source", "--exclude", str(self.excludes), *self.options(out)])
        self.assertEqual(result, 0)
        report = json.loads((out / "report.json").read_text())
        self.assertEqual(report["status"], "clean")
        self.assertEqual(report["mode"], "dry-run")
        self.assertTrue(any(item["status"] == "missing" for item in report["coverage"]))
        self.assertEqual((out / "tree/public.txt").read_text(), "source public fixture\n")
        self.assertTrue((out / "tree/names/a spaced file.txt").is_file())
        self.assertTrue((out / "tree/names/:(literal).txt").is_file())
        self.assertFalse((out / "tree/names/link").is_symlink())
        self.assertEqual((out / "tree/names/link").read_text(), "../../external-fixture")
        self.assertTrue((out / "tree/.env.example").exists())
        self.assertFalse((out / "tree/gone.txt").exists())

    def test_shipped_exclusions_remove_private_harness_and_its_tests(self):
        private = ["loopcut/harness/evals/tasks.py", "loopcut/harness/evals/production/fixture.py",
                   "loopcut/tests/test_eval_fixture.py", "loopcut/tests/test_chatgpt_eval.py",
                   "loopcut/tests/test_codex_answer.py"]
        public = ["loopcut/harness/inspection_check.py", "loopcut/tests/test_public_fixture.py"]
        for name in private + public:
            self.write(name, "# Synthetic fixture\n")
        self.commit("Synthetic private harness")
        out = self.root / "public-harness"
        result = self.run_main(["--dry-run", "--source", "source", "--exclude", str(EXCLUDES), *self.options(out)])
        self.assertEqual(result, 0)
        report = json.loads((out / "report.json").read_text())
        names = release.tree_entries(self.repo, report["snapshot"])
        self.assertTrue(set(private).isdisjoint(names))
        self.assertTrue(set(public) <= names.keys())
        self.assertFalse(any("harness/evals/" in name for name in names))
        self.assertFalse(any(hit["category"] == "private_path" for hit in report["hits"]))
        self.assertIn("private_path: 0 blocking hits", (out / "report.md").read_text())

    def test_missing_exclusion_blocks_private_paths_anywhere_in_tree(self):
        private = ["loopcut/harness/evals/fixture.py", "vendor/harness/evals/fixture.txt"]
        public = ["harness/evals_archive/fixture.txt", "out/evals/fixture.txt",
                  "harness/other/evals/fixture.txt"]
        for name in private + public:
            self.write(name, "# Synthetic fixture\n")
        self.commit("Synthetic nested harness")
        out = self.root / "private-paths"
        result = self.run_main(["--dry-run", "--source", "source", "--exclude", str(self.excludes), *self.options(out)])
        self.assertEqual(result, 1)
        report = json.loads((out / "report.json").read_text())
        self.assertEqual({hit["file"] for hit in report["hits"]}, set(private))
        self.assertEqual({hit["category"] for hit in report["hits"]}, {"private_path"})
        markdown = (out / "report.md").read_text()
        self.assertIn("private_path: 2 blocking hits", markdown)
        for name in private:
            self.assertIn(name, markdown)

    def test_local_path_in_a_public_test_blocks_the_dry_run(self):
        self.write("loopcut/tests/test_fixture.py", f'ROOT = "{LocalPathTest.TMP}"\n')
        self.write("loopcut/harness/fixture_check.py", f'ROOT = "{LocalPathTest.TMP}"\n')  # Not a public test.
        self.commit("Synthetic local path")
        out = self.root / "local"
        self.assertEqual(self.run_main(["--dry-run", "--source", "source", "--exclude", str(self.excludes),
                                        *self.options(out)]), 1)
        report = json.loads((out / "report.json").read_text())
        self.assertEqual([(hit["category"], hit["file"]) for hit in report["hits"]],
                         [("local_path", "loopcut/tests/test_fixture.py")])
        self.assertIn("local_path: 1 blocking hits", (out / "report.md").read_text())

    def test_private_import_blocks_code_but_not_docstrings(self):
        for label, code, expected in (
            ("code", 'sys.path.insert(0, str(ROOT / "harness" / "evals"))\n', 1),
            ("docstring", '"""Fixture output: out/evals/fixture. Eval harness notes only."""\n', 0),
        ):
            with self.subTest(label=label):
                self.write("loopcut/tests/test_fixture.py", code)
                self.commit("Synthetic " + label)
                out = self.root / label
                result = self.run_main(["--dry-run", "--source", "source", "--exclude", str(self.excludes),
                                        *self.options(out)])
                self.assertEqual(result, expected)
                report = json.loads((out / "report.json").read_text())
                self.assertEqual([hit["category"] for hit in report["hits"]], ["private_import"] * expected)
                summary = {item["category"]: item for item in report["scan_summary"]}
                self.assertEqual(summary["private_import"]["files"], 1)

    def test_private_blockers_cannot_be_allowlisted_even_when_unchanged(self):
        self.write("loopcut/harness/evals/fixture.py", "# Synthetic fixture\n")
        self.write("loopcut/tests/test_fixture.py", 'sys.path.append("harness/evals")\n')
        # Check scripts and non-Python files are outside the public-test import rule.
        self.write("loopcut/harness/fixture_check.py", 'sys.path.append("harness/evals")\n')
        self.write("loopcut/tests/fixture.txt", 'sys.path.append("harness/evals")\n')
        self.commit("Synthetic private blockers")
        self.git("update-index", "--add", "--cacheinfo", "160000", self.base, "vendor/harness/evals")
        self.git("commit", "-m", "Synthetic private gitlink")
        baseline = self.git("rev-parse", "HEAD").decode().strip()
        for label in ("initial", "allowlisted"):
            out = self.root / label
            args = self.options(out)
            args[1] = baseline
            self.assertEqual(self.run_main(["leak-check", baseline, *args]), 1)
            report = json.loads((out / "report.json").read_text())
            self.assertEqual(report["changed_files"], [])
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(sorted(hit["category"] for hit in report["hits"]),
                             ["private_import", "private_path", "private_path"])
            self.assertTrue(all(not hit["allowed"] and not hit["reason"] for hit in report["hits"]))
            self.allowlist.write_text("".join(hit[key] + " cannot suppress a private harness blocker\n"
                                              for hit in report["hits"]
                                              for key in ("fingerprint_sha256", "line_sha256")))

    def test_private_runtime_fixture_hits_tree_and_history_without_report_text(self):
        (self.skills / "fixture.md").write_text(private_text())
        token = "lc_" + opaque()
        self.write("public.txt", private_text() + "\n" + token + "\n")
        self.write("names/" + token + ".txt", "Synthetic filename credential fixture\n")
        self.commit("Leaking fixture")
        out = self.root / "blocked"
        result = self.run_main(["--dry-run", "--source", "source", "--exclude", str(self.excludes), *self.options(out)])
        self.assertEqual(result, 1)
        report = json.loads((out / "report.json").read_text())
        self.assertEqual({hit["scope"] for hit in report["hits"]}, {"tree", "history"})
        self.assertEqual({hit["category"] for hit in report["hits"]}, {"private-text", "secret:loopcut"})
        for name in ("report.md", "report.json"):
            self.assertNotIn(private_text()[:40], (out / name).read_text())
            self.assertNotIn(token, (out / name).read_text())

    def test_standalone_checks_deleted_wrapped_text_and_intermediate_history(self):
        (self.skills / "fixture.md").write_text(private_text())
        self.write("temporary.md", private_text().replace("instructions ", "instructions\n"))
        self.commit("Intermediate fixture")
        (self.repo / "temporary.md").unlink()
        self.commit("Remove fixture")
        source = self.git("rev-parse", "source").decode().strip()
        refs = self.git("show-ref")
        objects = self.git("count-objects", "-v")
        out = self.root / "history-review"
        self.assertEqual(self.run_main(["leak-check", self.base + ".." + source, *self.options(out)]), 1)
        report = json.loads((out / "report.json").read_text())
        self.assertTrue(any(hit["file"] == "temporary.md" and hit["side"] == "before" and
                            hit["line"] == 1 for hit in report["hits"]))
        self.assertTrue(all(hit["scope"] == "history" for hit in report["hits"]))
        self.assertEqual(self.git("show-ref"), refs)
        self.assertEqual(self.git("count-objects", "-v"), objects)
        # Squashing excludes intermediate source history entirely.
        _, _, snapshot, _ = self.build()
        clean = self.root / "squashed-review"
        self.assertEqual(self.run_main(["leak-check", snapshot, *self.options(clean)]), 0)

    def test_unchanged_withheld_constants_and_env_files_are_checked(self):
        for filename, (line, assignment, _) in withheld_modules().items():
            self.write("scripts/addons_core/loopcut/" + filename, "\n" * (line - 1) + assignment)
        self.write("config/.env.local", "EMPTY=\n")
        self.commit("Known blocker fixture")
        baseline = self.git("rev-parse", "HEAD").decode().strip()
        out = self.root / "known-blockers"
        args = self.options(out)
        args[1] = baseline
        self.assertEqual(self.run_main(["leak-check", baseline, *args]), 1)
        report = json.loads((out / "report.json").read_text())
        self.assertEqual(sum(hit["category"] == "withheld-prompt" for hit in report["hits"]), 8)
        self.assertEqual(sum(hit["category"] == "secret-file" for hit in report["hits"]), 1)
        self.assertIn("Known expected blocker", (out / "report.md").read_text())

    def test_unchanged_gitlinks_allow_dry_run_to_scan_all_withheld_modules(self):
        for filename, (line, assignment, _) in withheld_modules().items():
            self.write("scripts/addons_core/loopcut/" + filename, "\n" * (line - 1) + assignment)
        self.commit("Synthetic assignment shapes")
        links = ["lib/linux_x64", "lib/macos_arm64", "lib/windows_x64"]
        for name in links:
            # Gitlinks may reference commits absent from the superproject object store.
            self.git("update-index", "--add", "--cacheinfo", "160000", "a" * 40, name)
        self.git("commit", "-m", "Synthetic submodule pointers")
        baseline = self.git("rev-parse", "HEAD").decode().strip()
        out = self.root / "gitlink-review"
        args = self.options(out)
        args[1] = baseline
        self.assertEqual(self.run_main(["--dry-run", "--source", "source", "--exclude", str(self.excludes), *args]), 1)
        report = json.loads((out / "report.json").read_text())
        self.assertTrue(report["scan_complete"])
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["unexplained_hits"], 12)
        self.assertEqual([item["file"] for item in report["submodules"]], links)
        self.assertTrue(all(not item["changed"] for item in report["submodules"]))
        summary = {item["category"]: item for item in report["scan_summary"]}
        self.assertEqual(summary["withheld-prompt"]["files"], 12)
        self.assertEqual(summary["withheld-prompt"]["lines"],
                         3 * sum(line - 1 + len(text.splitlines()) for line, text, _ in withheld_modules().values()))
        self.assertGreater(summary["cost-review"]["files"], 0)
        self.assertGreater(summary["cost-review"]["lines"], 0)
        self.assertEqual(summary["private-text"]["files"], 0)
        self.assertEqual(summary["private-text"]["lines"], 0)
        self.assertFalse(any(hit["category"] == "cost-review" for hit in report["hits"]))
        markdown = (out / "report.md").read_text()
        self.assertIn("submodule pointers, not inspected", markdown)
        self.assertIn("withheld-prompt: 12 files,", markdown)

    def test_changed_added_removed_or_replaced_gitlink_is_an_error(self):
        self.git("update-index", "--add", "--cacheinfo", "160000", self.base, "lib/linux_x64")
        self.git("commit", "-m", "Synthetic base pointer")
        baseline = self.git("rev-parse", "HEAD").decode().strip()
        for state in ("changed", "removed", "replaced", "added"):
            with self.subTest(state=state):
                self.git("update-index", "--force-remove", "lib/linux_x64")
                if state in ("changed", "added"):
                    self.git("update-index", "--add", "--cacheinfo", "160000", self.source, "lib/linux_x64")
                elif state == "replaced":
                    oid = self.git("rev-parse", "HEAD:public.txt").decode().strip()
                    self.git("update-index", "--add", "--cacheinfo", "100644", oid, "lib/linux_x64")
                self.git("commit", "-m", "Synthetic " + state + " pointer")
                out = self.root / (state + "-pointer-review")
                args = self.options(out)
                args[1] = self.base if state == "added" else baseline
                self.assertEqual(self.run_main(["leak-check", "HEAD", *args]), 2)
                report = json.loads((out / "report.json").read_text())
                self.assertTrue(report["scan_complete"])
                self.assertEqual(report["errors"], ["Submodule pointer differs from base: lib/linux_x64"])
                self.assertTrue(report["submodules"][0]["changed"])

    def test_unreadable_blob_does_not_stop_tree_history_or_zip(self):
        self.write("a_unreadable.txt", "Synthetic unreadable fixture\n")
        self.write("z_readable.txt", private_text() + "\n")
        (self.skills / "fixture.md").write_text(private_text())
        self.commit("Synthetic blob failure fixtures")
        unreadable = self.git("rev-parse", "HEAD:a_unreadable.txt").decode().strip()
        extension = self.root / "readable.zip"
        with zipfile.ZipFile(extension, "w") as archive:
            archive.writestr("fixture.md", private_text())
        original_git = release.git

        def fail_one_blob(repo, *args, **kwargs):
            if args == ("cat-file", "blob", unreadable):
                raise release.ReleaseError("Synthetic blob read failure")
            return original_git(repo, *args, **kwargs)

        out = self.root / "partial-review"
        with patch.object(release, "git", side_effect=fail_one_blob):
            self.assertEqual(self.run_main(["leak-check", "HEAD", "--zip", str(extension), *self.options(out)]), 2)
        report = json.loads((out / "report.json").read_text())
        self.assertFalse(report["scan_complete"])
        self.assertIsNone(report["unexplained_hits"])
        self.assertGreater(report["observed_unexplained_hits"], 0)
        self.assertEqual({hit["scope"] for hit in report["hits"]}, {"tree", "history", "zip"})
        self.assertTrue(any(hit["file"] == "z_readable.txt" for hit in report["hits"]))
        markdown = (out / "report.md").read_text()
        self.assertIn("INCOMPLETE", "\n".join(markdown.splitlines()[:5]))
        self.assertNotIn("No hits", markdown)
        self.assertNotIn("Unexplained hits: 0", markdown)

    def test_bad_withheld_module_does_not_stop_other_tree_or_zip_entries(self):
        extension = self.root / "partial.zip"
        with zipfile.ZipFile(extension, "w") as archive:
            for filename, (_, assignment, _) in withheld_modules().items():
                text = "PROMPT = (" if filename == "orchestrator.py" else assignment
                self.write("scripts/addons_core/loopcut/" + filename, text)
                archive.writestr("addon/" + filename, text)
        self.commit("Synthetic parse failure")
        out = self.root / "partial-withheld-review"
        self.assertEqual(self.run_main(["leak-check", "HEAD", "--zip", str(extension), *self.options(out)]), 2)
        report = json.loads((out / "report.json").read_text())
        self.assertFalse(report["scan_complete"])
        self.assertEqual(sum(hit["category"] == "withheld-prompt" for hit in report["hits"]), 9)
        self.assertEqual(len(report["errors"]), 3)

    def test_unknown_tree_entry_does_not_stop_readable_entries(self):
        self.write("z_readable.txt", private_text() + "\n")
        (self.skills / "fixture.md").write_text(private_text())
        self.commit("Synthetic readable fixture")
        source = self.git("rev-parse", "HEAD").decode().strip()
        original_entries = release.tree_entries

        def add_unsupported(repo, revision):
            entries = original_entries(repo, revision)
            if revision == source:
                return {"a_unsupported": ("040000", "tree", self.base), **entries}
            return entries

        out = self.root / "unsupported-review"
        with patch.object(release, "tree_entries", side_effect=add_unsupported):
            self.assertEqual(self.run_main(["leak-check", source, *self.options(out)]), 2)
        report = json.loads((out / "report.json").read_text())
        self.assertFalse(report["scan_complete"])
        self.assertTrue(any(hit["file"] == "z_readable.txt" and hit["scope"] == "tree"
                            for hit in report["hits"]))
        self.assertIn("Cannot inspect non-blob tree entry: a_unsupported", report["errors"][0])

    def test_zip_scans_private_text_secrets_filenames_and_withheld_prompts(self):
        (self.skills / "fixture.md").write_text(private_text())
        _, _, snapshot, _ = self.build()
        extension = self.root / "extension.zip"
        with zipfile.ZipFile(extension, "w") as archive:
            archive.writestr("addon/readme.md", private_text())
            archive.writestr("addon/.env", "KEY=" + "sk-" + opaque())
            archive.writestr("addon/router.py", "PROMPT = 'Synthetic router fixture'\n")
            archive.writestr("addon/credentials.json", "{}")
        out = self.root / "zip-review"
        self.assertEqual(self.run_main(["leak-check", snapshot, "--zip", str(extension), *self.options(out)]), 1)
        report = json.loads((out / "report.json").read_text())
        self.assertEqual(report["zip_files"], 4)
        self.assertTrue(all(hit["scope"] == "zip" for hit in report["hits"]))
        self.assertEqual({hit["category"] for hit in report["hits"]},
                         {"private-text", "secret:openai", "secret-file", "withheld-prompt"})

    def test_runtime_cost_docs_research_nested_captures_and_env_defaults(self):
        research = self.root / "research"
        (research / "session/system_prompts").mkdir(parents=True)
        (research / "session/system_prompts/fixture.txt").write_text(private_text())
        (research / "irrelevant.txt").write_text(private_text())
        cost = self.root / "cost.txt"
        cost.write_text(private_text())
        env = {"LOOPCUT_RELEASE_SKILLS_DIR": str(self.skills),
               "LOOPCUT_RELEASE_PROMPTS_DIR": str(self.root / "missing"),
               "LOOPCUT_RELEASE_RESEARCH_DIR": str(research), "LOOPCUT_RELEASE_ECONOMICS_FILE": str(cost)}
        with patch.dict(os.environ, env):
            args = release.parser(True).parse_args(["base..source", "--cost-doc", str(cost)])
        samples, coverage = release.private_samples(args)
        self.assertEqual({sample.category for sample in samples}, {"research-text", "cost-fingerprint"})
        self.assertEqual(next(item for item in coverage if item["path"] == str(research))["files"], 1)
        self.assertEqual(len(samples), 3)

    def test_research_folders_come_from_the_private_file_and_stay_out_of_snapshots(self):
        listing = self.root / "research_folders.txt"
        self.assertEqual(release.research_folders(listing), ((), ""))  # Public checkouts have no such file.
        listing.write_text("# private\nfolder rival-captures\nfolder rival-app  # the app\n"
                           "sources rival-captures/sources\n")
        folders, sources = release.research_folders(listing)
        self.assertEqual((folders, sources), (("rival-captures", "rival-app"), "rival-captures/sources"))
        rules = release.research_exclusions(folders)
        for name in ("rival-captures/sources/a.txt", "rival-app", "loopcut/vendor/rival-app/b.py"):
            self.assertTrue(release.excluded(name, rules), name)
        self.assertFalse(release.excluded("loopcut/rival-apps.md", rules))
        for bad in ("folder a/b\n", "folder /abs\n", "sources ../up\n", "dir x\n", "folder\n"):
            listing.write_text(bad)
            with self.assertRaises(ValueError, msg=bad):
                release.research_folders(listing)
        args = release.parser(True).parse_args(["base..source", "--research-dir", ""])
        self.assertIn({"path": "(research folder not configured)", "status": "missing", "files": 0,
                       "binary_files": 0, "fingerprints": 0, "open_fingerprints_dropped": 0},
                      release.private_samples(args)[1])

    def test_reviewed_line_hash_clears_tree_and_history_findings(self):
        line = " ".join(("Synthetic", "gross", "margin", "fixture for reviewing the allowlist behavior."))
        self.write("public.txt", line + "\n")
        self.commit("Reviewed terminology fixture")
        self.allowlist.write_text(release.digest(line) + " synthetic false positive\n")
        _, _, snapshot, _ = self.build()
        out = self.root / "allowlist-review"
        self.assertEqual(self.run_main(["leak-check", snapshot, *self.options(out)]), 0)
        report = json.loads((out / "report.json").read_text())
        self.assertTrue(report["hits"])
        self.assertTrue(all(hit["allowed"] for hit in report["hits"]))
        self.assertEqual(report["unexplained_hits"], 0)

    def test_private_text_encodings_and_binary_coverage(self):
        for encoding in ("utf-8-sig", "utf-16", "utf-32", "latin-1"):
            (self.skills / (encoding + ".txt")).write_bytes((private_text() + " é").encode(encoding))
        (self.skills / "binary.dat").write_bytes(b"\0" + private_text().encode())
        args = release.parser(True).parse_args(["base..source", *self.options(self.root / "unused")])
        samples, coverage = release.private_samples(args)
        self.assertEqual(len(samples), 4)
        self.assertEqual(coverage[0]["files"], 4)
        self.assertEqual(coverage[0]["binary_files"], 1)

    def test_open_prompt_overlap_reports_only_private_hits_in_all_scopes(self):
        prompts = self.root / "prompts"
        prompts.mkdir()
        opened, private = private_text(), "Restricted " + private_text()
        (prompts / "base.md").write_text("prefix " + opened.replace(" ", "\n"))
        (prompts / "worker.md").write_text(opened + "\n\n\n\n" + private)
        (prompts / "manifest.json").write_text(json.dumps({"open": ["base.md"]}))
        name = "scripts/addons_core/loopcut/fixture.md"
        self.write(name, opened + "\n" + private + "\n")
        self.commit("Synthetic public and private overlap fixture")
        out = self.root / "open-review"
        self.assertEqual(self.run_main([
            "--dry-run", "--source", "source", "--exclude", str(self.excludes),
            *self.options(out), "--prompts-dir", str(prompts)]), 1)
        report = json.loads((out / "report.json").read_text())
        self.assertTrue(report["scan_complete"])
        self.assertEqual([(hit["scope"], hit["category"], hit["line"]) for hit in report["hits"]],
                         [(scope, "private-text", 2) for scope in ("build-inputs", "tree", "history", "zip")])
        source = next(item for item in report["coverage"] if item["path"] == str(prompts))
        self.assertEqual(source["open_fingerprints_dropped"], 1)
        self.assertIn("1 fingerprints dropped as open", (out / "report.md").read_text())

    def test_malformed_manifest_reports_error_before_snapshot(self):
        prompts = self.root / "prompts"
        prompts.mkdir()
        (prompts / "manifest.json").write_text("{")
        out = self.root / "bad-manifest-review"
        refs = self.git("show-ref")
        self.assertEqual(self.run_main([
            "--dry-run", "--source", "source", *self.options(out), "--prompts-dir", str(prompts)]), 2)
        report = json.loads((out / "report.json").read_text())
        self.assertEqual(report["status"], "error")
        self.assertFalse(report["scan_complete"])
        self.assertTrue(any("malformed prompts manifest.json" in error for error in report["errors"]))
        self.assertEqual(self.git("show-ref"), refs)

    def test_invalid_zip_range_and_output_fail_without_clean_reports(self):
        _, _, snapshot, _ = self.build()
        broken = self.root / "broken.zip"
        broken.write_text("not an archive")
        out = self.root / "broken-zip-review"
        self.assertEqual(self.run_main(["leak-check", snapshot, "--zip", str(broken), *self.options(out)]), 2)
        self.assertEqual(json.loads((out / "report.json").read_text())["status"], "error")
        out = self.root / "broken-range-review"
        self.assertEqual(self.run_main(["leak-check", "base...source", *self.options(out)]), 2)
        report = json.loads((out / "report.json").read_text())
        self.assertFalse(report["scan_complete"])
        self.assertIsNone(report["unexplained_hits"])
        self.assertTrue(all(item["files"] == item["lines"] == 0 for item in report["scan_summary"]))
        markdown = (out / "report.md").read_text()
        self.assertIn("INCOMPLETE", "\n".join(markdown.splitlines()[:5]))
        self.assertNotIn("No hits", markdown)
        self.assertNotIn("Unexplained hits: 0", markdown)
        refs = self.git("show-ref")
        occupied = self.root / "occupied"
        occupied.mkdir()
        (occupied / "report.json").write_text("existing fixture")
        self.assertEqual(self.run_main(["--dry-run", "--source", "source", *self.options(occupied)]), 2)
        self.assertEqual((occupied / "report.json").read_text(), "existing fixture")
        self.assertEqual(self.git("show-ref"), refs)

    def test_cli_requires_dry_run_and_exclusions_are_relative(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            release.main([], repo=self.repo)
        for bad in ("/absolute/private", "../private", "some/../../private", "some\\private"):
            self.excludes.write_text(bad)
            with self.assertRaises(release.ReleaseError):
                release.exclusions(self.excludes)


if __name__ == "__main__":
    unittest.main()
