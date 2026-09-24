import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_ID = "URNAudioLyrics";
const LOOKUP_WIDGET_NAME = "urnLyricsOvhLookupUI";
const EMBEDDED_WIDGET_NAME = "urnEmbeddedLyricsUI";
const LOOKUP_EVENT = "urn_audio_nodes.audio_lyrics.lookup_request";
const EMBEDDED_EVENT = "urn_audio_nodes.audio_lyrics.embedded_request";
const STATUS_EVENT = "urn_audio_nodes.audio_lyrics.status";
const STATUS_WIDGET_NAME = "urnLyricsStatusUI";
const LOOKUP_RESPONSE_ROUTE = "/urn_audio_nodes/audio_lyrics/lookup_response";
const LOOKUP_PENDING_ROUTE = "/urn_audio_nodes/audio_lyrics/lookup_pending";
const EMBEDDED_RESPONSE_ROUTE = "/urn_audio_nodes/audio_lyrics/embedded_response";
const EMBEDDED_PENDING_ROUTE = "/urn_audio_nodes/audio_lyrics/embedded_pending";

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

function getFilenameHint(node) {
    if (!hasConnectedAudioInput(node)) return "";
    const upstream = getUpstreamNode(node);
    if (!upstream) return "";

    // Read the actual upstream loader/file widget directly.  The Audio Lyrics
    // node no longer owns or mirrors an audio-player element.
    for (const name of ["audio", "filename", "file"]) {
        const widget = upstream.widgets?.find?.((w) => w?.name === name);
        const value = String(widget?.value || "").trim();
        if (value) return value;
    }
    return "";
}

function syncSourceFilenameHint(node) {
    const hintWidget = node?.widgets?.find?.((w) => w?.name === "source_filename_hint");
    if (!hintWidget) return;
    const next = getFilenameHint(node);
    if (hintWidget.value !== next) hintWidget.value = next;
}

function hideFilenameHintWidget(node) {
    const widget = node?.widgets?.find?.((w) => w?.name === "source_filename_hint");
    if (!widget || widget.__urnHidden) return;
    widget.__urnHidden = true;
    widget.computeSize = () => [0, -4];

    // IMPORTANT: do not serialize a cached filename. Load Audio can change its
    // selected file without changing the graph connection, so the old hidden
    // widget value can otherwise be sent again on the next queue. Resolve the
    // upstream loader's CURRENT widget value at prompt-serialization time.
    widget.serializeValue = () => {
        const current = getFilenameHint(node);
        widget.value = current;
        return current;
    };
    widget.draw = () => {};
}

function syncConnectedAudioPreview(node) {
    if (!isOurNode(node)) return;
    syncSourceFilenameHint(node);
}

function stopGraphEvent(e) {
    e.stopPropagation();
}

function makeStatusUI(node) {
    const root = document.createElement("div");
    Object.assign(root.style, {
        boxSizing: "border-box",
        width: "100%",
        height: "22px",
        display: "flex",
        alignItems: "center",
        padding: "0 8px",
        fontSize: "12px",
        lineHeight: "18px",
        color: "rgba(255,255,255,.72)",
        overflow: "hidden",
        textOverflow: "ellipsis",
        whiteSpace: "nowrap",
        userSelect: "none",
        pointerEvents: "none",
    });
    root.textContent = "Status: Ready";
    node.__urnLyricsStatusUI = root;
    return root;
}

function setStatus(node, message) {
    if (!node || !isOurNode(node)) return;
    installLyricsPreview(node);
    if (!node.__urnLyricsStatusUI) return;
    const clean = String(message || "Ready").trim() || "Ready";
    node.__urnLyricsStatusUI.textContent = `Status: ${clean}`;
    node.graph?.setDirtyCanvas?.(true, true);
}

