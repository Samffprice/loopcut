"""Runtime tools and skills, independent of a release of the add-on.

Network work stays on worker threads. Blender-facing preparation returns ToolResult.then,
so the existing agent gate, cards and main-thread pump also own remote tool calls. A cache
is scoped to the service and credential identity; signing out cannot reuse account access.
"""

import hashlib
import importlib
import itertools
import json
import math
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

from . import cloud_account, cloud_http, config, prompt_index
from .byok import catalog

NAMESPACE = "loopcut_cloud"
PREFIX = NAMESPACE + "__"
OFFER = "offer_tool"  # Local, never a server tool: shows the card for a locked tool (locked_tools).
MODEL_SOURCE_HEADER = "x-loopcut-model-source"
MODEL_SOURCES = ("chatgpt", "loopcut", "own_key", "local")
REFRESH_SECONDS = 600
MAX_SKILLS = 128
MAX_SKILL_BYTES = 256 * 1024
UPDATE_MESSAGE = "Update Loopcut to use cloud tools"
_APPLY = {"add_image_reference", "import_model", "import_material", "add_audio_strip",
          "load_frames", "attach_report", "none", "apply_shape_keys", "apply_motion"}
_FIELD = re.compile(r"[a-z][a-z0-9_]{0,63}")
_runtime = None
_snapshot_at = 0
# Deferred application only. cloud_jobs owns status, downloads and cancellation by server ID.
_jobs = {}
_jobs_lock = threading.Lock()


def tool_name(name):
    for prefix in (PREFIX, NAMESPACE + "."):
        if name.startswith(prefix):
            return name[len(prefix):]
    return None


def is_tool(name):
    return tool_name(name) is not None


def apply_args(descriptor):
    """Contract 0.7 apply_args: client-only arguments of the apply handler, as {name: JSON Schema}. They are
    offered to the model, stripped before quotes and jobs, and handed to the handler when it applies."""
    spec = (descriptor or {}).get("apply_args")
    properties = spec.get("properties") if isinstance(spec, dict) else None
    return dict(properties) if isinstance(properties, dict) else {}


def _apply_args_valid(value):
    spec = value.get("apply_args")
    if spec is None:
        return True
    if (not isinstance(spec, dict) or spec.get("type") != "object" or spec.get("additionalProperties") is not False
            or not isinstance(spec.get("properties"), dict)
            or not isinstance(spec.get("required", []), list)
            or any(not isinstance(f, str) or f not in spec["properties"] for f in spec.get("required", []))):
        return False
    taken = set((value.get("input_schema") or {}).get("properties", {})) | {
        f.get("field") for f in value.get("file_inputs", []) if isinstance(f, dict)}
    return all(isinstance(name, str) and _FIELD.fullmatch(name) and name not in taken and isinstance(schema, dict)
               for name, schema in spec["properties"].items())


def _descriptor(value):
    """Validate executable fields; optional display metadata such as category cannot reject a tool."""
    if not isinstance(value, dict):
        return False
    if not _apply_args_valid(value):
        return False
    schema, files = value.get("input_schema"), value.get("file_inputs", [])
    cost, approval = value.get("cost") or {}, value.get("approval") or {}
    bundle, byok = value.get("bundle_input"), value.get("byok")
    if bundle is not None and (not isinstance(bundle, dict) or type(bundle.get("required")) is not bool
                               or not isinstance(bundle.get("description"), str)):
        return False
    if byok is not None and not isinstance(byok, dict):
        return False
    if (not isinstance(cost, dict) or not isinstance(approval, dict)
            or type(cost.get("quote_required")) is not bool
            or type(cost.get("credits_estimate")) is not int):
        return False
    amounts = [cost.get("credits_estimate", 0), approval.get("confirm_over_credits", 50)]
    if any(type(amount) not in (int, float) or not math.isfinite(amount) or amount < 0 for amount in amounts):
        return False
    return (isinstance(value.get("name"), str)
            and re.fullmatch(r"[a-z][a-z0-9_]{0,43}", value["name"]) is not None
            and value["name"] != "job_status" and value.get("namespace") == NAMESPACE
            and type(value.get("version")) is int and value["version"] > 0
            and value.get("kind") in ("gen", "assist")
            and type(value.get("available")) is bool
            and value.get("status") in ("live", "beta", "coming_soon")
            and value.get("latency") in ("seconds", "minutes", "long")
            and isinstance(value.get('apply'), str) and value["apply"] in _APPLY
            and isinstance(value.get("description"), str) and isinstance(value.get("title"), str)
            and isinstance(value.get("backends"), list)
            and isinstance(schema, dict) and schema.get("type") == "object"
            and isinstance(schema.get("properties", {}), dict)
            and isinstance(schema.get("required", []), list)
            and all(isinstance(field, str) for field in schema.get("required", []))
            and isinstance(files, list) and all(isinstance(f, dict)
                and isinstance(f.get("field"), str) and re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", f["field"])
                and type(f.get('max_mb')) in (int, float) and math.isfinite(f['max_mb']) and f['max_mb'] >= 0
                and isinstance(f.get('accept'), list) and f['accept']
                and all(isinstance(mime, str) and mime for mime in f['accept'])
                and isinstance(f.get("required"), bool) for f in files))


def _manifest(value):
    if (not isinstance(value, dict) or not isinstance(value.get("flags"), dict)
            or any(type(value["flags"].get(k)) is not bool for k in ("services", "premium_skills"))
            or not isinstance(value.get("tools"), list) or not isinstance(value.get("skills"), list)
            or (value.get("account") is not None and not isinstance(value["account"], dict))):
        raise cloud_http.CloudError("Invalid tool manifest from Loopcut", "invalid_response")
    account = value.get("account") or {}
    if (not isinstance(account.get("credits", {}), dict) or not isinstance(account.get("settings", {}), dict)
            or any(type(v) is not bool for k, v in account.get("settings", {}).items()
                   if k in ("no_generation", "private_mode"))):
        raise cloud_http.CloudError("Invalid account settings in Loopcut manifest", "invalid_response")
    try:
        prompt_index.validate(value)
    except prompt_index.IndexError as ex:
        raise cloud_http.CloudError(str(ex), "invalid_response") from ex
    return deepcopy(value)


def merge_tools(manifest, *, signed_in=False, keys_set=None, preferred=None, policy=None, supported=True,
                on_the_house=()):
    """Return executable descriptors with a backend, using key-presence snapshots only. on_the_house: tools a setup
    Try it card's turn calls for free (try_samples), offered on the managed route even when the account's balance is
    below their estimate; only a refusal for credits is lifted, and a call the sample does not answer is an ordinary
    one that the server refuses for want of credits."""
    keys_set, preferred, policy = keys_set or {}, preferred or {}, policy or {}
    if not supported:
        return []
    account = manifest.get("account") or {}
    settings = account.get("settings") or {}
    no_generation = settings.get("no_generation", False) or policy.get("no_generation", False)
    private_mode = ((settings.get("private_mode", True) or policy.get('private_mode', False))
                    if signed_in else policy.get("private_mode", True))
    local = {d["name"]: d for d in catalog.descriptors(no_generation=no_generation,
                                                      private_mode=private_mode, keys_set=keys_set)}
    merged = dict(local)
    remote = {d["name"]: d for d in manifest.get("tools", []) if _descriptor(d)}
    for name, descriptor in merged.items():
        if name not in remote:
            descriptor["backends"] = ["byok"]
    merged.update(remote)
    balance = (account.get("credits") or {}).get("balance", 0)
    funded = signed_in and type(balance) in (int, float) and balance > 0
    result = []
    for name, descriptor in merged.items():
        if descriptor.get("status") not in ("live", "beta") or (no_generation and descriptor["kind"] == "gen"):
            continue
        if descriptor.get('apply') in {'import_material', 'apply_shape_keys'}:
            continue  # These handlers are not implemented; do not offer a paid call we cannot apply.
        if private_mode and (descriptor.get("byok") or {}).get("data_class") in ("C", "D"):
            continue
        byok = local.get(name)
        has_key = bool(byok and byok["available"] and "byok" in descriptor.get("backends", []))
        route = descriptor
        free = (descriptor.get("cost") or {}).get("credits_estimate") == 0 and not descriptor["cost"].get("quote_required")
        house = (name in on_the_house and name in remote and descriptor.get("unavailable_reason") == "credits"
                 and bool(manifest.get("flags", {}).get("services")))
        selection = catalog.choose_backend(route, signed_in_with_credits=funded or (signed_in and (free or house)),
                    preferred_backend=preferred.get(name), has_key=has_key)
        backend = selection["backend"]
        if backend == "managed" and not house and (not descriptor.get("available")
                                                   or not manifest.get("flags", {}).get("services")):
            # An off switch for managed services need not take away the user's local vendor route.
            backend = catalog.choose_backend(route, signed_in_with_credits=False,
                        preferred_backend=preferred.get(name), has_key=has_key)["backend"]
        if backend is None:
            continue
        # The BYOK adapters implement the local input contract, which can differ from managed.
        chosen = deepcopy(byok if backend == "byok" else descriptor)
        chosen.update(description=descriptor["description"], title=descriptor["title"], backend=backend,
                      available=True, status=descriptor["status"])
        result.append(chosen)
    return result


