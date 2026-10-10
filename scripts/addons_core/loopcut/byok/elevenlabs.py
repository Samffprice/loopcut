"""ElevenLabs SFX v2; class C remains refused in private mode even with a saved key."""

import json

from .payloads import artifact

# Picks: loopcut-web/docs/platform/model-picks.md, Audio / Data use.
# LIVE CHECK: blocked on the owner's own keys
MODEL = "eleven_text_to_sound_v2"
URL = "https://api.elevenlabs.io/v1/sound-generation?output_format=mp3_44100_128"
FIELDS = {"model": "model_id", "prompt": "text", "duration": "duration_seconds",
          "influence": "prompt_influence", "loop": "loop"}
# The v2 model_id and loop fields need a live schema check; the research confirms the model ID.
# SFX has no documented request-level training/sharing opt-out, and zero retention excludes
# SFX. model-picks.md requires disabling training and SFX sharing in the vendor account.


def execute(tool, inputs, files, client, key) -> list[dict]:
    payload = {FIELDS["model"]: MODEL, FIELDS["prompt"]: inputs["prompt"]}
    for source, target in (("duration_seconds", FIELDS["duration"]), ("prompt_influence", FIELDS["influence"]),
                           ("loop", FIELDS["loop"])):
        if source in inputs:
            payload[target] = inputs[source]
    response = client.request("POST", URL, headers={"xi-api-key": key, "Content-Type": "application/json",
                                                   "Accept": "audio/mpeg"}, body=json.dumps(payload).encode())
    return [artifact("sfx", "audio/mpeg", response.body)]
