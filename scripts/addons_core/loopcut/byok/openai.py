"""OpenAI Images API, sending only the prompt and the selected edit reference."""

import json
import uuid

from .http import ByokError
from .payloads import artifact, decode_base64

# Picks: loopcut-web/docs/platform/model-picks.md, Images and materials / Data use.
# Owner requested the base gpt-image-2.5 identifier rather than the research's flare variant.
# LIVE CHECK: blocked on the owner's own keys
GENERATE_URL = "https://api.openai.com/v1/images/generations"
EDIT_URL = "https://api.openai.com/v1/images/edits"
MODEL = "gpt-image-2.5"
FIELDS = {"model": "model", "prompt": "prompt", "size": "size", "quality": "quality",
          "format": "output_format", "image": "image"}
# store=false is named in model-picks.md, but the Images API has no documented store field.
# Do not send a Responses/Chat-only option here; API no-training defaults apply. Recheck live.


def _multipart(fields, reference):
    boundary = "loopcut_" + uuid.uuid4().hex
    chunks = []
    for name, value in fields.items():
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    data, mime = reference
    suffix = "png" if mime == "image/png" else "jpg"
    chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{FIELDS["image"]}"; '
                   f'filename="reference.{suffix}"\r\nContent-Type: {mime}\r\n\r\n').encode())
    chunks.extend((data, f"\r\n--{boundary}--\r\n".encode()))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def execute(tool, inputs, files, client, key) -> list[dict]:
    fields = {FIELDS["model"]: MODEL, FIELDS["prompt"]: inputs["prompt"],
              FIELDS["size"]: inputs.get("size", "1024x1024"),
              FIELDS["quality"]: inputs.get("quality", "auto"), FIELDS["format"]: "png"}
    headers = {"Authorization": f"Bearer {key}"}
    if tool == "image_edit":
        body, content_type = _multipart(fields, files["reference"])
        response = client.request("POST", EDIT_URL, headers={**headers, "Content-Type": content_type}, body=body,
                                  max_bytes=16 * 1024 * 1024)
        try:
            result = json.loads(response.body)
        except (ValueError, UnicodeError, RecursionError):
            raise ByokError("vendor_failed", "Vendor returned invalid image JSON") from None
    else:
        result = client.json("POST", GENERATE_URL, headers=headers, payload=fields)
    rows = result.get("data") if isinstance(result, dict) else None
    if not isinstance(rows, list) or not rows or len(rows) > 10:
        raise ByokError("vendor_failed", "Vendor returned no image outputs")
    outputs = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ByokError("vendor_failed", "Vendor returned an invalid image output")
        outputs.append(artifact(f"image_{index + 1}", "image/png", decode_base64(row.get("b64_json"))))
    return outputs