function makeEmbeddedUI(node) {
    const root = document.createElement("div");
    Object.assign(root.style, {
        boxSizing: "border-box",
        width: "100%",
        display: "none",
        flexDirection: "column",
        gap: "6px",
        padding: "8px",
        border: "1px solid rgba(255,184,70,.48)",
        borderRadius: "7px",
        background: "rgba(26,20,12,.72)",
        userSelect: "none",
    });

    const status = document.createElement("div");
    Object.assign(status.style, {
        fontSize: "12px",
        lineHeight: "16px",
        color: "rgba(255,235,205,.92)",
        overflow: "hidden",
        textOverflow: "ellipsis",
        whiteSpace: "nowrap",
    });
    status.textContent = "Embedded lyrics found in audio metadata";

    const preview = document.createElement("textarea");
    preview.readOnly = true;
    preview.spellcheck = false;
    Object.assign(preview.style, {
        boxSizing: "border-box",
        width: "100%",
        height: "150px",
        resize: "vertical",
        border: "1px solid rgba(255,255,255,.18)",
        borderRadius: "6px",
        background: "#161616",
        color: "#eee",
        padding: "7px 8px",
        fontSize: "12px",
        lineHeight: "16px",
        fontFamily: "inherit",
        whiteSpace: "pre-wrap",
    });

    const row = document.createElement("div");
    Object.assign(row.style, { display: "flex", gap: "7px", width: "100%" });

    const use = document.createElement("button");
    use.type = "button";
    use.textContent = "Use Embedded Lyrics";
    const ignore = document.createElement("button");
    ignore.type = "button";
    ignore.textContent = "Ignore / Search Online";
    const stop = document.createElement("button");
    stop.type = "button";
    stop.textContent = "Stop Workflow";

    for (const button of [use, ignore, stop]) {
        Object.assign(button.style, {
            flex: "1 1 0",
            minHeight: "30px",
            border: "1px solid rgba(255,255,255,.20)",
            borderRadius: "7px",
            color: "#fff",
            background: "rgba(110,110,110,.56)",
            fontSize: "12px",
            cursor: "pointer",
        });
    }
    use.style.borderColor = "rgba(255,160,36,.80)";
    use.style.background = "rgba(120,75,20,.68)";
    stop.style.borderColor = "rgba(255,92,92,.78)";
    stop.style.background = "rgba(120,34,34,.68)";

    row.append(use, ignore, stop);
    root.append(status, preview, row);

    for (const el of [root, preview, use, ignore, stop]) {
        for (const evt of ["pointerdown", "mousedown", "mouseup", "dblclick", "wheel"]) {
            el.addEventListener(evt, stopGraphEvent);
        }
    }
    preview.addEventListener("keydown", stopGraphEvent);

    node.__urnEmbeddedLyricsUI = { root, status, preview, use, ignore, stop };
    return root;
}

function setEmbeddedVisible(node, visible) {
    const ui = node?.__urnEmbeddedLyricsUI;
    if (!ui) return;
    node.__urnEmbeddedLyricsActive = !!visible;
    ui.root.style.display = visible ? "flex" : "none";
    if (visible) {
        const w = Math.max(Number(node.size?.[0]) || 0, 470);
        const h = Math.max(Number(node.size?.[1]) || 0, 475);
        node.setSize?.([w, h]);
    }
    node.graph?.setDirtyCanvas?.(true, true);
}

function populateEmbedded(node, detail) {
    const ui = node?.__urnEmbeddedLyricsUI;
    if (!ui) return;
    node.__urnEmbeddedLyricsToken = String(detail?.token || node.__urnEmbeddedLyricsToken || "");
    const artist = String(detail?.artist || "").trim();
    const title = String(detail?.title || "").trim();
    ui.status.textContent = artist || title
        ? `Embedded lyrics found — ${artist}${artist && title ? " — " : ""}${title}`
        : "Embedded lyrics found in audio metadata";
    ui.preview.value = String(detail?.lyrics || "");
    ui.use.disabled = false;
    ui.ignore.disabled = false;
    ui.stop.disabled = false;
    setEmbeddedVisible(node, true);
}

