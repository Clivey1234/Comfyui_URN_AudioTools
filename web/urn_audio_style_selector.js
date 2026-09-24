import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_ID = "URNAudioStyleSelector";
const EVENT_NAME = "urn_audio_nodes.style_selector.request";
const ACCEPT_ROUTE = "/urn_audio_nodes/style_selector/accept";
const PENDING_ROUTE = "/urn_audio_nodes/style_selector/pending";
const STYLES_ROUTE = "/urn_audio_nodes/style_selector/styles";
const ADD_CUSTOM_ROUTE = "/urn_audio_nodes/style_selector/add_custom";
const UI_WIDGET = "urnAudioStyleSelectorUI";
const DEFAULT_SIZE = [790, 740];
const MIN_SIZE = [520, 470];

const COLORS = {
    panelAvailable: "#000000",
    panelSelected: "#43AE5F",
    tabActive: "#1688B8",
    tabInactive: "#C5A600",
    tagAvailable: "#5A123F",
    tagAvailableMuted: "#3F6F7A",
    tagGenerated: "#8D8D8D",
    tagUser: "#7D4F74",
    quickCustom: "#9A176F",
    buttonAccept: "#8A8A8A",
    buttonAcceptDisabled: "#8A8A8A",
    border: "#383838",
    inputBg: "#1B1B1B",
    inputBorder: "#555555",
    textDark: "#111111",
    textLight: "#F1F1F1",
    textMuted: "#B2B2B2",
};

function isOurNode(node) {
    return node?.comfyClass === NODE_ID || node?.constructor?.comfyClass === NODE_ID;
}

function stopGraphEvent(e) {
    e.stopPropagation();
}

function keyOf(value) {
    return String(value ?? "").trim().toLocaleLowerCase();
}

function makeButton(label) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    Object.assign(button.style, {
        border: "0",
        borderRadius: "9px",
        minHeight: "38px",
        padding: "6px 16px",
        fontFamily: "inherit",
        fontSize: "16px",
        fontWeight: "700",
        lineHeight: "1.05",
        cursor: "pointer",
        boxSizing: "border-box",
    });
    for (const evt of ["pointerdown", "mousedown", "mouseup", "dblclick", "wheel"]) {
        button.addEventListener(evt, stopGraphEvent);
    }
    return button;
}

