import { app } from "../../scripts/app.js";

const NODE_ID = "URNTabbedMarkdown";
const UI_WIDGET = "urnTabbedMarkdownUI";
const STATE_PROP = "urn_tabbed_markdown_state";
const DEFAULT_SIZE = [520, 390];
const MIN_SIZE = [340, 240];
const MAX_TABS = 40;

function isOurNode(node) {
    return node?.comfyClass === NODE_ID || node?.constructor?.comfyClass === NODE_ID;
}

function stopGraphEvent(e) {
    e.stopPropagation();
}

function cleanTitle(value, fallback = "Tab") {
    const text = String(value ?? "").replace(/[\r\n\t]+/g, " ").trim();
    return (text || fallback).slice(0, 80);
}

function defaultState() {
    return {
        active: 0,
        tabs: [{ title: "Tab 1", text: "" }],
    };
}

function normaliseState(raw) {
    const state = raw && typeof raw === "object" ? raw : {};
    const tabs = Array.isArray(state.tabs) ? state.tabs : [];
    const cleaned = [];

    for (let i = 0; i < tabs.length && cleaned.length < MAX_TABS; i++) {
        const tab = tabs[i] && typeof tabs[i] === "object" ? tabs[i] : {};
        cleaned.push({
            title: cleanTitle(tab.title, `Tab ${cleaned.length + 1}`),
            text: String(tab.text ?? ""),
        });
    }

    if (!cleaned.length) cleaned.push({ title: "Tab 1", text: "" });
    let active = Number.isFinite(Number(state.active)) ? Math.trunc(Number(state.active)) : 0;
    active = Math.max(0, Math.min(cleaned.length - 1, active));
    return { active, tabs: cleaned };
}

function parseStoredState(raw) {
    if (typeof raw === "string" && raw.trim()) {
        try {
            return normaliseState(JSON.parse(raw));
        } catch (_) {
            return null;
        }
    }
    if (raw && typeof raw === "object") return normaliseState(raw);
    return null;
}

function loadState(node) {
    // Primary store: node properties.  Fallbacks cover workflows saved by the
    // serialized DOM widget and older frontend configure paths.
    const candidates = [
        node?.properties?.[STATE_PROP],
        node?.widgets_values_named?.[UI_WIDGET],
        Array.isArray(node?.widgets_values) ? node.widgets_values[0] : null,
        node?.widgets?.find?.((w) => w?.name === UI_WIDGET)?.value,
    ];

    for (const stored of candidates) {
        const parsed = parseStoredState(stored);
        if (parsed) return parsed;
    }
    return defaultState();
}

function persistState(node) {
    const ui = node?.__urnTabbedMarkdownUI;
    if (!ui) return;

    // If serialization happens while the textarea still has focus, always copy
    // its live value into the active tab first.
    if (ui.editing) {
        const tab = activeTab(ui);
        if (tab) tab.text = ui.editor.value;
    }

    const value = JSON.stringify(ui.state);
    node.properties ??= {};
    const changed = node.properties[STATE_PROP] !== value;

    if (changed) {
        if (typeof node.setProperty === "function") node.setProperty(STATE_PROP, value);
        else node.properties[STATE_PROP] = value;
    }

    // Keep the workflow-serialized DOM widget in sync as a second persistence
    // path.  Its prompt serialization is disabled, so this never reaches the
    // backend execution payload.
    const widget = node.__urnTabbedMarkdownWidget;
    if (widget && widget.value !== value) widget.value = value;

    if (changed) markGraphDirty(node);
}

function installPersistenceHooks(node) {
    if (node.__urnTabbedMarkdownPersistenceInstalled) return;
    node.__urnTabbedMarkdownPersistenceInstalled = true;

    const previousSerialize = node.onSerialize;
    node.onSerialize = function(serialized) {
        const result = previousSerialize?.call(this, serialized);
        persistState(this);
        const value = this.properties?.[STATE_PROP];
        if (value != null && serialized) {
            serialized.properties ??= {};
            serialized.properties[STATE_PROP] = value;
        }
        return result;
    };

    const previousConfigure = node.onConfigure;
    node.onConfigure = function(info) {
        const result = previousConfigure?.call(this, info);
        const restored =
            parseStoredState(info?.properties?.[STATE_PROP]) ||
            parseStoredState(info?.widgets_values_named?.[UI_WIDGET]) ||
            parseStoredState(Array.isArray(info?.widgets_values) ? info.widgets_values[0] : null);

        if (restored) {
            this.properties ??= {};
            this.properties[STATE_PROP] = JSON.stringify(restored);
            const ui = this.__urnTabbedMarkdownUI;
            if (ui) {
                ui.state = restored;
                if (ui.editing) ui.editor.value = String(activeTab(ui)?.text ?? "");
                renderTabs(this);
                renderPreview(this);
            }
        }
        return result;
    };
}

