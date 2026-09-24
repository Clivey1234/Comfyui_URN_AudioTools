import threading
import time
import uuid

from aiohttp import web
from comfy_api.latest import io
from server import PromptServer

try:
    import comfy.model_management as model_management
except Exception:
    model_management = None


NODE_TAG = "[URN Text Edit]"
EVENT_NAME = "urn_audio_nodes.text_edit.request"
ACCEPT_ROUTE = "/urn_audio_nodes/text_edit/accept"
PENDING_ROUTE = "/urn_audio_nodes/text_edit/pending"

_waiters = {}
_waiters_lock = threading.Lock()


def _log(message: str):
    print(f"{NODE_TAG} {message}")


@PromptServer.instance.routes.post(ACCEPT_ROUTE)
async def urn_text_edit_accept(request):
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid JSON payload."}, status=400)

    token = str(payload.get("token") or "")
    node_id = str(payload.get("node_id") or "")
    if not token:
        return web.json_response({"ok": False, "error": "Missing edit token."}, status=400)

    with _waiters_lock:
        waiter = _waiters.get(token)
        if waiter is None:
            return web.json_response(
                {"ok": False, "error": "This edit request is no longer active."},
                status=404,
            )
        if node_id and node_id != waiter["node_id"]:
            return web.json_response({"ok": False, "error": "Node ID mismatch."}, status=409)

        waiter["text"] = str(payload.get("text") if payload.get("text") is not None else "")
        waiter["event"].set()

    return web.json_response({"ok": True})


@PromptServer.instance.routes.get(PENDING_ROUTE)
async def urn_text_edit_pending(request):
    # Recovery path for missed websocket events.  The browser polls this endpoint
    # while a workflow is active, so the editor can still attach to a waiting
    # execution even if the custom event was emitted before the listener saw it.
    with _waiters_lock:
        pending = [
            {
                "token": token,
                "node_id": str(waiter["node_id"]),
                "text": str(waiter["text"]),
            }
            for token, waiter in _waiters.items()
            if not waiter["event"].is_set()
        ]
    return web.json_response({"ok": True, "pending": pending})


class URNTextEdit(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="URNTextEdit",
            display_name="URN Text Edit",
            category="URN Audio Tools",
            description=(
                "Interactive STRING editor. When execution reaches this node, the workflow pauses, "
                "shows the incoming text in the node, and resumes only after Accept Changes is clicked."
            ),
            # This node is itself an execution root. Without this, ComfyUI may prune
            # the branch when the only downstream consumer does not request/cache-invalidates
            # the STRING input. That is why attaching a Show Any node made the editor work.
            # Marking it as an output node guarantees the interactive gate executes on
            # every queued workflow, independent of downstream lazy/cache behaviour.
            is_output_node=True,
            inputs=[
                io.String.Input(
                    "source",
                    display_name="source",
                    force_input=True,
                ),
            ],
            outputs=[
                io.String.Output(display_name="STRING"),
            ],
            # Interactive nodes must never be satisfied from ComfyUI's cache.
            # This guarantees the workflow stops here on every queued run.
            not_idempotent=True,
            hidden=[io.Hidden.unique_id],
        )

    @classmethod
    def fingerprint_inputs(cls, source, **kwargs):
        # NaN is ComfyUI's canonical "always changed" fingerprint.  Combined with
        # not_idempotent=True this prevents both ordinary result caching and
        # cross-node cache reuse for this interactive gate.
        return float("nan")

    @classmethod
    def execute(cls, source) -> io.NodeOutput:
        node_id = str(cls.hidden.unique_id)
        token = uuid.uuid4().hex
        event = threading.Event()
        waiter = {
            "event": event,
            "text": str(source if source is not None else ""),
            "node_id": node_id,
        }

        with _waiters_lock:
            _waiters[token] = waiter

        server = PromptServer.instance

        try:
            # Broadcast rather than targeting PromptServer.client_id.  On current
            # ComfyUI builds that field is not guaranteed to identify the browser
            # that queued this prompt, which can make the node look as if it was
            # skipped even though the backend is waiting.
            server.send_sync(
                EVENT_NAME,
                {
                    "node_id": node_id,
                    "token": token,
                    "text": waiter["text"],
                },
            )
            _log(f"Waiting for Accept Changes on node {node_id}.")

            # The node executes on ComfyUI's worker side, so waiting here pauses the
            # graph while the aiohttp/websocket server remains free to receive the
            # browser's Accept Changes request.
            while not event.wait(0.10):
                if model_management is not None:
                    model_management.throw_exception_if_processing_interrupted()

            result = waiter["text"]
            _log(f"Accepted edited text on node {node_id}; workflow continuing.")
            return io.NodeOutput(result)
        finally:
            with _waiters_lock:
                _waiters.pop(token, None)
