import { app } from "../../scripts/app.js";

const TARGET = "URNAudioChannel";
const UI_WIDGET = "urnAudioChannelVisualUI";
const DEFAULT_NODE_SIZE = [500, 540];

const BACKEND_WIDGETS = [
    "left_volume", "right_volume", "swap_lr", "pan", "auto_pan", "pan_curve", "prevent_clipping",
];

function isOurNode(node) {
    return !!node && (
        node.comfyClass === TARGET || node.type === TARGET ||
        node.constructor?.comfyClass === TARGET || node.constructor?.type === TARGET
    );
}
function widget(node, name) { return node?.widgets?.find?.((w) => w?.name === name) ?? null; }
function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, Number(v) || 0)); }
function stopGraphEvent(e) { e.stopPropagation(); }

function hideBackendWidget(node, name) {
    const w = widget(node, name);
    if (!w || w.__urnChannelHidden) return;
    w.__urnChannelHidden = true;
    // Hide visually only. Keep the real widget serializable for workflows/prompts.
    w.computeSize = () => [0, -4];
    w.hidden = true;
}

function getValue(node, name, fallback) {
    const w = widget(node, name);
    return w == null ? fallback : w.value;
}
function setValue(node, name, value) {
    const w = widget(node, name);
    if (!w) return;
    if (w.value === value) return;
    w.value = value;
    try { w.callback?.(value, node, w); } catch (_) {}
    node.graph?.setDirtyCanvas?.(true, true);
    node.setDirtyCanvas?.(true, true);
    refresh(node);
}

function el(tag, className = "", text = "") {
    const x = document.createElement(tag);
    if (className) x.className = className;
    if (text) x.textContent = text;
    return x;
}

function sliderRow(labelText, min, max, step, value, suffix, onInput) {
    const row = el("div", "urnch-slider-row");
    const label = el("div", "urnch-slider-label", labelText);
    const range = document.createElement("input");
    range.type = "range";
    range.min = String(min);
    range.max = String(max);
    range.step = String(step);
    range.value = String(value);
    range.className = "urnch-range";

    const valueWrap = el("div", "urnch-value-wrap");
    const number = document.createElement("input");
    number.type = "number";
    number.min = String(min);
    number.max = String(max);
    number.step = String(step);
    number.value = String(value);
    number.className = "urnch-number";
    const unit = el("span", "urnch-unit", suffix);
    valueWrap.append(number, unit);
    row.append(label, range, valueWrap);

    const update = (raw) => {
        const v = clamp(raw, min, max);
        range.value = String(v);
        number.value = String(Math.round(v));
        onInput(v);
    };
    range.addEventListener("input", () => update(range.value));
    number.addEventListener("change", () => update(number.value));
    number.addEventListener("input", () => {
        const n = Number(number.value);
        if (Number.isFinite(n)) range.value = String(clamp(n, min, max));
    });
    return { row, range, number };
}

function segmented(options, current, onPick) {
    const wrap = el("div", "urnch-segmented");
    const buttons = [];
    for (const option of options) {
        const b = el("button", "urnch-seg", option);
        b.type = "button";
        b.dataset.value = option;
        b.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); onPick(option); });
        wrap.appendChild(b);
        buttons.push(b);
    }
    const set = (v) => buttons.forEach((b) => b.classList.toggle("active", b.dataset.value === v));
    set(current);
    return { wrap, buttons, set };
}

function toggleButton(label, initial, onToggle) {
    const b = el("button", "urnch-toggle");
    b.type = "button";
    const dot = el("span", "urnch-toggle-dot");
    const txt = el("span", "urnch-toggle-text", label);
    b.append(dot, txt);
    let current = !!initial;
    const set = (v) => {
        current = !!v;
        b.classList.toggle("active", current);
        b.setAttribute("aria-pressed", current ? "true" : "false");
    };
    set(current);
    b.addEventListener("click", (e) => {
        e.preventDefault(); e.stopPropagation();
        set(!current);
        onToggle(current);
    });
    return { button: b, set };
}

