import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const ACCEPT = ".wav,.mp3,.flac,.ogg,.m4a,.aac,.opus,.wma,.mp4,.mov,.mkv,.webm,audio/*,video/mp4,video/webm";

async function uploadToInput(file) {
    const body = new FormData();
    // ComfyUI's standard input upload endpoint uses the multipart field
    // name "image" for generic uploaded input media, including audio/video.
    body.append("image", file);
    body.append("type", "input");

    const resp = await api.fetchApi("/upload/image", {
        method: "POST",
        body,
    });

    if (!resp.ok) {
        throw new Error(`Upload failed: ${resp.status} ${resp.statusText}`);
    }

    const data = await resp.json();
    return data.subfolder ? `${data.subfolder}/${data.name}` : data.name;
}

function installLoopStatus(node) {
    if ((node.widgets || []).some((w) => w?.name === "urnLoopStatus")) return;

    const wrapper = document.createElement("div");
    wrapper.style.boxSizing = "border-box";
    wrapper.style.width = "100%";
    wrapper.style.minHeight = "48px";
    wrapper.style.padding = "7px 10px";
    wrapper.style.border = "1px solid var(--border-color, #555)";
    wrapper.style.borderRadius = "6px";
    wrapper.style.background = "var(--comfy-input-bg, rgba(0,0,0,0.28))";
    wrapper.style.display = "flex";
    wrapper.style.alignItems = "center";
    wrapper.style.justifyContent = "center";
    wrapper.style.textAlign = "center";

    const textEl = document.createElement("div");
    textEl.textContent = "Run the node to find a loop.";
    textEl.style.width = "100%";
    textEl.style.whiteSpace = "pre-wrap";
    textEl.style.overflowWrap = "anywhere";
    textEl.style.fontSize = "13px";
    textEl.style.lineHeight = "1.35";
    textEl.style.opacity = "0.8";
    wrapper.appendChild(textEl);

    const widget = node.addDOMWidget("urnLoopStatus", "urnLoopStatus", wrapper, {
        serialize: false,
        getMinHeight: () => 48,
    });
    widget.serialize = false;
    if (widget.options) widget.options.serialize = false;
    node.__urnLoopStatusTextEl = textEl;

    const previousExecuted = node.onExecuted;
    node.onExecuted = function (message) {
        previousExecuted?.apply(this, arguments);
        const payload = message?.urn_loop_status;
        const status = Array.isArray(payload) ? payload[0] : payload;
        if (typeof status === "string" && status.trim()) {
            textEl.textContent = status;
            textEl.style.opacity = "1";
        }
    };

    const previousWidgetChanged = node.onWidgetChanged;
    node.onWidgetChanged = function (name, value, oldValue, changedWidget) {
        previousWidgetChanged?.apply(this, arguments);
        if ((name === "minimum_loop_seconds" || name === "extension_mode") && value !== oldValue) {
            textEl.textContent = "Loop settings changed — run the node to find a loop.";
            textEl.style.opacity = "0.8";
        }
    };
}

app.registerExtension({
    name: "URN.SmartSeamlessAudioLoop.SingleAudioPickerV13",

    async nodeCreated(node) {
        if (node.comfyClass !== "URNSmartSeamlessAudioLoop") return;

        installLoopStatus(node);

        const audioWidget = (node.widgets || []).find((w) => w?.name === "audio_file");
        if (!audioWidget) return;

        // Guard against duplicate registration after frontend hot reloads.
        if ((node.widgets || []).some((w) => w?.name === "Choose Audio File")) return;

        node.addWidget("button", "Choose Audio File", null, async () => {
            const input = document.createElement("input");
            input.type = "file";
            input.accept = ACCEPT;
            input.multiple = false;
            input.style.display = "none";
            document.body.appendChild(input);

            const cleanup = () => {
                try { input.remove(); } catch (_) {}
            };

            input.addEventListener("change", async () => {
                try {
                    const file = input.files?.[0];
                    if (!file) return;

                    const uploaded = await uploadToInput(file);

                    const values = audioWidget.options?.values;
                    if (Array.isArray(values) && !values.includes(uploaded)) {
                        values.push(uploaded);
                    }

                    audioWidget.value = uploaded;
                    audioWidget.callback?.(uploaded);
                    node.setDirtyCanvas?.(true, true);
                    app.graph?.setDirtyCanvas?.(true, true);
                } catch (err) {
                    console.error("URN Smart Seamless Audio Loop upload failed", err);
                    alert(`Audio upload failed: ${err?.message || err}`);
                } finally {
                    cleanup();
                }
            }, { once: true });

            input.addEventListener("cancel", cleanup, { once: true });
            input.click();
        }, { serialize: false });
    },
});
