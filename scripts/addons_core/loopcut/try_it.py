"""Try it cards and their first tool call, shared by setup and the agent without loading the UI."""

import json
import uuid

# tool: the first call the agent makes; arguments: its fixed input, except the attached photo's name.
# free: try_samples supplies the shipped result. seconds: scales the manifest's displayed motion price.
# thumbnail: in media/, our studio renders of the jumping mannequin and Poly Haven's CC0 Treasure Chest,
# alongside the card photo (our dwarf warrior concept). The setup continues to expose these definitions as
# onboarding.CARDS.
CARDS = (
    {"id": "motion", "title": "Make it move", "tool": "motion_from_text", "seconds": 3, "free": True,
     "arguments": {"prompt": "a figure jumps for joy", "duration_s": 3},
     "thumbnail": "card_motion.jpg", "line": "A figure jumps for joy, with text to motion.",
     "prompt": "Remove the default cube, then use text to motion to make a figure jump for joy, 3 seconds long."},
    {"id": "photo_3d", "title": "Photo to 3D model", "tool": "image_to_3d", "photo": True, "free": True,
     "thumbnail": "dwarf-warrior.jpg", "line": "Turns this photo into a textured 3D model.",
     "prompt": "Turn the attached photo into a textured 3D model and put it in the scene in place of the default "
               "cube."},
    {"id": "model", "title": "Model a treasure chest", "tool": None,
     "thumbnail": "card_chest.jpg", "line": "Built step by step with Blender's own tools.",
     "prompt": "Model a stylized treasure chest with gold trim"},  # A starter checked to finish on the free tier.
)


def first_call(card_id: str, session: dict, schemas: list[dict]):
    """An offered card tool on its exact free sample, or None to leave the turn alone.

    The photo must be attached to this request, not merely present in an older turn. Resolve the
    reference name the same way cloud_jobs does, then require try_samples to recognise that file.
    Checking the sample before scheduling also keeps incomplete builds from starting a paid job.
    """
    from . import cloud, cloud_jobs, conversations, llm, try_samples
    card = next((c for c in CARDS if c["id"] == card_id and c.get("free") and c["tool"]), None)
    if card is None:
        return None
    function = next((s["function"] for s in schemas
                     if cloud.is_tool(s["function"]["name"])
                     and cloud.tool_name(s["function"]["name"]) == card["tool"]), None)
    if function is None:
        return None
    arguments, files = dict(card.get("arguments", {})), {}
    if card.get("photo"):
        message = session["messages"][-1] if session["messages"] else {}
        if message.get("role") != "user" or not message.get(conversations.ATTACHED):
            return None
        attached = {p["image_url"]["url"] for p in message["content"] if p.get("type") == "image_url"}
        properties = function["parameters"].get("properties", {})
        # Managed image_to_3d uses image; the bundled own-key adapter uses reference.
        field = next((key for key in ("image", "reference") if key in properties), None)
        if field is None:
            return None
        for reference in reversed(session.get("references") or []):
            if reference["ref"] not in attached or reference.get("video"):
                continue
            path = cloud_jobs.attached_image(reference["name"])
            if path is not None and try_samples.match(card_id, card["tool"], {field: path}):
                arguments[field], files[field] = reference["name"], path
                break
        else:
            return None
    if try_samples.match(card_id, card["tool"], files) is None:
        return None
    # Mistral requires nine alphanumeric characters; the other provider adapters accept these too.
    return llm.ToolCall(id=uuid.uuid4().hex[:9], name=function["name"], arguments=json.dumps(arguments))