async function respondEmbedded(node, action) {
    const ui = node?.__urnEmbeddedLyricsUI;
    const token = String(node?.__urnEmbeddedLyricsToken || "");
    if (!ui || !token) return;

    ui.use.disabled = true;
    ui.ignore.disabled = true;
    ui.stop.disabled = true;
    if (action === "use") ui.status.textContent = "Using embedded lyrics...";
    else if (action === "stop") ui.status.textContent = "Stopping workflow...";
    else ui.status.textContent = "Ignoring embedded lyrics — continuing...";

    try {
        const response = await api.fetchApi(EMBEDDED_RESPONSE_ROUTE, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                token,
                node_id: String(node.id),
                action,
            }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || data?.ok === false) throw new Error(data?.error || `HTTP ${response.status}`);
        node.__urnEmbeddedLyricsToken = "";
        setTimeout(() => setEmbeddedVisible(node, false), 450);
    } catch (error) {
        console.error("[URN Audio Lyrics] embedded lyrics response failed", error);
        ui.status.textContent = `Could not continue: ${error?.message || error}`;
        ui.use.disabled = false;
        ui.ignore.disabled = false;
        ui.stop.disabled = false;
    }
}

function makeLookupUI(node) {
    const root = document.createElement("div");
    Object.assign(root.style, {
        boxSizing: "border-box",
        width: "100%",
        display: "none",
        flexDirection: "column",
        gap: "6px",
        padding: "8px",
        border: "1px solid rgba(255,255,255,.18)",
        borderRadius: "7px",
        background: "rgba(12,12,13,.58)",
        userSelect: "none",
    });

    const status = document.createElement("div");
    Object.assign(status.style, {
        fontSize: "12px",
        lineHeight: "16px",
        color: "rgba(255,255,255,.78)",
        overflow: "hidden",
        textOverflow: "ellipsis",
        whiteSpace: "nowrap",
    });

    const manualRow = document.createElement("div");
    Object.assign(manualRow.style, { display: "none", gap: "7px", width: "100%" });

    const manualArtist = document.createElement("input");
    manualArtist.type = "text";
    manualArtist.placeholder = "Artist name";
    Object.assign(manualArtist.style, {
        flex: "1 1 auto",
        minWidth: "0",
        height: "30px",
        boxSizing: "border-box",
        border: "1px solid rgba(255,255,255,.22)",
        borderRadius: "6px",
        background: "#202022",
        color: "#eee",
        padding: "0 8px",
        fontSize: "12px",
    });

    const search = document.createElement("button");
    search.type = "button";
    search.textContent = "Search Artist";
    Object.assign(search.style, {
        flex: "0 0 112px",
        height: "30px",
        border: "1px solid rgba(90,180,255,.75)",
        borderRadius: "7px",
        color: "#fff",
        background: "rgba(35,85,125,.72)",
        fontSize: "12px",
        cursor: "pointer",
    });
    manualRow.append(manualArtist, search);

    const select = document.createElement("select");
    Object.assign(select.style, {
        boxSizing: "border-box",
        width: "100%",
        height: "30px",
        border: "1px solid rgba(255,255,255,.22)",
        borderRadius: "6px",
        background: "#202022",
        color: "#eee",
        padding: "0 8px",
        fontSize: "12px",
    });

    const row = document.createElement("div");
    Object.assign(row.style, { display: "flex", gap: "7px", width: "100%" });

    const accept = document.createElement("button");
    accept.type = "button";
    accept.textContent = "Accept";
    const reject = document.createElement("button");
    reject.type = "button";
    reject.textContent = "Reject / Manual Search";
    const stop = document.createElement("button");
    stop.type = "button";
    stop.textContent = "Stop Workflow";

    for (const button of [accept, reject, stop]) {
        Object.assign(button.style, {
            flex: "1 1 0",
            height: "30px",
            border: "1px solid rgba(255,255,255,.20)",
            borderRadius: "7px",
            color: "#fff",
            background: "rgba(110,110,110,.56)",
            fontSize: "12px",
            cursor: "pointer",
        });
    }
    accept.style.borderColor = "rgba(255,160,36,.75)";
    accept.style.background = "rgba(120,75,20,.58)";
    stop.style.borderColor = "rgba(255,92,92,.78)";
    stop.style.background = "rgba(120,34,34,.68)";

    row.append(accept, reject, stop);
    root.append(status, manualRow, select, row);

    for (const el of [root, manualRow, manualArtist, search, select, accept, reject, stop]) {
        for (const evt of ["pointerdown", "mousedown", "mouseup", "dblclick", "wheel"]) {
            el.addEventListener(evt, stopGraphEvent);
        }
    }
    select.addEventListener("keydown", stopGraphEvent);
    manualArtist.addEventListener("keydown", (e) => {
        e.stopPropagation();
        if (e.key === "Enter") {
            e.preventDefault();
            respondLookup(node, "search");
        }
    });

    node.__urnLyricsLookupUI = { root, status, manualRow, manualArtist, search, select, accept, reject, stop };
    return root;
}