def on_the_house_tools() -> set:
    """The tool of the setup Try it card whose turn is running (agent.Turn.try_it), which try_samples answers for
    free: the account needs no credits for it. Empty outside such a turn."""
    try:
        from . import state, try_samples
        session = state._session or {}  # Never creates one.
        sample = try_samples.CARDS.get(getattr(session.get("turn"), "try_it", "") or "") if session.get("busy") else None
    except Exception:  # No session yet (registration, tests): nothing is on the house.
        return set()
    return {sample["tool"]} if sample else set()


def _packs_scene(descriptor):
    """A managed tool whose input is the scene itself: Loopcut packs and uploads it per call (_upload_bundle)."""
    return descriptor.get("backend") == "managed" and bool(descriptor.get("bundle_input"))


def schema(descriptor):
    parameters = deepcopy(descriptor["input_schema"])
    properties = parameters.setdefault("properties", {})
    required = parameters.setdefault("required", [])
    description = descriptor["description"]
    for field in descriptor.get("file_inputs", []):
        name = field["field"]
        video = any(mime.startswith("video/") for mime in field.get("accept", []))
        properties[name] = {"type": "string", "description": (
            "Scene reference: a movie clip or video strip name, or a video file path. "
            if video else
            "Scene reference: an object name to export, an image datablock name, the name of an image attached to "
            "this conversation, or a capture/render file path. ")
            + "The client resolves and uploads this file. Accepted types: " + ", ".join(field.get("accept", []))}
        if field["required"] and name not in required:
            required.append(name)
    spec = descriptor.get("apply_args") or {}
    for name, value in apply_args(descriptor).items():
        properties.setdefault(name, deepcopy(value))
        if name in spec.get("required", []) and name not in required:
            required.append(name)
    if descriptor.get("backend") == "managed":
        properties["idempotency_key"] = {"type": "string", "minLength": 1, "maxLength": 200,
            "description": "Leave unset: Loopcut reuses its own key when a reply is lost. A key names one job, "
                           "so sending it again returns that job; never reuse one to retry a failure."}
        if (descriptor.get("cost") or {}).get("quote_required"):
            properties["quote_id"] = {"type": "string", "minLength": 1,
                "description": "Optional quote from a previous call with the same inputs. The client obtains a quote and asks for approval before submission."}
    if _packs_scene(descriptor):  # The model cannot build a bundle; the contract's bundle field is filled per call.
        description += " Loopcut packs and uploads the current scene as its bundle itself."
    return {"type": "function", "function": {"name": PREFIX + descriptor["name"],
            "title": descriptor["title"], "description": description, "parameters": parameters}}


JOB_SCHEMA = {"type": "function", "function": {"name": PREFIX + "job_status",
    "title": "Checking background progress",
    "description": "Check a long cloud job's progress and apply its finished outputs to the scene. Call again if running.",
    "parameters": {"type": "object", "properties": {"job_id": {"type": "string"},
                   "cancel": {"type": "boolean", "description": "Cancel this job."}}, "required": ["job_id"]}}}


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".cloud-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, allow_nan=False)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _skill_bytes(document, regex_quotes=None):
    """Rebuild the contract's LF/unquoted format. Never trust a digest without matching bytes.

    Parsed-only responses cannot preserve arbitrary spacing or key order. We can verify the
    canonical format and the mock's quoted regex variants; any other representation fails
    closed. Contract 0.2 responses with text bypass this legacy reconstruction.
    """
    fields, body = document.get("front_matter"), document.get("body")
    if not isinstance(fields, dict) or not isinstance(body, str):
        raise ValueError("Invalid skill document")
    lines = []
    for key, value in fields.items():
        rendered = "[" + ", ".join(value) + "]" if isinstance(value, list) else str(value)
        wrapper = (regex_quotes or {}).get(key, "")
        rendered = wrapper + rendered + wrapper
        lines.append(f"{key}: {rendered}")
    return ("---\n" + "\n".join(lines) + "\n---\n" + body).encode("utf-8")


def _verified_skill(document, entry):
    from .skills import loader
    if not isinstance(document, dict):
        raise ValueError('Invalid skill document')
    if "text" in document:
        if not isinstance(document["text"], str):
            raise ValueError("Invalid raw skill text")
        canonical = document["text"].encode("utf-8")
        matches = hashlib.sha256(canonical).hexdigest() == entry["sha256"]
    else:
        canonical = _skill_bytes(document)
        fields = [k for k in document["front_matter"] if k in ("words", "leads")]
        candidates = (dict(zip(fields, wrappers)) for wrappers in itertools.product(("", "'", '"'), repeat=len(fields)))
        matches = any(hashlib.sha256(_skill_bytes(document, wrappers)).hexdigest() == entry["sha256"] for wrappers in candidates)
    if len(canonical) > MAX_SKILL_BYTES or document.get("sha256") != entry["sha256"] or not matches:
        raise ValueError("Skill hash does not match the manifest")
    skill = loader.parse_skill(canonical.decode("utf-8"), "server skill")
    if skill.id != entry["id"] or skill.version != entry["version"]:
        raise ValueError("Skill identity does not match the manifest")
    return {"front_matter": dict(skill.front_matter), "body": skill.body, "sha256": entry["sha256"]}


def _empty_manifest():
    return {"account": None, "flags": {"services": False, "premium_skills": False}, "tools": [], "skills": []}


# Contract 0.6 (proposal 010): optional ToolDescriptor.display {title, summary, category} for buttons only;
# the top-level title and description stay model-facing. Category ids become short group labels.
VOTES_MAX = 3  # Until the server says otherwise.
VOTE_BADGES = ("most_wanted", "popular")
VOTE_LIMIT_NOTICE = "All {} votes used. Take one back first."
VOTE_FAILED_NOTICE = "Couldn't reach Loopcut; that vote wasn't saved. Try again."
VOTE_SIGN_IN_NOTICE = "Sign in to Loopcut to vote."
CATEGORY_LABELS = {"render": "Render", "images": "Images", "3d_models": "3D models", "audio": "Audio",
                   "characters": "Characters", "assets": "Assets"}


def _display(descriptor, key):
    display = descriptor.get("display")
    value = display.get(key) if isinstance(display, dict) else None
    return " ".join(value.split())[:120] if isinstance(value, str) else ""


def _category_label(value):
    if not isinstance(value, str) or not value.strip():
        return ""
    value = " ".join(value.split())[:40]
    return CATEGORY_LABELS.get(value, value.replace("_", " ").capitalize())


