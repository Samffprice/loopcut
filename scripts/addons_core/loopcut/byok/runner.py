"""BYOK jobs use the managed job envelope, with local paths and zero Loopcut credits.

run() is synchronous. The future agent integration owns threading and Blender imports. Pass
policy snapshots from the main thread for worker calls; only selected files go to the vendor.
"""

import hashlib
import math
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .. import config, credentials
from . import catalog, elevenlabs, fal, gemini, openai
from .http import ByokError, Client

_ADAPTERS = {"openai": openai, "fal": fal, "gemini": gemini, "elevenlabs": elevenlabs}
_MIMES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
          ".glb": "model/gltf-binary", ".obj": "model/obj"}
_SUFFIXES = {"image/png": ".png", "model/gltf-binary": ".glb", "model/vnd.fbx": ".fbx",
             "audio/wav": ".wav", "audio/mpeg": ".mp3"}


def _validate_input(schema, inputs):
    if not isinstance(inputs, dict):
        raise ByokError("invalid_input", "Tool input must be an object")
    properties = schema["properties"]
    for field in schema["required"]:
        if field not in inputs:
            raise ByokError("invalid_input", "Required tool input is missing", field=field)
    for field, value in inputs.items():
        spec = properties.get(field)
        if spec is None:
            raise ByokError("invalid_input", "Unknown tool input field")
        kind = spec["type"]
        valid = ((kind == "string" and isinstance(value, str) and bool(value.strip()))
                 or (kind == "boolean" and isinstance(value, bool))
                 or (kind == "integer" and type(value) is int)
                 or (kind == "number" and type(value) in (int, float)))
        if not valid:
            raise ByokError("invalid_input", "Tool input has the wrong type", field=field)
        if kind in ("integer", "number"):
            if not spec.get("minimum", -math.inf) <= value <= spec.get("maximum", math.inf) or not math.isfinite(value):
                raise ByokError("invalid_input", "Tool input is outside the allowed range", field=field)
        if kind == "string" and not spec.get("minLength", 0) <= len(value) <= spec.get("maxLength", math.inf):
            raise ByokError("invalid_input", "Tool input has an invalid length", field=field)
        if "enum" in spec and value not in spec["enum"]:
            raise ByokError("invalid_input", "Tool input is not an allowed value", field=field)


def _read_files(descriptor, files):
    if not isinstance(files, dict) or set(files) - {f["field"] for f in descriptor["file_inputs"]}:
        raise ByokError("invalid_input", "Files must use the tool's declared file fields")
    found = {}
    for spec in descriptor["file_inputs"]:
        field = spec["field"]
        if field not in files:
            if spec["required"]:
                raise ByokError("invalid_input", "Required tool file is missing", field=field)
            continue
        try:
            path = Path(files[field])
            mime = _MIMES.get(path.suffix.lower())
            if mime not in spec["accept"]:
                raise ByokError("invalid_input", "Unsupported file type", field=field)
            limit = spec["max_mb"] * 1024 * 1024
            with path.open("rb") as handle:
                data = handle.read(limit + 1)
        except (OSError, TypeError, ValueError) as ex:
            raise ByokError("invalid_input", "Tool file could not be read", field=field) from ex
        if not data or len(data) > limit:
            raise ByokError("invalid_input", "Tool file is empty or exceeds the size limit", field=field)
        found[field] = (data, mime)
    return found


def _output_directory(out_dir):
    directory = Path(out_dir)
    if directory.is_symlink():
        raise ByokError("byok_output_failed", "Output folder must not be a symlink")
    if directory.exists() and not directory.is_dir():
        raise ByokError("byok_output_failed", "Output folder is not a directory")
    return directory