function panText(value) {
    const p = Math.round(clamp(value, -100, 100));
    if (Math.abs(p) < 1) return "CENTER";
    return `${Math.abs(p)}% ${p < 0 ? "LEFT" : "RIGHT"}`;
}

function setStagePan(node, value) {
    if (String(getValue(node, "auto_pan", "Off")) !== "Off") return;
    setValue(node, "pan", Math.round(clamp(value, -100, 100)));
}

function buildUI(node) {
    const root = el("div", "urnch-root");
    root.innerHTML = `
<style>
.urnch-root{box-sizing:border-box;width:100%;padding:7px 8px 9px;font-family:Arial,sans-serif;color:var(--fg-color,#ddd);user-select:none}
.urnch-root *{box-sizing:border-box}
.urnch-panel{background:rgba(10,10,12,.34);border:1px solid rgba(255,255,255,.13);border-radius:9px;padding:8px;margin-bottom:7px;box-shadow:inset 0 1px 0 rgba(255,255,255,.025)}
.urnch-section-title{font-size:12px;font-weight:700;letter-spacing:.65px;color:rgba(245,245,245,.76);text-transform:uppercase;margin-bottom:7px}
.urnch-stage{position:relative;height:91px;border-radius:8px;background:linear-gradient(180deg,rgba(255,255,255,.035),rgba(0,0,0,.13));overflow:hidden;cursor:ew-resize}
.urnch-stage.disabled{cursor:default}
.urnch-stage-line{position:absolute;left:38px;right:38px;top:47px;height:3px;border-radius:2px;background:linear-gradient(90deg,rgba(96,160,255,.95),rgba(180,180,180,.52) 50%,rgba(255,142,79,.95))}
.urnch-centre-mark{position:absolute;top:33px;left:50%;width:1px;height:29px;background:rgba(255,255,255,.28)}
.urnch-side{position:absolute;top:29px;width:29px;height:29px;border:1px solid rgba(255,255,255,.27);border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:800;background:rgba(0,0,0,.34)}
.urnch-side.left{left:5px;color:#9dc6ff}.urnch-side.right{right:5px;color:#ffb28c}
.urnch-pan-handle{position:absolute;top:35px;width:25px;height:25px;margin-left:-12.5px;border-radius:50%;background:#ff9d28;border:3px solid rgba(255,255,255,.86);box-shadow:0 0 0 2px rgba(255,157,40,.26),0 3px 9px rgba(0,0,0,.52);transition:box-shadow .12s}
.urnch-stage:not(.disabled):hover .urnch-pan-handle{box-shadow:0 0 0 4px rgba(255,157,40,.24),0 3px 10px rgba(0,0,0,.58)}
.urnch-pan-readout{position:absolute;left:50%;transform:translateX(-50%);bottom:7px;font-size:12px;font-weight:800;color:#ffc16e;white-space:nowrap}
.urnch-auto-arrow{position:absolute;left:45px;right:45px;top:18px;height:20px;display:none;color:#ffab46;font-size:12px;font-weight:800;text-align:center;letter-spacing:.45px}
.urnch-auto-arrow.show{display:block}.urnch-auto-arrow .arrow{font-size:18px;vertical-align:-1px;margin:0 5px}
.urnch-slider-row{display:grid;grid-template-columns:93px 1fr 74px;align-items:center;gap:8px;min-height:34px}
.urnch-slider-label{font-size:12px;font-weight:700;color:rgba(245,245,245,.86)}
.urnch-range{width:100%;accent-color:#ff9d28;cursor:pointer}
.urnch-value-wrap{height:27px;display:flex;align-items:center;border:1px solid rgba(255,255,255,.17);background:rgba(0,0,0,.28);border-radius:6px;padding:0 6px}
.urnch-number{width:46px;border:0!important;outline:0!important;background:transparent!important;color:#ffd18d!important;text-align:right;font-size:12px;font-weight:800;padding:0!important}
.urnch-unit{font-size:11px;color:rgba(255,255,255,.58);margin-left:2px}
.urnch-grid{display:grid;grid-template-columns:1fr 1fr;gap:7px}
.urnch-group{background:rgba(255,255,255,.025);border-radius:7px;padding:7px}
.urnch-group-label{font-size:11px;font-weight:700;color:rgba(255,255,255,.58);margin-bottom:6px;text-transform:uppercase;letter-spacing:.55px}
.urnch-segmented{display:flex;gap:4px}
.urnch-seg{flex:1;min-width:0;height:29px;border:1px solid rgba(255,255,255,.15);border-radius:6px;background:rgba(255,255,255,.045);color:rgba(240,240,240,.77);font-size:11px;font-weight:700;cursor:pointer;padding:0 5px}
.urnch-seg:hover{background:rgba(255,255,255,.08)}.urnch-seg.active{border-color:#ff9d28;background:rgba(255,157,40,.16);color:#ffd59d}
.urnch-segmented.disabled{opacity:.42;pointer-events:none}
.urnch-actions{display:grid;grid-template-columns:1fr 1.25fr 70px;gap:6px}
.urnch-toggle,.urnch-reset{height:31px;border:1px solid rgba(255,255,255,.15);border-radius:7px;background:rgba(255,255,255,.045);color:rgba(240,240,240,.8);font-size:11px;font-weight:700;cursor:pointer}
.urnch-toggle{display:flex;align-items:center;justify-content:center;gap:7px}.urnch-toggle-dot{width:9px;height:9px;border-radius:50%;background:#666;box-shadow:0 0 0 2px rgba(255,255,255,.08)}
.urnch-toggle.active{border-color:rgba(255,157,40,.68);background:rgba(255,157,40,.12);color:#ffd49a}.urnch-toggle.active .urnch-toggle-dot{background:#ff9d28;box-shadow:0 0 0 2px rgba(255,157,40,.18)}
.urnch-reset:hover,.urnch-toggle:hover{background:rgba(255,255,255,.08)}
.urnch-foot{font-size:10px;color:rgba(255,255,255,.46);text-align:center;padding-top:1px}
</style>`;

    const stagePanel = el("div", "urnch-panel");
    stagePanel.appendChild(el("div", "urnch-section-title", "Stereo Position"));
    const stage = el("div", "urnch-stage");
    const line = el("div", "urnch-stage-line");
    const center = el("div", "urnch-centre-mark");
    const left = el("div", "urnch-side left", "L");
    const right = el("div", "urnch-side right", "R");
    const handle = el("div", "urnch-pan-handle");
    const readout = el("div", "urnch-pan-readout", "CENTER");
    const arrow = el("div", "urnch-auto-arrow");
    stage.append(line, center, left, right, handle, readout, arrow);
    stagePanel.appendChild(stage);

    const levelPanel = el("div", "urnch-panel");
    levelPanel.appendChild(el("div", "urnch-section-title", "Channel Level"));
    const leftControl = sliderRow("Left Volume", 0, 200, 1, getValue(node, "left_volume", 100), "%", (v) => setValue(node, "left_volume", v));
    const rightControl = sliderRow("Right Volume", 0, 200, 1, getValue(node, "right_volume", 100), "%", (v) => setValue(node, "right_volume", v));
    levelPanel.append(leftControl.row, rightControl.row);

    const motionPanel = el("div", "urnch-panel");
    motionPanel.appendChild(el("div", "urnch-section-title", "Pan Mode"));
    const grid = el("div", "urnch-grid");
    const autoGroup = el("div", "urnch-group");
    autoGroup.appendChild(el("div", "urnch-group-label", "Auto Pan"));
    const autoSeg = segmented(["Off", "Left → Right", "Right → Left"], String(getValue(node, "auto_pan", "Off")), (v) => setValue(node, "auto_pan", v));
    autoGroup.appendChild(autoSeg.wrap);
    const curveGroup = el("div", "urnch-group");
    curveGroup.appendChild(el("div", "urnch-group-label", "Movement Curve"));
    const curveSeg = segmented(["Cosine", "Linear"], String(getValue(node, "pan_curve", "Cosine")), (v) => setValue(node, "pan_curve", v));
    curveGroup.appendChild(curveSeg.wrap);
    grid.append(autoGroup, curveGroup);
    motionPanel.appendChild(grid);

    const actionPanel = el("div", "urnch-panel");
    actionPanel.appendChild(el("div", "urnch-section-title", "Channel Options"));
    const actions = el("div", "urnch-actions");
    const swapToggle = toggleButton("Swap L / R", !!getValue(node, "swap_lr", false), (v) => setValue(node, "swap_lr", v));
    const clipToggle = toggleButton("Prevent Clipping", !!getValue(node, "prevent_clipping", true), (v) => setValue(node, "prevent_clipping", v));
    const reset = el("button", "urnch-reset", "Reset");
    reset.type = "button";
    reset.addEventListener("click", (e) => {
        e.preventDefault(); e.stopPropagation();
        setValue(node, "left_volume", 100);
        setValue(node, "right_volume", 100);
        setValue(node, "swap_lr", false);
        setValue(node, "pan", 0);
        setValue(node, "auto_pan", "Off");
        setValue(node, "pan_curve", "Cosine");
        setValue(node, "prevent_clipping", true);
        refresh(node);
    });
    actions.append(swapToggle.button, clipToggle.button, reset);
    actionPanel.appendChild(actions);

    const foot = el("div", "urnch-foot", "Auto Pan travels across the full input audio duration.");
    root.append(stagePanel, levelPanel, motionPanel, actionPanel, foot);

    // Keep ComfyUI graph drag/select gestures out of interactive controls.
    for (const evt of ["pointerdown", "mousedown", "click", "wheel"]) root.addEventListener(evt, stopGraphEvent);

    let dragging = false;
    const panFromPointer = (e) => {
        const r = stage.getBoundingClientRect();
        const leftPad = 38;
        const rightPad = 38;
        const usable = Math.max(1, r.width - leftPad - rightPad);
        const x = clamp(e.clientX - r.left - leftPad, 0, usable);
        const p = (x / usable) * 200 - 100;
        setStagePan(node, p);
    };
    stage.addEventListener("pointerdown", (e) => {
        if (String(getValue(node, "auto_pan", "Off")) !== "Off") return;
        dragging = true;
        try { stage.setPointerCapture(e.pointerId); } catch (_) {}
        panFromPointer(e);
        e.preventDefault(); e.stopPropagation();
    });
    stage.addEventListener("pointermove", (e) => {
        if (!dragging) return;
        panFromPointer(e);
        e.preventDefault(); e.stopPropagation();
    });
    const stopDrag = (e) => {
        if (!dragging) return;
        dragging = false;
        try { stage.releasePointerCapture(e.pointerId); } catch (_) {}
        e.preventDefault(); e.stopPropagation();
    };
    stage.addEventListener("pointerup", stopDrag);
    stage.addEventListener("pointercancel", stopDrag);

    node.__urnChannelUI = {
        root, stage, handle, readout, arrow,
        leftRange: leftControl.range, leftNumber: leftControl.number,
        rightRange: rightControl.range, rightNumber: rightControl.number,
        autoSeg, curveSeg, curveGroup, swapToggle, clipToggle,
    };
    return root;
}

