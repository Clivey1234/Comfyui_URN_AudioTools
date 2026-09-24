import json
import os
import re
import threading
import uuid

from aiohttp import web
from comfy_api.latest import io
from server import PromptServer

try:
    import comfy.model_management as model_management
except Exception:
    model_management = None


NODE_TAG = "[URN Audio Style Selector]"
EVENT_NAME = "urn_audio_nodes.style_selector.request"
ACCEPT_ROUTE = "/urn_audio_nodes/style_selector/accept"
PENDING_ROUTE = "/urn_audio_nodes/style_selector/pending"
STYLES_ROUTE = "/urn_audio_nodes/style_selector/styles"
ADD_CUSTOM_ROUTE = "/urn_audio_nodes/style_selector/add_custom"
STYLES_FILE = os.path.join(os.path.dirname(__file__), "urn_audio_style_selector_styles.json")

_waiters = {}
_waiters_lock = threading.Lock()
_styles_lock = threading.Lock()


def _log(message: str):
    print(f"{NODE_TAG} {message}")


def _load_styles():
    """Read the editable preset file fresh each time so users can alter it without rebuilding the node."""
    try:
        with open(STYLES_FILE, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except Exception as exc:
        _log(f"Could not read {os.path.basename(STYLES_FILE)}: {exc}")
        return {}

    if not isinstance(raw, dict):
        _log("Style JSON root must be an object of tab-name -> list of tags.")
        return {}

    cleaned = {}
    for tab, values in raw.items():
        tab_name = str(tab).strip()
        if not tab_name or not isinstance(values, list):
            continue
        seen = set()
        tags = []
        for value in values:
            tag = str(value).strip()
            key = tag.casefold()
            if not tag or key in seen:
                continue
            seen.add(key)
            tags.append(tag)
        cleaned[tab_name] = tags
    return cleaned




def _write_styles(raw):
    """Persist the editable style catalog, preserving top-level tab order."""
    tmp_path = STYLES_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(raw, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp_path, STYLES_FILE)


def _add_user_custom_style(value):
    tag = str(value if value is not None else "").strip()
    if not tag:
        raise ValueError("Custom style cannot be empty.")
    if len(tag) > 120:
        raise ValueError("Custom style is too long (maximum 120 characters).")
    if "\n" in tag or "\r" in tag:
        raise ValueError("Custom style must be a single line.")

    with _styles_lock:
        try:
            with open(STYLES_FILE, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except Exception as exc:
            raise ValueError(f"Could not read {os.path.basename(STYLES_FILE)}: {exc}") from exc

        if not isinstance(raw, dict):
            raise ValueError("Style JSON root must be an object of tab-name -> list of tags.")

        custom = raw.get("User Custom")
        if not isinstance(custom, list):
            custom = []
            raw["User Custom"] = custom

        wanted = tag.casefold()
        existing = None
        for item in custom:
            clean = str(item).strip()
            if clean.casefold() == wanted:
                existing = clean
                break

        added = existing is None
        if added:
            custom.append(tag)
            _write_styles(raw)
            saved_tag = tag
        else:
            saved_tag = existing

    return saved_tag, added


def _parse_generated_styles(value):
    text = str(value if value is not None else "").strip()
    if not text:
        return []

    # Music_Description and YuE-style prompts are normally comma-separated.
    # Also accept semicolons/newlines to make the selector useful with hand-built strings.
    parts = re.split(r"\s*(?:,|;|\r?\n)\s*", text)
    result = []
    seen = set()
    for part in parts:
        tag = part.strip()
        key = tag.casefold()
        if not tag or key in seen:
            continue
        seen.add(key)
        result.append(tag)
    return result


def _normalise_selected(values):
    if not isinstance(values, list):
        values = []
    result = []
    seen = set()
    for value in values:
        tag = str(value).strip()
        key = tag.casefold()
        if not tag or key in seen:
            continue
        seen.add(key)
        result.append(tag)
    return result


@PromptServer.instance.routes.post(ACCEPT_ROUTE)
async def urn_audio_style_selector_accept(request):
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid JSON payload."}, status=400)

    token = str(payload.get("token") or "")
    node_id = str(payload.get("node_id") or "")
    if not token:
        return web.json_response({"ok": False, "error": "Missing selector token."}, status=400)

    with _waiters_lock:
        waiter = _waiters.get(token)
        if waiter is None:
            return web.json_response(
                {"ok": False, "error": "This style-selection request is no longer active."},
                status=404,
            )
        if node_id and node_id != waiter["node_id"]:
            return web.json_response({"ok": False, "error": "Node ID mismatch."}, status=409)

        selected = _normalise_selected(payload.get("selected"))
        waiter["selected"] = selected
        waiter["event"].set()

    return web.json_response({"ok": True, "Final_Styles": ", ".join(selected)})




@PromptServer.instance.routes.post(ADD_CUSTOM_ROUTE)
async def urn_audio_style_selector_add_custom(request):
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid JSON payload."}, status=400)

    try:
        tag, added = _add_user_custom_style(payload.get("tag"))
    except ValueError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    except Exception as exc:
        _log(f"Could not save User Custom style: {exc}")
        return web.json_response({"ok": False, "error": "Could not save custom style."}, status=500)

    _log(f"{'Added' if added else 'Reused'} User Custom style: {tag}")
    return web.json_response({
        "ok": True,
        "tag": tag,
        "added": added,
        "styles": _load_styles(),
    })


@PromptServer.instance.routes.get(PENDING_ROUTE)
async def urn_audio_style_selector_pending(request):
    with _waiters_lock:
        pending = [
            {
                "token": token,
                "node_id": str(waiter["node_id"]),
                "generated": list(waiter["generated"]),
                "styles": waiter["styles"],
            }
            for token, waiter in _waiters.items()
            if not waiter["event"].is_set()
        ]
    return web.json_response({"ok": True, "pending": pending})


@PromptServer.instance.routes.get(STYLES_ROUTE)
async def urn_audio_style_selector_styles(request):
    return web.json_response({"ok": True, "styles": _load_styles()})


class URNAudioStyleSelector(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="URNAudioStyleSelector",
            display_name="URN Audio Style Selector",
            category="URN Audio Tools",
            description=(
                "Interactive visual style/tag selector. Incoming Generated_Styles are shown in grey. "
                "Choose extra predefined tags from tabbed lists; user additions are shown in purple. "
                "Click any selected tag to remove it. Add Quick Custom can persist new tags into the "
                "User Custom JSON tab, then Accept Changes outputs Final_Styles."
            ),
            is_output_node=True,
            inputs=[
                io.String.Input(
                    "Generated_Styles",
                    display_name="Generated_Styles",
                    force_input=True,
                ),
            ],
            outputs=[
                io.String.Output(display_name="Final_Styles"),
            ],
            not_idempotent=True,
            hidden=[io.Hidden.unique_id],
        )

    @classmethod
    def fingerprint_inputs(cls, Generated_Styles, **kwargs):
        # Always execute: this is an explicit human review gate.
        return float("nan")

    @classmethod
    def execute(cls, Generated_Styles) -> io.NodeOutput:
        node_id = str(cls.hidden.unique_id)
        token = uuid.uuid4().hex
        event = threading.Event()
        generated = _parse_generated_styles(Generated_Styles)
        styles = _load_styles()
        waiter = {
            "event": event,
            "node_id": node_id,
            "generated": generated,
            "selected": list(generated),
            "styles": styles,
        }

        with _waiters_lock:
            _waiters[token] = waiter

        try:
            PromptServer.instance.send_sync(
                EVENT_NAME,
                {
                    "node_id": node_id,
                    "token": token,
                    "generated": generated,
                    "styles": styles,
                },
            )
            _log(f"Waiting for Accept Changes on node {node_id}.")

            while not event.wait(0.10):
                if model_management is not None:
                    model_management.throw_exception_if_processing_interrupted()

            final_styles = ", ".join(waiter["selected"])
            _log(f"Accepted {len(waiter['selected'])} style tag(s) on node {node_id}.")
            return io.NodeOutput(final_styles)
        finally:
            with _waiters_lock:
                _waiters.pop(token, None)