function escapeHtml(value) {
    return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

function safeUrl(value) {
    const raw = String(value ?? "").trim();
    if (/^(https?:\/\/|mailto:)/i.test(raw)) return escapeHtml(raw);
    return "#";
}

function renderInline(source) {
    let text = escapeHtml(source);
    const codeTokens = [];
    text = text.replace(/`([^`]+)`/g, (_m, code) => {
        const token = `\u0000CODE${codeTokens.length}\u0000`;
        codeTokens.push(`<code>${code}</code>`);
        return token;
    });

    text = text.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_m, label, href) =>
        `<a href="${safeUrl(href)}" target="_blank" rel="noopener noreferrer">${label}</a>`
    );
    text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    text = text.replace(/__([^_]+)__/g, "<strong>$1</strong>");
    text = text.replace(/~~([^~]+)~~/g, "<del>$1</del>");
    text = text.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
    text = text.replace(/(^|[^_])_([^_\n]+)_(?!_)/g, "$1<em>$2</em>");

    text = text.replace(/\u0000CODE(\d+)\u0000/g, (_m, idx) => codeTokens[Number(idx)] || "");
    return text;
}

function isTableSeparator(line) {
    const cells = String(line ?? "").trim().replace(/^\||\|$/g, "").split("|");
    return cells.length >= 2 && cells.every((cell) => /^\s*:?-{3,}:?\s*$/.test(cell));
}

function tableCells(line) {
    return String(line ?? "").trim().replace(/^\||\|$/g, "").split("|").map((x) => x.trim());
}

function renderMarkdown(markdown) {
    const lines = String(markdown ?? "").replace(/\r\n?/g, "\n").split("\n");
    const out = [];
    let i = 0;
    let inCode = false;
    let code = [];
    let codeLang = "";
    let listType = "";

    const closeList = () => {
        if (listType) {
            out.push(`</${listType}>`);
            listType = "";
        }
    };

    while (i < lines.length) {
        const line = lines[i];

        if (/^```/.test(line)) {
            closeList();
            if (!inCode) {
                inCode = true;
                codeLang = line.slice(3).trim();
                code = [];
            } else {
                const cls = codeLang ? ` class="language-${escapeHtml(codeLang)}"` : "";
                out.push(`<pre><code${cls}>${escapeHtml(code.join("\n"))}</code></pre>`);
                inCode = false;
                codeLang = "";
                code = [];
            }
            i++;
            continue;
        }

        if (inCode) {
            code.push(line);
            i++;
            continue;
        }

        if (i + 1 < lines.length && line.includes("|") && isTableSeparator(lines[i + 1])) {
            closeList();
            const headers = tableCells(line);
            i += 2;
            const rows = [];
            while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
                rows.push(tableCells(lines[i]));
                i++;
            }
            out.push("<table><thead><tr>" + headers.map((c) => `<th>${renderInline(c)}</th>`).join("") + "</tr></thead><tbody>");
            for (const row of rows) {
                out.push("<tr>" + headers.map((_h, idx) => `<td>${renderInline(row[idx] ?? "")}</td>`).join("") + "</tr>");
            }
            out.push("</tbody></table>");
            continue;
        }

        if (!line.trim()) {
            closeList();
            out.push("<div class=\"urn-md-gap\"></div>");
            i++;
            continue;
        }

        const heading = /^(#{1,6})\s+(.+)$/.exec(line);
        if (heading) {
            closeList();
            const level = heading[1].length;
            out.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
            i++;
            continue;
        }

        if (/^\s*((-{3,})|(\*{3,})|(_{3,}))\s*$/.test(line)) {
            closeList();
            out.push("<hr>");
            i++;
            continue;
        }

        const quote = /^\s*>\s?(.*)$/.exec(line);
        if (quote) {
            closeList();
            const quoted = [];
            while (i < lines.length) {
                const m = /^\s*>\s?(.*)$/.exec(lines[i]);
                if (!m) break;
                quoted.push(renderInline(m[1]));
                i++;
            }
            out.push(`<blockquote>${quoted.join("<br>")}</blockquote>`);
            continue;
        }

        const ul = /^\s*[-+*]\s+(.+)$/.exec(line);
        if (ul) {
            if (listType && listType !== "ul") closeList();
            if (!listType) {
                listType = "ul";
                out.push("<ul>");
            }
            out.push(`<li>${renderInline(ul[1])}</li>`);
            i++;
            continue;
        }

        const ol = /^\s*\d+[.)]\s+(.+)$/.exec(line);
        if (ol) {
            if (listType && listType !== "ol") closeList();
            if (!listType) {
                listType = "ol";
                out.push("<ol>");
            }
            out.push(`<li>${renderInline(ol[1])}</li>`);
            i++;
            continue;
        }

        closeList();
        const para = [line];
        i++;
        while (i < lines.length && lines[i].trim() && !/^(#{1,6})\s+/.test(lines[i]) && !/^```/.test(lines[i]) && !/^\s*>/.test(lines[i]) && !/^\s*[-+*]\s+/.test(lines[i]) && !/^\s*\d+[.)]\s+/.test(lines[i])) {
            if (i + 1 < lines.length && lines[i].includes("|") && isTableSeparator(lines[i + 1])) break;
            para.push(lines[i]);
            i++;
        }
        out.push(`<p>${para.map(renderInline).join("<br>")}</p>`);
    }

    if (inCode) {
        const cls = codeLang ? ` class="language-${escapeHtml(codeLang)}"` : "";
        out.push(`<pre><code${cls}>${escapeHtml(code.join("\n"))}</code></pre>`);
    }
    closeList();
    return out.join("");
}

