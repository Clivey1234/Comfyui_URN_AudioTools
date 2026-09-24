import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_ID = "URNAudioLyricsOVH";
const LOOKUP_WIDGET = "urnLyricsOvhLookupUI";
const TEXT_WIDGET = "urnLyricsOvhTextPreview";
const EVENT_NAME = "urn_audio_nodes.audio_lyrics_ovh.lookup_request";
const RESPONSE_ROUTE = "/urn_audio_nodes/audio_lyrics_ovh/lookup_response";
const PENDING_ROUTE = "/urn_audio_nodes/audio_lyrics_ovh/lookup_pending";

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

function filenameFromAudioUrl(value) {
    const raw = String(value || "");
    if (!raw) return "";
    try {
        const url = new URL(raw, globalThis.location?.href || "http://localhost/");
        return url.searchParams.get("filename") || "";
    } catch (_) {
        const match = raw.match(/[?&]filename=([^&]+)/i);
        if (!match) return "";
        try { return decodeURIComponent(match[1]); } catch (_) { return match[1]; }
    }
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

function setAudioPreview(node, url) {
    const widget = node?.widgets?.find?.((w) => w?.name === "audioUI");
    const audio = widget?.element;
    if (!audio) return;
    const next = String(url || "");
    if (next) {
        if (audio.src !== next) {
            audio.src = next;
            try { audio.load(); } catch (_) {}
        }
        audio.classList.remove("empty-audio-widget");
        widget.value = next;
    } else {
        try { audio.pause(); } catch (_) {}
        audio.removeAttribute("src");
        try { audio.load(); } catch (_) {}
        audio.classList.add("empty-audio-widget");
        widget.value = "";
    }
}

function getFilenameHint(node) {
    if (hasConnectedAudioInput(node)) {
        const upstream = getUpstreamNode(node);
        if (upstream) {
            for (const name of ["audio", "filename", "file"]) {
                const widget = upstream.widgets?.find?.((w) => w?.name === name);
                const value = String(widget?.value || "").trim();
                if (value) return value;
            }
            const fromUrl = filenameFromAudioUrl(findAudioElement(upstream)?.src);
            if (fromUrl) return fromUrl;
        }
    }
    return String(node?.widgets?.find?.((w) => w?.name === "audio")?.value || "").trim();
}

function syncFilenameHint(node) {
    const widget = node?.widgets?.find?.((w) => w?.name === "source_filename_hint");
    if (!widget) return;
    const next = getFilenameHint(node);
    if (next && widget.value !== next) widget.value = next;
}

function hideFilenameHint(node) {
    const widget = node?.widgets?.find?.((w) => w?.name === "source_filename_hint");
    if (!widget || widget.__urnHidden) return;
    widget.__urnHidden = true;
    widget.computeSize = () => [0, -4];
    widget.serializeValue = () => widget.value ?? "";
    widget.draw = () => {};
}

function detachUpstreamObserver(node) {
    try { node.__urnOvhObserver?.disconnect?.(); } catch (_) {}
    node.__urnOvhObserver = null;
    if (node.__urnOvhUpstreamAudio && node.__urnOvhAudioListener) {
        for (const evt of ["loadstart", "loadedmetadata", "emptied"]) {
            try { node.__urnOvhUpstreamAudio.removeEventListener(evt, node.__urnOvhAudioListener); } catch (_) {}
        }
    }
    node.__urnOvhUpstreamAudio = null;
    node.__urnOvhAudioListener = null;
}

function syncAudioPreview(node) {
    if (!isOurNode(node)) return;
    detachUpstreamObserver(node);
    syncFilenameHint(node);

    if (!hasConnectedAudioInput(node)) {
        const picker = node?.widgets?.find?.((w) => w?.name === "audio");
        setAudioPreview(node, inputFileUrl(picker?.value));
        return;
    }

    const upstream = getUpstreamNode(node);
    const upstreamAudio = findAudioElement(upstream);
    const copy = () => {
        if (!hasConnectedAudioInput(node)) return;
        setAudioPreview(node, upstreamAudio?.src || "");
        syncFilenameHint(node);
    };
    if (upstreamAudio) {
        copy();
        const observer = new MutationObserver(copy);
        observer.observe(upstreamAudio, { attributes: true, attributeFilter: ["src"] });
        for (const evt of ["loadstart", "loadedmetadata", "emptied"]) upstreamAudio.addEventListener(evt, copy);
        node.__urnOvhObserver = observer;
        node.__urnOvhUpstreamAudio = upstreamAudio;
        node.__urnOvhAudioListener = copy;
    } else {
        setAudioPreview(node, "");
    }
}

function stopGraphEvent(e) { e.stopPropagation(); }

function makeLookupUI(node) {
    const root = document.createElement("div");
    Object.assign(root.style, {
        boxSizing: "border-box", width: "100%", display: "none", flexDirection: "column", gap: "7px",
        padding: "9px", border: "1px solid rgba(255,255,255,.18)", borderRadius: "7px",
        background: "rgba(12,12,13,.60)", userSelect: "none",
    });

    const status = document.createElement("div");
    Object.assign(status.style, { fontSize: "12px", lineHeight: "16px", color: "rgba(255,255,255,.82)" });

    const select = document.createElement("select");
    Object.assign(select.style, {
        boxSizing: "border-box", width: "100%", height: "31px", border: "1px solid rgba(255,255,255,.22)",
        borderRadius: "6px", background: "#202022", color: "#eee", padding: "0 8px", fontSize: "12px",
    });

    const row = document.createElement("div");
    Object.assign(row.style, { display: "flex", gap: "7px", width: "100%" });

    const accept = document.createElement("button");
    accept.type = "button";
    accept.textContent = "Accept";
    const reject = document.createElement("button");
    reject.type = "button";
    reject.textContent = "Reject / Stop";

    for (const button of [accept, reject]) {
        Object.assign(button.style, {
            flex: "1 1 0", height: "31px", border: "1px solid rgba(255,255,255,.20)", borderRadius: "7px",
            color: "#fff", background: "rgba(100,100,100,.58)", fontSize: "12px", cursor: "pointer",
        });
    }
    accept.style.borderColor = "rgba(255,160,36,.80)";
    accept.style.background = "rgba(120,75,20,.62)";

    row.append(accept, reject);
    root.append(status, select, row);
    for (const el of [root, select, accept, reject]) {
        for (const evt of ["pointerdown", "mousedown", "mouseup", "dblclick", "wheel"]) el.addEventListener(evt, stopGraphEvent);
    }
    select.addEventListener("keydown", stopGraphEvent);
    node.__urnOvhLookupUI = { root, status, select, accept, reject };
    return root;
}

function makeTextPreview(node) {
    const root = document.createElement("div");
    Object.assign(root.style, {
        boxSizing: "border-box", width: "100%", minHeight: "72px", maxHeight: "180px", padding: "8px 10px",
        border: "1px solid var(--border-color, #555)", borderRadius: "6px",
        background: "var(--comfy-input-bg, rgba(0,0,0,.28))", overflowY: "auto", overflowX: "hidden",
    });
    const text = document.createElement("div");
    text.textContent = "Run the node to search Lyrics.ovh.";
    Object.assign(text.style, { whiteSpace: "pre-wrap", overflowWrap: "anywhere", fontSize: "13px", lineHeight: "1.35", opacity: ".8" });
    root.append(text);
    node.__urnOvhTextUI = { root, text };
    return root;
}

function setLookupVisible(node, visible) {
    const ui = node?.__urnOvhLookupUI;
    if (!ui) return;
    node.__urnOvhLookupActive = !!visible;
    ui.root.style.display = visible ? "flex" : "none";
    if (visible) {
        const w = Math.max(Number(node.size?.[0]) || 0, 380);
        const h = Math.max(Number(node.size?.[1]) || 0, 360);
        node.setSize?.([w, h]);
    }
    node.graph?.setDirtyCanvas?.(true, true);
}

function populateLookup(node, detail) {
    const ui = node?.__urnOvhLookupUI;
    if (!ui) return;
    node.__urnOvhToken = String(detail?.token || "");
    const options = Array.isArray(detail?.options) ? detail.options : [];
    ui.select.replaceChildren();
    for (let i = 0; i < options.length; i++) {
        const item = options[i] || {};
        const option = document.createElement("option");
        option.value = String(i);
        option.textContent = String(item.title || "Untitled");
        if (item.album) option.title = `${item.title} — ${item.album}`;
        ui.select.appendChild(option);
    }
    if (!options.length) {
        const option = document.createElement("option");
        option.value = "0";
        option.textContent = "No songs found for this artist";
        ui.select.appendChild(option);
    }
    const selectedIndex = Math.max(0, Math.min(Number(detail?.selected_index) || 0, Math.max(0, options.length - 1)));
    ui.select.value = String(selectedIndex);
    ui.status.textContent = options.length
        ? `${detail?.artist || "Artist"} — select a song title (${options.length} found)`
        : `${detail?.artist || "Artist"} — no song titles returned`;
    ui.accept.disabled = options.length === 0;
    ui.reject.disabled = false;
    setLookupVisible(node, true);
}

async function respond(node, action) {
    const ui = node?.__urnOvhLookupUI;
    const token = String(node?.__urnOvhToken || "");
    if (!ui || !token) return;
    ui.accept.disabled = true;
    ui.reject.disabled = true;
    ui.status.textContent = action === "accept" ? "Accepted — fetching lyrics..." : "Rejected — stopping workflow...";
    try {
        const response = await api.fetchApi(RESPONSE_ROUTE, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                token,
                node_id: String(node.id),
                action,
                selected_index: Number(ui.select.value || 0),
            }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || data?.ok === false) throw new Error(data?.error || `HTTP ${response.status}`);
        node.__urnOvhToken = "";
        if (action === "accept") setTimeout(() => setLookupVisible(node, false), 450);
    } catch (error) {
        console.error("[URN Audio LyricsOVH] response failed", error);
        ui.status.textContent = `Could not continue: ${error?.message || error}`;
        ui.accept.disabled = false;
        ui.reject.disabled = false;
    }
}

