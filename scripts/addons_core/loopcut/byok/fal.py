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
FIELDS = {"images": "input_image_urls", "format": "geometry_file_format", "mesh": "model_url",
          "faces": "face_limit", "quad": "quad"}
PRIVACY_HEADERS = {"X-Fal-Store-IO": "0",
                   "X-Fal-Object-Lifecycle-Preference": json.dumps({"expiration_duration_seconds": 3600})}
MAX_POLLS = 120
POLL_INTERVAL = 2.0
# The picks confirm face_limit/quad, but not the remesh upload field model_url or result shape.
# input_image_urls/geometry_file_format and GLB data-URI acceptance also need live validation.
# Keep these assumptions explicit; do not silently try alternative fields after a paid submit.


def _queue_url(result, field, fallback):
    return check_url(result.get(field, fallback), host="queue.fal.run")


def execute(tool, inputs, files, client, key, *, sleep=None, max_polls=MAX_POLLS,
            poll_interval=POLL_INTERVAL) -> list[dict]:
    sleep = time.sleep if sleep is None else sleep
    model = MODELS[tool]
    if tool == "image_to_3d":
        payload = {FIELDS["images"]: [data_uri(files["reference"])], FIELDS["format"]: "glb"}
    else:
        quad, faces = inputs.get("quad", False), inputs.get("face_limit", 5000)
        if quad and faces > 10000:
            raise ByokError("invalid_input", "Quad retopology supports at most 10000 faces", field="face_limit")
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
    # Rodin returns model_mesh; Remesh is expected to return model. Exact Remesh shape is unverified.
    output = result.get("model_mesh" if tool == "image_to_3d" else "model")
    if not isinstance(output, dict) or not isinstance(output.get("url"), str):
        raise ByokError("vendor_failed", "Vendor returned no GLB model")
    return [artifact("model", "model/gltf-binary", client.download(output["url"]), request_id=request_id)]
