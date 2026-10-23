"""Hosts the panel inside a Blender area: draw handler, input operators, keymap.

In the Loopcut build of Blender the panel lives in its own editor type (SpaceLoopcut). In stock
Blender, which the dev loop and the evals use, it borrows Text Editor areas that were opted in
with `loopcut.open`. Everything below the HOST_* constants is the same for both.
"""

import json
import sys
import time
from pathlib import Path

import bpy

from .. import account, agent, checkpoints, config, conversations, scene_context, settings, state, update
from . import draw, layout, textedit

NATIVE = hasattr(bpy.types, "SpaceLoopcut")
HOST_SPACE = bpy.types.SpaceLoopcut if NATIVE else bpy.types.SpaceTextEditor
HOST_UI_TYPE = "LOOPCUT" if NATIVE else "TEXT_EDITOR"
HOST_KEYMAP = ("Loopcut", "LOOPCUT") if NATIVE else ("Text", "TEXT_EDITOR")
SCROLL_STEP = 60
DOCK_FACTOR = 0.72           # Share of the 3D viewport's width that stays a viewport.
CONFIG_TTL = 2.0             # Seconds the model label is trusted before config is read again.
PATH_TTL = 3.0               # File availability is refreshed by the lifecycle poll, never by drawing.
COMMAND = "oskey" if sys.platform == "darwin" else "ctrl"

_displays: dict[int, dict] = {}
_runtime: dict = {}


def hosts() -> set:
    return state.hosts


def is_host(area) -> bool:
    if area is None:
        return False
    return area.type == HOST_UI_TYPE if NATIVE else area.as_pointer() in hosts()


def tag_redraw_all() -> None:
    account.refresh_expired_usage()
    _refresh_missing_paths(state.session())
    _prune_images()  # lifecycle's regular poll also catches panels closed through Blender's UI.
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if is_host(area):
                area.tag_redraw()


def config_changed() -> None:
    """Settings were edited: show the new model (or the lack of a key) on the next redraw."""
    _runtime.pop("model", None)
    _runtime.pop("connections", None)
    tag_redraw_all()


def _connections_cached() -> dict:
    """The sign-in states the cards draw from (both connections, the tips switch), reread every CONFIG_TTL:
    the ChatGPT one reads a file, and drawing happens on every mouse move."""
    cached = _runtime.get("connections")
    if cached is None or time.monotonic() - cached[1] > CONFIG_TTL:
        from .. import chatgpt_auth, nudges
        try:
            chatgpt = chatgpt_auth.plan_signed_in()
        except Exception as ex:  # An unreadable sign-in file must not stop the panel drawing.
            print(f"Loopcut: could not read the ChatGPT sign-in: {ex!r}")
            chatgpt = False
        cached = ({"chatgpt_plan": chatgpt, "loopcut_signed_in": account.signed_in(),
                   "tips_off": nudges.tips_off()}, time.monotonic())
        _runtime["connections"] = cached
    return cached[0]


def _model_label() -> tuple[str, bool]:
    """(what to show in the input's footer, whether Loopcut still needs setting up)."""
    return _config_cached()[:2]


def _config_cached() -> tuple[str, bool, int]:
    """(model label, whether Loopcut still needs setting up, context budget), reread every CONFIG_TTL."""
    cached = _runtime.get("model")
    if cached is None or time.monotonic() - cached[3] > CONFIG_TTL:
        try:
            cfg = config.load()
            cached = (cfg.model, False, cfg.context_budget, time.monotonic())
        except config.ConfigError:
            cached = ("Set up Loopcut", True, config.DEFAULT_CONTEXT_BUDGET, time.monotonic())
        _runtime["model"] = cached
    return cached[0], cached[1], cached[2]


def _key_needed(needs_setup: bool) -> str:
    """The own-key provider chosen in Preferences that still has no usable setup ("OpenAI"): the panel asks for
    its key rather than showing the first-run sign-in card while the user's accounts stay connected (F0287)."""
    prefs = settings.preferences() if needs_setup else None
    if prefs is None or prefs.provider in {"LOOPCUT", "CHATGPT"}:
        return ""
    return settings.provider_label(prefs.provider)


def _checkpoint_statuses(session: dict) -> dict:
    """id -> status for the checkpoints on screen. Re-read only when the index file changes."""
    index = checkpoints.data_root() / "checkpoints" / session["id"] / checkpoints.INDEX_NAME
    stamp = index.stat().st_mtime_ns if index.is_file() else None
    if _runtime.get("checkpoint_stamp") != (session["id"], stamp):
        # No index means the store is gone (dropped after 30 idle days) while the conversation,
        # which is kept, still points at it: those checkpoints show as expired.
        ids = [i["checkpoint"] for i in session["items"] if i.get("checkpoint")]
        _runtime["checkpoint_statuses"] = {i: checkpoints.status(session, i) if stamp else "missing" for i in ids}
        _runtime["checkpoint_stamp"] = (session["id"], stamp)
    return _runtime["checkpoint_statuses"]


def _restore_later(checkpoint_id: str, edit: bool = False) -> None:
    """Loading a file from inside a running operator frees that operator under its own feet,
    so the restore runs from a timer, after the click has been fully handled. After an Edit the
    message is back in the box: the box takes the keys, as after a dropped photo."""
    # The clicked window's place in the list, not the window: an in-place restore loads the snapshot's
    # windows and frees these, and temp_override reads a freed window before checking it (0.2.6 crashed).
    windows = list(bpy.context.window_manager.windows)
    place = next((n for n, w in enumerate(windows) if w == bpy.context.window), 0)

    def run():
        session = state.session()
        try:
            checkpoints.restore(session, checkpoint_id)
            restored = True
        except checkpoints.CheckpointError as ex:
            session["items"].append(state.item_error(str(ex)))
            restored = False
        session["confirm_restore"] = None
        _runtime.pop("checkpoint_stamp", None)
        tag_redraw_all()
        window = _window_after_load(place) if edit and restored else None
        if window is not None:
            try:
                with bpy.context.temp_override(window=window):
                    bpy.ops.loopcut.focus()
            except RuntimeError:
                pass  # The message is in the box all the same.
    state.session()["focused"] = False
    bpy.app.timers.register(run, first_interval=0.01)


def _window_after_load(place: int):
    """The window to go on in after a file read replaced every window, screen and area: the one at the
    clicked window's place in the list, unless it lost the panel and another window shows it."""
    windows = list(bpy.context.window_manager.windows)
    order = windows[place:place + 1] + windows
    return next((w for w in order if find_panel(w)), order[0] if order else None)


