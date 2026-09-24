import { app } from "../../scripts/app.js";

const TARGET = "URN_MP3_Trim_Fade";
const WIDGET_NAME = "urnTrimFadeVisualEditorV9_2";
const REQUIRED_WIDGETS = ["length", "start_sec", "fade_in_sec", "fade_out_sec", "curve", "trim_mode", "end_sec"];
const cache = new Map();

function widget(node, name) {
    return node?.widgets?.find?.((w) => w?.name === name) ?? null;
}

function isTarget(node) {
    if (!node) return false;
    if (node.comfyClass === TARGET || node.type === TARGET || node.constructor?.comfyClass === TARGET || node.constructor?.type === TARGET) return true;
    return REQUIRED_WIDGETS.every((name) => !!widget(node, name)) && !!node.inputs?.some?.((i) => i?.name === "audio_input");
}

const LEGACY_VISUAL_WIDGETS = new Set([
    "urnTrimFadeWaveform",
    "urnTrimFadeVisualEditorV9",
    "urnTrimFadeVisualEditorV8",
    "urnTrimFadeVisualEditorV7",
    "urnTrimFadeVisualEditorV6",
]);

function removeNodeWidget(node, w) {
    if (!node || !w) return;
    try { w.onRemove?.(); } catch (_) {}
    try { w.element?.remove?.(); } catch (_) {}
    try { w.inputEl?.remove?.(); } catch (_) {}
    const i = node.widgets?.indexOf?.(w) ?? -1;
    if (i >= 0) node.widgets.splice(i, 1);
}

function cleanupLegacyEditors(node) {
    if (!node?.widgets) return;

    // V6/V7 used __urnWaveformPoll; V8 used __urnAudioPoll. Stop those
    // old timers so stale JS files left behind by an unzip-over install cannot
    // keep servicing hidden/duplicate editors. V9 deliberately uses its own
    // uniquely named timer below.
    try { if (node.__urnAudioPollV9) clearInterval(node.__urnAudioPollV9); } catch (_) {}
    node.__urnAudioPollV9 = null;
    try { if (node.__urnWaveformPoll) clearInterval(node.__urnWaveformPoll); } catch (_) {}
    node.__urnWaveformPoll = null;
    try { node.__urnWaveResizeObserver?.disconnect?.(); } catch (_) {}
    node.__urnWaveResizeObserver = null;
    try { if (node.__urnAudioPoll) clearInterval(node.__urnAudioPoll); } catch (_) {}
    node.__urnAudioPoll = null;

    for (const w of [...node.widgets]) {
        const name = String(w?.name ?? "");
        // Also remove the old V16 internal file-picker button if its JS was
        // left in the folder from an older install. Audio now comes only from
        // the AUDIO input.
        if (LEGACY_VISUAL_WIDGETS.has(name) || name === "Choose Audio File" || name === "Uploading...") {
            removeNodeWidget(node, w);
        }
    }

    node.graph?.setDirtyCanvas?.(true, true);
    node.setDirtyCanvas?.(true, true);
}

function num(node, name, fallback = 0) {
    const n = Number(widget(node, name)?.value);
    return Number.isFinite(n) ? n : fallback;
}

function setNum(node, name, value) {
    const w = widget(node, name);
    if (!w) return;
    const v = Math.max(0, Number(value) || 0);
    w.value = Number(v.toFixed(3));
    try { w.callback?.(w.value, node, w); } catch (_) {}
    node.graph?.setDirtyCanvas?.(true, true);
    node.setDirtyCanvas?.(true, true);
    draw(node);
}

function state(node) {
    const total = Math.max(0, Number(node.__urnAudioDuration || 0));
    let start = Math.max(0, num(node, "start_sec", 0));
    const mode = String(widget(node, "trim_mode")?.value || "Length");
    let end = mode === "End Sec" ? Math.max(0, num(node, "end_sec", 10)) : start + Math.max(0, num(node, "length", 10));
    if (total > 0) {
        start = Math.min(start, total);
        end = Math.min(Math.max(start, end), total);
    } else {
        end = Math.max(start, end);
    }
    const duration = Math.max(0, end - start);
    const fadeIn = Math.min(duration, Math.max(0, num(node, "fade_in_sec", 3)));
    const fadeOut = Math.min(duration, Math.max(0, num(node, "fade_out_sec", 3)));
    const curve = String(widget(node, "curve")?.value || "cosine");
    return { total, start, end, duration, fadeIn, fadeOut, curve, mode };
}