function setLookupVisible(node, visible) {
    const ui = node?.__urnLyricsLookupUI;
    if (!ui) return;
    node.__urnLyricsLookupActive = !!visible;
    ui.root.style.display = visible ? "flex" : "none";
    if (visible) {
        const w = Math.max(Number(node.size?.[0]) || 0, 430);
        const h = Math.max(Number(node.size?.[1]) || 0, 390);
        node.setSize?.([w, h]);
    }
    node.graph?.setDirtyCanvas?.(true, true);
}

function applyLookupState(node, detail) {
    const ui = node?.__urnLyricsLookupUI;
    if (!ui) return;

    const mode = String(detail?.mode || "automatic");
    node.__urnLyricsLookupMode = mode;
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
    const selectedIndex = Math.max(0, Math.min(Number(detail?.selected_index) || 0, Math.max(0, options.length - 1)));
    ui.select.value = String(selectedIndex);
    ui.select.disabled = options.length === 0;
    ui.accept.disabled = options.length === 0;
    ui.stop.disabled = false;
    ui.search.disabled = false;

    if (mode === "manual") {
        ui.manualRow.style.display = "flex";
        if (detail?.artist != null && !ui.manualArtist.matches(":focus")) {
            ui.manualArtist.value = String(detail.artist || "");
        }
        ui.accept.textContent = "Use Selected";
        ui.reject.textContent = "Reject / Use Whisper";
        ui.reject.disabled = false;
        if (options.length) {
            ui.status.textContent = `${detail?.artist || "Artist"} — select the song title`;
        } else if (detail?.searched) {
            ui.status.textContent = `No titles found for ${detail?.artist || "that artist"} — try another artist`;
        } else {
            ui.status.textContent = "Manual search — enter the artist name, then Search Artist";
        }
    } else {
        ui.manualRow.style.display = "none";
        ui.accept.textContent = "Accept";
        ui.reject.textContent = "Reject / Manual Search";
        ui.reject.disabled = false;
        ui.status.textContent = `${detail?.artist || "Artist"} — select the song title`;
    }
}

function populateLookup(node, detail) {
    const ui = node?.__urnLyricsLookupUI;
    if (!ui) return;
    node.__urnLyricsLookupToken = String(detail?.token || node.__urnLyricsLookupToken || "");
    applyLookupState(node, detail);
    setLookupVisible(node, true);
}

async function respondLookup(node, action) {
    const ui = node?.__urnLyricsLookupUI;
    const token = String(node?.__urnLyricsLookupToken || "");
    if (!ui || !token) return;

    const mode = String(node.__urnLyricsLookupMode || "automatic");
    const actualAction = action === "reject" && mode !== "manual" ? "manual" : action;

    if (actualAction === "search") {
        const artist = String(ui.manualArtist.value || "").trim();
        if (!artist) {
            ui.status.textContent = "Enter an artist name first.";
            ui.manualArtist.focus();
            return;
        }
        ui.search.disabled = true;
        ui.accept.disabled = true;
        ui.status.textContent = `Searching for ${artist}...`;
    } else {
        ui.accept.disabled = true;
        ui.reject.disabled = true;
        ui.stop.disabled = true;
        ui.search.disabled = true;
        if (actualAction === "accept") ui.status.textContent = "Fetching selected lyrics...";
        else if (actualAction === "stop") ui.status.textContent = "Stopping workflow...";
        else if (actualAction === "manual") ui.status.textContent = "Opening manual artist search...";
        else ui.status.textContent = "Rejected — using Whisper...";
    }

    try {
        const response = await api.fetchApi(LOOKUP_RESPONSE_ROUTE, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                token,
                node_id: String(node.id),
                action: actualAction,
                selected_index: Number(ui.select.value || 0),
                artist: String(ui.manualArtist.value || "").trim(),
            }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || data?.ok === false) throw new Error(data?.error || `HTTP ${response.status}`);

        if (actualAction === "manual" || actualAction === "search") {
            applyLookupState(node, data);
            setLookupVisible(node, true);
            if (actualAction === "manual") {
                setTimeout(() => ui.manualArtist.focus(), 0);
            }
            return;
        }

        node.__urnLyricsLookupToken = "";
        setTimeout(() => setLookupVisible(node, false), 650);
    } catch (error) {
        console.error("[URN Audio Lyrics] lookup response failed", error);
        ui.status.textContent = `Could not continue: ${error?.message || error}`;
        ui.accept.disabled = ui.select.options.length === 0;
        ui.reject.disabled = false;
        ui.stop.disabled = false;
        ui.search.disabled = false;
    }
}

