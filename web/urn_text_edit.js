import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_ID = "URNTextEdit";
const EVENT_NAME = "urn_audio_nodes.text_edit.request";
const ACCEPT_ROUTE = "/urn_audio_nodes/text_edit/accept";
const PENDING_ROUTE = "/urn_audio_nodes/text_edit/pending";
const UI_WIDGET = "urnTextEditUI";
const DEFAULT_SIZE = [410, 540];
const MIN_SIZE = [205, 270];

function isOurNode(node) {
    return node?.comfyClass === NODE_ID || node?.constructor?.comfyClass === NODE_ID;
}

function stopGraphEvent(e) {
    e.stopPropagation();
}

function setWaitingState(node, detail) {
    if (!node?.__urnTextEditUI) return;
    const ui = node.__urnTextEditUI;
    node.__urnTextEditToken = String(detail?.token || "");
    ui.textarea.value = String(detail?.text ?? "");
    ui.textarea.disabled = false;
    ui.button.disabled = !node.__urnTextEditToken;
    ui.button.textContent = "Accept Changes";
    ui.button.classList.add("ready");
    ui.status.textContent = "Workflow paused — edit the text, then accept changes.";
    ui.status.classList.add("active");
    requestAnimationFrame(() => {
        try { ui.textarea.focus({ preventScroll: true }); } catch (_) {}
    });
    node.graph?.setDirtyCanvas?.(true, true);
}

async function acceptChanges(node) {
    const ui = node?.__urnTextEditUI;
    const token = String(node?.__urnTextEditToken || "");
    if (!ui || !token) return;

    ui.button.disabled = true;
    ui.button.textContent = "Accepting...";
    ui.button.classList.remove("ready");

    try {
        const response = await api.fetchApi(ACCEPT_ROUTE, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                token,
                node_id: String(node.id),
                text: ui.textarea.value,
            }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || data?.ok === false) {
            throw new Error(data?.error || `HTTP ${response.status}`);
        }

        node.__urnTextEditToken = "";
        ui.button.textContent = "Accepted";
        ui.status.textContent = "Changes accepted — workflow continuing.";
        ui.status.classList.remove("active");
        setTimeout(() => {
            if (!node.__urnTextEditToken) {
                ui.button.textContent = "Accept Changes";
                ui.button.disabled = true;
                ui.status.textContent = "Waiting for workflow...";
            }
        }, 900);
    } catch (error) {
        console.error("[URN Text Edit] Accept failed", error);
        ui.button.disabled = false;
        ui.button.textContent = "Accept Changes";
        ui.button.classList.add("ready");
        ui.status.textContent = `Could not accept changes: ${error?.message || error}`;
        ui.status.classList.add("active");
    }
}


function setUtilityStatus(node, message, isError = false) {
    const ui = node?.__urnTextEditUI;
    if (!ui) return;
    ui.status.textContent = String(message || "");
    ui.status.classList.toggle("active", !!node?.__urnTextEditToken || isError);
    ui.status.style.color = isError ? "#ff9d9d" : "rgba(255,255,255,.66)";
}

function txtPickerTypes() {
    return [{
        description: "Text files",
        accept: { "text/plain": [".txt"] },
    }];
}

async function saveChangesToTextFile(node) {
    const ui = node?.__urnTextEditUI;
    if (!ui) return;
    const text = String(ui.textarea.value ?? "");

    try {
        if (typeof window.showSaveFilePicker === "function") {
            const handle = await window.showSaveFilePicker({
                suggestedName: "lyrics.txt",
                types: txtPickerTypes(),
                excludeAcceptAllOption: true,
            });

            if (!String(handle?.name || "").toLowerCase().endsWith(".txt")) {
                setUtilityStatus(node, "Save cancelled: the file must use the .txt extension.", true);
                return;
            }

            const writable = await handle.createWritable();
            await writable.write(text);
            await writable.close();
            setUtilityStatus(
                node,
                `Saved ${handle.name}${node.__urnTextEditToken ? " — workflow still paused." : "."}`,
            );
            return;
        }

        // Fallback for browsers without the File System Access API.  This still
        // saves a real .txt file, but the browser controls the download location.
        const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = "lyrics.txt";
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1500);
        setUtilityStatus(
            node,
            `Saved lyrics.txt using the browser download${node.__urnTextEditToken ? " — workflow still paused." : "."}`,
        );
    } catch (error) {
        if (error?.name === "AbortError") return;
        console.error("[URN Text Edit] Save Changes failed", error);
        setUtilityStatus(node, `Could not save text: ${error?.message || error}`, true);
    }
}