def draw_area() -> None:
    context = bpy.context
    area, region = context.area, context.region
    if not is_host(area):
        return
    from .. import cloud, cloud_account
    cloud.panel_seen()
    if not _runtime.get("first_panel_seen"):
        from .. import telemetry
        telemetry.first_use("panel_opened")
        _runtime["first_panel_seen"] = True
    prefs = settings.preferences()
    session = state.session()
    draw.retain_images(_image_paths(session))
    model, needs_setup, budget = _config_cached()
    key_needed = _key_needed(needs_setup)
    selection = [o.name for o in context.view_layer.objects.selected] if context.view_layer else []
    options = settings.model_options() if state.ui.get("model_menu") else []
    from .. import cloud_jobs, playback, workspace
    from .. import chatgpt_ui, onboarding
    if any(item["kind"] == "user" for item in session["items"]):
        onboarding.dismiss_hints()
    display = layout.build(session, region.width, region.height, context.preferences.system.ui_scale,
                           draw.measure, model, _checkpoint_statuses(session),
                           {**state.ui, "now": time.time(), "needs_setup": needs_setup, "selection": selection,
                            "key_needed": key_needed,
                            "provider": getattr(prefs, "provider", "LOOPCUT"),
                            "allowance_notice": state.ui.get("allowance_notice"),
                            "missing_paths": _missing_paths(session), "audio_playing": playback.playing(),
                            "context_budget": budget, "model_options": options, "model_label": f"{key_needed} · no key" if key_needed else settings.model_label(model),
                            "hints_dismissed": onboarding.hints_dismissed(),
                            "sign_in": {"loopcut": account.status(), "chatgpt": chatgpt_ui.status()},
                            "update": update.banner(), "cloud_jobs": cloud_jobs.ui_rows(),
                            "local_processes": workspace.live_rows(),
                            "service_policy": {key: getattr(prefs, key, default) for key, default in cloud_account.DEFAULTS.items()},
                            "service_signed_in": bool(cloud._runtime and cloud._runtime.signed_in),
                            "project_override": cloud_account.project_override(context.scene),
                            **_connections_cached(), **_cloud_panel_state(session)})
    if state.ui.get("view") == "vote":
        state.ui["vote_scroll"] = min(max(state.ui.get("vote_scroll", 0.0), 0.0), display["max_scroll"])
    else:
        session["scroll"] = min(max(session["scroll"], 0.0), display["max_scroll"])
    draw.render(display)
    _displays[area.as_pointer()] = display


def _image_paths(session):
    paths = {item["image_path"] for item in session["items"] if item.get("image_path")}
    paths.update(row["image_path"] for item in session["items"] if item.get("kind") == "wave"
                 for row in item.get("rows", []) if row.get("image_path"))
    return paths


def _missing_paths(session):
    cached = _runtime.get("paths")
    return cached[3] if cached is not None and cached[0] == session["id"] else frozenset()


def _refresh_missing_paths(session, *, force=False):
    """Called outside draw on lifecycle ticks, conversation changes and file actions."""
    paths = _image_paths(session)
    for item in session["items"]:
        paths.update(ref["target"] for ref in item.get("file_references", [])[:layout.REFERENCE_LIMIT]
                     if ref["kind"] == "file")
        if item.get("audio"):  # A sound card's Play is disabled once its file is gone.
            paths.add(item["audio"]["path"])
    now = time.monotonic()
    cached = _runtime.get("paths")
    if not force and cached is not None and cached[:2] == (session["id"], paths) and now - cached[2] < PATH_TTL:
        return
    missing = set()
    for path in paths:
        try:
            if not Path(path).exists():
                missing.add(path)
        except (OSError, ValueError):
            missing.add(path)
    _runtime["paths"] = (session["id"], paths, now, frozenset(missing))


def _prune_images():
    # Also handles panels closed by joining areas or switching editor type, with no further draw.
    visible = any(is_host(area) for window in bpy.context.window_manager.windows for area in window.screen.areas)
    draw.retain_images(_image_paths(state.session()) if visible else ())


def _cloud_panel_state(session: dict) -> dict:
    """Defer catalog changes until a turn ends; interest responses can update in place."""
    from .. import cloud, cloud_account
    service = cloud_account.panel_data(cloud._runtime)
    current = cloud.coming_soon()
    message = service["message"] or cloud.ui_message()
    if not session["busy"] or "cloud_panel" not in _runtime:
        _runtime["cloud_panel"] = {"rows": current, "message": message}
    saved = _runtime["cloud_panel"]
    responses = {row["name"]: {key: row[key] for key in ("state", "voted", "level", "badge") if key in row}
                 for row in current}
    rows = [{**row, **responses.get(row["name"], {})} for row in saved["rows"]]
    if state.ui.get("view") == "vote" and not state.ui.get("vote_order") and any(row.get("level") for row in rows):
        # Rank once per visit, when the levels arrive: rows must not jump while someone is voting.
        state.ui["vote_order"] = [row["name"] for row in sorted(rows, key=lambda row: -(row.get("level") or 0))]
    if state.ui.get("view") == "vote":
        rows = _eased_levels(rows)
    return {"coming_soon": rows, "vote_notice": cloud.vote_notice(),
            "cloud_message": message if message == saved["message"] else "",
            "cloud_message_reserved": bool(saved["message"]), "credits": service["credits"],
            "credit_balance": service.get("credit_balance"),
            "today": service.get("today", "")}


VOTE_EASE_SECONDS = 0.5     # A bar's move to a new level.
VOTE_INTRO_STAGGER = 0.035  # On opening the view, each rank starts its grow this much after the one above.


def _eased_levels(rows: list[dict]) -> list[dict]:
    """Bars grow from nothing when the vote view opens (state.ui["vote_anim"] cleared), then ease to each
    new level the server sends, from wherever they are drawn."""
    now, anim = time.time(), state.ui.setdefault("vote_anim", {})
    order = {name: rank for rank, name in enumerate(state.ui.get("vote_order") or [])}
    eased = []
    for row in rows:
        target = row.get("level")
        if target is None:
            eased.append(row)
            continue
        start, end, began = anim.get(row["name"], (0.0, target, now + order.get(row["name"], 0) * VOTE_INTRO_STAGGER))
        if end != target:
            start, end, began = _ease(start, end, began, now), target, now
        anim[row["name"]] = (start, end, began)
        if now < began + VOTE_EASE_SECONDS:
            state.ui["animate_until"] = max(state.ui.get("animate_until", 0.0), began + VOTE_EASE_SECONDS)
        eased.append({**row, "level": _ease(start, end, began, now)})
    return eased


def _ease(start: float, end: float, began: float, now: float) -> float:
    t = min(1.0, max(0.0, (now - began) / VOTE_EASE_SECONDS))
    return start + (end - start) * (1 - (1 - t) ** 3)  # Ease out: quick, then settling.


def _draw_trampoline() -> None:
    # Global lookup on purpose: after a hot reload this resolves to the new draw_area.
    draw_area()


def dump_display(path: str) -> None:
    """Write the last drawn frame as JSON, for debugging layout without looking at pixels."""
    if not _displays:
        raise RuntimeError("No Loopcut panel has been drawn yet")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(next(iter(_displays.values())), handle, indent=1, ensure_ascii=False)


# ---------------------------------------------------------------- input editing

def _update_mentions(session: dict) -> None:
    """Refresh the completion list for the @name under the caret, if there is one."""
    prefix = scene_context.mention_prefix(session["input"], session["cursor"])
    rows = [] if prefix is None else scene_context.candidates_from(scene_context.all_names(), prefix)
    state.ui["mentions"] = rows
    session["mention"] = min(session.get("mention", 0), max(0, len(rows) - 1))


def _pick_mention(session: dict, index: int) -> None:
    rows = state.ui.get("mentions") or []
    if not 0 <= index < len(rows):
        return
    start = session["input"].rfind("@", 0, session["cursor"])
    textedit.replace_range(session, start, session["cursor"], scene_context.mention_text(rows[index][0]) + " ")
    state.ui["mentions"], session["mention"] = [], 0


