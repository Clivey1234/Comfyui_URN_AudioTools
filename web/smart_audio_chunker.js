import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_ID = "URNSmartAudioChunker";
const SUBTITLE_WIDGET_NAME = "urnSubtitlePreview";

function ensureAudioUI(nodeData) {
    if (!nodeData?.input?.required) return;

    const required = nodeData.input.required;
    if (!required.audio) return;

    // ComfyUI's AUDIOUPLOAD widget expects an AUDIO_UI widget to already
    // exist. Core only injects AUDIO_UI automatically for its own LoadAudio
    // family, so custom audio-upload nodes need to provide it themselves.
    const reordered = {};
    let inserted = false;

    for (const [name, spec] of Object.entries(required)) {
        if (name === "audioUI") continue;
        reordered[name] = spec;
        if (name === "audio") {
            reordered.audioUI = ["AUDIO_UI", {}];
            inserted = true;
        }
    }

    if (!inserted) {
        reordered.audioUI = ["AUDIO_UI", {}];
    }

    nodeData.input.required = reordered;
}

function parseSrtTime(value) {
    const match = String(value || "").trim().match(/^(\d+):(\d{2}):(\d{2})[,.](\d{3})$/);
    if (!match) return null;
    const hours = Number(match[1]);
    const minutes = Number(match[2]);
    const seconds = Number(match[3]);
    const millis = Number(match[4]);
    return hours * 3600 + minutes * 60 + seconds + millis / 1000;
}

function parseSrt(text) {
    const source = String(text || "").replace(/^\uFEFF/, "").trim();
    if (!source) return [];

    const cues = [];
    const blocks = source.split(/\r?\n\s*\r?\n/);
    for (const block of blocks) {
        const lines = block.split(/\r?\n/);
        const timingIndex = lines.findIndex((line) => line.includes("-->"));
        if (timingIndex < 0) continue;

        const timing = lines[timingIndex].match(
            /(\d+:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d+:\d{2}:\d{2}[,.]\d{3})/
        );
        if (!timing) continue;

        const start = parseSrtTime(timing[1]);
        const end = parseSrtTime(timing[2]);
        if (start == null || end == null || end <= start) continue;

        const cueText = lines.slice(timingIndex + 1).join("\n").trim();
        if (!cueText) continue;
        cues.push({ start, end, text: cueText });
    }

    cues.sort((a, b) => a.start - b.start || a.end - b.end);
    return cues;
}

function findCue(cues, time) {
    let lo = 0;
    let hi = cues.length - 1;
    while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        const cue = cues[mid];
        if (time < cue.start) {
            hi = mid - 1;
        } else if (time > cue.end) {
            lo = mid + 1;
        } else {
            return cue;
        }
    }
    return null;
}

function isOurNode(node) {
    return node?.comfyClass === NODE_ID || node?.constructor?.comfyClass === NODE_ID;
}

function getInputSlot(node, name) {
    return node?.inputs?.findIndex?.((input) => input?.name === name) ?? -1;
}

function getInputLink(node, name) {
    const slot = getInputSlot(node, name);
    if (slot < 0) return null;
    const linkId = node.inputs?.[slot]?.link;
    if (linkId == null) return null;
    return node.graph?.links?.[linkId] ?? node.graph?._links?.[linkId] ?? null;
}

function hasConnectedAudioInput(node) {
    return !!getInputLink(node, "audio_input");
}

function getUpstreamNode(node) {
    const link = getInputLink(node, "audio_input");
    if (!link) return null;
    const originId = link.origin_id ?? link.originId;
    if (originId == null) return null;
    return node.graph?.getNodeById?.(originId) ?? null;
}

function findAudioElement(node) {
    return node?.widgets?.find?.((w) => w?.name === "audioUI")?.element ?? null;
}

function inputFileUrl(value) {
    const raw = String(value || "").replace(/\\/g, "/");
    if (!raw) return "";
    const parts = raw.split("/");
    const filename = parts.pop() || "";
    const subfolder = parts.join("/");
    const params = new URLSearchParams({ filename, type: "input", subfolder });
    return api.apiURL(`/view?${params.toString()}`);
}

function setAudioPreview(node, url, sourceKind) {
    const audioWidget = node?.widgets?.find?.((w) => w?.name === "audioUI");
    const audio = audioWidget?.element;
    if (!audio) return;

    const next = String(url || "");
    if (next) {
        if (audio.src !== next) {
            audio.src = next;
            try { audio.load(); } catch (_) {}
        }
        audio.classList.remove("empty-audio-widget");
        audioWidget.value = next;
    } else {
        try { audio.pause(); } catch (_) {}
        audio.removeAttribute("src");
        try { audio.load(); } catch (_) {}
        audio.classList.add("empty-audio-widget");
        audioWidget.value = "";
    }
    node.__urnPreviewSourceKind = sourceKind || "";
}