function findNodeById(id) {
    const direct = app.graph?.getNodeById?.(id);
    if (direct) return direct;
    return (app.graph?._nodes || []).find((n) => String(n.id) === String(id)) || null;
}

function handleStatusEvent(event) {
    const detail = event?.detail || {};
    const node = findNodeById(detail?.node_id);
    if (!node || !isOurNode(node)) return;
    setStatus(node, detail?.message || "Ready");
}

function handleLookupRequest(event) {
    const detail = event?.detail || {};
    const node = findNodeById(detail?.node_id);
    if (!node || !isOurNode(node)) return;
    installLyricsPreview(node);
    populateLookup(node, detail);
}

function handleEmbeddedRequest(event) {
    const detail = event?.detail || {};
    const node = findNodeById(detail?.node_id);
    if (!node || !isOurNode(node)) return;
    installLyricsPreview(node);
    populateEmbedded(node, detail);
}

async function recoverPendingEmbedded() {
    try {
        const response = await api.fetchApi(EMBEDDED_PENDING_ROUTE, { method: "GET" });
        if (!response.ok) return;
        const data = await response.json().catch(() => ({}));
        for (const detail of data?.pending || []) {
            const node = findNodeById(detail?.node_id);
            if (!node || !isOurNode(node)) continue;
            installLyricsPreview(node);
            if (String(node.__urnEmbeddedLyricsToken || "") !== String(detail?.token || "")) {
                populateEmbedded(node, detail);
            }
        }
    } catch (_) {}
}

async function recoverPendingLookups() {
    try {
        const response = await api.fetchApi(LOOKUP_PENDING_ROUTE, { method: "GET" });
        if (!response.ok) return;
        const data = await response.json().catch(() => ({}));
        for (const detail of data?.pending || []) {
            const node = findNodeById(detail?.node_id);
            if (!node || !isOurNode(node)) continue;
            installLyricsPreview(node);
            if (String(node.__urnLyricsLookupToken || "") !== String(detail?.token || "")) {
                populateLookup(node, detail);
            }
        }
    } catch (_) {}
}