function fmt(seconds) {
    const s = Math.max(0, Number(seconds) || 0);
    const m = Math.floor(s / 60);
    const sec = s - m * 60;
    return `${String(m).padStart(2, "0")}:${sec.toFixed(2).padStart(5, "0")}`;
}

function graphLink(node) {
    const slot = node.inputs?.findIndex?.((i) => i?.name === "audio_input") ?? -1;
    if (slot < 0) return null;
    const id = node.inputs?.[slot]?.link;
    if (id == null) return null;
    const links = node.graph?.links ?? node.graph?._links;
    if (!links) return null;
    if (typeof links.get === "function") return links.get(id) ?? null;
    return links[id] ?? null;
}

function upstream(node) {
    const link = graphLink(node);
    const id = link?.origin_id ?? link?.originId;
    return id == null ? null : node.graph?.getNodeById?.(id) ?? null;
}

function viewUrlFromFileValue(rawValue, defaultType = "input") {
    if (!rawValue) return "";

    let filename = "";
    let subfolder = "";
    let type = defaultType;

    if (typeof rawValue === "object") {
        filename = String(rawValue.filename ?? rawValue.name ?? rawValue.path ?? rawValue.value ?? "");
        subfolder = String(rawValue.subfolder ?? "");
        type = String(rawValue.type ?? defaultType);
    } else {
        let raw = String(rawValue).replaceAll("\\", "/").trim();

        // Some ComfyUI/VHS widgets can serialize a location suffix such as
        // "clip.mp4 [input]". Preserve the actual file name while using the
        // suffix as the /view type when present.
        const tagged = raw.match(/^(.*)\s+\[(input|output|temp)\]$/i);
        if (tagged) {
            raw = tagged[1].trim();
            type = tagged[2].toLowerCase();
        }

        const parts = raw.split("/");
        filename = parts.pop() || "";
        subfolder = parts.join("/");
    }

    // If an object provided a full relative path in filename/path and no
    // explicit subfolder, split it the same way as a string widget value.
    filename = filename.replaceAll("\\", "/");
    if (!subfolder && filename.includes("/")) {
        const parts = filename.split("/");
        filename = parts.pop() || "";
        subfolder = parts.join("/");
    }

    if (!filename) return "";
    const q = new URLSearchParams({ filename, subfolder, type });
    return `/view?${q.toString()}`;
}

function audioUrl(node) {
    const src = upstream(node);
    if (!src) return "";

    // Core Load Audio normally owns an HTML audio element. Prefer the element
    // because it already reflects ComfyUI's resolved source URL.
    for (const w of src.widgets || []) {
        const el = w?.element;
        const mediaSrc = el?.currentSrc || el?.src;
        if (el?.tagName === "AUDIO" && mediaSrc) return String(mediaSrc);
        if (w?.name === "audioUI" && mediaSrc) return String(mediaSrc);
    }

    // Normal Load Audio file combo.
    const audioFileUrl = viewUrlFromFileValue(widget(src, "audio")?.value);
    if (audioFileUrl) return audioFileUrl;

    // VHS Load Video (Upload) and compatible video-loader nodes expose AUDIO
    // from a selected video file rather than from an "audio" widget. The
    // browser can decode the audio track directly from common containers such
    // as MP4/WebM, so use the selected video as the waveform/preview source.
    for (const w of src.widgets || []) {
        const el = w?.element;
        const mediaSrc = el?.currentSrc || el?.src;
        if (el?.tagName === "VIDEO" && mediaSrc) return String(mediaSrc);
    }

    const videoFileUrl = viewUrlFromFileValue(widget(src, "video")?.value);
    if (videoFileUrl) return videoFileUrl;

    return "";
}