def _submit(session: dict) -> None:
    if agent.send(session["input"]):
        from .. import onboarding
        onboarding.dismiss_hints()
        textedit.set_text(session, "")
    state.ui["mentions"] = []


def start_request(window, text: str, images: tuple = (), try_it: str = "") -> None:
    """Open the panel and send a starter request with its images (onboarding's first projects). Like a
    starter prompt, it waits in the input while no model is connected. try_it: the setup card it came from, whose
    cloud call agent.send makes free if the text goes out unedited (try_samples)."""
    def send(area):
        from .. import attachments
        session = state.session()
        if session["busy"]:
            return
        for problem in attachments.add(session, list(images)):
            print(f"Loopcut: {problem}")
        if try_it:
            session["try_it"] = (try_it, text)
        else:
            session.pop("try_it", None)
        textedit.set_text(session, text)
        session["focused"] = True
        _close_popups()
        if not _config_cached()[1]:
            _submit(session)
        tag_redraw_all()

    panel = find_panel(window)
    if panel is not None:
        send(panel)
    else:
        dock(window, then=send)


# ---------------------------------------------------------------- the + menu

PICKER_ROWS = 8


def _picker_names() -> list[tuple[str, str]]:
    """Everything the + menu can add: an image file, then what @ completes."""
    return [("Image file…", "image")] + scene_context.all_names()


def _open_picker() -> None:
    state.ui["picker"] = {"query": "", "rows": [], "active": 0}
    state.ui["mentions"], state.ui["model_menu"] = [], False
    _filter_picker()


def _filter_picker() -> None:
    picker = state.ui.get("picker")
    if picker is None:
        return
    names = _picker_names()
    query = picker["query"]
    picker["rows"] = scene_context.candidates_from(names, query, PICKER_ROWS) if query else names[:PICKER_ROWS]
    picker["active"] = min(picker.get("active", 0), max(0, len(picker["rows"]) - 1))


def _close_popups() -> None:
    state.ui["picker"], state.ui["model_menu"] = None, False
    state.ui["service_settings"] = False


def _pick_from_picker(session: dict, index: int) -> None:
    picker = state.ui.get("picker") or {}
    rows = picker.get("rows") or []
    if not 0 <= index < len(rows):
        return
    name, kind = rows[index]
    _close_popups()
    if kind == "image":
        bpy.ops.loopcut.attach_images("INVOKE_DEFAULT")
        return
    textedit.insert(session, scene_context.mention_text(name) + " ")
    _update_mentions(session)


def _picker_key(session: dict, event) -> bool:
    """Keys while the + menu is open: they search it. True when the key was used."""
    picker = state.ui["picker"]
    kind = event.type
    if kind == "ESC":
        _close_popups()
    elif kind in {"UP_ARROW", "DOWN_ARROW"}:
        if picker["rows"]:
            picker["active"] = (picker["active"] + (1 if kind == "DOWN_ARROW" else -1)) % len(picker["rows"])
    elif kind in {"RET", "NUMPAD_ENTER", "TAB"}:
        _pick_from_picker(session, picker["active"])
    elif kind == "BACK_SPACE":
        picker["query"] = picker["query"][:-1]
        _filter_picker()
    elif event.unicode and event.unicode.isprintable() and not (event.oskey or event.ctrl):
        picker["query"] += event.unicode
        _filter_picker()
    else:
        return False
    return True


def _open_settings() -> None:
    if settings.preferences() is None:
        state.session()["items"].append(state.item_error(
            "Loopcut is running from a checkout, so it has no preferences page. Set LOOPCUT_API_KEY, "
            "LOOPCUT_BASE_URL and LOOPCUT_MODEL in .env."))
        return
    bpy.ops.preferences.addon_show(module=settings.PACKAGE)


