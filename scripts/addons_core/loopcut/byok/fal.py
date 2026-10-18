"""fal queue jobs with bounded polling and immediate, unauthenticated output download."""

import json
import re
import time

from .http import ByokError, check_url
from .payloads import artifact, data_uri

# Picks: loopcut-web/docs/platform/model-picks.md, 3D and rigging / Data use;
# endpoint IDs also appear in research/2026-10-01-models-3d-rigging.md.
# LIVE CHECK: blocked on the owner's own keys
QUEUE_URL = "https://queue.fal.run"
MODELS = {"image_to_3d": "fal-ai/hyper3d/rodin/v2.5", "retopology": "tripo3d/tripo/remesh"}
FIELDS = {"images": "input_image_urls", "format": "geometry_file_format", "mesh": "mesh_url",
          "faces": "face_limit", "quad": "quad"}
PRIVACY_HEADERS = {"X-Fal-Store-IO": "0",
                   "X-Fal-Object-Lifecycle-Preference": json.dumps({"expiration_duration_seconds": 3600})}
MAX_POLLS = 120
POLL_INTERVAL = 2.0
# Remesh follows fal's documented schema (fal.ai/models/tripo3d/tripo/remesh/api), not yet live-tested:
# input mesh_url, quad, face_limit (500-20000 triangles, 500-10000 quads) and bake (default true);
# result model_mesh (a File: url, content_type, file_name, file_size; GLB or FBX) and model_urls
# (glb, fbx, base_model, pbr_model; each a File or null). quad forces FBX on every Tripo host: GLB
# cannot hold quads. Rodin's input_image_urls/geometry_file_format and GLB data-URI acceptance still
# need live validation. Keep these assumptions explicit; do not silently try alternative fields after
# a paid submit.
FACE_LIMITS = {False: (500, 20000), True: (500, 10000)}
FBX_MIME, GLB_MIME = "model/vnd.fbx", "model/gltf-binary"
MAGIC = ((b"Kaydara FBX Binary", FBX_MIME), (b"glTF", GLB_MIME))


def _queue_url(result, field, fallback):
    return check_url(result.get(field, fallback), host="queue.fal.run")


def _remesh_output(result, quad):
    """model_mesh, else the model_urls entry for the requested topology (fbx for quads), else the other."""
    urls = result.get("model_urls") if isinstance(result.get("model_urls"), dict) else {}
    order = ("fbx", "glb") if quad else ("glb", "fbx")
    for output in (result.get("model_mesh"), *(urls.get(name) for name in order)):
        if isinstance(output, dict) and isinstance(output.get("url"), str):
            return output
    raise ByokError("vendor_failed", "Vendor returned no model")


def model_mime(data, output=None) -> str:
    """The downloaded bytes decide: binary FBX or GLB. The vendor's file_name/content_type is only a hint,
    used in the error; a file that is neither is never passed on under either label."""
    for magic, mime in MAGIC:
        if data.startswith(magic):
            return mime
    claimed = " ".join(str(output.get(k) or "") for k in ("file_name", "content_type")).lower() \
        if isinstance(output, dict) else ""
    hint = " although it was labelled FBX" if "fbx" in claimed else \
        " although it was labelled GLB" if ("glb" in claimed or "gltf" in claimed) else ""
    raise ByokError("vendor_failed", "Vendor returned a model that is neither a binary FBX nor a GLB" + hint)


def execute(tool, inputs, files, client, key, *, sleep=None, max_polls=MAX_POLLS,
            poll_interval=POLL_INTERVAL) -> list[dict]:
    sleep = time.sleep if sleep is None else sleep
    model = MODELS[tool]
    quad = False
    if tool == "image_to_3d":
        payload = {FIELDS["images"]: [data_uri(files["reference"])], FIELDS["format"]: "glb"}
    else:
        quad, faces = bool(inputs.get("quad", False)), inputs.get("face_limit", 5000)
        low, high = FACE_LIMITS[quad]
        if not low <= faces <= high:
            kind = "Quad" if quad else "Triangle"
            raise ByokError("invalid_input", f"{kind} retopology supports {low} to {high} faces", field="face_limit")
        payload = {FIELDS["mesh"]: data_uri(files["model"]), FIELDS["faces"]: faces, FIELDS["quad"]: quad}
    headers = {"Authorization": f"Key {key}", **PRIVACY_HEADERS}
    submitted = client.json("POST", f"{QUEUE_URL}/{model}", headers=headers, payload=payload)
    request_id = submitted.get("request_id")
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", request_id):
        raise ByokError("vendor_failed", "Vendor returned an invalid queue job")
    # fal may return canonical URLs with the model subpath removed; use them on the fixed queue host.
    base = f"{QUEUE_URL}/{model}/requests/{request_id}"
    status_url = _queue_url(submitted, "status_url", base + "/status")
    result_url = _queue_url(submitted, "response_url", base)
    for _ in range(max_polls):
        status = client.json("GET", status_url, headers=headers)
        state = status.get("status")
        if state == "COMPLETED":
            if status.get("error"):
                raise ByokError("vendor_failed", "Vendor queue job failed")
            break
        if state not in ("IN_QUEUE", "IN_PROGRESS"):
            raise ByokError("vendor_failed", "Vendor queue job failed")
        sleep(poll_interval)
    else:
        raise ByokError("vendor_failed", "Vendor queue job did not complete before the polling limit")
    result = client.json("GET", result_url, headers=headers)
    if tool == "image_to_3d":
        output = result.get("model_mesh")
        if not isinstance(output, dict) or not isinstance(output.get("url"), str):
            raise ByokError("vendor_failed", "Vendor returned no GLB model")
        return [artifact("model", GLB_MIME, client.download(output["url"]), request_id=request_id)]
    output = _remesh_output(result, quad)
    data = client.download(output["url"])
    return [artifact("model", model_mime(data, output), data, request_id=request_id)]