class Runtime:
    """Injectable manifest/cache owner. refresh() is synchronous and must run off the UI thread."""
    def __init__(self, client, cache_dir, *, signed_in=False, keys_set=None, preferred=None, policy=None):
        self.client, self.cache_dir, self.signed_in = client, Path(cache_dir), signed_in
        self.keys_set, self.preferred, self.policy = keys_set or {}, preferred or {}, policy or {}
        self.manifest = _empty_manifest()
        self.etag, self.message, self.supported = "", "", True
        self.refreshed_at = 0
        self.prompts_stale = False
        self.prompt_updates = 0
        self.skills = []
        self.interests = {}
        # Votes on what comes next (contract 0.7 /v1/votes): the server's picture, or only the picks made
        # here while the server has no votes ("server": False, the interest event stands in).
        self.votes = {"mine": [], "levels": {}, "badges": {}, "max": VOTES_MAX, "server": None}
        # Votes clicked but not yet answered (name -> casting): shown at once, sent one at a time under
        # vote_lock so the server's answers arrive, and apply, in click order.
        self.vote_states, self.vote_notice, self.vote_lock = {}, "", threading.Lock()
        self.dismissed_message = ""
        # chatgpt | loopcut | own_key | local: which kind of model this install uses, sent with the manifest
        # request only when signed in (the funnel's "both connected"; never which account, key or model).
        self.model_source = ""
        self.quotes = {}  # Session-only amounts for the approval card, never vendor input.
        self.quote_requests = {}
        self.lock, self.refresh_lock = threading.RLock(), threading.Lock()
        self.load_cache()

    def load_cache(self):
        try:
            with (self.cache_dir / "manifest.json").open("rb") as handle:
                raw = handle.read(cloud_http.MAX_RESPONSE + 1)
            if len(raw) > cloud_http.MAX_RESPONSE:
                raise ValueError("Cloud catalog cache is too large")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("Cloud catalog cache must be an object")
            self.manifest = _manifest(payload["manifest"])
            etag = payload.get("etag", "")
            if not isinstance(etag, str) or any(ord(c) < 32 or ord(c) == 127 for c in etag):
                raise ValueError("Invalid cached ETag")
            self.etag = etag
            self.supported = payload.get("supported", True) is True
            self.message = "" if self.supported else UPDATE_MESSAGE
            dismissed = payload.get("dismissed_message", "")
            if not isinstance(dismissed, str):
                raise ValueError("Invalid dismissed cloud message")
            self.dismissed_message = dismissed
            interests = payload.get("interests", {})
            if not isinstance(interests, dict):
                raise ValueError("Invalid cached interests")
            self.interests = {name: "thanks" for name, state in interests.items() if state == "thanks"}
            mine = payload.get("votes_mine", [])
            if not isinstance(mine, list) or not all(isinstance(name, str) for name in mine):
                raise ValueError("Invalid cached votes")
            self.votes["mine"] = mine[:VOTES_MAX]
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, cloud_http.CloudError) as ex:
            print(f"Loopcut: could not read the cloud catalog cache: {ex}")
        self._load_skills(fetch=False)

    def descriptors(self, on_the_house=None):
        """on_the_house: tools offered without credits (merge_tools); by default the running Try it card's tool."""
        house = on_the_house_tools() if on_the_house is None else on_the_house
        with self.lock:
            return merge_tools(self.manifest, signed_in=self.signed_in, keys_set=self.keys_set,
                               preferred=self.preferred, policy={**self.policy, "no_generation": self.no_generation(),
                               "private_mode": self.private_mode()}, supported=self.supported, on_the_house=house)

    def no_generation(self):
        with self.lock:
            settings = (self.manifest.get("account") or {}).get("settings") or {}
            return (settings.get("no_generation", False) or self.policy.get("no_generation", False)
                    or getattr(self, "account_pending", {}).get("no_generation", False))

    def blob_multipart(self):
        """The manifest's capabilities.blob_multipart (contract 0.6), or None: whole-file PUTs only."""
        from . import blobs
        with self.lock:
            capabilities = self.manifest.get("capabilities")
            return blobs.multipart_capability(capabilities.get("blob_multipart") if isinstance(capabilities, dict) else None)

    def private_mode(self):
        with self.lock:
            settings = (self.manifest.get("account") or {}).get("settings") or {}
            return ((settings.get("private_mode", True) if self.signed_in else self.policy.get("private_mode", True))
                    or getattr(self, "account_pending", {}).get("private_mode", False))

    def _save(self):
        try:
            _write_json(self.cache_dir / "manifest.json", {"manifest": safe_value(self.manifest, (self.client.api_key,)), "etag": self.etag,
                                                          "supported": self.supported, "interests": self.interests,
                                                          "votes_mine": self.votes["mine"],
                                                          "dismissed_message": self.dismissed_message})
        except OSError as ex:
            print(f"Loopcut: could not save the cloud catalog cache: {ex}")

    def refresh(self, *, wait=False):
        if not self.refresh_lock.acquire(blocking=wait):
            return False
        try:
            with self.lock:
                if wait and not self.prompts_stale:
                    return True  # A concurrent index user already refreshed while we waited.
                prompt_updates = self.prompt_updates
                headers = {"If-None-Match": self.etag} if self.etag else {}
                if self.signed_in and self.model_source in MODEL_SOURCES:
                    headers[MODEL_SOURCE_HEADER] = self.model_source
            response = self.client.request_response("GET", "/v1/manifest", headers=headers, auth=self.signed_in)
            with self.lock:
                if response.status != 304:
                    self.manifest = _manifest(safe_value(response.data, (self.client.api_key,)))
                    self.etag = next((v for k, v in response.headers.items() if k.lower() == "etag"), "")
                self.supported, self.message = True, ""
                self.prompts_stale = self.prompt_updates != prompt_updates
                self.refreshed_at = time.monotonic()
                self._save()
            self._load_skills(fetch=True)
            self._observe()
            return True
        except cloud_http.CloudError as ex:
            if ex.status == 404:
                # An older deployment has no catalog. Discard cached beta capabilities too;
                # unlike a transient outage, this server cannot expand hosted prompt ids.
                with self.lock:
                    self.manifest, self.skills = _empty_manifest(), []
                    self.etag, self.message, self.supported = "", "", True
                    self.prompts_stale = False
                    self.refreshed_at = time.monotonic()
                    self._save()
            self.handle_error(ex)
            return False
        finally:
            self.refresh_lock.release()

    def _observe(self):
        """Queue the trial and low-credit cards a fresh manifest calls for (nudges.py); never fails a refresh."""
        from . import nudges
        try:
            with self.lock:
                manifest, signed_in = deepcopy(self.manifest), self.signed_in
            live = [d["name"] for d in self.descriptors() if d.get("backend") == "managed"]
            nudges.observe(manifest, signed_in, live)
        except Exception as ex:  # Cards are a nicety; the catalog must still load.
            print(f"Loopcut: could not check credits for cards: {ex!r}")

    def handle_error(self, error):
        if error.code == "contract_unsupported":
            with self.lock:
                self.supported, self.message, self.skills = False, UPDATE_MESSAGE, []
                self._save()
        elif error.code in ("tool_disabled", "daily_limit_reached"):  # The daily line follows a refusal too.
            self.refreshed_at = 0
            self.refresh_async()
        elif (error.code == "quote_required" and isinstance(error.extra.get("quote_id"), str)
              and type(error.extra.get("credits")) is int):
            with self.lock:
                self.quotes[error.extra["quote_id"]] = deepcopy(error.extra)
                if len(self.quotes) > 32:
                    oldest = next(iter(self.quotes))
                    del self.quotes[oldest]
                    self.quote_requests.pop(oldest, None)

    def refresh_async(self):
        def refresh():
            self.refresh()
            from . import mainthread
            mainthread.request_redraw()
        threading.Thread(target=refresh, name="loopcut-manifest", daemon=True).start()

    def _load_skills(self, *, fetch):
        with self.lock:
            enabled = self.supported and self.signed_in and self.manifest["flags"]["premium_skills"]
            entries = deepcopy(self.manifest["skills"][:MAX_SKILLS]) if enabled else []
            paid = (self.manifest.get("account") or {}).get("plan") in ("pro", "max")
            self.skills = []
        found = []
        retained = set()
        for entry in entries:
            if (not isinstance(entry, dict) or not set(entry.get("roles") or ()).intersection(("single", "workspace"))
                    or (entry.get("tier") == "paid" and not paid)):
                continue
            if (not isinstance(entry, dict) or not isinstance(entry.get("id"), str)
                    or not re.fullmatch(r"[a-z0-9_-]+(?:/[a-z0-9_-]+)*", entry["id"])
                    or type(entry.get("version")) is not int or entry["version"] < 1
                    or not isinstance(entry.get("sha256"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])):
                continue
            path = self.cache_dir / "skills" / (entry["sha256"] + ".json")
            retained.add(path.name)
            try:
                with path.open("rb") as handle:
                    raw = handle.read(MAX_SKILL_BYTES * 2 + 1)
                if len(raw) > MAX_SKILL_BYTES * 2:
                    raise ValueError("Skill cache is too large")
                document = json.loads(raw)
                skill = _verified_skill(document, entry)
            except (OSError, ValueError, TypeError, KeyError):
                if not fetch:
                    continue
                try:
                    document = self.client.request("GET", f"/v1/skills/{quote(entry['id'], safe='/')}?version={entry['version']}")
                    skill = _verified_skill(document, entry)
                    _write_json(path, document)
                except (cloud_http.CloudError, OSError, ValueError, TypeError, KeyError) as ex:
                    if isinstance(ex, cloud_http.CloudError) and ex.code == "plan_required":
                        continue
                    print(f"Loopcut: skipping unverifiable server skill {entry['id']}")
                    continue
            found.append(skill)
        for path in (self.cache_dir / "skills").glob("*.json"):
            if path.name not in retained:
                try:
                    path.unlink()
                except OSError:
                    print("Loopcut: could not remove an obsolete skill cache")
        with self.lock:
            if self.supported and self.manifest["flags"]["premium_skills"]:
                self.skills = found

    def server_skills(self):
        with self.lock:
            return deepcopy(self.skills) if self.supported and self.manifest["flags"]["premium_skills"] else []

    def coming_soon(self):
        with self.lock:
            return [{"name": d["name"], "title": _display(d, "title") or d["title"],
                     "summary": _display(d, "summary"),
                     "state": "sending" if d["name"] in self.vote_states else "ready",
                     "category": _category_label(_display(d, "category") or d.get("category")),
                     "voted": d["name"] in self.votes["mine"], "level": self.votes["levels"].get(d["name"]),
                     "badge": self.votes["badges"].get(d["name"])}
                    for d in self.manifest["tools"] if _descriptor(d) and d["status"] == "coming_soon"]

    def _vote_identity(self):
        """The query and body identity for /v1/votes: the install id, which a signed-in call also sends
        so the server can move this install's earlier votes to the account."""
        from . import credentials
        install_id = credentials.interest_install_id()
        if install_id is None and not self.signed_in:
            raise OSError("No install id for a signed-out vote")
        return install_id

    def _apply_votes(self, data, settled=None):
        """Take the server's picture: our picks, and a level (0-1) and badge per tool. Never counts.
        Votes still waiting their turn (all but `settled`) stay drawn the way they were clicked."""
        tools = data.get("tools")
        mine, limit = data.get("mine"), data.get("max_votes")
        if (not isinstance(tools, list) or not isinstance(mine, list) or type(limit) is not int
                or not 1 <= limit <= 10 or not all(isinstance(name, str) for name in mine)):
            raise cloud_http.CloudError("Invalid votes from Loopcut", "invalid_response")
        levels, badges = {}, {}
        for row in tools:
            if not isinstance(row, dict) or not isinstance(row.get("tool"), str):
                continue
            level = row.get("level")
            if type(level) in (int, float) and math.isfinite(level):
                levels[row["tool"]] = max(0.0, min(1.0, float(level)))
            if row.get("badge") in VOTE_BADGES:
                badges[row["tool"]] = row["badge"]
        with self.lock:
            mine = mine[:limit]
            for name, casting in self.vote_states.items():
                if name != settled:
                    mine = mine + [name] if casting and name not in mine else \
                        [m for m in mine if m != name] if not casting else mine
            self.votes = {"mine": mine, "levels": levels, "badges": badges, "max": limit, "server": True}
            self._save()

    def refresh_votes(self):
        """GET /v1/votes. A server without votes (404) leaves the picks made here and no bars."""
        try:
            install_id = self._vote_identity()
            query = f"?install_id={quote(install_id)}" if install_id else ""
            with self.vote_lock:
                self._apply_votes(self.client.request("GET", "/v1/votes" + query, auth=self.signed_in))
        except cloud_http.CloudError as ex:
            if ex.status == 404:
                with self.lock:
                    self.votes["server"] = False
        except OSError:
            pass

    def vote(self, name):
        """Cast or take back one vote. The picks change at once and go back if the server refuses."""
        with self.lock:
            if name not in {d["name"] for d in self.coming_soon()} or name in self.vote_states:
                return False
            before = list(self.votes["mine"])
            casting = name not in before
            if casting and len(before) >= self.votes["max"]:
                self.vote_notice = VOTE_LIMIT_NOTICE.format(self.votes["max"])
                return False
            self.vote_notice = ""
            self.vote_states[name] = casting
            self.votes["mine"] = before + [name] if casting else [m for m in before if m != name]
        try:
            with self.vote_lock:
                return self._send_vote(name, casting)
        except (cloud_http.CloudError, OSError) as ex:
            refused = (not self.signed_in and isinstance(ex, cloud_http.CloudError)
                       and (ex.status == 401 or ex.code == "sign_in_required"))
            with self.lock:
                self.votes["mine"] = ([m for m in self.votes["mine"] if m != name] if casting
                                      else self.votes["mine"] + [name])
                # The server was reached and wants an account (signed-out votes switched off).
                self.vote_notice = VOTE_SIGN_IN_NOTICE if refused else VOTE_FAILED_NOTICE
            return False
        finally:
            with self.lock:
                self.vote_states.pop(name, None)
                self._save()

    def _send_vote(self, name, casting):
        """One vote's request, under vote_lock. Raises what the caller undoes."""
        with self.lock:
            server = self.votes["server"]
        if server is not False:
            install_id = self._vote_identity()
            path = "/v1/votes/" + quote(name, safe="")
            try:
                if casting:
                    body = {"app_version": config.app_version(), **({"install_id": install_id} if install_id else {})}
                    data = self.client.request("PUT", path, body, auth=self.signed_in)
                else:
                    query = f"?install_id={quote(install_id)}" if install_id else ""
                    data = self.client.request("DELETE", path + query, auth=self.signed_in)
                self._apply_votes(data, settled=name)
                return True
            except cloud_http.CloudError as ex:
                if ex.status == 409:
                    with self.lock:
                        self.votes["mine"] = [m for m in self.votes["mine"] if m != name]
                        self.vote_notice = VOTE_LIMIT_NOTICE.format(self.votes["max"])
                    return False
                if ex.status != 404 or ex.code == "unknown_tool":
                    raise
                with self.lock:
                    self.votes["server"] = False
        # A server without votes: a cast is still an interest event; a take-back stays on this computer.
        if casting:
            self.interest(name)
        return True

    def interest(self, name):
        """One in-flight/successful event per tool per runtime; failed sends can be retried."""
        with self.lock:
            if name not in {d["name"] for d in self.coming_soon()} or self.interests.get(name) in ("sending", "thanks"):
                return False
            self.interests[name] = "sending"
        try:
            # Contract 0.6: install_id (random, per install) and app_version on both paths; the bearer, when
            # signed in, still identifies the user. Required only without Authorization.
            from . import credentials
            payload = {"type": "interest", "tool": name, "source": "coming_soon_button",
                       "app_version": config.app_version()}
            install_id = credentials.interest_install_id()
            if install_id is not None:
                payload["install_id"] = install_id
            elif not self.signed_in:
                with self.lock:
                    self.interests[name] = "retry"
                return False
            try:
                self.client.request("POST", "/v1/events", payload, auth=self.signed_in)
            except cloud_http.CloudError as ex:
                if ex.status not in (400, 422):
                    raise
                # A server on contract 0.5 rejects the 0.6 fields: send its body once (anonymous still needs the id).
                older = {k: payload[k] for k in ("type", "tool", "source")}
                if not self.signed_in:
                    older["install_id"] = payload["install_id"]
                self.client.request("POST", "/v1/events", older, auth=self.signed_in)
        except (cloud_http.CloudError, OSError) as ex:
            anonymous_refused = (not self.signed_in and isinstance(ex, cloud_http.CloudError)
                                 and (ex.status in (400, 401, 404, 422, 429) or ex.code == "sign_in_required"))
            with self.lock:
                self.interests[name] = ("notify" if anonymous_refused else "sign_in" if isinstance(ex, cloud_http.CloudError)
                                        and (ex.status == 401 or ex.code == "sign_in_required") else "retry")
            if isinstance(ex, cloud_http.CloudError) and not anonymous_refused:
                self.handle_error(ex)
            return False
        with self.lock:
            self.interests[name] = "thanks"
            self._save()
        return True