def _do_action(session: dict, action) -> None:
    kind, index = action
    if kind == "reference_url":
        account.open_browser(index)
    elif kind == "preview_open":
        path, title = index
        title = " ".join(title.split())[:120] or "Scene preview"
        state.ui.pop("action_notice", None)
        preview = {"path": path, "title": title, "conversation": session["id"]}
        if state.ui.get("preview") == preview:
            state.ui.pop("preview", None)
            return
        state.ui.pop("preview", None)
        _refresh_missing_paths(session, force=True)
        if not Path(path).is_file():
            state.ui["action_notice"] = {"text": f"{title}: this preview is no longer available.",
                                         "until": time.time() + 6.0}
            return
        state.ui["preview"] = preview
    elif kind in {"reference_open", "reference_folder"}:
        state.ui.pop("action_notice", None)
        error = ""
        try:
            target = Path(index)
            if not target.exists():
                error = f"Could not open {target.name or target}: the file is no longer at {target}."
            else:
                if kind == "reference_folder" and not target.is_dir():
                    target = target.parent
                if "FINISHED" not in bpy.ops.wm.path_open(filepath=str(target)):
                    error = f"Could not open {target.name or target}: Blender could not hand it to another app."
                else:
                    app = "your default app"
                    if kind == "reference_folder":
                        app = "Finder" if sys.platform == "darwin" else "your file manager"
                    # UI only: never saved with the conversation or sent to the model.
                    state.ui["action_notice"] = {
                        "text": f"Opened {target.name or target} in {app} (outside Blender).",
                        "until": time.time() + 6.0,
                    }
        except (OSError, ValueError, RuntimeError) as ex:
            error = f"Could not open {index}: {ex}"
        finally:
            _refresh_missing_paths(session, force=True)
        if error:
            session["items"].append(state.item_error(error))
            session["scroll"] = 0.0
    elif kind == "audio_play":
        from .. import playback
        error = playback.play(index)  # The card's file; it stops the sound playing, if any.
        _refresh_missing_paths(session, force=True)
        if error:
            session["items"].append(state.item_error(error))
            session["scroll"] = 0.0
    elif kind == "audio_stop":
        from .. import playback
        playback.stop()
    elif kind == "approve":
        agent.decide(True, index=index)  # That card: a worker's paid ask may wait beside the turn's own.
    elif kind == "approve_always":
        agent.decide(True, always=True, index=index)
    elif kind == "keep_changes":
        session["items"][index]["resolved"] = "kept"
        conversations.save(session)
    elif kind == "mention_pick":
        _pick_mention(session, index)
    elif kind == "picker_open":
        _close_popups() if state.ui.get("picker") is not None else _open_picker()
    elif kind == "picker_pick":
        _pick_from_picker(session, index)
    elif kind == "model_menu":
        state.ui["service_settings"] = False
        state.ui["model_menu"] = not state.ui.get("model_menu")
        state.ui["picker"] = None
    elif kind == "model_pick":
        _close_popups()
        settings.choose_model(index)
        config_changed()
    elif kind == "noop":
        pass
    elif kind == "votes_open":
        from .. import cloud
        _close_popups()
        state.ui.update({"view": "vote", "vote_order": None, "vote_scroll": 0.0, "vote_anim": {}})
        cloud.refresh_votes()
    elif kind == "vote":
        from .. import cloud
        cloud.vote(index)
    elif kind == "service_settings":
        state.ui["service_settings"] = not state.ui.get("service_settings")
        state.ui["model_menu"], state.ui["picker"] = False, None
    elif kind == "service_toggle":
        prefs = settings.preferences()
        if prefs is not None:
            setattr(prefs, index, not getattr(prefs, index))
    elif kind == "project_generation":
        from .. import cloud, cloud_account
        cloud_account.set_project_override(bpy.context.scene, index)
        cloud.sync_settings()
    elif kind == "cloud_upgrade" and config.billing_open():
        from .. import cloud_http
        url = session["items"][index]["cloud_error"]["upgrade"]
        cloud_http.validate_url(url)
        account.open_browser(url)
    elif kind == "open_web":
        from .. import cloud_http
        cloud_http.validate_url(index)
        account.open_browser(index)
    elif kind == "offer":
        _offer_action(session, *index)
    elif kind == "tips_toggle":
        from .. import nudges
        nudges.set_tips_off(not nudges.tips_off())
        _runtime.pop("connections", None)
    elif kind == "limit_chatgpt":
        prefs = settings.preferences()  # The user's own pick: their ChatGPT plan carries the turn on (M7).
        if prefs is not None and not session["busy"]:
            prefs.provider = "CHATGPT"
            state.clear_allowance_notice(invalidate=True)
            config_changed()
            if _local_limit_action(session, index) and state.can_resume(session):
                agent.resume()
    elif kind == "cloud_dismiss":
        from .. import cloud
        cloud.dismiss_ui_message(index)
    elif kind == "sign_in":
        if index == "chatgpt":
            bpy.ops.loopcut.chatgpt_sign_in()
        else:
            bpy.ops.loopcut.sign_in()
    elif kind == "sign_in_new":
        bpy.ops.loopcut.chatgpt_sign_in(new_account=True)
    elif kind == "sign_in_copy":
        if index == "chatgpt":
            bpy.ops.loopcut.chatgpt_copy_link()
        else:
            bpy.ops.loopcut.copy_sign_in_link()
    elif kind == "sign_in_cancel":
        if index == "chatgpt":
            bpy.ops.loopcut.chatgpt_cancel()
        else:
            bpy.ops.loopcut.cancel_sign_in()
    elif kind == "use_provider" and index in {"CHATGPT", "LOOPCUT"} and not session["busy"]:
        prefs = settings.preferences()  # The user's own pick, from the add-your-key prompt (layout._key_prompt).
        if prefs is not None:
            prefs.provider = index
            if index == "LOOPCUT":
                settings.set_tier(account.default_model())
            config_changed()
    elif kind == "own_key":
        _close_popups()
        from .. import chatgpt_ui
        account.cancel_sign_in()
        chatgpt_ui.cancel_sign_in()
        prefs = settings.preferences()
        if prefs is not None and prefs.provider in {"LOOPCUT", "CHATGPT"}:
            prefs.provider = "OPENAI"
        _open_settings()
    elif kind == "example_request":
        if not session["busy"] and 0 <= index < len(layout.EXAMPLE_REQUESTS):
            # One click sends a starter prompt; with no model connected yet it waits in the input instead.
            textedit.set_text(session, layout.EXAMPLE_REQUESTS[index])
            session["focused"] = True
            _close_popups()
            if not _config_cached()[1]:
                _submit(session)
    elif kind == "open_settings":
        state.ui["service_settings"] = False
        _close_popups()
        _open_settings()
    elif kind == "open_url":
        item = state.notice_for_display(session["items"][index])
        url = item["url"]
        if url:
            if "plan_upgrade" in item and config.billing_open():
                _start_upgrade(session, item)
            else:
                account.open_browser(url)
    elif kind == "guard_show":
        from .. import cost_guard
        item = session["items"][index]
        cost_guard.show_again(item["modifiers"])  # The user's call, even if it is slow to draw.
        if session.get("turn") is not None:  # A running turn's reads may say something new now.
            session["turn"].ledger.scene_changed()
        item["action_label"] = ""
        conversations.save(session)
    elif kind == "open_account":
        account.open_browser(config.account_url())
    elif kind == "limit_upgrade" and config.billing_open():
        item = session["items"][index] if isinstance(index, int) else state.ui.get("allowance_notice", {}).get("item")
        if item:
            _start_upgrade(session if _local_limit_action(session, index) else None, item)
    elif kind == "limit_fast":
        settings.set_tier(account.default_model())
        if _local_limit_action(session, index) and state.can_resume(session):
            agent.resume()
    elif kind == "limit_retry":
        if _local_limit_action(session, index) and state.can_resume(session):
            stopped = session["messages"][-1]
            def refreshed():
                if state.session() is session and state.can_resume(session) and session["messages"][-1] is stopped:
                    agent.resume()
            account.refresh_account(refreshed)
    elif kind == "resume":
        agent.resume()
    elif kind == "attach":
        bpy.ops.loopcut.attach_images("INVOKE_DEFAULT")
    elif kind == "remove_attachment":
        del session["attachments"][index]
    elif kind == "unpin_reference":
        pinned = [r for r in session["references"] if r.get("pinned", True)]
        if index < len(pinned):
            pinned[index]["pinned"] = False
            conversations.save(session)
    elif kind == "reject":
        agent.decide(False, index=index)
    elif kind == "stop_turn":
        agent.stop()
    elif kind == "send_queued_now":
        agent.send_queued_now()
    elif kind == "toggle":
        item = session["items"][index]
        item["expanded"] = not item.get("expanded", False)
    elif kind == "toggle_details":
        item = session["items"][index]
        item["details_expanded"] = not item.get("details_expanded", False)
    elif kind in {"restore_ask", "edit_ask"}:
        session["confirm_restore"], session["confirm_edit"] = index, kind == "edit_ask"
    elif kind == "restore_cancel":
        session["confirm_restore"], session["confirm_edit"] = None, False
    elif kind == "restore_confirm":
        _restore_later(session["items"][index]["checkpoint"], edit=bool(session.get("confirm_edit")))
    elif kind == "restore":
        _restore_later(index)
    elif kind == "history_open":
        state.ui["history"] = conversations.list_for(conversations.project_key(bpy.data.filepath))
        state.ui["view"] = "history"
    elif kind == "history_close":
        state.ui["view"] = "chat"
    elif kind == "jobs_open":
        from .. import job_tools
        job_tools.refresh_ui()
        state.ui["view"] = "jobs"
        state.ui["jobs_page"] = 0
    elif kind == "jobs_page":
        state.ui["jobs_page"] = index
    elif kind in {"job_cancel", "job_resume", "job_preview", "job_open"}:
        from .. import job_tools
        job_tools.ui_action(kind, index)
    elif kind == "cloud_job_cancel":
        from .. import cloud_jobs
        if not cloud_jobs.cancel_job(index):  # Say so rather than ignore the click (C47).
            state.ui["action_notice"] = {"text": "That job already finished, so it can't be canceled.",
                                         "until": time.time() + 6.0}
    elif kind == "worker_cancel":
        from .. import waves
        waves.cancel_worker(*index)  # (wave, worker): that worker stops and is not merged; the rest go on.
    elif kind == "accept_quarantine":
        _accept_quarantine(session, session["items"][index])
    elif kind == "open_conversation":
        _switch(session, lambda: conversations.load(index))
    elif kind == "new_chat":
        def fresh():
            new = state.new_session()
            conversations.add_project(new, bpy.data.filepath)
            return new
        _switch(session, fresh)
    elif kind == "close_panel":
        _close_area_later(bpy.context.window, bpy.context.area)
    elif kind == "update_download":
        update.download()
    elif kind == "update_restart":
        update.restart_to_update()
    elif kind == "update_page":
        update.open_notes()
    elif kind == "update_dismiss":
        update.dismiss()
    elif kind == "update_defer":
        update.defer_startup()


