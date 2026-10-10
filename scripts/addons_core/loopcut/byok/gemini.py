"""Gemini paid-tier TTS: one line in, PCM wrapped as a local WAV for Blender's sequencer."""

import io
import re
import wave

from .http import ByokError
from .payloads import artifact, decode_base64

# Picks: loopcut-web/docs/platform/model-picks.md, Audio / Data use.
# LIVE CHECK: blocked on the owner's own keys
MODEL = "gemini-3.8-flash-tts"
URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
FIELDS = {"contents": "contents", "config": "generationConfig", "modalities": "responseModalities",
          "speech": "speechConfig", "voice": "voiceConfig", "prebuilt": "prebuiltVoiceConfig",
          "voice_name": "voiceName"}
DEFAULT_VOICE = "Kore"
SAMPLE_RATE = 24000
# generateContent fields and inlineData PCM are the established Gemini TTS format; the
# model-picks file doesn't specify the new 3.8 schema. Confirm those fields/model ID live.
# Paid-tier keys are required for class B; free Gemini keys are class D. A key doesn't reveal
# billing tier. There is no documented per-request no-storage/training switch for Gemini.


def _wav(data, rate):
    if len(data) % 2:
        raise ByokError("vendor_failed", "Vendor returned incomplete PCM audio")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(data)
    return buffer.getvalue()


def execute(tool, inputs, files, client, key) -> list[dict]:
    voice = inputs.get("voice", DEFAULT_VOICE)
    payload = {FIELDS["contents"]: [{"role": "user", "parts": [{"text": inputs["text"]}]}],
               FIELDS["config"]: {FIELDS["modalities"]: ["AUDIO"],
                                  FIELDS["speech"]: {FIELDS["voice"]: {FIELDS["prebuilt"]: {FIELDS["voice_name"]: voice}}}}}
    result = client.json("POST", URL, headers={"x-goog-api-key": key}, payload=payload)
    candidates = result.get("candidates")
    if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
        content = candidates[0].get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        if isinstance(parts, list):
            for part in parts:
                inline = part.get("inlineData") if isinstance(part, dict) else None
                if not isinstance(inline, dict):
                    continue
                mime = inline.get("mimeType", "")
                if not isinstance(mime, str):
                    break
                data = decode_base64(inline.get("data"))
                if mime in ("audio/wav", "audio/x-wav"):
                    return [artifact("voice", "audio/wav", data, voice=voice)]
                if mime.startswith(("audio/L16", "audio/pcm")):
                    match = re.search(r"(?:^|;)\s*rate=(\d+)(?:;|$)", mime)
                    if match and len(match[1]) > 5:
                        break
                    rate = int(match[1]) if match else SAMPLE_RATE
                    if not 8000 <= rate <= 48000:
                        break
                    return [artifact("voice", "audio/wav", _wav(data, rate), voice=voice, sample_rate=rate)]
    raise ByokError("vendor_failed", "Vendor returned no supported audio")