function makeUI(node) {
    const root = document.createElement("div");
    Object.assign(root.style, {
        width: "100%",
        height: "100%",
        minWidth: "0",
        minHeight: "0",
        boxSizing: "border-box",
        padding: "7px 10px 8px",
        display: "flex",
        flexDirection: "column",
        gap: "10px",
        userSelect: "none",
        overflow: "hidden",
    });

    const tabs = document.createElement("div");
    Object.assign(tabs.style, {
        display: "flex",
        flexWrap: "wrap",
        gap: "8px",
        flex: "0 0 auto",
        minHeight: "42px",
        alignItems: "center",
    });

    const available = document.createElement("div");
    Object.assign(available.style, {
        background: COLORS.panelAvailable,
        border: `1px solid ${COLORS.border}`,
        borderRadius: "11px",
        padding: "14px",
        display: "flex",
        flexWrap: "wrap",
        alignContent: "flex-start",
        gap: "10px",
        overflowY: "auto",
        flex: "1 1 47%",
        minHeight: "145px",
        boxSizing: "border-box",
    });

    const selected = document.createElement("div");
    Object.assign(selected.style, {
        background: COLORS.panelSelected,
        border: `1px solid ${COLORS.border}`,
        borderRadius: "11px",
        padding: "14px",
        display: "flex",
        flexWrap: "wrap",
        alignContent: "flex-start",
        gap: "10px",
        overflowY: "auto",
        flex: "1 1 38%",
        minHeight: "125px",
        boxSizing: "border-box",
    });

    const quickCustom = document.createElement("div");
    Object.assign(quickCustom.style, {
        display: "flex",
        flexWrap: "wrap",
        gap: "8px",
        alignItems: "center",
        justifyContent: "center",
        flex: "0 0 auto",
        minHeight: "42px",
    });

    const customInput = document.createElement("input");
    customInput.type = "text";
    customInput.placeholder = "Quick custom style...";
    customInput.maxLength = 120;
    Object.assign(customInput.style, {
        width: "30ch",
        maxWidth: "62%",
        minWidth: "180px",
        height: "36px",
        border: `1px solid ${COLORS.inputBorder}`,
        borderRadius: "8px",
        padding: "5px 10px",
        boxSizing: "border-box",
        fontFamily: "inherit",
        fontSize: "15px",
        background: COLORS.inputBg,
        color: "#ffffff",
        outline: "none",
        userSelect: "text",
    });

    const quickAdd = makeButton("ADD QUICK CUSTOM");
    Object.assign(quickAdd.style, {
        minWidth: "185px",
        height: "36px",
        minHeight: "36px",
        background: COLORS.quickCustom,
        color: COLORS.textLight,
        fontSize: "14px",
        padding: "5px 12px",
    });

    for (const evt of ["pointerdown", "mousedown", "mouseup", "dblclick", "wheel", "keydown", "keyup", "keypress"]) {
        customInput.addEventListener(evt, stopGraphEvent);
    }
    quickCustom.append(customInput, quickAdd);

    const status = document.createElement("div");
    status.textContent = "Waiting for workflow...";
    Object.assign(status.style, {
        color: COLORS.textMuted,
        fontSize: "11px",
        lineHeight: "16px",
        textAlign: "center",
        minHeight: "16px",
        flex: "0 0 16px",
        overflow: "hidden",
        textOverflow: "ellipsis",
        whiteSpace: "nowrap",
    });

    const accept = makeButton("ACCEPT CHANGES");
    accept.disabled = true;
    Object.assign(accept.style, {
        width: "240px",
        maxWidth: "80%",
        alignSelf: "center",
        flex: "0 0 42px",
        height: "42px",
        background: COLORS.buttonAcceptDisabled,
        color: "#111",
        fontSize: "17px",
        cursor: "default",
        opacity: ".65",
    });

    root.append(tabs, available, selected, quickCustom, status, accept);

    node.__urnStyleSelectorUI = {
        root,
        tabs,
        available,
        selected,
        quickCustom,
        customInput,
        quickAdd,
        status,
        accept,
        catalog: {},
        activeTab: "",
        enabled: false,
        generatedKeys: new Set(),
        selectedTags: [],
        origins: new Map(),
    };

    accept.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        acceptChanges(node);
    });

    quickAdd.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        addQuickCustom(node);
    });

    customInput.addEventListener("keydown", (e) => {
        if (e.key !== "Enter") return;
        e.preventDefault();
        e.stopPropagation();
        addQuickCustom(node);
    });

    return root;
}

function setEnabled(node, enabled) {
    const ui = node?.__urnStyleSelectorUI;
    if (!ui) return;
    ui.enabled = !!enabled;
    ui.accept.disabled = !ui.enabled || !node.__urnStyleSelectorToken;
    ui.accept.style.opacity = "1";
    ui.accept.style.cursor = ui.accept.disabled ? "default" : "pointer";
    ui.accept.style.background = COLORS.buttonAccept;
    ui.accept.style.color = COLORS.textDark;
    ui.customInput.disabled = !ui.enabled;
    ui.quickAdd.disabled = !ui.enabled;
    ui.customInput.style.opacity = "1";
    ui.quickAdd.style.opacity = "1";
    ui.quickAdd.style.cursor = ui.enabled ? "pointer" : "default";
    ui.available.style.filter = "none";
    ui.selected.style.filter = "none";
    renderTabs(node);
    renderAvailable(node);
    renderSelected(node);
}