def _accept_quarantine(session: dict, item: dict) -> None:
    """Resolve held datablocks and their worker receipts on the main thread."""
    from .. import merge, waves
    try:
        text = waves.accept_quarantine(session, item["wave"])
    except (merge.MergeError, RuntimeError) as ex:
        session["items"].append(state.item_error(f"Could not accept the held-back items: {ex}"))
        return
    session["items"].append(state.item_notice(text))
    conversations.save(session)


def _offer_action(session: dict, index: int, choice: int) -> None:
    """A conversion card's button (nudges.py). Purchases open loopcut.org; nothing is bought in Blender."""
    from .. import cloud_http, nudges
    items = session["items"]
    if not 0 <= index < len(items) or items[index].get("kind") != "offer":
        return
    item = items[index]
    if not 0 <= choice < len(item["actions"]):
        return
    do, arg = item["actions"][choice]["do"], item["actions"][choice].get("arg", "")
    if do == "sign_in_loopcut":
        bpy.ops.loopcut.sign_in()
    elif do == "sign_in_chatgpt":
        bpy.ops.loopcut.chatgpt_sign_in()
    elif do == "web":
        cloud_http.validate_url(arg)
        account.open_browser(arg)
    elif do == "manage_chatgpt":
        bpy.ops.loopcut.chatgpt_manage_usage()
    elif do == "vendor_keys":
        _close_popups()
        _open_settings()
    elif do == "own_key":
        _do_action(session, ("own_key", None))
    elif do == "prompt" and not session["busy"]:
        textedit.set_text(session, arg)
        session["focused"] = True
    elif do == "continue_loopcut" and not session["busy"]:
        prefs = settings.preferences()
        if prefs is None:
            return
        prefs.provider = "LOOPCUT"  # The user's own pick of Loopcut's models; it stays until they switch back.
        settings.set_tier(account.default_model())
        config_changed()
        item["resolved"] = "Carrying on with Loopcut Fast. Switch back any time in Preferences."
        agent.resume()
    elif do == "dismiss":
        if item.get("nudge") in nudges.SOFT + ("locked_tool",):
            nudges.snooze(item["nudge"])
        item["resolved"] = nudges.DISMISSED


def _local_limit_action(session: dict, index) -> bool:
    return state.session() is session and (isinstance(index, int) or index == session["id"])


def _start_upgrade(session: dict | None, item: dict) -> None:
    """Watch the plan and catalogue; resume only the same stopped turn belonging to this card."""
    if not config.billing_open():
        return
    account.open_browser(item["url"] if "plan_upgrade" in item else item["upgrade"]["url"])
    item["status"] = "waiting"
    stopped = session["messages"][-1] if session is not None and state.can_resume(session) and item["kind"] == "limit" else None

    def upgraded(payload: dict) -> None:
        if item.get("status") != "waiting":
            return
        item["status"] = "done"
        item["text"] = f"You are on the {str(payload.get('plan') or '').capitalize()} plan now."
        if "plan_upgrade" in item:
            item["action_label"] = ""
        if session is not None:
            conversations.save(session)
        if (stopped is not None and state.session() is session and state.can_resume(session)
                and session["messages"][-1] is stopped):
            agent.resume()

    account.watch_plan(item.get("plan_upgrade", item.get("plan", "")), upgraded)


def _close_area_later(window, area) -> None:
    """Join the panel's area into its neighbour, from a timer: the click arrives inside a modal
    operator that runs in this very area, which must finish before the area is freed."""
    def close():
        if area is None or window is None:
            return None
        try:
            with bpy.context.temp_override(window=window, area=area):
                bpy.ops.screen.area_close()
        except RuntimeError as ex:  # The last area, or full screen: Blender says no.
            print(f"Loopcut: could not close the panel: {ex}")
        return None
    bpy.app.timers.register(close, first_interval=0.05)


def _switch(session: dict, make_session) -> None:
    """Show another conversation. The one on screen is stopped and saved, never dropped."""
    from .. import lifecycle
    try:
        replacement = make_session()
    except conversations.ConversationError as ex:
        session["items"].append(state.item_error(str(ex)))
        state.ui["view"] = "chat"
        return
    replacement["focused"] = session["focused"]
    session["focused"] = False
    lifecycle.switch_to(replacement)
    _runtime.pop("checkpoint_stamp", None)


def _find_area(context, pointer: int):
    for area in context.window.screen.areas:
        if area.as_pointer() == pointer:
            return area
    return None


def _window_region(area):
    return next(r for r in area.regions if r.type == "WINDOW")


def _local(region, event) -> tuple[int, int, bool]:
    x, y = event.mouse_x - region.x, event.mouse_y - region.y
    return x, region.height - y, 0 <= x < region.width and 0 <= y < region.height


def _show_in(area) -> None:
    area.ui_type = HOST_UI_TYPE
    if not NATIVE:  # The borrowed Text Editor's own regions would sit around the panel.
        space = area.spaces.active
        space.show_region_header = False
        space.show_region_footer = False
        space.show_region_ui = False
        hosts().add(area.as_pointer())
    area.tag_redraw()


_docking: set = set()  # Windows with a dock() in flight; the new area is not a host until finish().


def find_panel(window):
    return next((a for a in window.screen.areas if is_host(a)), None)


def has_panel(window) -> bool:
    """True when the window shows Loopcut or is about to: dock() finishes on a later tick, and
    two callers racing it (startup and the onboarding import did) must not both dock."""
    return window.as_pointer() in _docking or find_panel(window) is not None


def _leftovers(window) -> list:
    """Text Editors the panel borrowed, back from a saved file as plain empty editors: `hosts` holds
    pointers, which a reload does not keep, but the file keeps the regions `_show_in` hid. A user's
    own Text Editor shows a text or its header, footer or sidebar."""
    found = []
    for area in window.screen.areas:
        if area.type != "TEXT_EDITOR" or is_host(area):
            continue
        space = area.spaces.active
        if not (space.text or space.show_region_header or space.show_region_footer or space.show_region_ui):
            found.append(area)
    return found