async function decode(url) {
    if (cache.has(url)) return cache.get(url);
    const p = (async () => {
        const response = await fetch(url);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.arrayBuffer();
        const AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) throw new Error("Web Audio API unavailable");
        const ctx = new AC();
        try {
            const b = await ctx.decodeAudioData(data.slice(0));
            const buckets = 720;
            const peaks = new Float32Array(buckets);
            const block = Math.max(1, Math.floor(b.length / buckets));
            for (let i = 0; i < buckets; i++) {
                const a = i * block;
                const z = i === buckets - 1 ? b.length : Math.min(b.length, a + block);
                const stride = Math.max(1, Math.floor((z - a) / 48));
                let peak = 0;
                for (let ch = 0; ch < b.numberOfChannels; ch++) {
                    const c = b.getChannelData(ch);
                    for (let j = a; j < z; j += stride) peak = Math.max(peak, Math.abs(c[j] || 0));
                }
                peaks[i] = peak;
            }
            return { duration: Number(b.duration || 0), peaks, rawData: data };
        } finally {
            try { await ctx.close(); } catch (_) {}
        }
    })();
    cache.set(url, p);
    try { return await p; } catch (e) { cache.delete(url); throw e; }
}

async function syncAudio(node) {
    if (!node || node.__urnAudioRemoved) return;
    const url = audioUrl(node);
    if (url === node.__urnAudioUrl && !node.__urnResetOnNextAudio) return;

    if (node.__urnPreview) stopPreview(node);

    const previousSavedSource = String(node.properties?.urnTrimFadeSourceKey || "");
    const shouldResetDefaults = !!url && (node.__urnResetOnNextAudio || previousSavedSource !== url);

    node.__urnAudioUrl = url;
    node.__urnResetOnNextAudio = false;
    node.__urnAudioPeaks = null;
    node.__urnAudioDuration = 0;
    node.__urnAudioStatus = url ? "Loading waveform…" : "Connect AUDIO to audio_input";
    draw(node);
    if (!url) return;
    const token = (node.__urnAudioToken || 0) + 1;
    node.__urnAudioToken = token;
    try {
        const r = await decode(url);
        if (node.__urnAudioRemoved || node.__urnAudioToken !== token) return;
        node.__urnAudioDuration = r.duration;
        node.__urnAudioPeaks = r.peaks;
        node.__urnAudioStatus = "";

        if (shouldResetDefaults) {
            setNum(node, "start_sec", 0);
            setNum(node, "fade_in_sec", 3);
            setNum(node, "fade_out_sec", 3);
            setNum(node, "length", r.duration);
            setNum(node, "end_sec", r.duration);
            node.properties = node.properties || {};
            node.properties.urnTrimFadeSourceKey = url;
        }
    } catch (e) {
        if (node.__urnAudioToken !== token) return;
        node.__urnAudioStatus = "Waveform unavailable";
        console.warn("[URN Audio Nodes V9.2] waveform load failed", e);
    }
    draw(node);
}


function previewButtonState(node, label, disabled = false) {
    const b = node?.__urnPreviewButton;
    if (!b) return;
    b.textContent = label;
    b.disabled = !!disabled;
    b.style.opacity = disabled ? ".65" : "1";
}

function ensurePreviewAnimation(node) {
    if (node?.__urnPreviewRAF) return;
    const tick = () => {
        if (!node?.__urnPreview) {
            node.__urnPreviewRAF = null;
            draw(node);
            return;
        }
        draw(node);
        node.__urnPreviewRAF = window.requestAnimationFrame(tick);
    };
    node.__urnPreviewRAF = window.requestAnimationFrame(tick);
}