def _snapshot():
    """Main thread only: preferences, credential directories and account identity may use bpy."""
    global _runtime
    from . import checkpoints, credentials
    key = credentials.api_key(config.SERVICE_BASE_URL)
    identity = hashlib.sha256((config.SERVICE_URL + "\n" + key).encode()).hexdigest()
    directory = checkpoints.data_root() / "cloud" / identity
    if _runtime is None or _runtime.cache_dir != directory:
        _runtime = Runtime(cloud_http.Client(api_key=key, client_headers=cloud_http.contract_headers()),
                           directory, signed_in=bool(key))
        _runtime.live = True
    settings = (_runtime.manifest.get("account") or {}).get("settings") or {}
    with _runtime.lock:
        _runtime.policy = config.service_policy(no_generation_default=settings.get("no_generation", False))
        _runtime.keys_set = credentials.vendor_keys_set()
        _runtime.preferred = config.service_backends(_runtime.keys_set)
        try:
            _runtime.model_source = model_source()
        except Exception as ex:  # Funnel metadata; never a reason for the catalog to fail.
            print(f"Loopcut: could not tell the model source: {ex!r}")
            _runtime.model_source = ""
    return _runtime


def model_source():
    """Main thread: chatgpt | loopcut | own_key | local, from the provider in use."""
    from . import settings
    prefs = settings.preferences()
    if settings.on_chatgpt_plan():
        return "chatgpt"
    if prefs is None:
        return ""
    if prefs.provider == "LOOPCUT":
        return "loopcut"
    if prefs.provider == "OLLAMA" or prefs.base_url.startswith(("http://127.0.0.1", "http://localhost")):
        return "local"
    return "own_key"


def sync_settings():
    """Main-thread callers update project policy even when the panel is closed."""
    if _runtime is not None and getattr(_runtime, "live", False):
        _snapshot()


def refresh_async():
    runtime = _snapshot()
    runtime.refresh_async()


def panel_seen():
    global _snapshot_at
    now = time.monotonic()
    if _runtime is None or now - _snapshot_at >= 1:
        _snapshot_at = now
        try:
            _snapshot()
        except config.ConfigError as ex:
            if _runtime is not None and _runtime.supported:
                _runtime.message = str(ex)
            return
    runtime = _runtime
    # 0 is "stale, refresh now". The monotonic clock starts at boot, so on a machine up for less
    # than REFRESH_SECONDS the subtraction alone would wait (as on a fresh Linux CI runner).
    if not runtime.refreshed_at or time.monotonic() - runtime.refreshed_at >= REFRESH_SECONDS:
        runtime.refreshed_at = time.monotonic()  # Throttle offline retries and repeated draws.
        runtime.refresh_async()


def _timer():
    import bpy
    from .ui import host
    if any(host.is_host(area) for window in bpy.context.window_manager.windows for area in window.screen.areas):
        panel_seen()
    return 10.0


def register():
    import bpy
    from . import account
    if refresh_async not in account.on_signed_in:
        account.on_signed_in.append(refresh_async)
    refresh_async()
    if not bpy.app.timers.is_registered(_timer):
        bpy.app.timers.register(_timer, first_interval=10, persistent=True)