def dock(window, then=None) -> None:
    """Put the panel on the right of the largest 3D viewport, where Cursor keeps its chat. The area
    it had when the file was saved is used again, so save, reopen and dock never adds one; else the
    viewport is split. The new area only has its geometry a tick later, so the panel is opened (and
    `then(area)` called) from a timer."""
    reuse = find_panel(window) or max(_leftovers(window), key=lambda a: a.width, default=None)
    views = [a for a in window.screen.areas if a.type == "VIEW_3D"]
    if reuse is None and not views:
        raise RuntimeError("Loopcut docks next to a 3D viewport, and this window has none.")
    before = {a.as_pointer() for a in window.screen.areas}
    key = window.as_pointer()
    _docking.add(key)
    if reuse is None:
        view = max(views, key=lambda a: a.width * a.height)
        region = next(r for r in view.regions if r.type == "WINDOW")
        try:
            with bpy.context.temp_override(window=window, area=view, region=region):
                bpy.ops.screen.area_split(direction="VERTICAL", factor=DOCK_FACTOR)
        except Exception:
            _docking.discard(key)
            raise

    def finish():
        try:
            panel = reuse
            if panel is None:
                fresh = [a for a in window.screen.areas if a.as_pointer() not in before]
                panel = max(fresh + [view], key=lambda a: a.x)
            _show_in(panel)
            # Files from builds that split on every reopen keep several. Left to right, each then borders
            # the viewport, which Blender joins it into: on a tie the older area wins, and they were split from it.
            for area in sorted(_leftovers(window), key=lambda a: a.x):
                try:
                    with bpy.context.temp_override(window=window, area=area):
                        bpy.ops.screen.area_close()
                except RuntimeError as ex:
                    print(f"Loopcut: could not close an empty editor: {ex}")
            if then:
                then(panel)
        finally:
            _docking.discard(key)
    bpy.app.timers.register(finish, first_interval=0.1)


class LOOPCUT_OT_open(bpy.types.Operator):
    """Show Loopcut in this area"""
    bl_idname = "loopcut.open"
    bl_label = "Open Loopcut Here"

    def execute(self, context):
        _show_in(context.area)
        return {"FINISHED"}


class LOOPCUT_OT_focus(bpy.types.Operator):
    """Start typing to Loopcut, opening its panel if this window has none"""
    bl_idname = "loopcut.focus"
    bl_label = "Ask Loopcut"

    def execute(self, context):
        window = context.window

        def focus(area):
            with bpy.context.temp_override(window=window, area=area, region=_window_region(area)):
                bpy.ops.loopcut.interact("INVOKE_DEFAULT", keyboard=True)

        panel = find_panel(window)
        if panel:
            focus(panel)
        else:
            try:
                dock(window, then=focus)
            except RuntimeError as ex:
                self.report({"WARNING"}, str(ex))
                return {"CANCELLED"}
        return {"FINISHED"}


class LOOPCUT_OT_scroll(bpy.types.Operator):
    bl_idname = "loopcut.scroll"
    bl_label = "Scroll Loopcut"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        return is_host(context.area)

    def invoke(self, context, event):
        scroll(state.session(), event)
        context.area.tag_redraw()
        return {"FINISHED"}


def scroll(session: dict, event) -> None:
    if state.ui.get("view") == "vote":  # Read from the top down, unlike the chat, which sits at its end.
        step = {"WHEELUPMOUSE": -SCROLL_STEP, "WHEELDOWNMOUSE": SCROLL_STEP}.get(event.type, 0.0)
        if event.type == "TRACKPADPAN":
            step = event.mouse_prev_y - event.mouse_y
        state.ui["vote_scroll"] = max(state.ui.get("vote_scroll", 0.0) + step, 0.0)
        return
    if event.type == "WHEELUPMOUSE":
        session["scroll"] += SCROLL_STEP
    elif event.type == "WHEELDOWNMOUSE":
        session["scroll"] -= SCROLL_STEP
    elif event.type == "TRACKPADPAN":
        session["scroll"] += event.mouse_y - event.mouse_prev_y
    session["scroll"] = max(session["scroll"], 0.0)  # Upper bound is clamped at draw time.