function installLyricsPreview(node) {
    if (!isOurNode(node)) return;
    if (node.__urnLyricsInstalled) return;
    if (typeof node.addDOMWidget !== "function") return;
    node.__urnLyricsInstalled = true;

    const songWidget = node.widgets?.find?.((w) => w.name === "song");
    if (songWidget) songWidget.label = "Song Analysis";
    const timestampsWidget = node.widgets?.find?.((w) => w.name === "include_timestamps");
    if (timestampsWidget) timestampsWidget.label = "Include timestamps";
    const lookupToggle = node.widgets?.find?.((w) => w.name === "get_lyrics_ovh");
    if (lookupToggle) lookupToggle.label = "Get Online Lyrics";
    const genderWidget = node.widgets?.find?.((w) => w.name === "gender_vocal_determination");
    if (genderWidget) genderWidget.label = "Gender Vocal Determination";

    // The backend deliberately keeps gender_vocal_determination LAST in the
    // serialized widget order so workflows saved with the V9.88 baseline still
    // map Song / Include timestamps / Get Online Lyrics correctly.  Visually it
    // belongs directly under Song Analysis, though, so move the real widget for
    // display and repair the legacy positional order again during serialization.
    if (genderWidget && songWidget && Array.isArray(node.widgets)) {
        const from = node.widgets.indexOf(genderWidget);
        const songIndex = node.widgets.indexOf(songWidget);
        if (from >= 0 && songIndex >= 0 && from !== songIndex + 1) {
            node.widgets.splice(from, 1);
            const refreshedSongIndex = node.widgets.indexOf(songWidget);
            node.widgets.splice(refreshedSongIndex + 1, 0, genderWidget);
        }
    }

    const previousSerializeForWidgetOrder = node.onSerialize;
    node.onSerialize = function (data) {
        const result = previousSerializeForWidgetOrder?.apply(this, arguments);
        if (data) {
            const getValue = (name, fallback) => {
                const w = this.widgets?.find?.((x) => x?.name === name);
                return w ? w.value : fallback;
            };
            const sourceWidget = this.widgets?.find?.((x) => x?.name === "source_filename_hint");
            let sourceValue = sourceWidget?.value ?? "";
            try {
                if (typeof sourceWidget?.serializeValue === "function") {
                    sourceValue = sourceWidget.serializeValue(this, 3);
                }
            } catch (_) {}

            // Canonical backend/V9.88-compatible positional order.  The visual
            // order may differ, but saved workflows remain safe to reopen.
            data.widgets_values = [
                getValue("song", true),
                getValue("include_timestamps", false),
                getValue("get_lyrics_ovh", true),
                sourceValue,
                getValue("gender_vocal_determination", true),
            ];
            if (data.widgets_values_named && typeof data.widgets_values_named === "object") {
                data.widgets_values_named.song = getValue("song", true);
                data.widgets_values_named.include_timestamps = getValue("include_timestamps", false);
                data.widgets_values_named.get_lyrics_ovh = getValue("get_lyrics_ovh", true);
                data.widgets_values_named.source_filename_hint = sourceValue;
                data.widgets_values_named.gender_vocal_determination = getValue("gender_vocal_determination", true);
            }
        }
        return result;
    };

    hideFilenameHintWidget(node);
    syncSourceFilenameHint(node);

    const statusRoot = makeStatusUI(node);
    const statusWidget = node.addDOMWidget(STATUS_WIDGET_NAME, STATUS_WIDGET_NAME, statusRoot, {
        serialize: false,
        getMinHeight: () => 22,
    });
    statusWidget.serialize = false;
    if (statusWidget.options) statusWidget.options.serialize = false;
    node.__urnLyricsStatusWidget = statusWidget;

    const embeddedRoot = makeEmbeddedUI(node);
    const embeddedWidget = node.addDOMWidget(EMBEDDED_WIDGET_NAME, EMBEDDED_WIDGET_NAME, embeddedRoot, {
        serialize: false,
        getMinHeight: () => node.__urnEmbeddedLyricsActive ? 220 : 0,
    });
    embeddedWidget.serialize = false;
    if (embeddedWidget.options) embeddedWidget.options.serialize = false;
    node.__urnEmbeddedLyricsWidget = embeddedWidget;
    node.__urnEmbeddedLyricsUI.use.addEventListener("click", (e) => {
        e.preventDefault(); e.stopPropagation(); respondEmbedded(node, "use");
    });
    node.__urnEmbeddedLyricsUI.ignore.addEventListener("click", (e) => {
        e.preventDefault(); e.stopPropagation(); respondEmbedded(node, "continue");
    });
    node.__urnEmbeddedLyricsUI.stop.addEventListener("click", (e) => {
        e.preventDefault(); e.stopPropagation(); respondEmbedded(node, "stop");
    });

    const lookupRoot = makeLookupUI(node);
    const lookupWidget = node.addDOMWidget(LOOKUP_WIDGET_NAME, LOOKUP_WIDGET_NAME, lookupRoot, {
        serialize: false,
        getMinHeight: () => node.__urnLyricsLookupActive ? (node.__urnLyricsLookupMode === "manual" ? 140 : 102) : 0,
    });
    lookupWidget.serialize = false;
    if (lookupWidget.options) lookupWidget.options.serialize = false;
    node.__urnLyricsLookupWidget = lookupWidget;
    node.__urnLyricsLookupUI.accept.addEventListener("click", (e) => {
        e.preventDefault(); e.stopPropagation(); respondLookup(node, "accept");
    });
    node.__urnLyricsLookupUI.reject.addEventListener("click", (e) => {
        e.preventDefault(); e.stopPropagation(); respondLookup(node, "reject");
    });
    node.__urnLyricsLookupUI.search.addEventListener("click", (e) => {
        e.preventDefault(); e.stopPropagation(); respondLookup(node, "search");
    });
    node.__urnLyricsLookupUI.stop.addEventListener("click", (e) => {
        e.preventDefault(); e.stopPropagation(); respondLookup(node, "stop");
    });

    // Keep the permanent one-line status directly below Get Online Lyrics,
    // with the interactive song-selection panel immediately below the status line.
    const widgets = node.widgets || [];
    const moveAfter = (widgetName, afterName, offset = 1) => {
        const from = widgets.findIndex((w) => w.name === widgetName);
        const after = widgets.findIndex((w) => w.name === afterName);
        if (from < 0 || after < 0) return;
        const [widget] = widgets.splice(from, 1);
        const refreshedAfter = widgets.findIndex((w) => w.name === afterName);
        widgets.splice(refreshedAfter + offset, 0, widget);
    };
    moveAfter(STATUS_WIDGET_NAME, "get_lyrics_ovh", 1);
    moveAfter(EMBEDDED_WIDGET_NAME, STATUS_WIDGET_NAME, 1);
    moveAfter(LOOKUP_WIDGET_NAME, EMBEDDED_WIDGET_NAME, 1);

    const previousWidgetChanged = node.onWidgetChanged;
    node.onWidgetChanged = function (name, value, oldValue, widget) {
        previousWidgetChanged?.apply(this, arguments);
        if (name === "get_lyrics_ovh" && !value) {
            this.__urnLyricsLookupToken = "";
            setLookupVisible(this, false);
        }
    };

    const previousConnectionsChange = node.onConnectionsChange;
    node.onConnectionsChange = function () {
        previousConnectionsChange?.apply(this, arguments);
        queueMicrotask(() => syncConnectedAudioPreview(this));
    };

    const previousGraphConfigured = node.onGraphConfigured;
    node.onGraphConfigured = function () {
        previousGraphConfigured?.apply(this, arguments);
        queueMicrotask(() => syncConnectedAudioPreview(this));
    };

    const setCompactSize = () => {
        const current = node.size || [0, 0];
        const width = Math.max(Number(current[0]) || 0, 340);
        const height = Math.max(Number(current[1]) || 0, 250);
        node.setSize?.([width, height]);
    };
    setCompactSize();
    requestAnimationFrame(setCompactSize);

    queueMicrotask(() => syncConnectedAudioPreview(node));
    node.graph?.setDirtyCanvas?.(true, true);
}