def unregister():
    import bpy
    from . import account
    if refresh_async in account.on_signed_in:
        account.on_signed_in.remove(refresh_async)
    if bpy.app.timers.is_registered(_timer):
        bpy.app.timers.unregister(_timer)
    from . import cloud_jobs
    cloud_jobs.cancel_all()


def locked_tools(runtime=None):
    """Live tools the user could unlock (M1): their route is missing only for want of a sign-in, credits or
    a plan. Anything No generation, Private mode, the region or an off switch blocks is never listed, and
    nothing is while the server does not sell (config.billing_open). [{name, title, why, credits, own_key}]."""
    runtime = runtime or _runtime
    if runtime is None or not runtime.supported or not config.billing_open():
        return []
    available = {d["name"] for d in runtime.descriptors()}
    no_generation, private = runtime.no_generation(), runtime.private_mode()
    with runtime.lock:
        manifest = deepcopy(runtime.manifest)
        signed_in = runtime.signed_in
    if not manifest.get("flags", {}).get("services"):
        return []
    local = set(catalog.tool_keys())  # Tools with an own-key adapter: the card can offer that route too.
    rows = []
    for d in manifest.get("tools", []):
        if not isinstance(d, dict) or d.get("name") in available or d.get("name") == OFFER:
            continue
        reason = d.get("unavailable_reason")
        if (d.get("status") not in ("live", "beta") or (no_generation and d.get("kind") == "gen")
                or d.get("apply") in {"import_material", "apply_shape_keys"}
                or reason in ("no_generation", "private_mode", "disabled", "region")
                or (private and cloud_account.data_class(d) in ("C", "D"))):
            continue
        cost = (d.get("cost") or {}).get("credits_estimate")
        rows.append({"name": d["name"], "title": _display(d, "title") or d.get("title") or d["name"],
                     "why": "sign_in" if not signed_in else "plan" if reason == "plan" else "credits",
                     "credits": cost if type(cost) is int else None, "own_key": d["name"] in local})
    return rows


UNAVAILABLE_NOTE = "Loopcut cloud tools you can't call now; if asked for one, say why in one line and offer what you can do"
OWN_KEY_NOTE = "* also works with the user's own vendor key (Preferences > Your own keys)"


def unavailable_line(runtime=None):
    """One line for the prompt's end: the cloud tools this user cannot call now, and why (coming soon, a sign-in,
    credits, No generation or Private mode), so "generate an image" ends in a reason and an alternative rather
    than a dead end (persona round 1, F0276/F0278). Tools offer_tool covers (locked_tools) are left to its
    card. No generation and Private mode only explain: nothing is offered around them. "" when nothing is
    missing or there is no catalog."""
    runtime = runtime or _runtime
    if runtime is None or not runtime.supported:
        return ""
    available = {d["name"] for d in runtime.descriptors()} | {row["name"] for row in locked_tools(runtime)}
    no_generation, private, billing = runtime.no_generation(), runtime.private_mode(), config.billing_open()
    with runtime.lock:
        manifest, signed_in = deepcopy(runtime.manifest), runtime.signed_in
    own_keys = {d["name"]: (d.get("byok") or {}).get("data_class")  # Tools with an own-key adapter, and its class.
                for d in catalog.descriptors(no_generation=False, private_mode=False, keys_set={})}
    groups, starred = {}, False
    for d in manifest.get("tools", []):
        name = d.get("name") if isinstance(d, dict) else None
        if not name or name in available or name == OFFER or d.get("apply") in {"import_material", "apply_shape_keys"}:
            continue
        reason, generates = d.get("unavailable_reason"), d.get("kind") == "gen"
        if reason == "disabled":
            continue
        if (no_generation and generates) or reason == "no_generation":
            why = "off: No generation is on"  # The user's own setting governs, whatever the tool's status.
        elif d.get("status") not in ("live", "beta"):
            why = "coming soon"
        elif (private and cloud_account.data_class(d) in ("C", "D")) or reason == "private_mode":
            why = "off: Private mode blocks this vendor"
        elif reason == "region":
            why = "not in the user's region"
        elif not signed_in:
            why = "needs a free Loopcut sign-in"
        elif reason == "plan":
            why = "needs another plan" if billing else "coming soon"
        else:
            why = "needs credits" if billing else "coming soon"
        title = _display(d, "title") or str(d.get("title") or name)
        if not why.startswith("off") and name in own_keys and not (private and own_keys[name] in ("C", "D")):
            title, starred = title + "*", True
        groups.setdefault(why, []).append(title)
    if not groups:
        return ""
    listed = "; ".join(f"{', '.join(titles)}: {why}" for why, titles in groups.items())
    return f"{UNAVAILABLE_NOTE}: {listed}." + (f" {OWN_KEY_NOTE}." if starred else "")


def offer_schema(locked):
    """One short tool, sent only while something is locked: the model asks the add-on to show the way."""
    titles = ", ".join(row["title"] for row in locked)
    return {"type": "function", "function": {"name": PREFIX + OFFER,
            "title": "Checking tool availability", "description":
            f"Locked Loopcut cloud tools: {titles}. Only when the user asks for what one does, call this once "
            "so the app shows them how to unlock it. Never call it unprompted.",
            "parameters": {"type": "object", "properties": {"tool": {"type": "string",
                           "enum": [row["name"] for row in locked]}}, "required": ["tool"]}}}


def _offer(arguments):
    from . import nudges, state, tools
    tool = next((row for row in locked_tools() if row["name"] == (arguments or {}).get("tool")), None)
    if tool is None:
        return tools.ToolResult("That tool is not locked; use the tools you have.", ok=False)
    with _runtime.lock:
        balance = ((_runtime.manifest.get("account") or {}).get("credits") or {}).get("balance")
    return tools.ToolResult(nudges.offer_locked(state.session(), tool, balance if tool["why"] == "credits" else None))


def tool_schemas():
    descriptors = _runtime.descriptors() if _runtime else []
    schemas = [schema(d) for d in descriptors]
    locked = locked_tools()
    if locked:
        schemas.append(offer_schema(locked))
    with _jobs_lock:
        pending = any(row["result"] is None for row in _jobs.values())
    if any(d["latency"] == "long" for d in descriptors) or pending:
        schemas.append(deepcopy(JOB_SCHEMA))
    return schemas


def skill_options():
    if _runtime is None:
        return {}
    return {"server_skills": _runtime.server_skills(),
            "available_tools": [NAMESPACE + "." + tool_name(s["function"]["name"]) for s in tool_schemas()
                                if tool_name(s["function"]["name"]) != OFFER],
            "no_generation": _runtime.no_generation()}


def prompt_index_available():
    """Cached capability for main-thread loop selection; never refresh over the network."""
    index = prompt_index.override()
    if index is not None:
        return index.get('prompts') is not None
    runtime = _runtime
    if runtime is None:
        return False
    with runtime.lock:
        return runtime.manifest.get('prompts') is not None


def hosted_index(*, required=False):
    """The injection index, refreshing stale metadata best-effort. Never fetch worker text."""
    index = prompt_index.override()
    runtime = _runtime
    if index is None and runtime is not None:
        while True:
            with runtime.lock:
                if not runtime.prompts_stale:
                    index = {"prompts": deepcopy(runtime.manifest.get("prompts")),
                             "skills": deepcopy(runtime.manifest["skills"])}
                    break
            if not runtime.refresh(wait=True):
                with runtime.lock:
                    index = {"prompts": deepcopy(runtime.manifest.get("prompts")),
                             "skills": deepcopy(runtime.manifest["skills"])}
                if index['prompts'] is not None:
                    # Ids are permanent; the server still expands the currently live text.
                    print('Loopcut: could not refresh the service prompt index; using the cached index')
                break
    if index is None or index.get('prompts') is None:
        if required:
            raise prompt_index.IndexError('No Loopcut prompt index for hosted requests or workers. Refresh the signed-in manifest '
                                          'or set LOOPCUT_PROMPT_INDEX for a service stand-in.')
        return None
    return index


def note_prompt_version(version):
    """A deployment changed injection bytes. The next index user refreshes synchronously."""
    runtime = _runtime
    if not version or os.environ.get('LOOPCUT_PROMPT_INDEX') or runtime is None:
        return
    with runtime.lock:
        if version != (runtime.manifest.get('prompts') or {}).get('version'):
            runtime.prompt_updates += 1
            runtime.prompts_stale = True
            runtime.refreshed_at = 0


def worker_skill_options():
    index = hosted_index(required=True)
    return {'skill_index': index['skills'],
            'available_tools': [NAMESPACE + '.' + tool_name(s['function']['name']) for s in tool_schemas()],
            'no_generation': _runtime.no_generation() if _runtime else False}


def coming_soon():
    return _runtime.coming_soon() if _runtime else []


def ui_message():
    if _runtime:
        with _runtime.lock:
            return _runtime.message if _runtime.message != _runtime.dismissed_message else ""
    return ""


def dismiss_ui_message(message):
    if _runtime:
        with _runtime.lock:
            if message and message == _runtime.message:
                _runtime.dismissed_message = message
                _runtime._save()


def vote_notice():
    if _runtime:
        with _runtime.lock:
            return _runtime.vote_notice
    return ""