class LOOPCUT_OT_interact(bpy.types.Operator):
    """Click and type in the Loopcut panel"""
    bl_idname = "loopcut.interact"
    bl_label = "Loopcut Input"
    bl_options = {"INTERNAL"}

    keyboard: bpy.props.BoolProperty(options={"SKIP_SAVE", "HIDDEN"})  # Started by the shortcut, not a click.

    @classmethod
    def poll(cls, context):
        return is_host(context.area)

    def invoke(self, context, event):
        if not self.keyboard and event.shift:
            display = _displays.get(context.area.as_pointer())
            x, y, inside = _local(_window_region(context.area), event)
            action = layout.hit_test(display, x, y) if inside and display else None
            if not action or action[0] != "focus":
                return {"PASS_THROUGH"}
        session = state.session()
        self.area_pointer = context.area.as_pointer()
        self._selecting = False
        self._input_click = None
        if not self.keyboard:
            self._click(session, context.area, event)
        session["focused"] = True
        context.area.tag_redraw()
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def _click(self, session, area, event) -> bool:
        previous_click = self._input_click if not self._selecting else None
        self._selecting = False
        self._input_click = None
        display = _displays.get(area.as_pointer())
        x, y, inside = _local(_window_region(area), event)
        if inside and display:
            action = layout.hit_test(display, x, y)
            kind = action[0] if action else None
            if kind not in POPUP_ACTIONS and kind not in ("picker_open", "model_menu"):
                _close_popups()  # A click anywhere else puts a menu away.
            if kind == "focus":
                index = layout.input_index(display, x, y, draw.measure)
                if index is not None:
                    double_click = event.value == "DOUBLE_CLICK"
                    count, now = 2 if double_click else 1, time.monotonic()
                    if event.value == "PRESS" and not event.shift:
                        if previous_click:
                            # Blender downgrades DOUBLE_CLICK to PRESS for modal operators.
                            then, click_x, click_y, before = previous_click
                            prefs = bpy.context.preferences.inputs
                            if (now - then < prefs.mouse_double_click_time / 1000 and
                                    max(abs(x - click_x), abs(y - click_y)) <= prefs.drag_threshold_mouse):
                                count = before + 1
                    double_click = count >= 2
                    if count < 3:
                        self._input_click = (now, x, y, count)  # A third click in time selects the line.
                    if count >= 3:
                        textedit.select_line(session, index)
                    elif double_click:
                        textedit.select_word(session, index)
                    elif event.shift:
                        textedit.extend_selection(session, index)
                    else:
                        textedit.place_caret(session, index)
                    self._selecting = event.value == "PRESS" and not double_click
                    _update_mentions(session)
            elif action:
                _do_action(session, action)
        return inside

    def _finish(self, session, area):
        self._selecting = False
        self._input_click = None
        session["focused"] = False
        state.ui["mentions"] = []
        if area:
            area.tag_redraw()
        return {"FINISHED", "PASS_THROUGH"}

    def _key(self, context, session, event) -> bool:
        """One key press in the input box. False means: leave the panel."""
        kind, shift = event.type, event.shift
        preview = state.ui.get("preview")
        if kind == "ESC" and preview and preview.get("conversation") == session["id"]:
            state.ui.pop("preview", None)
            return True
        command = event.oskey or event.ctrl       # Cmd on macOS, Ctrl elsewhere; both accepted.
        by_word = event.alt or (event.ctrl and sys.platform != "darwin")
        if state.ui.get("picker") is not None and _picker_key(session, event):
            return True
        if (state.ui.get("model_menu") or state.ui.get("service_settings")) and kind == "ESC":
            _close_popups()
            return True
        mentions = state.ui.get("mentions") or []
        if mentions and kind in {"UP_ARROW", "DOWN_ARROW"}:
            session["mention"] = (session.get("mention", 0) + (1 if kind == "DOWN_ARROW" else -1)) % len(mentions)
            return True
        if mentions and kind in {"TAB", "RET", "NUMPAD_ENTER"}:
            _pick_mention(session, session.get("mention", 0))
            return True
        if kind == "ESC":
            if mentions:
                state.ui["mentions"] = []
            elif not agent.decide(False):
                if session["busy"]:
                    agent.stop()
                else:
                    return False
            return True
        if kind in {"RET", "NUMPAD_ENTER"}:
            if shift:
                textedit.insert(session, "\n")
            elif not (not session["input"].strip() and agent.decide(True)):
                _submit(session)
        elif kind in {"BACK_SPACE", "DEL"}:
            textedit.delete(session, backwards=kind == "BACK_SPACE", word=by_word)
        elif kind in {"LEFT_ARROW", "RIGHT_ARROW"}:
            side = "left" if kind == "LEFT_ARROW" else "right"
            target = f"line_{'home' if side == 'left' else 'end'}" if event.oskey else \
                (f"word_{side}" if by_word else side)
            textedit.move(session, target, select=shift)
        elif kind in {"UP_ARROW", "DOWN_ARROW"} and session["input"]:
            area = _find_area(context, self.area_pointer)
            lines, width = layout.input_rows(_displays.get(area.as_pointer()) if area else None, session["input"],
                                             draw.measure)
            if lines is not None:
                textedit.vertical(session, lines, width, down=kind == "DOWN_ARROW", select=shift)
        elif kind == "UP_ARROW" and not session["input"]:
            previous = next((i["text"] for i in reversed(session["items"]) if i["kind"] == "user"), "")
            textedit.set_text(session, previous)
        elif kind in {"HOME", "END"}:
            textedit.move(session, "line_home" if kind == "HOME" else "line_end", select=shift)
        elif command and kind == "A":
            textedit.select_all(session)
        elif command and kind == "C":
            if textedit.selected_text(session):
                context.window_manager.clipboard = textedit.selected_text(session)
        elif command and kind == "X":
            cut = textedit.cut(session)
            if cut:
                context.window_manager.clipboard = cut
        elif command and kind == "V":
            textedit.insert(session, context.window_manager.clipboard)
        elif command and kind == "Z":
            textedit.undo(session)
        elif event.unicode and event.unicode.isprintable() and not command:
            textedit.insert(session, event.unicode)
        else:
            return True  # Not ours, and nothing changed.
        if session["input"].strip() and not session.get("first_prompt_typed"):
            from .. import telemetry
            telemetry.first_use("prompt_typed")
            session["first_prompt_typed"] = True
        _update_mentions(session)
        return True

    def modal(self, context, event):
        session = state.session()
        area = _find_area(context, self.area_pointer)
        if area is None or not session["focused"]:
            return self._finish(session, area)

        kind = event.type
        if kind == "WINDOW_DEACTIVATE":
            self._selecting = False
            self._input_click = None
            return {"PASS_THROUGH"}
        elif kind in {"WHEELUPMOUSE", "WHEELDOWNMOUSE", "TRACKPADPAN"}:
            if not _local(_window_region(area), event)[2]:
                return {"PASS_THROUGH"}
            scroll(session, event)
        elif kind == "LEFTMOUSE":
            if event.value == "RELEASE":
                selecting = self._selecting
                self._selecting = False
                return {"RUNNING_MODAL"} if selecting else {"PASS_THROUGH"}
            if event.value not in {"PRESS", "DOUBLE_CLICK"}:
                return {"PASS_THROUGH"}
            if not self._click(session, area, event):
                return self._finish(session, area)
        elif kind in {"MOUSEMOVE", "INBETWEEN_MOUSEMOVE"}:
            # Recover a missed release without mistaking a modifier release for the mouse button.
            if (self._selecting and getattr(event, "type_prev", None) == "LEFTMOUSE" and
                    getattr(event, "value_prev", None) == "RELEASE"):
                self._selecting = False
                self._input_click = None
            if not self._selecting:
                return {"PASS_THROUGH"}
            x, y, _ = _local(_window_region(area), event)
            index = layout.input_index(_displays.get(area.as_pointer(), {}), x, y, draw.measure)
            if index is not None:
                if index != session["cursor"]:
                    self._input_click = None  # Dragging must not turn the next click into a double-click.
                textedit.extend_selection(session, index)
                _update_mentions(session)
        elif event.value != "PRESS" or kind in {"TIMER", "RIGHTMOUSE", "MIDDLEMOUSE"}:
            return {"PASS_THROUGH"}
        else:
            self._input_click = None
            if not self._key(context, session, event):
                return self._finish(session, area)
        area.tag_redraw()
        return {"RUNNING_MODAL"}


POPUP_ACTIONS = {"noop", "picker_pick", "model_pick", "mention_pick", "open_settings", "service_settings",
                 "service_toggle", "project_generation"}


class LOOPCUT_OT_hover(bpy.types.Operator):
    """Follows the mouse over the panel for hover highlights and tooltips"""
    bl_idname = "loopcut.hover"
    bl_label = "Loopcut Hover"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        # From the window keymap, so leaving the panel clears the highlight; cheap when neither applies.
        return is_host(context.area) or bool(state.ui.get("hover"))

    def invoke(self, context, event):
        area = context.area
        hovered = None
        if is_host(area):
            display = _displays.get(area.as_pointer())
            x, y, inside = _local(_window_region(area), event)
            hit = layout.hit_at(display, x, y) if inside and display else None
            hovered = hit["id"] if hit else None
            state.ui["mouse"] = (x, y)
        if state.ui.get("hover") != hovered:
            state.ui["hover"] = hovered
            tag_redraw_all()
        return {"PASS_THROUGH"}


class LOOPCUT_OT_attach_images(bpy.types.Operator):
    """Attach images or a video clip to your next message"""
    bl_idname = "loopcut.attach_images"
    bl_label = "Attach Images"
    bl_options = {"INTERNAL"}

    # What a file drop fills in (see LOOPCUT_FH_images) and what the file browser returns.
    directory: bpy.props.StringProperty(subtype="DIR_PATH", options={"SKIP_SAVE", "HIDDEN"})
    files: bpy.props.CollectionProperty(type=bpy.types.OperatorFileListElement, options={"SKIP_SAVE", "HIDDEN"})
    filter_image: bpy.props.BoolProperty(default=True, options={"HIDDEN"})
    filter_movie: bpy.props.BoolProperty(default=True, options={"HIDDEN"})
    filter_folder: bpy.props.BoolProperty(default=True, options={"HIDDEN"})

    def invoke(self, context, event):
        if self.files:  # Dropped.
            return self.execute(context)
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        from .. import attachments
        session = state.session()
        paths = [str(Path(self.directory) / entry.name) for entry in self.files if entry.name]
        clips = [p for p in paths if attachments.is_video(p)]
        paths = [p for p in paths if p not in clips]
        problems = attachments.add(session, paths)
        if clips:  # One clip at a time: each needs its part chosen.
            problems += [f"{Path(p).name}: attach one clip at a time." for p in clips[1:]]
            window, clip = context.window, clips[0]

            def trim():
                with bpy.context.temp_override(window=window):
                    bpy.ops.loopcut.attach_clip("INVOKE_DEFAULT", filepath=clip)
            bpy.app.timers.register(trim, first_interval=0.0)
            paths.append(clip)  # Not a failure: the dialog takes it from here.
        for problem in problems:
            session["items"].append(state.item_error(problem))
        if session["attachments"] and not session["focused"]:
            window = context.window  # Dropping a photo is followed by typing about it.

            def focus():
                with bpy.context.temp_override(window=window):
                    bpy.ops.loopcut.focus()
            bpy.app.timers.register(focus, first_interval=0.0)
        conversations.save(session)
        tag_redraw_all()
        return {"FINISHED"} if len(problems) < len(paths) else {"CANCELLED"}