async function stopPreview(node, natural = false) {
    const p = node?.__urnPreview;
    if (!p) {
        previewButtonState(node, "▶ Preview Changes");
        return;
    }
    node.__urnPreview = null;
    try { if (p.timer) clearTimeout(p.timer); } catch (_) {}
    try { if (node.__urnPreviewRAF) cancelAnimationFrame(node.__urnPreviewRAF); } catch (_) {}
    node.__urnPreviewRAF = null;
    if (!natural) {
        try { p.source?.stop?.(); } catch (_) {}
    }
    try { p.source?.disconnect?.(); } catch (_) {}
    try { p.envelope?.disconnect?.(); } catch (_) {}
    try { p.postGain?.disconnect?.(); } catch (_) {}
    try { p.clipper?.disconnect?.(); } catch (_) {}
    try { await p.ctx?.close?.(); } catch (_) {}
    previewButtonState(node, "▶ Preview Changes");
    draw(node);
}

function shapedFade(p, curve) {
    p = Math.max(0, Math.min(1, p));
    return curve === "cosine" ? 0.5 - 0.5 * Math.cos(Math.PI * p) : p;
}

function envelopeAt(t, duration, fadeIn, fadeOut, curve) {
    let g = 1.0;
    if (fadeIn > 0 && t < fadeIn) g *= shapedFade(t / fadeIn, curve);
    if (fadeOut > 0 && t > duration - fadeOut) g *= shapedFade((duration - t) / fadeOut, curve);
    return Math.max(0, Math.min(1, g));
}

function segmentPeak(buffer, startSec, duration, fadeIn, fadeOut, curve) {
    const sr = Number(buffer.sampleRate || 0);
    if (!sr || duration <= 0) return 0;
    const a = Math.max(0, Math.min(buffer.length, Math.round(startSec * sr)));
    const z = Math.max(a, Math.min(buffer.length, Math.round((startSec + duration) * sr)));
    let peak = 0;
    for (let ch = 0; ch < buffer.numberOfChannels; ch++) {
        const data = buffer.getChannelData(ch);
        for (let i = a; i < z; i++) {
            const t = (i - a) / sr;
            const v = Math.abs(data[i] || 0) * envelopeAt(t, duration, fadeIn, fadeOut, curve);
            if (v > peak) peak = v;
        }
    }
    return peak;
}

function makeHardClipCurve() {
    const n = 65537;
    const curve = new Float32Array(n);
    for (let i = 0; i < n; i++) curve[i] = -1 + (2 * i) / (n - 1);
    return curve;
}

