"""Verify local preference persistence, project policy and jobs in a Loopcut Blender build.

    <blender> -b --factory-startup --python loopcut/harness/cloud_account_check.py

Loads this checkout, saves/reloads temporary preferences and a .blend, and uses the canonical mock's
in-memory HTTP transport. Set LOOPCUT_CONTRACT_DIR if the contract lives elsewhere.
No stored credentials, network, or user project files are used.
"""

import io
import os
import sys
import tempfile
import traceback
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout  # noqa: E402
checkout.use()
os.environ["LOOPCUT_DATA_DIR"] = tempfile.mkdtemp(prefix="loopcut-account-data-")
os.environ["LOOPCUT_CONFIG_DIR"] = tempfile.mkdtemp(prefix="loopcut-account-config-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from mock_service import MockTransport  # noqa: E402
from loopcut import cloud, cloud_account, cloud_http, cloud_jobs, config, credentials, settings  # noqa: E402


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print("PASS " + message, flush=True)


def check_preferences(mock, directory):
    """Use real RNA callbacks and a private userpref.blend, without enabling cloud workers."""
    import addon_utils

    folder = Path(directory) / "preferences"
    folder.mkdir()
    empty_env = Path(directory) / "empty.env"
    empty_env.touch()
    errors = io.StringIO()
    environment = {"BLENDER_USER_CONFIG": str(folder), "LOOPCUT_ENV_FILE": str(empty_env),
                   "LOOPCUT_NO_GENERATION": "", "LOOPCUT_PRIVATE_MODE": ""}
    with patch.dict(os.environ, environment), redirect_stderr(errors), \
            patch.object(credentials, "api_key", return_value=""), \
            patch.object(cloud_http, "urllib_transport", mock), patch.object(cloud, "_runtime", None):
        check(Path(bpy.utils.user_resource("CONFIG", create=True)).resolve() == folder.resolve(),
              "preference saves use only the temporary config folder")
        settings.register()
        addon = bpy.context.preferences.addons.get(settings.PACKAGE)
        if addon is None:
            addon = bpy.context.preferences.addons.new()
            addon.module = settings.PACKAGE
        try:
            prefs = settings.preferences()
            check(prefs.no_generation is False and prefs.private_mode is True, "signed-out preference defaults")
            runtime = cloud._snapshot()
            check(not runtime.signed_in, "local preference checks are signed out")
            for no_generation, private_mode in ((True, False), (False, True)):
                prefs.no_generation = no_generation
                prefs.private_mode = private_mode
                check(prefs.no_generation == no_generation and prefs.private_mode == private_mode,
                      "both signed-out preferences toggle through RNA callbacks")
                check(prefs.local_no_generation == no_generation and prefs.local_private_mode == private_mode,
                      "both values are stored in hidden preference fields")
                check(runtime.policy == {"no_generation": no_generation, "private_mode": private_mode},
                      "local preferences reach the runtime policy")
                check(bpy.ops.wm.save_userpref() == {"FINISHED"}, "temporary preferences saved")
                prefs.no_generation = not no_generation
                prefs.private_mode = not private_mode
                # Reload only preference data; keep our checkout's registered RNA classes.
                with patch.object(addon_utils, "disable_all"), patch.object(addon_utils, "reset_all"):
                    check(bpy.ops.wm.read_userpref() == {"FINISHED"}, "temporary preferences reloaded")
                prefs = settings.preferences()
                check(prefs.no_generation == no_generation and prefs.private_mode == private_mode,
                      "both signed-out values survive preference save and reload")
            check(not any(row["path"] == "/v1/settings" for row in mock.calls),
                  "signed-out toggles send no account settings request")
        finally:
            # Preference reload invalidates the old Addon pointer.
            addon = bpy.context.preferences.addons.get(settings.PACKAGE)
            if addon is not None:
                bpy.context.preferences.addons.remove(addon)
            settings.unregister()
    check(not errors.getvalue(), "preference registration, reads and toggles print no traceback" +
          (": " + errors.getvalue() if errors.getvalue() else ""))


def main():
    mock = MockTransport(live_tools=["asset_search", "image_generate"])
    client = cloud_http.Client(base_url="http://127.0.0.1:3000", api_key="lc_demo", transport=mock,
                               client_headers=cloud_http.contract_headers())
    with tempfile.TemporaryDirectory(prefix="loopcut-account-check-") as directory:
        check_preferences(mock, directory)
        runtime = cloud.Runtime(client, Path(directory) / "cache", signed_in=True)
        check(runtime.refresh(), "manifest loaded from mock")
        blend = Path(directory) / "project-policy.blend"
        for override in ("inherit", "on", "off"):
            cloud_account.set_project_override(bpy.context.scene, override)
            bpy.ops.wm.save_as_mainfile(filepath=str(blend))
            bpy.context.scene[cloud_account.PROJECT_PROPERTY] = "changed-after-save"
            bpy.ops.wm.open_mainfile(filepath=str(blend))
            check(cloud_account.project_override(bpy.context.scene) == override,
                  f"project {override} survives save and reload")
        cloud_account.set_project_override(bpy.context.scene, "on")
        bpy.ops.wm.save_as_mainfile(filepath=str(blend))
        bpy.ops.wm.open_mainfile(filepath=str(blend))
        with patch.object(cloud, "_runtime", runtime), patch.object(cloud_jobs, "_client", return_value=client):
            runtime.policy = config.service_policy()
            check(runtime.policy["no_generation"], "effective policy reads the saved scene property")
            check(not any(d["kind"] == "gen" for d in runtime.descriptors()), "generation tools hidden")
            check(cloud.skill_options()["no_generation"], "skill selection receives no-generation")
            # Use the actual add-on dispatch, including policy snapshot, consent and submit.
            with cloud_account.approval_context(lambda details: not details.get("required", False)):
                pending = cloud.execute("loopcut_cloud__asset_search", {"query": "chair"})
                check(pending.then is not None, "assist call prepared")
                result = pending.then(lambda: False, lambda fn: fn())
                check(result.ok, "assist job succeeded and applied")
            request = next(row["body"] for row in mock.calls if row["path"] == "/v1/jobs")
            check(request["project"]["no_generation"] is True, "effective value reached mock job request")
            check(mock.state.jobs[next(iter(mock.state.jobs))]["body"]["project"]["no_generation"] is True,
                  "mock accepted the project policy")
    print("PASS cloud account check", flush=True)


try:
    with patch("urllib.request.urlopen", side_effect=AssertionError("Unexpected network request")):
        main()
except Exception:
    traceback.print_exc()
    print("FAIL cloud account check", flush=True)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(1)