function renderTabs(node) {
    const ui = node?.__urnStyleSelectorUI;
    if (!ui) return;
    ui.tabs.replaceChildren();
    const names = Object.keys(ui.catalog || {});
    if (!names.length) return;
    if (!names.includes(ui.activeTab)) ui.activeTab = names[0];

    for (const name of names) {
        const button = makeButton(name);
        const active = name === ui.activeTab;
        Object.assign(button.style, {
            background: active ? COLORS.tabActive : COLORS.tabInactive,
            color: COLORS.textLight,
            minWidth: "92px",
            opacity: "1",
            cursor: ui.enabled ? "pointer" : "default",
        });
        button.addEventListener("click", (e) => {
            e.preventDefault();
            e.stopPropagation();
            if (!ui.enabled) return;
            ui.activeTab = name;
            renderTabs(node);
            renderAvailable(node);
            node.graph?.setDirtyCanvas?.(true, true);
        });
        ui.tabs.append(button);
    }
}

function renderAvailable(node) {
    const ui = node?.__urnStyleSelectorUI;
    if (!ui) return;
    ui.available.replaceChildren();
    const values = Array.isArray(ui.catalog?.[ui.activeTab]) ? ui.catalog[ui.activeTab] : [];
    const selectedKeys = new Set(ui.selectedTags.map(keyOf));

    for (const tag of values) {
        const button = makeButton(tag);
        const alreadySelected = selectedKeys.has(keyOf(tag));
        Object.assign(button.style, {
            // Already-selected tags use a distinct muted teal background so the user can
            // immediately see which available styles are already in the selection.
            background: alreadySelected
                ? COLORS.tagAvailableMuted
                : COLORS.tagAvailable,
            color: COLORS.textLight,
            minWidth: "150px",
            maxWidth: "100%",
            flex: "0 1 auto",
            opacity: "1",
            cursor: !ui.enabled || alreadySelected ? "default" : "pointer",
        });
        button.title = alreadySelected ? "Already selected" : `Add ${tag}`;
        button.addEventListener("click", (e) => {
            e.preventDefault();
            e.stopPropagation();
            if (!ui.enabled || alreadySelected) return;
            ui.selectedTags.push(String(tag));
            ui.origins.set(keyOf(tag), "user");
            renderAvailable(node);
            renderSelected(node);
            node.graph?.setDirtyCanvas?.(true, true);
        });
        ui.available.append(button);
    }
}

function renderSelected(node) {
    const ui = node?.__urnStyleSelectorUI;
    if (!ui) return;
    ui.selected.replaceChildren();

    if (!ui.selectedTags.length) {
        const empty = document.createElement("div");
        empty.textContent = "No styles selected";
        Object.assign(empty.style, {
            color: COLORS.textMuted,
            fontSize: "14px",
            fontWeight: "700",
            padding: "6px 4px",
        });
        ui.selected.append(empty);
        return;
    }

    for (const tag of ui.selectedTags) {
        const origin = ui.origins.get(keyOf(tag)) || "user";
        const button = makeButton(tag);
        Object.assign(button.style, {
            background: origin === "generated" ? COLORS.tagGenerated : COLORS.tagUser,
            color: COLORS.textLight,
            minWidth: "150px",
            maxWidth: "100%",
            flex: "0 1 auto",
            opacity: "1",
            cursor: ui.enabled ? "pointer" : "default",
        });
        button.title = ui.enabled ? `Remove ${tag}` : tag;
        button.addEventListener("click", (e) => {
            e.preventDefault();
            e.stopPropagation();
            if (!ui.enabled) return;
            const k = keyOf(tag);
            ui.selectedTags = ui.selectedTags.filter((value) => keyOf(value) !== k);
            ui.origins.delete(k);
            renderAvailable(node);
            renderSelected(node);
            node.graph?.setDirtyCanvas?.(true, true);
        });
        ui.selected.append(button);
    }
}

function applyCatalog(node, catalog) {
    const ui = node?.__urnStyleSelectorUI;
    if (!ui) return;
    ui.catalog = catalog && typeof catalog === "object" ? catalog : {};
    const names = Object.keys(ui.catalog);
    if (!names.includes(ui.activeTab)) ui.activeTab = names[0] || "";
    renderTabs(node);
    renderAvailable(node);
}