async function loadExistingTextFile(node) {
    const ui = node?.__urnTextEditUI;
    if (!ui) return;

    const applyFile = async (file) => {
        if (!file) return;
        if (!String(file.name || "").toLowerCase().endsWith(".txt")) {
            setUtilityStatus(node, "Load cancelled: please choose a .txt file.", true);
            return;
        }
        const text = await file.text();
        ui.textarea.value = text;
        setUtilityStatus(
            node,
            `Loaded ${file.name}${node.__urnTextEditToken ? " — click Accept Changes when ready." : "."}`,
        );
        try { ui.textarea.focus({ preventScroll: true }); } catch (_) {}
        node.graph?.setDirtyCanvas?.(true, true);
    };

    try {
        if (typeof window.showOpenFilePicker === "function") {
            const handles = await window.showOpenFilePicker({
                multiple: false,
                types: txtPickerTypes(),
                excludeAcceptAllOption: true,
            });
            if (!handles?.length) return;
            await applyFile(await handles[0].getFile());
            return;
        }

        // Fallback for browsers without showOpenFilePicker.
        const input = document.createElement("input");
        input.type = "file";
        input.accept = ".txt,text/plain";
        input.style.display = "none";
        document.body.appendChild(input);
        input.addEventListener("change", async () => {
            try {
                await applyFile(input.files?.[0]);
            } catch (error) {
                console.error("[URN Text Edit] Load Existing failed", error);
                setUtilityStatus(node, `Could not load text: ${error?.message || error}`, true);
            } finally {
                input.remove();
            }
        }, { once: true });
        input.click();
    } catch (error) {
        if (error?.name === "AbortError") return;
        console.error("[URN Text Edit] Load Existing failed", error);
        setUtilityStatus(node, `Could not load text: ${error?.message || error}`, true);
    }
}

function makeUI(node) {
    const root = document.createElement("div");
    root.className = "urn-text-edit-root";
    Object.assign(root.style, {
        width: "100%",
        height: "100%",
        minWidth: "0",
        minHeight: "0",
        boxSizing: "border-box",
        padding: "8px 8px 7px",
        display: "flex",
        flexDirection: "column",
        gap: "7px",
        userSelect: "none",
    });

    const textarea = document.createElement("textarea");
    textarea.placeholder = "Waiting for workflow...";
    textarea.spellcheck = false;
    textarea.wrap = "soft";
    Object.assign(textarea.style, {
        boxSizing: "border-box",
        width: "100%",
        minWidth: "0",
        flex: "1 1 auto",
        minHeight: "120px",
        resize: "none",
        border: "1px solid rgba(255,255,255,.20)",
        borderRadius: "7px",
        outline: "none",
        padding: "12px 12px",
        background: "rgba(9,9,10,.78)",
        color: "#f2f2f2",
        fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
        fontSize: "13px",
        lineHeight: "1.45",
        whiteSpace: "pre-wrap",
        overflow: "auto",
        userSelect: "text",
        scrollbarColor: "#777 rgba(255,255,255,.05)",
    });

    const status = document.createElement("div");
    status.textContent = "Waiting for workflow...";
    Object.assign(status.style, {
        minWidth: "0",
        minHeight: "16px",
        padding: "0 3px",
        color: "rgba(255,255,255,.58)",
        fontSize: "11px",
        lineHeight: "16px",
        textAlign: "center",
        overflow: "hidden",
        textOverflow: "ellipsis",
        whiteSpace: "nowrap",
    });

    const buttonRow = document.createElement("div");
    Object.assign(buttonRow.style, {
        width: "100%",
        minWidth: "0",
        height: "28px",
        flex: "0 0 28px",
        display: "flex",
        gap: "6px",
    });

    const makeActionButton = (label) => {
        const b = document.createElement("button");
        b.type = "button";
        b.textContent = label;
        Object.assign(b.style, {
            boxSizing: "border-box",
            minWidth: "0",
            height: "28px",
            flex: "1 1 0",
            border: "1px solid rgba(255,255,255,.22)",
            borderRadius: "9px",
            background: "rgba(155,155,155,.66)",
            color: "#fff",
            fontFamily: "inherit",
            fontSize: "12px",
            fontWeight: "500",
            cursor: "pointer",
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
        });
        return b;
    };

    const saveButton = makeActionButton("Save Changes");
    const loadButton = makeActionButton("Load Existing");
    const button = makeActionButton("Accept Changes");
    button.disabled = true;
    button.style.flex = "1.08 1 0";

    const refreshButton = () => {
        if (button.disabled) {
            button.style.opacity = ".72";
            button.style.cursor = "default";
            button.style.background = "rgba(190,190,190,.55)";
        } else {
            button.style.opacity = "1";
            button.style.cursor = "pointer";
            button.style.background = "rgba(180,180,180,.72)";
        }
    };
    const observer = new MutationObserver(refreshButton);
    observer.observe(button, { attributes: true, attributeFilter: ["disabled"] });
    refreshButton();

    saveButton.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        saveChangesToTextFile(node);
    });

    loadButton.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        loadExistingTextFile(node);
    });

    button.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        acceptChanges(node);
    });

    for (const evt of ["pointerdown", "mousedown", "mouseup", "dblclick", "wheel", "keydown", "keyup"]) {
        textarea.addEventListener(evt, stopGraphEvent);
    }
    for (const control of [saveButton, loadButton, button]) {
        for (const evt of ["pointerdown", "mousedown", "mouseup", "dblclick", "wheel"]) {
            control.addEventListener(evt, stopGraphEvent);
        }
    }

    buttonRow.append(saveButton, loadButton, button);
    root.append(textarea, status, buttonRow);
    node.__urnTextEditUI = {
        root,
        textarea,
        status,
        button,
        saveButton,
        loadButton,
        buttonRow,
        observer,
    };
    return root;
}