async function startPreview(node) {
    if (node.__urnPreview) {
        await stopPreview(node);
        return;
    }

    const url = audioUrl(node);
    if (!url) {
        previewButtonState(node, "Connect AUDIO source first");
        setTimeout(() => previewButtonState(node, "▶ Preview Changes"), 1600);
        return;
    }

    const s = state(node);
    if (s.duration <= 0) {
        previewButtonState(node, "Nothing to preview");
        setTimeout(() => previewButtonState(node, "▶ Preview Changes"), 1400);
        return;
    }

    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) {
        previewButtonState(node, "Preview unavailable");
        return;
    }

    // Create the context from the click gesture so normal browser autoplay
    // restrictions do not block the preview after the asynchronous decode.
    const ctx = new AC();
    const token = Symbol("urnTrimFadePreview");
    node.__urnPreview = { ctx, token, source: null, envelope: null, postGain: null, clipper: null, timer: null };
    previewButtonState(node, "Loading preview…");

    try {
        await ctx.resume();
        const decoded = await decode(url);
        if (node.__urnPreview?.token !== token) { try { await ctx.close(); } catch (_) {} return; }
        const buffer = await ctx.decodeAudioData(decoded.rawData.slice(0));
        if (node.__urnPreview?.token !== token) { try { await ctx.close(); } catch (_) {} return; }

        const start = Math.max(0, Math.min(s.start, buffer.duration));
        const realDuration = Math.max(0, Math.min(s.end, buffer.duration) - start);
        if (realDuration <= 0) throw new Error("Selected range contains no source audio");

        // Match the backend: fades are applied to the real trimmed material,
        // then optional normalize, then gain. Any pad_if_short silence comes after.
        const fadeIn = Math.min(realDuration, s.fadeIn);
        const fadeOut = Math.min(realDuration, s.fadeOut);
        const normalize = !!widget(node, "normalize")?.value;
        const gainDb = num(node, "gain_db", 0);

        let scalar = Math.pow(10, gainDb / 20);
        if (normalize) {
            const peak = segmentPeak(buffer, start, realDuration, fadeIn, fadeOut, s.curve);
            if (peak > 0) scalar *= 1 / peak;
        }

        const source = ctx.createBufferSource();
        const envelopeGain = ctx.createGain();
        const postGain = ctx.createGain();
        const clipper = ctx.createWaveShaper();
        source.buffer = buffer;
        postGain.gain.value = scalar;
        clipper.curve = makeHardClipCurve();
        clipper.oversample = "none";

        const points = Math.max(128, Math.min(4096, Math.ceil(realDuration * 120)));
        const env = new Float32Array(points);
        for (let i = 0; i < points; i++) {
            const t = realDuration * i / Math.max(1, points - 1);
            env[i] = envelopeAt(t, realDuration, fadeIn, fadeOut, s.curve);
        }
        const now = ctx.currentTime + 0.02;
        envelopeGain.gain.setValueCurveAtTime(env, now, realDuration);

        source.connect(envelopeGain);
        envelopeGain.connect(postGain);
        postGain.connect(clipper);
        clipper.connect(ctx.destination);

        const requestedDuration = !!widget(node, "pad_if_short")?.value ? s.duration : realDuration;
        Object.assign(node.__urnPreview, {
            source,
            envelope: envelopeGain,
            postGain,
            clipper,
            playAt: now,
            sourceStartSec: start,
            sourceEndSec: Math.min(buffer.duration, start + realDuration),
            realDuration,
            requestedDuration,
        });
        ensurePreviewAnimation(node);
        source.onended = () => {
            if (node.__urnPreview?.token !== token) return;
            // If padding is requested, keep the preview alive for the silent tail
            // so its total duration matches the node output.
            const silenceTailMs = Math.max(0, requestedDuration - realDuration) * 1000;
            if (silenceTailMs > 1) {
                node.__urnPreview.timer = setTimeout(() => {
                    if (node.__urnPreview?.token === token) stopPreview(node, true);
                }, silenceTailMs);
            } else {
                stopPreview(node, true);
            }
        };
        source.start(now, start, realDuration);
        previewButtonState(node, "■ Stop Preview");
    } catch (e) {
        console.warn("[URN Audio Nodes V9.2] preview failed", e);
        if (node.__urnPreview?.token === token) {
            await stopPreview(node);
            previewButtonState(node, "Preview failed");
            setTimeout(() => previewButtonState(node, "▶ Preview Changes"), 1500);
        } else {
            try { await ctx.close(); } catch (_) {}
        }
    }
}

function metrics(node) {
    const c = node.__urnAudioCanvas;
    if (!c) return null;
    const r = c.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(1, Math.round(r.width * dpr));
    const h = Math.max(1, Math.round(r.height * dpr));
    if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
    return { c, dpr, w: w / dpr, h: h / dpr };
}