def _in_background(name, work):
    def run():
        work()
        from . import mainthread
        mainthread.request_redraw()
    threading.Thread(target=run, name=name, daemon=True).start()


def refresh_votes():
    if _runtime:
        _in_background("loopcut-votes", _runtime.refresh_votes)


def vote(name):
    if _runtime:
        runtime = _runtime
        _in_background("loopcut-vote", lambda: runtime.vote(name))


def record_interest(name):
    if _runtime:
        runtime = _runtime
        def send():
            runtime.interest(name)
            from . import mainthread
            mainthread.request_redraw()
        threading.Thread(target=send, name="loopcut-interest", daemon=True).start()


def approval(name, arguments=None, roots=(), *, files_only=False):
    bare = tool_name(name)
    if _runtime:
        descriptor = next((d for d in _runtime.descriptors() if d["name"] == bare), None)
        if descriptor:
            from . import files
            for field in descriptor.get("file_inputs", []):
                reference = (arguments or {}).get(field["field"])
                if isinstance(reference, str):
                    if files.needs_approval("read_file", {"path": reference}, roots):
                        return True
            if files_only:
                return False
            return cloud_account.approval_details(descriptor)["required"]
    return False


def changes_scene(name):
    # job_status may apply finished outputs; checkpoint before either call. offer_tool only draws a card.
    return is_tool(name) and tool_name(name) != OFFER


def describe(name, arguments):
    if not is_tool(name):
        return None
    bare = tool_name(name)
    descriptor = next((d for d in (_runtime.descriptors() if _runtime else []) if d["name"] == bare), None)
    if bare == "job_status":
        return f"Cloud job {arguments.get('job_id', '')}", ""
    if bare == OFFER:
        row = next((r for r in locked_tools() if r["name"] == (arguments or {}).get("tool")), None)
        return f"How to unlock {row['title'] if row else 'a cloud tool'}", ""
    title = descriptor["title"] if descriptor else bare
    quote = None
    if descriptor and descriptor["backend"] == "managed" and isinstance(arguments.get("quote_id"), str):
        with _runtime.lock:
            quote = _runtime.quotes.get(arguments["quote_id"])
    route = cloud_account.approval_details(descriptor, quote)["text"] if descriptor else "cloud"
    return f"{title} · {route}", ""


def error_text(error):
    # Preserve contract extras for retries, quotes, refunds and policy explanations.
    # A confirm_token (C47) is a single-use approval capability: never in model text or history.
    extra = {k: v for k, v in error.extra.items() if k not in {"job", "upgrade", "confirm_token"}}
    return safe_value(_error_message(error) + ("\n" + json.dumps({"error": {"code": error.code,
        "type": error.error_type, **extra}}, ensure_ascii=False) if extra else ""))