function findNodeById(id) {
    return app.graph?.getNodeById?.(id) || (app.graph?._nodes || []).find((n) => String(n.id) === String(id)) || null;
}

function handleLookupRequest(event) {
    const detail = event?.detail || {};
    const node = findNodeById(detail?.node_id);
    if (!node || !isOurNode(node)) return;
    install(node);
    populateLookup(node, detail);
}

async function recoverPending() {
    try {
        const response = await api.fetchApi(PENDING_ROUTE, { method: "GET" });
        if (!response.ok) return;
        const data = await response.json().catch(() => ({}));
        for (const detail of data?.pending || []) {
            const node = findNodeById(detail?.node_id);
            if (!node || !isOurNode(node)) continue;
            install(node);
            if (String(node.__urnOvhToken || "") !== String(detail?.token || "")) populateLookup(node, detail);
        }
    } catch (_) {}
}

function install(node) {
    if (!isOurNode(node) || node.__urnOvhInstalled || typeof node.addDOMWidget !== "function") return;
    node.__urnOvhInstalled = true;
    hideFilenameHint(node);
    syncFilenameHint(node);

    const lookupRoot = makeLookupUI(node);
    const lookupWidget = node.addDOMWidget(LOOKUP_WIDGET, LOOKUP_WIDGET, lookupRoot, {
        serialize: false,
        getMinHeight: () => node.__urnOvhLookupActive ? 106 : 0,
    });
    lookupWidget.serialize = false;
    if (lookupWidget.options) lookupWidget.options.serialize = false;

    const textRoot = makeTextPreview(node);
    const textWidget = node.addDOMWidget(TEXT_WIDGET, TEXT_WIDGET, textRoot, {
        serialize: false,
        getMinHeight: () => 72,
    });
    textWidget.serialize = false;
    if (textWidget.options) textWidget.options.serialize = false;

    // Keep the lookup controls followed by the lyrics preview.  Unlike V9.40,
    // this node deliberately does NOT inject an AUDIO_UI player widget.
    const widgets = node.widgets || [];
    const lookupIndex = widgets.findIndex((w) => w.name === LOOKUP_WIDGET);
    const textIndex = widgets.findIndex((w) => w.name === TEXT_WIDGET);
    if (lookupIndex >= 0 && textIndex >= 0 && textIndex !== lookupIndex + 1) {
        const [w] = widgets.splice(textIndex, 1);
        const li = widgets.findIndex((x) => x.name === LOOKUP_WIDGET);
        widgets.splice(li + 1, 0, w);
    }

    node.__urnOvhLookupUI.accept.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); respond(node, "accept"); });
    node.__urnOvhLookupUI.reject.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); respond(node, "reject"); });

    const previousExecuted = node.onExecuted;
    node.onExecuted = function (message) {
        previousExecuted?.apply(this, arguments);
        const payload = message?.urn_lyrics_ovh_text;
        const lyrics = Array.isArray(payload) ? payload[0] : payload;
        const selectedPayload = message?.urn_lyrics_ovh_selected;
        const selected = Array.isArray(selectedPayload) ? selectedPayload[0] : selectedPayload;
        if (typeof lyrics === "string" && lyrics.trim()) {
            this.__urnOvhTextUI.text.textContent = selected ? `${selected}\n\n${lyrics}` : lyrics;
            this.__urnOvhTextUI.text.style.opacity = "1";
        }
    };

    const previousWidgetChanged = node.onWidgetChanged;
    node.onWidgetChanged = function (name, value, oldValue, widget) {
        previousWidgetChanged?.apply(this, arguments);
        if (name === "audio" && value !== oldValue) {
            this.__urnOvhToken = "";
            setLookupVisible(this, false);
            this.__urnOvhTextUI.text.textContent = "Audio changed — run the node to search Lyrics.ovh.";
            this.__urnOvhTextUI.text.style.opacity = ".8";
            queueMicrotask(() => syncFilenameHint(this));
        }
    };

    const previousConnectionsChange = node.onConnectionsChange;
    node.onConnectionsChange = function () {
        previousConnectionsChange?.apply(this, arguments);
        this.__urnOvhToken = "";
        setLookupVisible(this, false);
        this.__urnOvhTextUI.text.textContent = "Audio changed — run the node to search Lyrics.ovh.";
        this.__urnOvhTextUI.text.style.opacity = ".8";
        queueMicrotask(() => syncFilenameHint(this));
    };

    const previousGraphConfigured = node.onGraphConfigured;
    node.onGraphConfigured = function () {
        previousGraphConfigured?.apply(this, arguments);
        queueMicrotask(() => syncFilenameHint(this));
    };

    const picker = node.widgets?.find?.((w) => w.name === "audio");
    if (picker) {
        const previousCallback = picker.callback;
        picker.callback = function () {
            const result = previousCallback?.apply(this, arguments);
            queueMicrotask(() => syncFilenameHint(node));
            return result;
        };
    }

    const setSize = () => {
        const width = Math.max(Number(node.size?.[0]) || 0, 380);
        const height = Math.max(Number(node.size?.[1]) || 0, 300);
        node.setSize?.([width, height]);
    };
    setSize();
    requestAnimationFrame(setSize);
    queueMicrotask(() => syncFilenameHint(node));
    node.graph?.setDirtyCanvas?.(true, true);
}

function scan() {
    for (const node of app.graph?._nodes || []) install(node);
}

app.registerExtension({
    name: "URN.AudioLyricsOVH.InteractiveLookup",
    nodeCreated(node) { install(node); },
    loadedGraphNode(node) { install(node); },
    setup() {
        api.addEventListener(EVENT_NAME, handleLookupRequest);
        scan();
        recoverPending();
        setTimeout(() => { scan(); recoverPending(); }, 250);
        setTimeout(() => { scan(); recoverPending(); }, 1000);
        if (!globalThis.__urnLyricsOvhPendingPoll) {
            globalThis.__urnLyricsOvhPendingPoll = setInterval(recoverPending, 500);
        }
    },
});