function envelope(ctx, x0, x1, top, bottom, curve, fadeIn) {
    const width = Math.max(1, x1 - x0);
    const steps = Math.max(8, Math.min(80, Math.floor(width / 3)));
    ctx.beginPath();
    for (let i = 0; i <= steps; i++) {
        const p = i / steps;
        const shaped = curve === "cosine" ? 0.5 - 0.5 * Math.cos(Math.PI * p) : p;
        // Canvas Y grows downward. Amplitude 1 therefore belongs at the top,
        // amplitude 0 at the bottom. Fade-in rises; fade-out falls.
        const amplitude = fadeIn ? shaped : 1 - shaped;
        const x = x0 + width * p;
        const y = bottom - amplitude * (bottom - top);
        if (!i) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();
}

function draw(node) {
    const m = metrics(node);
    if (!m) return;
    const { c, dpr, w, h } = m;
    const ctx = c.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = "rgba(0,0,0,.25)";
    ctx.fillRect(0, 0, w, h);

    const left = 10, right = w - 10, top = 24, bottom = h - 24, mid = (top + bottom) / 2;
    const span = Math.max(1, right - left);
    const s = state(node);
    const total = s.total || Math.max(s.end, 1);
    const tx = (t) => left + Math.max(0, Math.min(total, t)) / total * span;

    // Match ComfyUI's normal node text size instead of the previous tiny 11px labels.
    const fontPx = Math.max(13, Number(window.LiteGraph?.NODE_TEXT_SIZE || 14));
    ctx.font = `${fontPx}px sans-serif`;
    ctx.fillStyle = "rgba(230,230,230,.8)";
    ctx.fillText("0:00", left, fontPx + 1);
    const rt = fmt(total);
    ctx.fillText(rt, right - ctx.measureText(rt).width, fontPx + 1);

    const peaks = node.__urnAudioPeaks;
    if (peaks?.length) {
        ctx.strokeStyle = "rgba(205,215,225,.75)";
        ctx.lineWidth = 1;
        ctx.beginPath();
        const amp = (bottom - top) * .44;
        for (let i = 0; i < peaks.length; i++) {
            const x = left + i / Math.max(1, peaks.length - 1) * span;
            const ph = Math.max(.5, peaks[i] * amp);
            ctx.moveTo(x, mid - ph); ctx.lineTo(x, mid + ph);
        }
        ctx.stroke();
    } else {
        ctx.fillStyle = "rgba(230,230,230,.7)";
        ctx.textAlign = "center";
        ctx.fillText(node.__urnAudioStatus || "Visual Trim / Fade ready", w / 2, mid);
        ctx.textAlign = "left";
    }

    const sx = tx(s.start), ex = tx(s.end), fix = tx(s.start + s.fadeIn), fox = tx(s.end - s.fadeOut);
    ctx.fillStyle = "rgba(0,0,0,.58)";
    ctx.fillRect(left, top, Math.max(0, sx - left), bottom - top);
    ctx.fillRect(ex, top, Math.max(0, right - ex), bottom - top);
    ctx.fillStyle = "rgba(120,175,225,.14)";
    ctx.fillRect(sx, top, Math.max(1, ex - sx), bottom - top);

    ctx.strokeStyle = "rgba(250,250,250,.95)";
    ctx.lineWidth = 2;
    for (const x of [sx, ex]) {
        ctx.beginPath(); ctx.moveTo(x, top - 2); ctx.lineTo(x, bottom + 2); ctx.stroke();
        ctx.fillRect(x - 4, top - 4, 8, 8);
    }
    ctx.lineWidth = 1.5;
    if (fix > sx + 1) envelope(ctx, sx, fix, top + 2, bottom - 2, s.curve, true);
    if (ex > fox + 1) envelope(ctx, fox, ex, top + 2, bottom - 2, s.curve, false);

    const diamond = (x) => {
        const y = top + 10;
        ctx.beginPath(); ctx.moveTo(x, y - 5); ctx.lineTo(x + 5, y); ctx.lineTo(x, y + 5); ctx.lineTo(x - 5, y); ctx.closePath(); ctx.fill();
    };
    diamond(fix); diamond(fox);

    // While a fade handle is being dragged, show an unambiguous 1 CSS-pixel
    // orange guide through the waveform area at the active fade position.
    // Snap to a half-pixel so the 1px stroke stays crisp on normal canvases.
    if (node.__urnAudioDrag === "fadeIn" || node.__urnAudioDrag === "fadeOut") {
        const activeX = node.__urnAudioDrag === "fadeIn" ? fix : fox;
        const guideX = Math.round(activeX) + 0.5;
        ctx.save();
        ctx.strokeStyle = "#ff9800";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(guideX, top);
        ctx.lineTo(guideX, bottom);
        ctx.stroke();
        ctx.restore();
    }

    // During Preview Changes playback, show a moving red playhead so the user
    // can see where the current playback position sits within the selected
    // timeline. It tracks the actual source-audio position and then holds at
    // the trimmed end during any silent tail.
    const preview = node.__urnPreview;
    if (preview?.ctx && Number.isFinite(preview.playAt)) {
        const elapsed = Math.max(0, preview.ctx.currentTime - preview.playAt);
        if (elapsed <= Math.max(0, preview.requestedDuration || preview.realDuration || 0) + 0.05) {
            const playPos = elapsed <= (preview.realDuration || 0)
                ? (preview.sourceStartSec || 0) + elapsed
                : (preview.sourceEndSec || s.end);
            const px = Math.round(tx(playPos)) + 0.5;
            ctx.save();
            ctx.strokeStyle = "#ff3b30";
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(px, top);
            ctx.lineTo(px, bottom);
            ctx.stroke();
            ctx.restore();
        }
    }

    ctx.textAlign = "center";
    ctx.font = `${fontPx}px sans-serif`;
    ctx.fillStyle = "rgba(245,245,245,.95)";
    ctx.fillText(`${fmt(s.start)}  →  ${fmt(s.end)}   (${s.duration.toFixed(2)}s)`, w / 2, h - 7);
    ctx.textAlign = "left";
    node.__urnAudioGeom = { left, span, total, sx, ex, fix, fox };
}

function pointerX(c, e) { return e.clientX - c.getBoundingClientRect().left; }
function nearest(node, x) {
    const g = node.__urnAudioGeom;
    if (!g) return null;
    const points = [["start", g.sx], ["end", g.ex], ["fadeIn", g.fix], ["fadeOut", g.fox]];
    let pick = null, best = 13;
    for (const [name, px] of points) { const d = Math.abs(px - x); if (d <= best) { pick = name; best = d; } }
    return pick;
}
function toTime(node, x) {
    const g = node.__urnAudioGeom;
    if (!g) return 0;
    return Math.max(0, Math.min(1, (x - g.left) / g.span)) * g.total;
}
function drag(node, kind, time) {
    const s = state(node);
    const minGap = .001;
    if (kind === "start") {
        const v = Math.max(0, Math.min(time, s.end - minGap));
        setNum(node, "start_sec", v);
        if (s.mode === "Length") setNum(node, "length", s.end - v);
    } else if (kind === "end") {
        const v = Math.max(s.start + minGap, Math.min(s.total || time, time));
        if (s.mode === "End Sec") setNum(node, "end_sec", v); else setNum(node, "length", v - s.start);
    } else if (kind === "fadeIn") {
        setNum(node, "fade_in_sec", Math.max(0, Math.min(s.duration, time - s.start)));
    } else if (kind === "fadeOut") {
        setNum(node, "fade_out_sec", Math.max(0, Math.min(s.duration, s.end - time)));
    }
}

function install(node) {
    if (!isTarget(node)) return;

    // V9.75 removed the obsolete audio_length connector. Older saved workflows
    // may still serialize that input slot, so remove it on load instead of
    // leaving a dead connector visible.
    const oldLengthSlot = node.inputs?.findIndex?.((i) => i?.name === "audio_length") ?? -1;
    if (oldLengthSlot >= 0) {
        try { node.removeInput?.(oldLengthSlot); } catch (_) {}
    }

    cleanupLegacyEditors(node);
    if (node.__urnAudioEditorV9_2Installed) return;
    if (typeof node.addDOMWidget !== "function") return;
    node.__urnAudioEditorV9_2Installed = true;

    const wrap = document.createElement("div");
    Object.assign(wrap.style, { boxSizing: "border-box", width: "100%", height: "226px", padding: "4px 6px", userSelect: "none" });
    const title = document.createElement("div");
    title.textContent = "Visual Trim / Fade";
    Object.assign(title.style, { fontSize: "14px", fontWeight: "700", padding: "0 2px 4px", opacity: ".9" });
    const canvas = document.createElement("canvas");
    Object.assign(canvas.style, { display: "block", width: "100%", height: "160px", border: "1px solid #666", borderRadius: "6px", background: "rgba(0,0,0,.28)", cursor: "default" });
    const preview = document.createElement("button");
    preview.type = "button";
    preview.textContent = "▶ Preview Changes";
    Object.assign(preview.style, {
        display: "block", width: "100%", height: "30px", marginTop: "6px",
        border: "1px solid #666", borderRadius: "6px", background: "rgba(255,255,255,.07)",
        color: "inherit", fontWeight: "600", cursor: "pointer"
    });
    preview.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); startPreview(node); });
    wrap.append(title, canvas, preview);

    const dw = node.addDOMWidget(WIDGET_NAME, WIDGET_NAME, wrap, { serialize: false, getMinHeight: () => 226 });
    dw.serialize = false;
    if (dw.options) dw.options.serialize = false;
    node.__urnAudioCanvas = canvas;
    node.__urnPreviewButton = preview;
    node.__urnAudioStatus = "Connect AUDIO to audio_input";

    canvas.addEventListener("pointerdown", (e) => {
        const k = nearest(node, pointerX(canvas, e));
        if (!k) return;
        node.__urnAudioDrag = k; canvas.setPointerCapture?.(e.pointerId); draw(node); e.preventDefault();
    });
    canvas.addEventListener("pointermove", (e) => {
        const x = pointerX(canvas, e);
        if (node.__urnAudioDrag) { drag(node, node.__urnAudioDrag, toTime(node, x)); e.preventDefault(); }
        else canvas.style.cursor = nearest(node, x) ? "ew-resize" : "default";
    });
    const stop = (e) => { if (node.__urnAudioDrag) { node.__urnAudioDrag = null; try { canvas.releasePointerCapture?.(e.pointerId); } catch (_) {} draw(node); } };
    canvas.addEventListener("pointerup", stop); canvas.addEventListener("pointercancel", stop);

    const visualAndPreviewWidgets = [...new Set([...REQUIRED_WIDGETS, "gain_db", "normalize", "pad_if_short"])];
    for (const name of visualAndPreviewWidgets) {
        const w = widget(node, name);
        if (!w || w.__urnVisualWrappedV9_2) continue;
        w.__urnVisualWrappedV9_2 = true;
        const old = w.callback;
        w.callback = function () {
            const r = old?.apply(this, arguments);
            if (node.__urnPreview) stopPreview(node);
            draw(node);
            return r;
        };
    }

    node.__urnLastAudioLink = node.inputs?.find?.((i) => i?.name === "audio_input")?.link ?? null;
    const oldConn = node.onConnectionsChange;
    node.onConnectionsChange = function () {
        const r = oldConn?.apply(this, arguments);
        const currentLink = this.inputs?.find?.((i) => i?.name === "audio_input")?.link ?? null;
        if (currentLink !== this.__urnLastAudioLink) {
            this.__urnResetOnNextAudio = currentLink != null;
            this.__urnLastAudioLink = currentLink;
            // Force a fresh source check even if reconnecting the same Load Audio node.
            this.__urnAudioUrl = null;
        }
        setTimeout(() => syncAudio(this), 0);
        return r;
    };
    const oldRemoved = node.onRemoved;
    node.onRemoved = function () {
        this.__urnAudioRemoved = true;
        try { clearInterval(this.__urnAudioPollV9_2); } catch (_) {}
        try { stopPreview(this); } catch (_) {}
        cleanupLegacyEditors(this);
        return oldRemoved?.apply(this, arguments);
    };

    node.__urnAudioPollV9_2 = setInterval(() => {
        cleanupLegacyEditors(node);
        syncAudio(node);
    }, 1000);
    if (Array.isArray(node.size)) node.setSize?.([Math.max(node.size[0] || 0, 440), Math.max(node.size[1] || 0, 688)]);
    setTimeout(() => { syncAudio(node); draw(node); }, 50);
}

function scan() {
    for (const node of app.graph?._nodes || []) install(node);
}

app.registerExtension({
    name: "URN.AudioNodes.TrimFade.VisualEditor.v9_2",
    nodeCreated(node) { install(node); },
    loadedGraphNode(node) { install(node); },
    setup() {
        scan();
        // One short startup scan catches workflows loaded before this extension's setup hook.
        setTimeout(scan, 250);
        setTimeout(scan, 1000);
        setTimeout(scan, 2000);
    },
});