function markGraphDirty(node) {
    node.graph?.setDirtyCanvas?.(true, true);
}

function activeTab(ui) {
    return ui.state.tabs[ui.state.active] || ui.state.tabs[0];
}

function renderPreview(node) {
    const ui = node?.__urnTabbedMarkdownUI;
    if (!ui) return;
    const tab = activeTab(ui);
    const text = String(tab?.text ?? "");
    if (!text.trim()) {
        ui.preview.innerHTML = '<div class="urn-md-placeholder">Double-click here to edit this Markdown tab.</div>';
        return;
    }

    // Use ComfyUI's own Markdown renderer whenever it is available.
    // This is the same frontend service used for normal Markdown rendering and
    // gives us proper GFM/CommonMark behaviour (headings, bold/italic, nested
    // lists, tables, links, blockquotes, code, etc.) plus ComfyUI sanitisation.
    try {
        const render = app?.extensionManager?.renderMarkdownToHtml;
        if (typeof render === "function") {
            ui.preview.innerHTML = render.call(app.extensionManager, text);
            return;
        }
    } catch (error) {
        console.warn("[URN Tabbed Markdown] ComfyUI Markdown renderer failed; using fallback renderer.", error);
    }

    // Compatibility fallback for older ComfyUI frontends.
    ui.preview.innerHTML = renderMarkdown(text);
}

function finishEditing(node) {
    const ui = node?.__urnTabbedMarkdownUI;
    if (!ui || !ui.editing) return;
    ui.editing = false;
    const tab = activeTab(ui);
    if (tab) tab.text = ui.editor.value;
    persistState(node);
    ui.editor.style.display = "none";
    ui.preview.style.display = "block";
    renderPreview(node);
    ui.hint.textContent = "Double-click note to edit • Ctrl+Enter finishes editing • Double-click a tab to rename";
    markGraphDirty(node);
}

function beginEditing(node) {
    const ui = node?.__urnTabbedMarkdownUI;
    if (!ui || ui.editing) return;
    ui.editing = true;
    ui.editor.value = String(activeTab(ui)?.text ?? "");
    ui.preview.style.display = "none";
    ui.editor.style.display = "block";
    ui.hint.textContent = "Editing Markdown • Ctrl+Enter or click outside the editor to return to preview";
    requestAnimationFrame(() => {
        try { ui.editor.focus({ preventScroll: true }); } catch (_) { ui.editor.focus(); }
    });
}