def safe_value(value, secrets=()):
    """Keep credentials and URL query capabilities out of cards, history and scene metadata."""
    if isinstance(value, str):
        for secret in secrets:
            if isinstance(secret, str) and secret:
                value = value.replace(secret, "[redacted]")
        value = re.sub(r'https?://[^\s<>"\x27]+',
                       lambda m: m[0].split("?", 1)[0] + "?[redacted]" if "?" in m[0] else m[0], value)
        return re.sub(r'\b(?:lc_|sk[-_]|AIza)[A-Za-z0-9_-]+', '[redacted]', value)
    if isinstance(value, dict):
        return {safe_value(k, secrets): safe_value(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [safe_value(v, secrets) for v in value]
    return value


# The render service checks the packed scene before rendering (render_job.py SceneRejected) and says only that
# the bundle was refused. The packer already refuses Python drivers and animation of driver settings before upload;
# this names what else it refuses.
_BUNDLE_REFUSED = ("vendor_failed: the render service refused the packed scene, so nothing was rendered; {refunded}. "
                   "It refuses Python (Python driver expressions, animation that changes driver settings, OSL script "
                   "nodes), files outside the scene bundle, a missing or invalid scene camera, and frames over "
                   "4096×4096 px. Tell the user which of these the scene has and how to fix it, fix what you can, "
                   "then call again. Service detail: {detail}")


def _error_message(error):
    account_error = cloud_account.report_error(error)
    if account_error:
        return account_error
    code, extra = error.code, error.extra
    if code == "quote_required":
        return f"quote_required: {extra.get('credits', '?')} credits. Ask the user to approve this quote, then repeat the same inputs with quote_id."
    if code == "vendor_failed" and extra.get("field") == "input.bundle":
        return _BUNDLE_REFUSED.format(refunded="credits refunded" if extra.get("refunded") else "no credits charged",
                                      detail=re.sub(r"\s*To try again,.*", "", str(error))[:300])
    if code == "vendor_failed":
        return "vendor_failed: the vendor failed; credits refunded." if extra.get("refunded") else "vendor_failed: the vendor failed. Try again later."
    if code in ("rate_limited", "concurrency_limit"):
        return f"{code}: retry after {extra.get('retry_after', '?')} seconds."
    if code == "contract_unsupported":
        return UPDATE_MESSAGE
    if code == "tool_disabled":
        return "tool_disabled: this tool was switched off. The catalog is refreshing; choose another available tool."
    return f"{code}: {str(error)[:400]}"


class _Cancel(threading.Event):
    def __init__(self, is_cancelled):
        super().__init__()
        self.is_cancelled = is_cancelled

    def is_set(self):
        return super().is_set() or self.is_cancelled()

    def wait(self, timeout=None):
        end = time.monotonic() + timeout if timeout is not None else float("inf")
        while not self.is_set() and time.monotonic() < end:
            super().wait(min(0.1, max(0, end - time.monotonic())))
        return self.is_set()

    def detach(self):
        """A long job outlives its originating turn, but keeps the same panel cancel event."""
        if self.is_set():
            self.set()
        self.is_cancelled = lambda: False


def _progress(name, progress):
    from . import mainthread, state
    def update():
        card = next((i for i in reversed(state.session()["items"]) if i.get("name") == name and i.get("status") == "running"), None)
        if card is not None:
            card["output"] = safe_value(f"{progress.get('pct', 0)}% · {progress.get('message', 'Working…')}"
                                        if isinstance(progress, dict) else str(progress))
        mainthread.request_redraw()
    mainthread.run_on_main(update)


def _run(runtime, module, descriptor, arguments, files, out_dir, cancel, progress):
    try:
        if cancel.is_set():
            raise cloud_http.CloudError("Cloud call canceled before submission", "canceled")
        if descriptor["backend"] == "managed":
            job = cloud_account.submit(module, descriptor, arguments, files, runtime=runtime.account,
                project={"id": "local", "no_generation": runtime.policy.get("no_generation", False)},
                on_progress=progress, cancel=cancel, client=runtime.client)
        else:
            from .byok import runner
            inputs = {k: v for k, v in arguments.items() if k not in files}
            job = runner.run(descriptor["name"], inputs, files, out_dir,
                             no_generation=runtime.policy.get("no_generation", False),
                             private_mode=runtime.policy["private_mode"], api_key=runtime.vendor_key)
            if cancel.is_set():
                raise cloud_http.CloudError("BYOK call canceled; no output applied. Vendor charges may still apply.", "canceled")
        allowed = {"succeeded", "failed", "canceled"}
        if descriptor["backend"] == "managed" and descriptor["latency"] == "long":
            allowed.update(("queued", "running"))
        if not isinstance(job, dict) or job.get("status") not in allowed:
            raise cloud_http.CloudError("Cloud job returned an invalid result", "invalid_response")
        if job.get("status") == "failed":
            error = job.get("error") or {}
            code = error.get("code", "vendor_failed")
            raise cloud_http.CloudError(error.get("message", "Cloud job failed"), code,
                                        extra={k: v for k, v in error.items() if k not in ("code", "message", "type")},
                                        error_type=error.get("type"))
        return job
    except cloud_http.CloudError as ex:
        runtime.handle_error(ex)
        raise


def _validate_arguments(descriptor, arguments):
    """Check the scene-reference boundary before exporting anything or starting a paid call."""
    json.dumps(arguments, allow_nan=False)
    parameters = schema(descriptor)["function"]["parameters"]
    properties = parameters.get("properties", {})
    file_fields = {f["field"]: f for f in descriptor.get("file_inputs", [])}
    required = set(parameters.get("required", [])) | {
        field for field, spec in file_fields.items() if spec["required"]}
    missing = required - arguments.keys()
    if missing:
        raise ValueError("Required tool input missing: " + ", ".join(sorted(missing)))
    if parameters.get("additionalProperties") is False and arguments.keys() - properties.keys() - file_fields.keys():
        raise ValueError("Unknown tool input; use the fields in this tool's schema.")
    types = {"string": str, "object": dict, "array": list, "boolean": bool, "integer": int}
    for field, value in arguments.items():
        spec = {"type": "string"} if field in file_fields else properties.get(field, {})
        kind = spec.get("type")
        expected = types.get(kind) if isinstance(kind, str) else None
        if ((expected and (not isinstance(value, expected) or kind == "integer" and type(value) is not int))
                or kind == "number" and (type(value) not in (int, float) or not math.isfinite(value))
                or field in file_fields and not value.strip()):
            raise ValueError(f"Invalid {field}: use the type described in this tool's schema.")
    if "bundle" in arguments and (not isinstance(arguments["bundle"], str)
                                  or not re.fullmatch(r"[0-9a-f]{64}", arguments["bundle"])):
        raise ValueError("Invalid bundle: use an uploaded manifest SHA-256.")


def _discard_preparation(directory):
    try:
        shutil.rmtree(directory)
    except FileNotFoundError:
        pass
    except OSError:
        print('Loopcut: could not remove temporary cloud inputs')


BUNDLE_NAMED = 10  # Inputs a refused bundle names, per list; the rest are counted.


def _bundle_refusal(packed):
    """A scene that would not render as it looks here is not sent: name what is missing or unsupported."""
    def named(rows):
        lines = [" ".join(str(row[key]) for key in ("type", "name", "path") if row.get(key))
                 + (f": {row['reason']}" if row.get("reason") else "") for row in rows[:BUNDLE_NAMED]]
        return lines + ([f"and {len(rows) - BUNDLE_NAMED} more"] if len(rows) > BUNDLE_NAMED else [])
    lists = {key: packed[key] for key in ("missing", "unsupported") if packed[key]}
    counts = " and ".join(f"{len(rows)} {key}" for key, rows in lists.items())
    return cloud_http.CloudError(f"Loopcut can't pack this scene: {counts} item(s), listed below with what each "
                                 "needs. Relink or pack missing files, fix or remove the rest, then call again. "
                                 "Nothing was uploaded.",
                                 "bundle_incomplete", extra={key: named(rows) for key, rows in lists.items()})


def blob_multipart():
    """For upload workers: whether large files may go in parts (SPEC 3.3); None before a manifest arrives."""
    runtime = _runtime
    return runtime.blob_multipart() if runtime is not None else None


def _upload_bundle(session, checkpoint, staging, client, cancel, progress):
    """Worker: collect the call's snapshot in a child Blender, refuse it if incomplete, upload it, and return
    the manifest's SHA-256, the job's bundle. Once per call: the quote, its card and Approve reuse the hash."""
    from . import bundles, checkpoints
    cloud_account._check_cancel(cancel)
    progress("Packing the scene…")
    try:
        packed = checkpoints.bundle_from_checkpoint(session, checkpoint, staging)
    except (checkpoints.CheckpointError, bundles.BundleError, OSError, ValueError) as ex:
        raise cloud_http.CloudError(f"Could not pack the scene: {ex}", "bundle_failed") from ex
    if packed["missing"] or packed["unsupported"]:
        raise _bundle_refusal(packed)
    cloud_account._check_cancel(cancel)
    size = f"{sum(item['size'] for item in packed['manifest']['files']) / 1_000_000:,.1f} MB"
    progress(f"Uploading {size}…")
    try:
        digest = bundles.upload(staging, packed["manifest"], client=client, cancel=cancel, progress=progress,
                                multipart=blob_multipart())
    except (bundles.BundleError, OSError, ValueError) as ex:
        raise cloud_http.CloudError(f"Could not upload the scene: {ex}", "bundle_failed") from ex
    progress(f"Scene uploaded ({size}).")  # What the card says while its quote waits for approval.
    return digest


def execute(name, arguments):
    """Main thread: resolve scene references, then hand remote work back to the agent thread."""
    from . import checkpoints, state, tools
    import bpy
    origin_scene = getattr(getattr(bpy, 'context', None), 'scene', None)
    try:
        sync_settings()
    except config.ConfigError as ex:
        return tools.ToolResult(str(ex), ok=False)
    if tool_name(name) == "job_status":
        return _job_status(arguments)
    if tool_name(name) == OFFER:
        return _offer(arguments)
    runtime = _runtime
    descriptor = next((d for d in (runtime.descriptors() if runtime else []) if d["name"] == tool_name(name)), None)
    if descriptor is None:
        known = next((d for d in (runtime.manifest["tools"] if runtime else [])
                      if isinstance(d, dict) and d.get("name") == tool_name(name)), None)
        local = next((d for d in catalog.descriptors(no_generation=False,
                     private_mode=runtime.private_mode(), keys_set=runtime.keys_set)
                     if d["name"] == tool_name(name)), None) if runtime else None
        refused = known or local
        if runtime and runtime.supported and runtime.no_generation() and refused and refused.get("kind") == "gen":
            scope = "account" if ((runtime.manifest.get("account") or {}).get("settings") or {}).get("no_generation") else "project"
            return tools.ToolResult(error_text(cloud_http.CloudError("Generation disabled", "generation_disabled", 403,
                                                                    {"scope": scope})), ok=False)
        if runtime and refused and (refused.get("unavailable_reason") == "private_mode"
                or runtime.private_mode() and (refused.get("byok") or {}).get("data_class") in ("C", "D")):
            error = cloud_http.CloudError("Private mode refuses this vendor route", "private_mode", 403,
                                          {"data_class": (refused.get("byok") or {}).get("data_class", "unknown")})
            return tools.ToolResult(error_text(error), ok=False)
        if runtime and refused:
            reason = cloud_account.refusal(refused, {"no_generation": runtime.no_generation(),
                                                     "private_mode": runtime.private_mode()})
            if reason:
                return tools.ToolResult(reason, ok=False)
        return tools.ToolResult((runtime.message if runtime else "") or "Cloud tool unavailable. Sign in, add credits or configure a vendor key, then choose an available tool.", ok=False)
    try:
        module = importlib.import_module(".cloud_jobs", __package__)
    except ModuleNotFoundError as ex:
        if ex.name != __package__ + ".cloud_jobs":
            raise
        return tools.ToolResult("Cloud tools are not available in this build (cloud_jobs is missing).", ok=False)
    if any(not callable(getattr(module, fn, None)) for fn in ("resolve_files", "run", "apply_result")):
        return tools.ToolResult("Cloud tools are not available in this build (cloud_jobs is incomplete).", ok=False)
    out_dir, session, bundle_checkpoint = None, None, None
    if _packs_scene(descriptor):
        arguments = {k: v for k, v in arguments.items() if k != "bundle"}  # Only the scene packed here is sent.
    try:
        _validate_arguments(descriptor, arguments)
        out_dir = runtime.cache_dir / "jobs" / uuid.uuid4().hex
        out_dir.mkdir(parents=True, exist_ok=True)
        files = module.resolve_files(descriptor, arguments, out_dir)
        prepare = getattr(module, "prepare_inputs", None)
        if callable(prepare):  # Measured inputs (a video's length) and the apply handler's resolved target.
            arguments = prepare(descriptor, arguments, files)
        from . import try_samples
        sample = try_samples.match(getattr(state.session().get("turn"), "try_it", ""), tool_name(name), files)
        if sample is not None:  # A setup Try it card: its shipped result, not a job (try_samples).
            _discard_preparation(out_dir)
            return _sample_call(name, descriptor, module, arguments, sample, origin_scene)
        # Exports are ours. Unresolved local paths require their own gate before hashing or upload,
        # including tools invoked outside agent.run_loop and workers that cannot ask the user.
        from . import files as local_files
        external = [str(path) for path in files.values() if not Path(path).is_relative_to(out_dir)]
        file_consent = bool(external and any(not local_files.inside(Path(path), local_files.project_roots())
                                            for path in external))
        from . import credentials
        client, vendor_key = None, None
        if descriptor["backend"] == "managed":
            source = runtime.client
            key = source.api_key if source.api_key is not None else credentials.api_key(source.base_url + "/v1")
            client = cloud_http.Client(base_url=source.base_url, api_key=key, transport=source.transport,
                                       timeout=source.timeout, client_headers=source.version_headers())
        else:
            vendor_key = credentials.vendor_key(descriptor["byok"]["key_name"])
        if _packs_scene(descriptor):  # Last, so nothing above can strand it: the scene as it is at this call.
            session = state.session()
            bundle_checkpoint = checkpoints.for_bundle(session)
    except (cloud_http.CloudError, config.ConfigError, OSError, ValueError, tools.ToolError,
            checkpoints.CheckpointError) as ex:
        if out_dir is not None:
            _discard_preparation(out_dir)
        if isinstance(ex, cloud_http.CloudError):
            runtime.handle_error(ex)
        return tools.ToolResult(error_text(ex) if isinstance(ex, cloud_http.CloudError) else str(ex), ok=False)
    # Freeze policies and routing before leaving Blender's thread.
    snapshot = SimpleNamespace(policy={"no_generation": runtime.no_generation(), "private_mode": runtime.private_mode()},
                               handle_error=runtime.handle_error, client=client, vendor_key=vendor_key, account=runtime)
    def finish(is_cancelled, on_main):
        cancel = _Cancel(is_cancelled)
        progress = lambda p: _progress(name, p)
        report = cloud_account.charge_reporter()  # Settles the chat's spend on this call's card (C47).
        authorized = False
        try:
            cloud_account._check_cancel(cancel)
            if file_consent:
                cloud_account.confirm({'required': True, 'text': 'Upload local files: ' + ', '.join(external),
                                       'credits': None, 'file_access': True})
            call_arguments = arguments
            if bundle_checkpoint:  # Packed once: the quote, its card and an Approve retry all send this hash.
                call_arguments = {**arguments, "bundle": _upload_bundle(session, bundle_checkpoint, out_dir / "bundle",
                                                                        snapshot.client, cancel, progress)}
            approved_arguments = cloud_account.authorize(module, descriptor, call_arguments, files, runtime=runtime,
                project={"id": "local", "no_generation": snapshot.policy["no_generation"]},
                cancel=cancel, client=snapshot.client)
            on_main(lambda: cloud_account.recheck(runtime, descriptor, snapshot))
            authorized = True
        except cloud_http.CloudError as ex:
            runtime.handle_error(ex)
            report(0)  # Nothing was submitted: a hold taken at consent is released.
            return tools.ToolResult(error_text(ex), ok=False)
        finally:
            if bundle_checkpoint:  # Collected or not, its snapshot is no restore point; not waited for.
                from . import mainthread
                mainthread.run_on_main(lambda: checkpoints.discard(session, bundle_checkpoint))
            if not authorized:
                _discard_preparation(out_dir)
        try:
            job = _run(snapshot, module, descriptor, approved_arguments, files, out_dir, cancel, progress)
            if descriptor["latency"] == "long" and descriptor["backend"] == "managed":
                cancel.detach()
                release = lambda spent=False: cloud_account.complete(runtime, approved_arguments, spent)
                with _jobs_lock:
                    finished = [key for key, row in _jobs.items() if row["result"] is not None]
                    for key in finished[:-31]:
                        del _jobs[key]
                    _jobs.setdefault(job["id"], {"descriptor": descriptor, "module": module,
                                                "handle_error": runtime.handle_error,
                                                "report_error": cloud_account.error_reporter(), "result": None,
                                                'scene': origin_scene, 'complete': release})
                watch = getattr(module, "watch", None)
                if job.get("status") in ("succeeded", "failed", "canceled"):
                    _settle_final(report, job, None)
                elif callable(watch):  # Done, failed or canceled from the panel: the card gets its charge.
                    def settle(final, error):
                        _settle_final(report, final, error)
                        if final.get("status") in ("failed", "canceled"):
                            release(True)  # A retry before job_status already gets a new key.
                    watch(job["id"], settle)
                return tools.ToolResult(json.dumps({"job_id": job["id"], "status": job["status"],
                                                   "next": "loopcut_cloud.job_status"}))
            if descriptor["backend"] == "managed":
                _settle_final(report, job, None)
            if cancel.is_set():
                return tools.ToolResult("Cloud call canceled; no output applied.", ok=False)
            if job.get("status") != "succeeded":
                return tools.ToolResult(f"Cloud job {job.get('status', 'incomplete')}; no output applied.", ok=False)
            return on_main(lambda: _apply_to_scene(module, descriptor, job, origin_scene))
        except cloud_http.CloudError as ex:
            _settle_error(report, ex)
            return tools.ToolResult(error_text(ex), ok=False)
        finally:
            if descriptor['backend'] == 'managed':
                _discard_preparation(out_dir)
    return tools.ToolResult("Preparing cloud job…", then=finish)


def _sample_call(name, descriptor, module, arguments, sample, origin_scene):
    """A setup Try it card's call (try_samples): the run's progress on its card at production's pace, then the
    shipped outputs applied as a job's are. Nothing is quoted, held, uploaded or charged; the card says Free."""
    from . import tools, try_samples
    client_only = set(apply_args(descriptor))
    inputs = {k: v for k, v in arguments.items() if k not in client_only}
    applies = {k: v for k, v in arguments.items() if k in client_only}

    def finish(is_cancelled, on_main):
        cancel = _Cancel(is_cancelled)
        consent = getattr(cloud_account._approval, "callback", None)  # agent._Consent: the call's card(s).
        for card in getattr(consent, "shown", ()):
            card["cost"] = "Free"
        if not try_samples.play(try_samples.timeline(sample["tool"], arguments), lambda p: _progress(name, p),
                                cancel.is_set):
            return tools.ToolResult("Cloud call canceled; no output applied.", ok=False)
        job = try_samples.job(sample, descriptor.get("kind", "gen"), inputs, applies)
        return on_main(lambda: _apply_to_scene(module, descriptor, job, origin_scene))
    return tools.ToolResult("Preparing cloud job…", then=finish)


NOT_SUBMITTED ={"canceled", "approval_required", "declined", "quote_changed", "submission_pending", "pending_limit",
                 "invalid_input", "account_changed", "tool_changed"}


def _charged(job):
    charged = ((job or {}).get("credits") or {}).get("charged") if isinstance(job, dict) else None
    return charged if type(charged) in (int, float) and math.isfinite(charged) and charged >= 0 else None


def _settle_final(report, job, error):
    """A managed job's outcome, for the chat's spend: what it was charged, or nothing (C47)."""
    if error is not None:
        return _settle_error(report, error)
    from . import spending
    charged = _charged(job)
    if charged is not None:
        report(charged, spending.receipt_text(charged, canceled=job.get("status") == "canceled"))
    elif job.get("status") in ("failed", "canceled"):
        report(0, spending.receipt_text(0, canceled=job.get("status") == "canceled"))


def _settle_error(report, error):
    """A failed call keeps its hold only when the server may have charged it (a lost reply, a 5xx)."""
    job = error.extra.get("job")
    if isinstance(job, dict):
        return _settle_final(report, job, None)
    if error.code in NOT_SUBMITTED or (error.status is not None and 400 <= error.status < 500):
        report(0)


def _apply(module, descriptor, job):
    from . import scene_diff, tools
    before = scene_diff.snapshot()
    try:
        summary = module.apply_result(descriptor, job)
    except cloud_http.CloudError as ex:
        return tools.ToolResult(error_text(ex), ok=False, scene_before=before, scene_after=scene_diff.snapshot())
    except (tools.ToolError, OSError, ValueError) as ex:
        return tools.ToolResult(f"Could not apply cloud output: {str(ex)[:400]}", ok=False,
                                scene_before=before, scene_after=scene_diff.snapshot())
    receipt = cloud_account.receipt(job) if descriptor.get("backend") == "managed" else ""
    if receipt and _runtime is not None:
        _runtime.refresh_async()  # The panel's balance follows the charge.
    return tools.ToolResult(summary, scene_before=before, scene_after=scene_diff.snapshot(), receipt=receipt)


def _apply_to_scene(module, descriptor, job, scene):
    from . import tools
    if not _same_scene(scene):
        return tools.ToolResult('Cloud output belongs to another scene; switch back before applying.', ok=False)
    return _apply(module, descriptor, job)


def _same_scene(scene):
    import bpy
    current = getattr(getattr(bpy, 'context', None), 'scene', None)
    try:
        if scene is not None and current is not None and hasattr(scene, 'as_pointer'):
            return scene.as_pointer() == current.as_pointer()
        return scene is current
    except ReferenceError:
        return False  # The originating .blend or scene was closed while the job ran.


def _job_status(arguments):
    from . import tools
    if set(arguments) - {"job_id", "cancel"} or not isinstance(arguments.get("job_id"), str) or type(arguments.get("cancel", False)) is not bool:
        return tools.ToolResult("job_status needs a string job_id and optional boolean cancel.", ok=False)
    with _jobs_lock:
        record = _jobs.get(arguments["job_id"])
        if record is None:
            return tools.ToolResult("Unknown cloud job handle; use the job_id returned by the tool.", ok=False)
    module = record["module"]
    if record["result"] is not None:
        return tools.ToolResult(record["result"].text, ok=record["result"].ok)
    try:
        if arguments.get("cancel"):
            module.cancel_job(arguments["job_id"])
        job = module.job_status(arguments["job_id"])
    except cloud_http.CloudError as ex:
        record["handle_error"](ex)
        record["report_error"](ex)
        if record.get('complete') and ex.extra.get('job', {}).get('status') in {'failed', 'canceled'}:
            record['complete'](True)
        result = tools.ToolResult(error_text(ex), ok=False)
        if ex.code not in {'connection_failed', 'output_save_failed', 'hash_mismatch'}:
            record["result"] = result
        return result
    if job["status"] in ("queued", "running"):
        return tools.ToolResult(json.dumps(safe_value({"job_id": job["id"], "status": job["status"], "progress": job["progress"]})))
    if not _same_scene(record.get('scene')):
        return tools.ToolResult('Cloud output belongs to another scene; switch back before applying.', ok=False)
    if record.get('complete'):
        record['complete'](job['status'] != 'succeeded')
    if arguments.get("cancel"):
        record["result"] = tools.ToolResult("Cloud job finished before cancellation; no output applied.", ok=False)
    elif _runtime and _runtime.no_generation() and record["descriptor"]["kind"] == "gen":
        return tools.ToolResult("generation_disabled: generation is disabled for this project; output was not applied.", ok=False)
    else:
        record["result"] = _apply(module, record["descriptor"], job)
    return record["result"]