function restorePickerPreview(node) {
    const picker = node?.widgets?.find?.((w) => w?.name === "audio");
    setAudioPreview(node, inputFileUrl(picker?.value), "picker");
}

function detachUpstreamPreviewObserver(node) {
    try { node.__urnUpstreamPreviewObserver?.disconnect?.(); } catch (_) {}
    node.__urnUpstreamPreviewObserver = null;
    if (node.__urnUpstreamAudioElement && node.__urnUpstreamAudioListener) {
        try {
            node.__urnUpstreamAudioElement.removeEventListener("loadstart", node.__urnUpstreamAudioListener);
            node.__urnUpstreamAudioElement.removeEventListener("loadedmetadata", node.__urnUpstreamAudioListener);
            node.__urnUpstreamAudioElement.removeEventListener("emptied", node.__urnUpstreamAudioListener);
        } catch (_) {}
    }
    node.__urnUpstreamAudioElement = null;
    node.__urnUpstreamAudioListener = null;
}

function syncConnectedAudioPreview(node) {
    if (!isOurNode(node)) return;
    detachUpstreamPreviewObserver(node);

    if (!hasConnectedAudioInput(node)) {
        restorePickerPreview(node);
        return;
    }

    const upstream = getUpstreamNode(node);
    const upstreamAudio = findAudioElement(upstream);
    const copyFromUpstream = () => {
        if (!hasConnectedAudioInput(node)) return;
        const src = upstreamAudio?.src || "";
        setAudioPreview(node, src, "connected");
    };

    // Loader/preview nodes already have a browser audio element. Mirror its URL
    // immediately so the Splitter player shows the same source before execution.
    if (upstreamAudio) {
        copyFromUpstream();
        const observer = new MutationObserver(copyFromUpstream);
        observer.observe(upstreamAudio, { attributes: true, attributeFilter: ["src"] });
        upstreamAudio.addEventListener("loadstart", copyFromUpstream);
        upstreamAudio.addEventListener("loadedmetadata", copyFromUpstream);
        upstreamAudio.addEventListener("emptied", copyFromUpstream);
        node.__urnUpstreamPreviewObserver = observer;
        node.__urnUpstreamAudioElement = upstreamAudio;
        node.__urnUpstreamAudioListener = copyFromUpstream;
        return;
    }

    // If the upstream AUDIO was generated and has no preview widget, do not show
    // the unrelated fallback picker audio. Execution will populate the player
    // from the connected source via the backend UI payload.
    setAudioPreview(node, "", "connected-awaiting-execution");
}


function migrateLegacyOutputFormat(node) {
    if (!isOurNode(node)) return;
    const widget = node?.widgets?.find?.((w) => w?.name === "output_format");
    if (!widget) return;
    const value = String(widget.value ?? "").trim().toUpperCase();
    if (value !== "FLAC" && value !== "MP3") {
        widget.value = "FLAC";
        try { widget.callback?.("FLAC", node, widget); } catch (_) {}
        node.graph?.setDirtyCanvas?.(true, true);
    }
}