function renderTabs(node) {
    const ui = node?.__urnTabbedMarkdownUI;
    if (!ui) return;
    ui.tabs.replaceChildren();

    ui.state.tabs.forEach((tab, idx) => {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = tab.title;
        button.title = "Click to open. Double-click to rename.";
        Object.assign(button.style, {
            border: idx === ui.state.active ? "1px solid rgba(255,255,255,.48)" : "1px solid rgba(255,255,255,.22)",
            borderRadius: "7px",
            padding: "4px 11px",
            height: "30px",
            flex: "0 0 auto",
            width: "max-content",
            maxWidth: "none",
            overflow: "visible",
            textOverflow: "clip",
            whiteSpace: "nowrap",
            background: idx === ui.state.active ? "#8e8168" : "#54452a",
            color: "#ffffff",
            fontFamily: "inherit",
            fontSize: "13px",
            cursor: "pointer",
        });
        for (const evt of ["pointerdown", "mousedown", "mouseup", "wheel"]) button.addEventListener(evt, stopGraphEvent);
        button.addEventListener("click", (e) => {
            e.preventDefault();
            e.stopPropagation();
            if (idx === ui.state.active) return;
            if (ui.editing) finishEditing(node);
            ui.state.active = idx;
            persistState(node);
            renderTabs(node);
            renderPreview(node);
            markGraphDirty(node);
        });
        button.addEventListener("dblclick", (e) => {
            e.preventDefault();
            e.stopPropagation();
            const wanted = window.prompt("Rename Markdown tab", tab.title);
            if (wanted === null) return;
            tab.title = cleanTitle(wanted, tab.title || `Tab ${idx + 1}`);
            persistState(node);
            renderTabs(node);
            markGraphDirty(node);
        });
        ui.tabs.appendChild(button);
    });

    ui.remove.disabled = ui.state.tabs.length <= 1;
    ui.remove.style.opacity = ui.remove.disabled ? ".42" : "1";
    ui.add.disabled = ui.state.tabs.length >= MAX_TABS;
    ui.add.style.opacity = ui.add.disabled ? ".42" : "1";
}

function addTab(node) {
    const ui = node?.__urnTabbedMarkdownUI;
    if (!ui || ui.state.tabs.length >= MAX_TABS) return;
    if (ui.editing) finishEditing(node);
    let n = ui.state.tabs.length + 1;
    const used = new Set(ui.state.tabs.map((t) => t.title.toLocaleLowerCase()));
    while (used.has(`tab ${n}`.toLocaleLowerCase())) n++;
    ui.state.tabs.push({ title: `Tab ${n}`, text: "" });
    ui.state.active = ui.state.tabs.length - 1;
    persistState(node);
    renderTabs(node);
    renderPreview(node);
    markGraphDirty(node);
}

function removeTab(node) {
    const ui = node?.__urnTabbedMarkdownUI;
    if (!ui || ui.state.tabs.length <= 1) return;
    if (ui.editing) finishEditing(node);
    ui.state.tabs.splice(ui.state.active, 1);
    ui.state.active = Math.max(0, Math.min(ui.state.tabs.length - 1, ui.state.active));
    persistState(node);
    renderTabs(node);
    renderPreview(node);
    markGraphDirty(node);
}