function applyInitialSize(node) {
    node.properties ??= {};
    if (node.properties.__urnTextEditInitialSizeApplied) return;

    const w = Number(node.size?.[0]) || 0;
    const h = Number(node.size?.[1]) || 0;
    node.setSize?.([
        Math.max(w, DEFAULT_SIZE[0]),
        Math.max(h, DEFAULT_SIZE[1]),
    ]);
    node.properties.__urnTextEditInitialSizeApplied = true;
}

function installResizeLimits(node) {
    if (node.__urnTextEditResizeLimitsInstalled) return;
    node.__urnTextEditResizeLimitsInstalled = true;

    // LiteGraph/ComfyUI builds differ in how they honour minimum node sizes.
    // Set the conventional property and also clamp in onResize so the editor
    // can shrink to roughly half its historical size, but not collapse away.
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
    if (!isOurNode(node) || node.__urnTextEditInstalled) return;
    if (typeof node.addDOMWidget !== "function") return;
    node.__urnTextEditInstalled = true;

    installResizeLimits(node);

    const root = makeUI(node);
    const widget = node.addDOMWidget(UI_WIDGET, UI_WIDGET, root, {
        serialize: false,
        getMinHeight: () => 195,
    });
    widget.serialize = false;
    if (widget.options) widget.options.serialize = false;

    // Keep the familiar initial size for a newly-created/legacy node, but only
    // apply it once. After that, the user's resized dimensions are respected
    // across workflow saves/reloads down to MIN_SIZE.
    applyInitialSize(node);
    node.graph?.setDirtyCanvas?.(true, true);
}

function findNodeById(id) {
    const direct = app.graph?.getNodeById?.(id);
    if (direct) return direct;
    return (app.graph?._nodes || []).find((n) => String(n.id) === String(id)) || null;
}

function handleEditRequest(event) {
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

            // Do not overwrite text the user is already editing for the same
            // request.  Only attach when this is a new/missed backend request.
            if (String(node.__urnTextEditToken || "") !== String(detail?.token || "")) {
                setWaitingState(node, detail);
            }
        }
    } catch (_) {
        // ComfyUI may be starting/restarting; the next poll will recover.
    }
}

function scan() {
    for (const node of app.graph?._nodes || []) install(node);
}

app.registerExtension({
    name: "URN.AudioNodes.TextEdit.Interactive",
    nodeCreated(node) { install(node); },
    loadedGraphNode(node) { install(node); },
    setup() {
        api.addEventListener(EVENT_NAME, handleEditRequest);
        scan();
        recoverPendingRequests();
        setTimeout(() => { scan(); recoverPendingRequests(); }, 250);
        setTimeout(() => { scan(); recoverPendingRequests(); }, 1000);

        // Websocket delivery is normally immediate.  This lightweight recovery
        // poll makes the interactive pause robust across frontend reconnects,
        // browser timing races, and ComfyUI builds where a custom event can be
        // missed during graph/layout initialization.
        if (!globalThis.__urnTextEditPendingPoll) {
            globalThis.__urnTextEditPendingPoll = setInterval(recoverPendingRequests, 500);
        }
    },
});