function installSubtitlePreview(node) {
    if (!isOurNode(node)) return;
    if (node.widgets?.some((w) => w.name === SUBTITLE_WIDGET_NAME)) return;

    const wrapper = document.createElement("div");
    wrapper.style.boxSizing = "border-box";
    wrapper.style.width = "100%";
    wrapper.style.minHeight = "64px";
    wrapper.style.padding = "8px 10px";
    wrapper.style.border = "1px solid var(--border-color, #555)";
    wrapper.style.borderRadius = "6px";
    wrapper.style.background = "var(--comfy-input-bg, rgba(0,0,0,0.28))";
    wrapper.style.display = "flex";
    wrapper.style.alignItems = "center";
    wrapper.style.justifyContent = "center";
    wrapper.style.textAlign = "center";
    wrapper.style.overflow = "hidden";

    const textEl = document.createElement("div");
    textEl.textContent = "Run the node to generate synced subtitles.";
    textEl.style.width = "100%";
    textEl.style.whiteSpace = "pre-wrap";
    textEl.style.overflowWrap = "anywhere";
    textEl.style.fontSize = "14px";
    textEl.style.lineHeight = "1.35";
    textEl.style.opacity = "0.8";
    wrapper.appendChild(textEl);

    const subtitleWidget = node.addDOMWidget(
        SUBTITLE_WIDGET_NAME,
        SUBTITLE_WIDGET_NAME,
        wrapper,
        {
            serialize: false,
            getMinHeight: () => 64,
        }
    );
    subtitleWidget.serialize = false;
    subtitleWidget.options.serialize = false;

    // Keep the subtitle display immediately below ComfyUI's existing audio player.
    const widgets = node.widgets || [];
    const audioIndex = widgets.findIndex((w) => w.name === "audioUI");
    const subtitleIndex = widgets.findIndex((w) => w.name === SUBTITLE_WIDGET_NAME);
    if (audioIndex >= 0 && subtitleIndex >= 0 && subtitleIndex !== audioIndex + 1) {
        const [widget] = widgets.splice(subtitleIndex, 1);
        const refreshedAudioIndex = widgets.findIndex((w) => w.name === "audioUI");
        widgets.splice(refreshedAudioIndex + 1, 0, widget);
    }

    node.__urnSrtCues = [];
    node.__urnSubtitleTextEl = textEl;
    node.__urnLastSubtitle = null;

    const getAudio = () => node.widgets?.find((w) => w.name === "audioUI")?.element;

    const updateSubtitle = () => {
        const audio = getAudio();
        const cues = node.__urnSrtCues || [];
        if (!audio || !cues.length) {
            if (node.__urnLastSubtitle !== "__none__") {
                textEl.textContent = cues.length ? "" : "Run the node to generate synced subtitles.";
                textEl.style.opacity = cues.length ? "1" : "0.8";
                node.__urnLastSubtitle = "__none__";
            }
            return;
        }

        const cue = findCue(cues, Number(audio.currentTime || 0));
        const nextText = cue?.text || "";
        if (nextText !== node.__urnLastSubtitle) {
            textEl.textContent = nextText;
            textEl.style.opacity = nextText ? "1" : "0.45";
            node.__urnLastSubtitle = nextText;
        }
    };

    const audio = getAudio();
    if (audio) {
        audio.addEventListener("timeupdate", updateSubtitle);
        audio.addEventListener("seeking", updateSubtitle);
        audio.addEventListener("seeked", updateSubtitle);
        audio.addEventListener("play", updateSubtitle);
        audio.addEventListener("loadedmetadata", updateSubtitle);
    }

    const previousExecuted = node.onExecuted;
    node.onExecuted = function (message) {
        previousExecuted?.apply(this, arguments);
        const payload = message?.urn_srt;
        const srtText = Array.isArray(payload) ? payload[0] : payload;
        if (typeof srtText === "string") {
            this.__urnSrtCues = parseSrt(srtText);
            this.__urnLastSubtitle = null;
            if (this.__urnSrtCues.length) {
                textEl.textContent = "Subtitles ready — press play.";
                textEl.style.opacity = "0.8";
            } else {
                textEl.textContent = "No subtitle cues were generated.";
                textEl.style.opacity = "0.8";
            }
            updateSubtitle();
        }
    };

    const previousWidgetChanged = node.onWidgetChanged;
    node.onWidgetChanged = function (name, value, oldValue, widget) {
        previousWidgetChanged?.apply(this, arguments);
        if (name === "audio" && value !== oldValue) {
            this.__urnSrtCues = [];
            this.__urnLastSubtitle = null;
            textEl.textContent = "Audio changed — run the node to refresh subtitles.";
            textEl.style.opacity = "0.8";
        }
    };

    const previousConnectionsChange = node.onConnectionsChange;
    node.onConnectionsChange = function () {
        previousConnectionsChange?.apply(this, arguments);
        // Let LiteGraph finish updating link state before resolving the source.
        queueMicrotask(() => syncConnectedAudioPreview(this));
    };

    const previousGraphConfigured = node.onGraphConfigured;
    node.onGraphConfigured = function () {
        previousGraphConfigured?.apply(this, arguments);
        queueMicrotask(() => {
            migrateLegacyOutputFormat(this);
            syncConnectedAudioPreview(this);
        });
    };

    // Core AUDIOUPLOAD continues updating the local player when the fallback
    // picker changes. Re-assert the connected source afterward so the picker can
    // never visually override an active audio_input connection.
    const pickerWidget = node.widgets?.find((w) => w.name === "audio");
    if (pickerWidget) {
        const previousPickerCallback = pickerWidget.callback;
        pickerWidget.callback = function () {
            const result = previousPickerCallback?.apply(this, arguments);
            queueMicrotask(() => syncConnectedAudioPreview(node));
            return result;
        };
    }

    queueMicrotask(() => syncConnectedAudioPreview(node));
    node.graph?.setDirtyCanvas(true, true);
}

app.registerExtension({
    name: "URN.SmartAudioChunker.AudioUploadCompatAndSRTPreview",

    addCustomNodeDefs(defs) {
        const nodeData = defs?.[NODE_ID];
        if (nodeData) ensureAudioUI(nodeData);
    },

    beforeRegisterNodeDef(_nodeType, nodeData) {
        if (nodeData?.name === NODE_ID) {
            ensureAudioUI(nodeData);
        }
    },

    nodeCreated(node) {
        installSubtitlePreview(node);
        queueMicrotask(() => migrateLegacyOutputFormat(node));
    },
});