function refresh(node) {
    const ui = node?.__urnChannelUI;
    if (!ui) return;
    const lv = clamp(getValue(node, "left_volume", 100), 0, 200);
    const rv = clamp(getValue(node, "right_volume", 100), 0, 200);
    const pan = clamp(getValue(node, "pan", 0), -100, 100);
    const auto = String(getValue(node, "auto_pan", "Off"));
    const curve = String(getValue(node, "pan_curve", "Cosine"));

    ui.leftRange.value = String(lv); ui.leftNumber.value = String(Math.round(lv));
    ui.rightRange.value = String(rv); ui.rightNumber.value = String(Math.round(rv));
    ui.autoSeg.set(auto); ui.curveSeg.set(curve);
    ui.swapToggle.set(!!getValue(node, "swap_lr", false));
    ui.clipToggle.set(!!getValue(node, "prevent_clipping", true));

    const manual = auto === "Off";
    ui.stage.classList.toggle("disabled", !manual);
    ui.curveSeg.wrap.classList.toggle("disabled", manual);
    ui.curveGroup.style.opacity = manual ? ".55" : "1";

    if (manual) {
        const pct = (pan + 100) / 200;
        ui.handle.style.display = "block";
        ui.handle.style.left = `calc(${(pct * 100).toFixed(3)}% + ${(38 - 76 * pct).toFixed(3)}px)`;
        ui.readout.textContent = panText(pan);
        ui.arrow.classList.remove("show");
    } else {
        const leftToRight = auto === "Left → Right";
        ui.handle.style.display = "block";
        ui.handle.style.left = leftToRight ? "38px" : "calc(100% - 38px)";
        ui.readout.textContent = `${auto.toUpperCase()} • WHOLE CLIP`;
        ui.arrow.innerHTML = leftToRight
            ? `LEFT <span class="arrow">→</span> RIGHT`
            : `RIGHT <span class="arrow">→</span> LEFT`;
        ui.arrow.classList.add("show");
    }
}