async function loadCatalog(node) {
    try {
        const response = await api.fetchApi(STYLES_ROUTE, { method: "GET" });
        if (!response.ok) return;
        const data = await response.json().catch(() => ({}));
        if (data?.ok) applyCatalog(node, data.styles || {});
    } catch (_) {
        // The backend may still be starting. The workflow event will provide a fresh catalog later.
    }
}

function setWaitingState(node, detail) {
    const ui = node?.__urnStyleSelectorUI;
    if (!ui) return;

    node.__urnStyleSelectorToken = String(detail?.token || "");
    if (detail?.styles && typeof detail.styles === "object") applyCatalog(node, detail.styles);

    const generated = Array.isArray(detail?.generated) ? detail.generated.map(String) : [];
    ui.generatedKeys = new Set(generated.map(keyOf));
    ui.selectedTags = [];
    ui.origins = new Map();

    const seen = new Set();
    for (const tag of generated) {
        const clean = String(tag).trim();
        const key = keyOf(clean);
        if (!clean || seen.has(key)) continue;
        seen.add(key);
        ui.selectedTags.push(clean);
        ui.origins.set(key, "generated");
    }

    ui.status.textContent = "Workflow paused — choose styles, remove unwanted tags, then Accept Changes.";
    setEnabled(node, true);
    node.graph?.setDirtyCanvas?.(true, true);
}

async function addQuickCustom(node) {
    const ui = node?.__urnStyleSelectorUI;
    if (!ui || !ui.enabled || ui.quickAdd.disabled) return;

    const requested = String(ui.customInput.value || "").trim();
    if (!requested) {
        ui.status.textContent = "Enter a custom style first.";
        ui.customInput.focus();
        return;
    }

    ui.quickAdd.disabled = true;
    ui.quickAdd.textContent = "SAVING...";
    ui.status.textContent = `Saving custom style: ${requested}`;

    try {
        const response = await api.fetchApi(ADD_CUSTOM_ROUTE, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ tag: requested }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || data?.ok === false) {
            throw new Error(data?.error || `HTTP ${response.status}`);
        }

        const tag = String(data?.tag || requested).trim();
        if (data?.styles && typeof data.styles === "object") {
            applyCatalog(node, data.styles);
        }

        const k = keyOf(tag);
        if (tag && !ui.selectedTags.some((value) => keyOf(value) === k)) {
            ui.selectedTags.push(tag);
        }
        if (tag) ui.origins.set(k, "user");

        ui.customInput.value = "";
        renderAvailable(node);
        renderSelected(node);
        ui.status.textContent = data?.added === false
            ? `Custom style already saved; added to selection: ${tag}`
            : `Added custom style and saved to User Custom: ${tag}`;
        node.graph?.setDirtyCanvas?.(true, true);
    } catch (error) {
        console.error("[URN Audio Style Selector] Quick custom save failed", error);
        ui.status.textContent = `Could not save custom style: ${error?.message || error}`;
    } finally {
        ui.quickAdd.textContent = "ADD QUICK CUSTOM";
        ui.quickAdd.disabled = !ui.enabled;
        ui.quickAdd.style.opacity = "1";
        ui.quickAdd.style.cursor = ui.enabled ? "pointer" : "default";
    }
}

async function acceptChanges(node) {
    const ui = node?.__urnStyleSelectorUI;
    const token = String(node?.__urnStyleSelectorToken || "");
    if (!ui || !token || ui.accept.disabled) return;

    ui.accept.disabled = true;
    ui.accept.textContent = "ACCEPTING...";
    ui.accept.style.opacity = ".65";
    ui.status.textContent = "Sending selected styles...";

    try {
        const response = await api.fetchApi(ACCEPT_ROUTE, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                token,
                node_id: String(node.id),
                selected: ui.selectedTags,
            }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || data?.ok === false) {
            throw new Error(data?.error || `HTTP ${response.status}`);
        }

        node.__urnStyleSelectorToken = "";
        ui.accept.textContent = "ACCEPTED";
        ui.status.textContent = `Accepted ${ui.selectedTags.length} style tag${ui.selectedTags.length === 1 ? "" : "s"} — workflow continuing.`;
        setEnabled(node, false);
        setTimeout(() => {
            if (!node.__urnStyleSelectorToken) {
                ui.accept.textContent = "ACCEPT CHANGES";
                ui.status.textContent = "Waiting for workflow...";
            }
        }, 1000);
    } catch (error) {
        console.error("[URN Audio Style Selector] Accept failed", error);
        ui.accept.textContent = "ACCEPT CHANGES";
        ui.status.textContent = `Could not accept styles: ${error?.message || error}`;
        setEnabled(node, true);
    }
}