def _save_outputs(artifacts, descriptor, out_dir, job_id):
    outputs, saved = [], []
    try:
        directory = _output_directory(out_dir)
        directory.mkdir(parents=True, exist_ok=True)
        for item in artifacts:
            suffix = _SUFFIXES[item["mime"]]
            path = directory / (job_id + "_" + item["name"] + suffix)
            fd, temporary = tempfile.mkstemp(prefix=".byok_", dir=directory)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(item["data"])
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            saved.append(path)
            outputs.append({"name": item["name"], "type": descriptor["output"]["type"], "mime": item["mime"],
                            "sha256": hashlib.sha256(item["data"]).hexdigest(), "size": len(item["data"]),
                            "path": str(path.resolve()), "meta": item["meta"]})
    except (OSError, TypeError, ValueError) as ex:
        for path in saved:
            path.unlink(missing_ok=True)
        raise ByokError("byok_output_failed", "Could not save BYOK outputs; check the output folder") from ex
    return outputs


def run(tool_name, input, files, out_dir, *, transport=None, no_generation=None, private_mode=None, api_key=None) -> dict:
    """Return a completed/failed contract job. HTTP injection accepts (method,url,headers,body,timeout)."""
    job_id = "byok_" + uuid.uuid4().hex
    job = {"id": job_id, "tool": tool_name, "status": "running", "progress": {"pct": 0, "message": "Running"},
           "credits": {"reserved": 0, "charged": 0, "refunded": 0}, "outputs": [], "provenance": None, "error": None,
           # What was asked, for the applied result's names (cloud_jobs._add_audio).
           "input": dict(input) if isinstance(input, dict) else {}}
    try:
        if no_generation is None or private_mode is None:
            policy = config.service_policy()
            no_generation = policy["no_generation"] if no_generation is None else no_generation
            private_mode = policy["private_mode"] if private_mode is None else private_mode
        descriptor = next((d for d in catalog.descriptors(no_generation=no_generation, private_mode=private_mode,
                                                          keys_set={}) if d["name"] == tool_name), None)
        if descriptor is None:
            raise ByokError("invalid_input", "Unknown BYOK tool", field="tool")
        vendor = descriptor["byok"]["provider"]
        adapter = _ADAPTERS[vendor]
        model = fal.MODELS[tool_name] if vendor == "fal" else adapter.MODEL
        job["provenance"] = {"job_id": job_id, "tool": tool_name, "vendor": vendor, "model": model,
                             "kind": descriptor["kind"], "backend": "byok",
                             "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
        if no_generation and descriptor["kind"] == "gen":
            raise ByokError("generation_disabled", "Generation is disabled in your service settings", scope="account")
        if private_mode and descriptor["byok"]["data_class"] in ("C", "D"):
            raise ByokError("private_mode", "Private mode refuses this vendor's data class",
                            data_class=descriptor["byok"]["data_class"])
        # Agent calls pass a main-thread snapshot; folder() may otherwise access bpy.
        key = credentials.vendor_key(descriptor["byok"]["key_name"]) if api_key is None else api_key
        if not key:
            raise ByokError("byok_key_missing", "Add the vendor key in Your own keys")
        if len(key) > 8192 or any(not 33 <= ord(char) <= 126 for char in key):
            raise ByokError("byok_key_invalid", "Vendor key contains invalid characters")
        _validate_input(descriptor["input_schema"], input)
        selected = _read_files(descriptor, files)
        try:
            directory = _output_directory(out_dir)
            directory.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=directory):
                pass  # Verify writes before a paid submit; outputs may still fail if disk space runs out.
        except (OSError, TypeError, ValueError):
            raise ByokError("byok_output_failed", "Could not open the BYOK output folder") from None
        artifacts = adapter.execute(tool_name, input, selected, Client(transport), key)
        job["outputs"] = _save_outputs(artifacts, descriptor, out_dir, job_id)
        job.update(status="succeeded", progress={"pct": 100, "message": "Done"})
    except ByokError as ex:
        job.update(status="failed", error=ex.error, progress={"pct": 0, "message": "Failed"})
    except config.ConfigError as ex:
        job.update(status="failed", error={"code": "invalid_input", "type": "byok_error", "message": str(ex)})
    return job