function makeSmallButton(label, title) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.title = title;
    Object.assign(button.style, {
        width: "30px",
        height: "30px",
        minWidth: "30px",
        border: "1px solid rgba(255,255,255,.28)",
        borderRadius: "7px",
        background: "#54452a",
        color: "#ffffff",
        fontFamily: "inherit",
        fontSize: "18px",
        lineHeight: "24px",
        cursor: "pointer",
        padding: "0",
    });
    for (const evt of ["pointerdown", "mousedown", "mouseup", "dblclick", "wheel"]) button.addEventListener(evt, stopGraphEvent);
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
        padding: "8px",
        display: "flex",
        flexDirection: "column",
        gap: "7px",
        userSelect: "none",
        overflow: "hidden",
        background: "#665533",
        color: "#ffffff",
    });

    const bar = document.createElement("div");
    Object.assign(bar.style, {
        display: "flex",
        alignItems: "center",
        gap: "8px",
        flex: "0 0 auto",
        minHeight: "32px",
    });

    const tabs = document.createElement("div");
    Object.assign(tabs.style, {
        display: "flex",
        alignItems: "center",
        flexWrap: "wrap",
        gap: "7px",
        flex: "1 1 auto",
        minWidth: "0",
    });

    const controls = document.createElement("div");
    Object.assign(controls.style, {
        display: "flex",
        alignItems: "center",
        gap: "6px",
        flex: "0 0 auto",
        alignSelf: "flex-start",
    });
    const add = makeSmallButton("+", "Add Markdown tab");
    const remove = makeSmallButton("−", "Remove current Markdown tab");
    controls.append(add, remove);
    bar.append(tabs, controls);

    const panel = document.createElement("div");
    Object.assign(panel.style, {
        position: "relative",
        flex: "1 1 auto",
        minHeight: "140px",
        minWidth: "0",
        overflow: "hidden",
        border: "1px solid rgba(255,255,255,.18)",
        borderRadius: "8px",
        background: "#665533",
    });

    const preview = document.createElement("div");
    preview.className = "urn-tabbed-markdown-preview";
    Object.assign(preview.style, {
        position: "absolute",
        inset: "0",
        overflow: "auto",
        padding: "13px 15px",
        boxSizing: "border-box",
        userSelect: "text",
        color: "#eeeeee",
        fontFamily: "system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
        fontSize: "13px",
        lineHeight: "1.5",
        scrollbarColor: "#9e937d #54452a",
    });

    const editor = document.createElement("textarea");
    editor.spellcheck = true;
    editor.wrap = "soft";
    editor.placeholder = "Type Markdown here...";
    Object.assign(editor.style, {
        position: "absolute",
        inset: "0",
        display: "none",
        width: "100%",
        height: "100%",
        resize: "none",
        border: "0",
        outline: "none",
        padding: "13px 15px",
        boxSizing: "border-box",
        background: "#54452a",
        color: "#f3f3f3",
        fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
        fontSize: "13px",
        lineHeight: "1.5",
        userSelect: "text",
        overflow: "auto",
        scrollbarColor: "#9e937d #54452a",
    });

    const hint = document.createElement("div");
    hint.textContent = "Double-click note to edit • Ctrl+Enter finishes editing • Double-click a tab to rename";
    Object.assign(hint.style, {
        minHeight: "15px",
        flex: "0 0 15px",
        color: "rgba(255,255,255,.72)",
        fontSize: "10px",
        lineHeight: "15px",
        textAlign: "center",
        whiteSpace: "nowrap",
        overflow: "hidden",
        textOverflow: "ellipsis",
    });

    panel.append(preview, editor);
    root.append(bar, panel, hint);

    // Scoped Markdown styling. No raw HTML from the note is ever inserted; the
    // renderer escapes the source before applying its supported Markdown syntax.
    const style = document.createElement("style");
    style.textContent = `
        .urn-tabbed-markdown-preview h1,.urn-tabbed-markdown-preview h2,.urn-tabbed-markdown-preview h3,
        .urn-tabbed-markdown-preview h4,.urn-tabbed-markdown-preview h5,.urn-tabbed-markdown-preview h6 {
            color:#f5f5f5; margin:.15em 0 .45em; line-height:1.22; font-weight:700;
        }
        .urn-tabbed-markdown-preview h1{font-size:1.55em}.urn-tabbed-markdown-preview h2{font-size:1.35em}
        .urn-tabbed-markdown-preview h3{font-size:1.18em}.urn-tabbed-markdown-preview h4{font-size:1.05em}
        .urn-tabbed-markdown-preview p{margin:.35em 0 .7em}.urn-tabbed-markdown-preview ul,.urn-tabbed-markdown-preview ol{margin:.3em 0 .75em;padding-left:1.7em}
        .urn-tabbed-markdown-preview li{margin:.14em 0}.urn-tabbed-markdown-preview strong{color:#ffffff}.urn-tabbed-markdown-preview em{color:#f1f1f1}
        .urn-tabbed-markdown-preview code{background:#443322;border:1px solid #8e8168;border-radius:4px;padding:.1em .32em;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
        .urn-tabbed-markdown-preview pre{background:#443322;border:1px solid #8e8168;border-radius:6px;padding:10px;overflow:auto}
        .urn-tabbed-markdown-preview pre code{border:0;padding:0;background:transparent;white-space:pre}
        .urn-tabbed-markdown-preview blockquote{margin:.55em 0;padding:.25em .8em;border-left:3px solid #b4ac9b;color:#f0ede7;background:rgba(68,51,34,.34)}
        .urn-tabbed-markdown-preview hr{border:0;border-top:1px solid #8e8168;margin:1em 0}.urn-tabbed-markdown-preview a{color:#d7d2c9;text-decoration:underline}
        .urn-tabbed-markdown-preview table{border-collapse:collapse;width:100%;margin:.6em 0}.urn-tabbed-markdown-preview th,.urn-tabbed-markdown-preview td{border:1px solid #8e8168;padding:5px 7px;text-align:left}
        .urn-tabbed-markdown-preview th{background:#54452a;color:#fff}.urn-md-gap{height:.25em}.urn-md-placeholder{color:rgba(255,255,255,.62);font-style:italic;padding-top:4px}
    `;
    root.appendChild(style);

    node.__urnTabbedMarkdownUI = {
        root,
        bar,
        tabs,
        controls,
        add,
        remove,
        panel,
        preview,
        editor,
        hint,
        state: loadState(node),
        editing: false,
        saveTimer: null,
    };

    add.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); addTab(node); });
    remove.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); removeTab(node); });

    preview.addEventListener("dblclick", (e) => {
        e.preventDefault();
        e.stopPropagation();
        beginEditing(node);
    });
    for (const evt of ["pointerdown", "mousedown", "mouseup", "wheel"]) preview.addEventListener(evt, stopGraphEvent);

    for (const evt of ["pointerdown", "mousedown", "mouseup", "wheel", "keydown", "keyup", "keypress"]) editor.addEventListener(evt, stopGraphEvent);
    editor.addEventListener("input", () => {
        const ui = node.__urnTabbedMarkdownUI;
        const tab = activeTab(ui);
        if (tab) tab.text = editor.value;

        // Persist immediately.  This mirrors normal ComfyUI widgets: typing in a
        // note marks the workflow dirty straight away, so browser refresh/workflow
        // save cannot lose the last edit because a debounce timer had not fired.
        persistState(node);
        markGraphDirty(node);
    });
    editor.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            e.stopPropagation();
            finishEditing(node);
        }
    });
    editor.addEventListener("blur", () => {
        // Delay so toolbar/tab clicks can finish cleanly before the preview swaps back.
        setTimeout(() => {
            const ui = node.__urnTabbedMarkdownUI;
            if (ui?.editing && document.activeElement !== editor) finishEditing(node);
        }, 0);
    });

    renderTabs(node);
    renderPreview(node);
    return root;
}