function install(node) {
    if (!isOurNode(node) || node.__urnAudioChannelInstalled) return;
    if (typeof node.addDOMWidget !== "function") return;
    node.__urnAudioChannelInstalled = true;

    for (const name of BACKEND_WIDGETS) hideBackendWidget(node, name);

    const root = buildUI(node);
    const dw = node.addDOMWidget(UI_WIDGET, UI_WIDGET, root, { serialize: false, getMinHeight: () => 372 });
    dw.serialize = false;
    if (dw.options) dw.options.serialize = false;

    // If native widget values are changed by workflow restore or other frontend
    // code, keep the visual controls in sync without creating duplicate widgets.
    for (const name of BACKEND_WIDGETS) {
        const w = widget(node, name);
        if (!w || w.__urnChannelWrapped) continue;
        w.__urnChannelWrapped = true;
        const old = w.callback;
        w.callback = function () {
            const result = old?.apply(this, arguments);
            queueMicrotask(() => refresh(node));
            return result;
        };
    }

    // Give the visual layout enough room on first placement. ComfyUI can do a
    // native auto-size pass immediately after nodeCreated, so re-apply the
    // minimum once on the next tick as well. This prevents the lower panels
    // from being squeezed outside the node until the user manually resizes it.
    const applyDefaultSize = () => {
        // LiteGraph/ComfyUI may expose node.size as a Float32Array rather than
        // a normal JavaScript Array. V9.26 used Array.isArray(node.size), so
        // this block silently skipped on freshly-created nodes and they kept
        // ComfyUI's narrow default size. Read the indexed values directly.
        const currentWidth = Number(node.size?.[0]) || 0;
        const currentHeight = Number(node.size?.[1]) || 0;
        node.setSize?.([
            Math.max(currentWidth, DEFAULT_NODE_SIZE[0]),
            Math.max(currentHeight, DEFAULT_NODE_SIZE[1]),
        ]);
    };
    applyDefaultSize();
    queueMicrotask(applyDefaultSize);
    requestAnimationFrame(() => requestAnimationFrame(() => { applyDefaultSize(); refresh(node); }));
    setTimeout(() => { applyDefaultSize(); refresh(node); }, 100);
}

function scan() { for (const node of app.graph?._nodes || []) install(node); }

app.registerExtension({
    name: "URN.AudioNodes.AudioChannel.Visual",
    nodeCreated(node) { install(node); },
    loadedGraphNode(node) { install(node); },
    setup() {
        scan();
        setTimeout(scan, 250);
        setTimeout(scan, 1000);
    },
});