function scan() {
    for (const node of app.graph?._nodes || []) installLyricsPreview(node);
}

app.registerExtension({
    name: "URN.AudioLyrics.LookupOnly",


    nodeCreated(node) {
        installLyricsPreview(node);
    },

    loadedGraphNode(node) {
        installLyricsPreview(node);
    },

    setup() {
        api.addEventListener(LOOKUP_EVENT, handleLookupRequest);
        api.addEventListener(EMBEDDED_EVENT, handleEmbeddedRequest);
        api.addEventListener(STATUS_EVENT, handleStatusEvent);
        scan();
        recoverPendingEmbedded();
        recoverPendingLookups();
        setTimeout(() => { scan(); recoverPendingEmbedded(); recoverPendingLookups(); }, 250);
        setTimeout(() => { scan(); recoverPendingEmbedded(); recoverPendingLookups(); }, 1000);
        if (!globalThis.__urnAudioLyricsLookupPendingPoll) {
            globalThis.__urnAudioLyricsLookupPendingPoll = setInterval(() => {
                recoverPendingEmbedded();
                recoverPendingLookups();
                // Load Audio file selection changes do not fire a cable/connection event.
                // Keep every URN Audio Lyrics filename hint in step with its upstream loader.
                for (const node of app.graph?._nodes || []) {
                    if (isOurNode(node)) syncSourceFilenameHint(node);
                }
            }, 500);
        }
    },
});