function applySize(node) {
    node.min_size = [MIN_SIZE[0], MIN_SIZE[1]];
    node.properties ??= {};
    if (!node.properties.__urnTabbedMarkdownInitialSizeApplied) {
        const w = Number(node.size?.[0]) || 0;
        const h = Number(node.size?.[1]) || 0;
        node.setSize?.([Math.max(w, DEFAULT_SIZE[0]), Math.max(h, DEFAULT_SIZE[1])]);
        node.properties.__urnTabbedMarkdownInitialSizeApplied = true;
    }

    if (!node.__urnTabbedMarkdownResizeInstalled) {
        node.__urnTabbedMarkdownResizeInstalled = true;
        const previous = node.onResize;
        node.onResize = function(size) {
            if (size?.length >= 2) {
                size[0] = Math.max(MIN_SIZE[0], Number(size[0]) || MIN_SIZE[0]);
                size[1] = Math.max(MIN_SIZE[1], Number(size[1]) || MIN_SIZE[1]);
            }
            return previous?.call(this, size);
        };
    }
}

function install(node) {
    if (!isOurNode(node) || node.__urnTabbedMarkdownInstalled) return;
    if (typeof node.addDOMWidget !== "function") return;
    node.__urnTabbedMarkdownInstalled = true;

    // Match ComfyUI's built-in Markdown Note brown/tan palette.
    node.color = "#443322";
    node.bgcolor = "#665533";

    installPersistenceHooks(node);
    applySize(node);
    const root = makeUI(node);
    const widget = node.addDOMWidget(UI_WIDGET, UI_WIDGET, root, {
        // This is the prompt/API serialization flag.  The node is display-only,
        // so its note data must never be sent to the backend prompt.
        serialize: false,
        getMinHeight: () => 240,
    });

    // IMPORTANT: widget.serialize controls WORKFLOW persistence and is separate
    // from widget.options.serialize above.  V9.78 set both to false, which meant
    // ComfyUI deliberately omitted the note from workflow JSON.
    widget.serialize = true;
    if (widget.options) widget.options.serialize = false;
    widget.value = JSON.stringify(node.__urnTabbedMarkdownUI.state);
    node.__urnTabbedMarkdownWidget = widget;
    node.serialize_widgets = true;

    // Sync both the property and serialized widget immediately so a newly loaded
    // workflow has identical state regardless of which persistence path ComfyUI
    // restores first.
    persistState(node);
    markGraphDirty(node);
}

function scan() {
    for (const node of app.graph?._nodes || []) install(node);
}

app.registerExtension({
    name: "URN.AudioNodes.TabbedMarkdown",
    nodeCreated(node) { install(node); },
    loadedGraphNode(node) { install(node); },
    setup() {
        scan();
        setTimeout(scan, 250);
        setTimeout(scan, 1000);
    },
});