_clip_previews: dict = {}  # bpy.utils.previews collection for the clip dialog's thumbnail.


def _clip_preview(path: str) -> int:
    """An icon id for the clip's thumbnail, or 0. Blender's own movie thumbnails; best effort."""
    try:
        from bpy.utils import previews  # Not `import bpy.utils.previews`: that makes bpy a local name.
        if "collection" not in _clip_previews:
            _clip_previews["collection"] = previews.new()
        collection = _clip_previews["collection"]
        preview = collection.get(path) or collection.load(path, path, "MOVIE")
        return preview.icon_id
    except (ImportError, KeyError, RuntimeError, AttributeError):
        return 0


class LOOPCUT_OT_attach_clip(bpy.types.Operator):
    """Choose the part of a video clip to attach (at most 15 s) and what to watch in it"""
    bl_idname = "loopcut.attach_clip"
    bl_label = "Attach Clip"
    bl_options = {"INTERNAL"}

    filepath: bpy.props.StringProperty(subtype="FILE_PATH", options={"SKIP_SAVE", "HIDDEN"})
    duration: bpy.props.FloatProperty(options={"SKIP_SAVE", "HIDDEN"})
    summary: bpy.props.StringProperty(options={"SKIP_SAVE", "HIDDEN"})
    start: bpy.props.FloatProperty(name="In", description="Seconds from the clip's start", min=0.0,
                                   precision=2, step=10, options={"SKIP_SAVE"})
    end: bpy.props.FloatProperty(name="Out", description="Seconds from the clip's start; at most 15 s after In",
                                 min=0.0, precision=2, step=10, options={"SKIP_SAVE"})
    watch: bpy.props.StringProperty(name="Watch", description="Optional: what to look at, in your words, "
                                    "e.g. \"the crossbow and my hands\"", options={"SKIP_SAVE"})
    solve_camera: bpy.props.BoolProperty(name="Solve camera motion", default=True, options={"SKIP_SAVE"},
                                         description="Track the footage to recover handheld camera motion; "
                                                     "dropped when the solve is poor")

    def invoke(self, context, event):
        from .. import video_attach
        from ..videoref import MAX_SECONDS, ClipError
        try:
            info = video_attach.probe(Path(self.filepath))
        except ClipError as ex:
            state.session()["items"].append(state.item_error(str(ex)))
            tag_redraw_all()
            return {"CANCELLED"}
        self.duration = info["frames"] / info["fps"]
        width, height = info["size"]
        self.summary = f"{width}x{height} · {info['fps']:.2f} fps · {self.duration:.1f} s"
        self.start, self.end = 0.0, min(self.duration, MAX_SECONDS)
        return context.window_manager.invoke_props_dialog(self, width=360, confirm_text="Attach")

    def draw(self, context):
        from ..videoref import MAX_SECONDS
        layout = self.layout
        layout.label(text=Path(self.filepath).name, icon="FILE_MOVIE")
        icon = _clip_preview(self.filepath)
        if icon:
            layout.template_icon(icon_value=icon, scale=6.0)
        layout.label(text=self.summary)
        row = layout.row(align=True)
        row.prop(self, "start")
        row.prop(self, "end")
        length = self.end - self.start
        layout.label(text=f"Uses {length:.2f} s of at most {MAX_SECONDS:g} s",
                     icon="ERROR" if length > MAX_SECONDS or length <= 0 else "TIME")
        layout.prop(self, "watch")
        layout.prop(self, "solve_camera")
        layout.label(text="The clip stays on this computer; the model sees still frames.", icon="LOCKED")

    def execute(self, context):
        from .. import video_attach
        from ..videoref import ClipError
        session = state.session()
        try:
            video_attach.start(session, Path(self.filepath), self.start, self.end, self.watch, self.solve_camera)
        except (ClipError, OSError) as ex:
            session["items"].append(state.item_error(f"{Path(self.filepath).name}: {ex}"))
            tag_redraw_all()
            return {"CANCELLED"}
        conversations.save(session)
        tag_redraw_all()
        return {"FINISHED"}


class LOOPCUT_FH_images(bpy.types.FileHandler):
    bl_idname = "LOOPCUT_FH_images"
    bl_label = "Attach to Loopcut"
    bl_import_operator = LOOPCUT_OT_attach_images.bl_idname
    bl_file_extensions = ".png;.jpg;.jpeg;.webp;.bmp;.tif;.tiff;.tga;.mp4;.mov"  # attachments.ACCEPTED

    @classmethod
    def poll_drop(cls, context):
        return context.area is not None and is_host(context.area)


_CLASSES = (LOOPCUT_OT_open, LOOPCUT_OT_focus, LOOPCUT_OT_scroll, LOOPCUT_OT_interact, LOOPCUT_OT_hover,
            LOOPCUT_OT_attach_images, LOOPCUT_OT_attach_clip,
            LOOPCUT_FH_images)


def register() -> None:
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    _runtime["handler"] = HOST_SPACE.draw_handler_add(_draw_trampoline, (), "WINDOW", "POST_PIXEL")
    keyconfig = bpy.context.window_manager.keyconfigs.addon
    if keyconfig:  # None in background mode.
        keymap = keyconfig.keymaps.new(name=HOST_KEYMAP[0], space_type=HOST_KEYMAP[1])
        items = [keymap.keymap_items.new(LOOPCUT_OT_interact.bl_idname, "LEFTMOUSE", "PRESS", shift=-1, head=True)]
        for wheel in ("WHEELUPMOUSE", "WHEELDOWNMOUSE", "TRACKPADPAN"):
            value = "ANY" if wheel == "TRACKPADPAN" else "PRESS"
            items.append(keymap.keymap_items.new(LOOPCUT_OT_scroll.bl_idname, wheel, value, head=True))
        # Cmd+L / Ctrl+Alt+L from anywhere: Cursor's shortcut, moved off Blender's Ctrl+L (Link Data).
        window_keymap = keyconfig.keymaps.new(name="Window", space_type="EMPTY")
        focus = window_keymap.keymap_items.new(LOOPCUT_OT_focus.bl_idname, "L", "PRESS", **(
            {"oskey": True} if COMMAND == "oskey" else {"ctrl": True, "alt": True}))
        hover = window_keymap.keymap_items.new(LOOPCUT_OT_hover.bl_idname, "MOUSEMOVE", "ANY")
        _runtime["keymap"] = [(keymap, items), (window_keymap, [focus, hover])]


def unregister() -> None:
    from .. import playback
    playback.stop()  # A card's sound does not outlive the panel's code.
    draw.retain_images(())
    if "collection" in _clip_previews:
        from bpy.utils import previews
        previews.remove(_clip_previews.pop("collection"))
    state.session()["focused"] = False
    if "keymap" in _runtime:
        for keymap, items in _runtime.pop("keymap"):
            for item in items:
                keymap.keymap_items.remove(item)
    if "handler" in _runtime:
        HOST_SPACE.draw_handler_remove(_runtime.pop("handler"), "WINDOW")
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
    _displays.clear()