function applyInitialSize(node) {
    node.properties ??= {};
    if (node.properties.__urnAudioStyleSelectorInitialSizeApplied) return;
    const w = Number(node.size?.[0]) || 0;
    const h = Number(node.size?.[1]) || 0;
    node.setSize?.([Math.max(w, DEFAULT_SIZE[0]), Math.max(h, DEFAULT_SIZE[1])]);
    node.properties.__urnAudioStyleSelectorInitialSizeApplied = true;
}

function installResizeLimits(node) {
    if (node.__urnAudioStyleSelectorResizeLimitsInstalled) return;
    node.__urnAudioStyleSelectorResizeLimitsInstalled = true;
    node.min_size = [MIN_SIZE[0], MIN_SIZE[1]];
    const previousOnResize = node.onResize;
    node.onResize = function(size) {
        if (size && size.length >= 2) {
            size[0] = Math.max(MIN_SIZE[0], Number(size[0]) || MIN_SIZE[0]);
            size[1] = Math.max(MIN_SIZE[1], Number(size[1]) || MIN_SIZE[1]);
        }
        return previousOnResize?.call(this, size);
    };
}

function install(node) {
    if (!isOurNode(node) || node.__urnAudioStyleSelectorInstalled) return;
    if (typeof node.addDOMWidget !== "function") return;
    node.__urnAudioStyleSelectorInstalled = true;

    installResizeLimits(node);
    const root = makeUI(node);
    const widget = node.addDOMWidget(UI_WIDGET, UI_WIDGET, root, {
        serialize: false,
        getMinHeight: () => 350,
    });
    widget.serialize = false;
    if (widget.options) widget.options.serialize = false;

    applyInitialSize(node);
    loadCatalog(node);
    setEnabled(node, false);
    node.graph?.setDirtyCanvas?.(true, true);
}

function findNodeById(id) {
    const direct = app.graph?.getNodeById?.(id);
    if (direct) return direct;
    return (app.graph?._nodes || []).find((n) => String(n.id) === String(id)) || null;
}

function handleRequest(event) {
    const detail = event?.detail || {};
    const node = findNodeById(detail.node_id);
    if (!node || !isOurNode(node)) return;
    install(node);
    setWaitingState(node, detail);
}

async function recoverPendingRequests() {
    try {
        const response = await api.fetchApi(PENDING_ROUTE, { method: "GET" });
        if (!response.ok) return;
        const data = await response.json().catch(() => ({}));
        for (const detail of data?.pending || []) {
            const node = findNodeById(detail?.node_id);
            if (!node || !isOurNode(node)) continue;
            install(node);
            if (String(node.__urnStyleSelectorToken || "") !== String(detail?.token || "")) {
                setWaitingState(node, detail);
            }
        }
    } catch (_) {
        // ComfyUI may be starting/restarting; a later poll can recover the active request.
    }
}

function scan() {
    for (const node of app.graph?._nodes || []) install(node);
}

app.registerExtension({
    name: "URN.AudioNodes.StyleSelector",
    nodeCreated(node) { install(node); },
    loadedGraphNode(node) { install(node); },
    setup() {
        api.addEventListener(EVENT_NAME, handleRequest);
        scan();
        recoverPendingRequests();
        setTimeout(() => { scan(); recoverPendingRequests(); }, 250);
        setTimeout(() => { scan(); recoverPendingRequests(); }, 1000);
        if (!globalThis.__urnAudioStyleSelectorPendingPoll) {
            globalThis.__urnAudioStyleSelectorPendingPoll = setInterval(recoverPendingRequests, 500);
        }
    },
});
