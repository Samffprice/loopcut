"""Local ToolDescriptors mirror contract-v0.md so result handlers can share both backends.

Missing keys use the contract's `disabled` reason; backend selection returns a reason beside
the backend. Availability here describes the BYOK route, not managed-account availability.
"""

from copy import deepcopy

from .. import config, credentials

_IMAGE = {"prompt": {"type": "string", "minLength": 1, "maxLength": 32000},
          "size": {"type": "string", "enum": ["1024x1024", "1536x1024", "1024x1536"]},
          "quality": {"type": "string", "enum": ["low", "medium", "high", "auto"]}}


def _file(field, accept, max_mb):
    return {"field": field, "accept": accept, "max_mb": max_mb, "required": True}


def _tool(name, title, description, kind, provider, key, data_class, properties, required,
          files, output_type, formats, apply, latency="seconds"):
    return {"name": name, "namespace": "loopcut_cloud", "version": 1, "title": title,
            "description": description, "kind": kind, "status": "beta", "available": False,
            "unavailable_reason": "disabled",
            "input_schema": {"type": "object", "properties": deepcopy(properties), "required": required,
                             "additionalProperties": False},
            "file_inputs": files, "output": {"type": output_type, "formats": formats},
            "apply": apply, "cost": None, "latency": latency, "approval": {"confirm_over_credits": 50},
            "backends": ["managed", "byok"],
            "byok": {"provider": provider, "key_name": key, "data_class": data_class}}


_TOOLS = (
    _tool("image_generate", "Generate image", "Generate a PNG reference image from a text prompt.",
          "gen", "openai", "OPENAI_API_KEY", "A", _IMAGE, ["prompt"], [],
          "image", ["png"], "add_image_reference"),
    _tool("image_edit", "Edit image", "Edit a supplied PNG or JPEG reference using a text prompt.",
          "gen", "openai", "OPENAI_API_KEY", "A", _IMAGE, ["prompt"],
          [_file("reference", ["image/png", "image/jpeg"], 20)], "image", ["png"], "add_image_reference"),
    _tool("image_to_3d", "Image to 3D", "Generate a GLB model from one image with Rodin; geometry may differ.",
          "gen", "fal", "FAL_KEY", "A", {}, [],
          [_file("reference", ["image/png", "image/jpeg"], 20)],
          "model_3d", ["glb"], "import_model", "minutes"),
    _tool("retopology", "Retopology",
          "Remesh a GLB or OBJ at a target face count (500-20000 triangles; 500-10000 with quad). "
          "Triangles come back as a GLB; quad returns an FBX, since GLB cannot hold quads.",
          "assist", "fal", "FAL_KEY", "B",
          {"face_limit": {"type": "integer", "minimum": 500, "maximum": 20000},
           "quad": {"type": "boolean"}}, [],
          [_file("model", ["model/gltf-binary", "model/obj"], 150)],
          "model_3d", ["glb", "fbx"], "import_model", "minutes"),
    _tool("voice_line", "Voice line", "Speak one text line using a Gemini prebuilt voice. Requires paid Gemini API.",
          "gen", "gemini", "GEMINI_API_KEY", "B",
          {"text": {"type": "string", "minLength": 1, "maxLength": 8000},
           "voice": {"type": "string", "minLength": 1, "maxLength": 100}}, ["text"], [],
          "audio", ["wav"], "add_audio_strip"),
    _tool("sfx", "Sound effect", "Generate one sound effect, optionally looping. May train (class C); private mode refuses it.",
          "gen", "elevenlabs", "ELEVENLABS_API_KEY", "C",
          {"prompt": {"type": "string", "minLength": 1, "maxLength": 10000},
           "duration_seconds": {"type": "number", "minimum": 0.5, "maximum": 30},
           "prompt_influence": {"type": "number", "minimum": 0, "maximum": 1},
           "loop": {"type": "boolean"}}, ["prompt"], [], "audio", ["mp3"], "add_audio_strip"),
)


def descriptors(*, no_generation=None, private_mode=None, keys_set=None) -> list[dict]:
    """Fresh descriptors, with current key presence and policy; explicit snapshots avoid bpy."""
    if no_generation is None or private_mode is None:
        policy = config.service_policy()
        no_generation = policy["no_generation"] if no_generation is None else no_generation
        private_mode = policy["private_mode"] if private_mode is None else private_mode
    keys_set = credentials.vendor_keys_set() if keys_set is None else keys_set
    found = deepcopy(_TOOLS)
    for tool in found:
        reason = "disabled" if not keys_set.get(tool["byok"]["key_name"]) else None
        if private_mode and tool["byok"]["data_class"] in ("C", "D"):
            reason = "private_mode"
        if no_generation and tool["kind"] == "gen":
            reason = "no_generation"
        tool.update(available=reason is None, unavailable_reason=reason)
    return found


def tool_keys() -> dict[str, str]:
    """{tool name: vendor key name} for every tool that has an own-key adapter."""
    return {tool["name"]: tool["byok"]["key_name"] for tool in _TOOLS}


def choose_backend(descriptor, *, signed_in_with_credits, preferred_backend, has_key) -> dict:
    """Return {backend: managed|byok|None, unavailable_reason: contract enum|None} (§1.6).

    preferred_backend "byok" (config.service_backends: the key is set and "Use this key instead of
    credits" is on) takes the own-key route even with credits, and never falls back to credits.
    Otherwise credits come first when the account has them, then the key. The local catalog's
    disabled/sign_in/credits availability is route-specific. Generation, Private mode, plan and
    region refusals remain binding.
    """
    reason = descriptor.get("unavailable_reason")
    if reason in ("no_generation", "private_mode", "plan", "region") or descriptor.get("status") == "coming_soon":
        return {"backend": None, "unavailable_reason": reason or "disabled"}
    backends = descriptor.get("backends", [])
    byok = "byok" in backends and has_key
    if preferred_backend == "byok" and "byok" in backends:
        return {"backend": "byok" if byok else None, "unavailable_reason": None if byok else "disabled"}
    if signed_in_with_credits and "managed" in backends:
        backend = "byok" if preferred_backend == "byok" and byok else "managed"
    else:
        backend = "byok" if byok else None
    return {"backend": backend, "unavailable_reason": None if backend else reason or "sign_in"}
