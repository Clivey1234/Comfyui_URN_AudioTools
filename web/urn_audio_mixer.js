import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const TARGET = "URNAudioMixer";
const STATE_WIDGET = "mix_state_json";
const PAD_START_WIDGET = "pad_start_sec";
const PAD_END_WIDGET = "pad_end_sec";
const UI_WIDGET = "urnAudioMixerUI";
const STATE_PROPERTY = "urnAudioMixerStateJson";
const AUDIO_EXTS = new Set(["mp3", "wav", "flac", "ogg", "oga", "opus", "m4a", "aac", "aif", "aiff", "webm"]);
const decodeCache = new Map();

function isOurNode(node) {
    return !!node && (node.comfyClass === TARGET || node.type === TARGET || node.constructor?.comfyClass === TARGET || node.constructor?.type === TARGET);
}
function widget(node, name) { return node?.widgets?.find?.((w) => w?.name === name) ?? null; }
function makeId(prefix = "urnmix") {
    try { return `${prefix}_${crypto.randomUUID()}`; } catch (_) {}
    return `${prefix}_${Date.now()}_${Math.random().toString(36).slice(2)}`;
}
function isAudioFile(file) {
    if (!file) return false;
    if (String(file.type || "").toLowerCase().startsWith("audio/")) return true;
    return AUDIO_EXTS.has(String(file.name || "").split(".").pop()?.toLowerCase() || "");
}
function stopEvent(e) { e.preventDefault(); e.stopPropagation(); }

function normalizedClip(raw = {}) {
    return {
        id: String(raw.id || makeId("clip")),
        name: String(raw.name || raw.serverName || ""),
        subfolder: String(raw.subfolder || ""),
        type: String(raw.type || "input"),
        displayName: String(raw.displayName || raw.name || "Audio"),
        sourceDuration: Math.max(0, Number(raw.sourceDuration || 0) || 0),
        position: Math.max(0, Number(raw.position ?? raw.timelineStart ?? 0) || 0),
        start: Math.max(0, Number(raw.start) || 0),
        end: Math.max(0, Number(raw.end) || 0),
        fadeIn: Math.max(0, Number(raw.fadeIn ?? 0) || 0),
        fadeOut: Math.max(0, Number(raw.fadeOut ?? 0) || 0),
        fadeDefaultsPending: raw.fadeDefaultsPending === true,
    };
}
function normalizedTrack(raw = {}, index = 1) {
    let clips = Array.isArray(raw.clips) ? raw.clips.map(normalizedClip) : [];
    return {
        id: String(raw.id || makeId("track")),
        name: String(raw.name || `Track ${index}`),
        volume: Math.max(0, Math.min(100, Number(raw.volume ?? 100) || 0)),
        clips,
    };
}
function migrateParsedState(parsed) {
    const items = Array.isArray(parsed) ? parsed : (Array.isArray(parsed?.tracks) ? parsed.tracks : []);
    if (!items.length) return [];
    const newShape = items.some((x) => x && typeof x === "object" && Array.isArray(x.clips));
    if (newShape) return items.filter((x) => x && typeof x === "object").map((x, i) => normalizedTrack(x, i + 1));
    // V9.11 and earlier: one file was one top-level module. Preserve it as one track containing one clip.
    return items.filter((x) => x && typeof x === "object").map((old, i) => {
        const clip = { ...old };
        const oldVolume = Math.max(0, Math.min(100, Number(clip.volume ?? 100) || 0));
        delete clip.volume;
        return normalizedTrack({ name: `Track ${i + 1}`, volume: oldVolume, clips: [clip] }, i + 1);
    });
}
function loadState(node) {
    const w = widget(node, STATE_WIDGET);
    const widgetValue = String(w?.value || "").trim();
    const propertyValue = String(node?.properties?.[STATE_PROPERTY] || "").trim();

    // The hidden backend widget is what ComfyUI sends to the Python node at
    // execution time, while node.properties is our workflow-persistence copy.
    // Older mixer builds changed the hidden widget to a converted-widget. That
    // could cause its value to be omitted when the workflow was saved, leaving
    // the mixer empty after a restart. Prefer a non-empty widget payload, then
    // fall back to the durable node property.
    let raw = widgetValue && widgetValue !== "[]" ? widgetValue : propertyValue;
    if (!raw) raw = "[]";

    let parsed = [];
    try { parsed = JSON.parse(raw); } catch (_) {}
    node.__urnMixerTracks = migrateParsedState(parsed);
    node.__urnMixerRuntime = node.__urnMixerRuntime || new Map();
    node.__urnMixerTrackRuntime = node.__urnMixerTrackRuntime || new Map();
    node.__urnMixerSelectedClips = node.__urnMixerSelectedClips || new Map();

    // Rehydrate the normal hidden widget from the persisted property so the
    // next prompt execution receives exactly the restored mixer state.
    if (w && propertyValue && (!widgetValue || widgetValue === "[]")) w.value = propertyValue;
}
function saveState(node) {
    const w = widget(node, STATE_WIDGET);
    const payload = JSON.stringify({
        version: 2,
        tracks: (node.__urnMixerTracks || []).map((track) => ({
            id: track.id,
            name: track.name,
            volume: Number(track.volume ?? 100),
            clips: (track.clips || []).map((c) => ({
                id: c.id, name: c.name, subfolder: c.subfolder, type: c.type,
                displayName: c.displayName, sourceDuration: Number(c.sourceDuration || 0),
                position: Number(c.position || 0), start: Number(c.start || 0), end: Number(c.end || 0),
                fadeIn: Number(c.fadeIn || 0), fadeOut: Number(c.fadeOut || 0),
                fadeDefaultsPending: c.fadeDefaultsPending === true,
            })),
        })),
    });
    if (w && w.value !== payload) {
        // Keep the backend STRING widget as a normal widget so ComfyUI includes
        // it in workflow/prompt serialization. We hide it only visually.
        w.value = payload;
    }
    node.properties = node.properties || {};
    node.properties[STATE_PROPERTY] = payload;
    node.graph?.setDirtyCanvas?.(true, true);
    node.setDirtyCanvas?.(true, true);
}
function hideStateWidget(node) {
    const w = widget(node, STATE_WIDGET);
    if (!w || w.__urnMixerHidden) return;
    w.__urnMixerHidden = true;
    // Do NOT change this to converted-widget. Converted widgets can be omitted
    // from saved widget_values, which was the cause of mixer state disappearing
    // after closing/reopening a workflow or restarting ComfyUI.
    w.computeSize = () => [0, -4];
    w.hidden = true;
}
function hideBackendWidget(node, name) {
    const w = widget(node, name);
    if (!w || w.__urnMixerHiddenBackend) return;
    w.__urnMixerHiddenBackend = true;
    w.computeSize = () => [0, -4];
    w.hidden = true;
}
function padValue(node, name) {
    return Math.max(0, Math.round(Number(widget(node, name)?.value) || 0));
}
function setPadValue(node, name, value) {
    const w = widget(node, name);
    const v = Math.max(0, Math.round(Number(value) || 0));
    if (w) {
        w.value = v;
        try { w.callback?.(v, node, w); } catch (_) {}
    }
    node.graph?.setDirtyCanvas?.(true, true);
    node.setDirtyCanvas?.(true, true);
    return v;
}
function syncPadControls(node) {
    if (node.__urnMixerPadStartInput) node.__urnMixerPadStartInput.value = String(padValue(node, PAD_START_WIDGET));
    if (node.__urnMixerPadEndInput) node.__urnMixerPadEndInput.value = String(padValue(node, PAD_END_WIDGET));
}

function allClips(node) {
    const out = [];
    for (const track of node.__urnMixerTracks || []) for (const clip of track.clips || []) out.push({ track, clip });
    return out;
}
function fileUrl(clip) {
    const params = new URLSearchParams({ filename: clip.name, subfolder: clip.subfolder || "", type: clip.type || "input" });
    return api.apiURL(`/view?${params.toString()}`);
}
async function decodeClip(clip) {
    const url = fileUrl(clip);
    if (decodeCache.has(url)) return decodeCache.get(url);
    const promise = (async () => {
        const resp = await fetch(url);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const bytes = await resp.arrayBuffer();
        const AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) throw new Error("Web Audio API unavailable");
        const ctx = new AC();
        try {
            const buffer = await ctx.decodeAudioData(bytes.slice(0));
            const buckets = 1000;
            const peaks = new Float32Array(buckets);
            const block = Math.max(1, Math.floor(buffer.length / buckets));
            for (let i = 0; i < buckets; i++) {
                const a = i * block;
                const z = i === buckets - 1 ? buffer.length : Math.min(buffer.length, a + block);
                const stride = Math.max(1, Math.floor(Math.max(1, z - a) / 56));
                let peak = 0;
                for (let ch = 0; ch < buffer.numberOfChannels; ch++) {
                    const data = buffer.getChannelData(ch);
                    for (let j = a; j < z; j += stride) peak = Math.max(peak, Math.abs(data[j] || 0));
                }
                peaks[i] = peak;
            }
            return { duration: Number(buffer.duration || 0), peaks, rawData: bytes };
        } finally { try { await ctx.close(); } catch (_) {} }
    })();
    decodeCache.set(url, promise);
    try { return await promise; } catch (e) { decodeCache.delete(url); throw e; }
}

function fmt(seconds) {
    const s = Math.max(0, Number(seconds) || 0);
    const m = Math.floor(s / 60);
    const sec = s - m * 60;
    return `${String(m).padStart(2, "0")}:${sec.toFixed(2).padStart(5, "0")}`;
}
function fmtPreviewPosition(seconds) {
    const totalCs = Math.max(0, Math.round((Number(seconds) || 0) * 100));
    const minutes = Math.floor(totalCs / 6000);
    const secondsPart = Math.floor((totalCs % 6000) / 100);
    const centis = totalCs % 100;
    return `${String(minutes).padStart(2, "0")}:${String(secondsPart).padStart(2, "0")}:${String(centis).padStart(2, "0")}`;
}
function clipSourceDuration(node, clip) {
    const rt = node.__urnMixerRuntime?.get(clip.id);
    return Math.max(0, Number(rt?.duration || 0), Number(clip.sourceDuration || 0), Number(clip.end || 0));
}
function clipTimelineEnd(node, clip) {
    // The virtual/master timeline should end where the USED portion of the clip
    // ends, not at the end of the original source file. This keeps the visible
    // timeline, preview range, and exported AUDIO length aligned with the last
    // trimmed clip across all tracks.
    const sourceTotal = clipSourceDuration(node, clip);
    const start = Math.max(0, Math.min(Number(clip.start || 0), sourceTotal));
    const end = Number(clip.end) > 0
        ? Math.max(start, Math.min(Number(clip.end), sourceTotal))
        : sourceTotal;
    return Math.max(0, Number(clip.position || 0)) + end;
}
function trackTimelineEnd(node, track) {
    let end = 0;
    for (const clip of track?.clips || []) end = Math.max(end, clipTimelineEnd(node, clip));
    return end;
}
function masterDuration(node) {
    // USED master duration. This is the real audible/export end: the furthest
    // trimmed clip end across all tracks. Preview controls use this duration so
    // they stop where the final audible clip finishes.
    let end = 0;
    for (const track of node.__urnMixerTracks || []) end = Math.max(end, trackTimelineEnd(node, track));
    return Math.max(0.001, end || 1);
}
function displayMasterDuration(node) {
    // DISPLAY duration must not collapse when the final clip's Trim End handle
    // is dragged left. If the canvas scale followed the USED master duration,
    // the right edge of the timeline would move left at exactly the same rate
    // as the trim handle, making the end line appear stuck at the right edge
    // while the fade curves rescaled underneath it.
    //
    // Keep enough visual timeline to show every source file at its timeline
    // position. The backend/export duration still uses masterDuration() above,
    // so trimmed-away tails are NOT included in the AUDIO output.
    let end = masterDuration(node);
    for (const { clip } of allClips(node)) {
        end = Math.max(end, Math.max(0, Number(clip.position || 0)) + clipSourceDuration(node, clip));
    }
    return Math.max(0.001, end || 1);
}
function usedTimelineSpan(node) {
    let earliest = Infinity;
    let latest = 0;
    let found = false;
    for (const { clip } of allClips(node)) {
        const sourceTotal = clipSourceDuration(node, clip);
        const start = Math.max(0, Math.min(Number(clip.start || 0), sourceTotal));
        const end = Number(clip.end) > 0
            ? Math.max(start, Math.min(Number(clip.end), sourceTotal))
            : sourceTotal;
        if (end <= start) continue;
        const position = Math.max(0, Number(clip.position || 0));
        earliest = Math.min(earliest, position + start);
        latest = Math.max(latest, position + end);
        found = true;
    }
    return found ? { start: earliest, end: latest, duration: Math.max(0, latest - earliest) } : { start: 0, end: 0, duration: 0 };
}
function exportDuration(node) {
    const span = usedTimelineSpan(node);
    return Math.max(0, padValue(node, PAD_START_WIDGET) + span.duration + padValue(node, PAD_END_WIDGET));
}
function updateExportLengthReadout(node) {
    const label = node?.__urnMixerExportLength;
    if (!label) return;
    label.textContent = fmtPreviewPosition(exportDuration(node));
}

function clampClip(clip, duration) {
    const total = Math.max(0, Number(duration || 0));
    clip.start = Math.max(0, Math.min(Number(clip.start) || 0, total));
    if (!(Number(clip.end) > 0)) clip.end = total;
    clip.end = Math.max(clip.start, Math.min(Number(clip.end) || total, total));
    const selected = Math.max(0, clip.end - clip.start);
    clip.fadeIn = Math.max(0, Math.min(Number(clip.fadeIn) || 0, selected));
    clip.fadeOut = Math.max(0, Math.min(Number(clip.fadeOut) || 0, selected));
    // Do not clamp the timeline position to the current master duration. The
    // position itself is allowed to extend the virtual timeline.
    clip.position = Math.max(0, Number(clip.position) || 0);
}
function selectedClipForTrack(node, track) {
    if (!track?.clips?.length) return null;
    const map = node.__urnMixerSelectedClips || (node.__urnMixerSelectedClips = new Map());
    let id = map.get(track.id);
    let clip = track.clips.find((c) => c.id === id) || null;
    if (!clip) {
        clip = track.clips[0] || null;
        if (clip) map.set(track.id, clip.id);
    }
    return clip;
}
function selectClip(node, track, clipId) {
    const map = node.__urnMixerSelectedClips || (node.__urnMixerSelectedClips = new Map());
    if (clipId && track?.clips?.some((c) => c.id === clipId)) map.set(track.id, clipId);
    else map.delete(track.id);
    const trt = node.__urnMixerTrackRuntime?.get(track.id);
    if (trt?.removeFileButton) {
        const has = !!selectedClipForTrack(node, track);
        trt.removeFileButton.disabled = !has;
        trt.removeFileButton.style.opacity = has ? '1' : '.45';
    }
    if (trt?.canvas) drawTrack(node, track, trt.canvas);
}
function redrawAll(node) {
    for (const track of node.__urnMixerTracks || []) {
        const trt = node.__urnMixerTrackRuntime?.get(track.id);
        if (trt?.canvas) drawTrack(node, track, trt.canvas);
    }
}

function shapedFadeValue(p) { p = Math.max(0, Math.min(1, Number(p) || 0)); return 0.5 - 0.5 * Math.cos(Math.PI * p); }
function envelopeValue(t, duration, fadeIn, fadeOut) {
    let g = 1.0;
    if (fadeIn > 0 && t < fadeIn) g *= shapedFadeValue(t / fadeIn);
    if (fadeOut > 0 && t > duration - fadeOut) g *= shapedFadeValue((duration - t) / fadeOut);
    return Math.max(0, Math.min(1, g));
}
function updatePreviewPositionReadout(node, seconds = null) {
    const label = node?.__urnMixerPreviewPosition;
    if (!label) return;
    const master = masterDuration(node);
    const sliderValue = Number(node?.__urnMixerPreviewSlider?.value) || 0;
    const t = seconds == null ? sliderValue : Number(seconds) || 0;
    label.textContent = fmtPreviewPosition(Math.max(0, Math.min(master, t)));
}
function updatePreviewControls(node) {
    const slider = node?.__urnMixerPreviewSlider;
    if (!slider) return;
    const master = masterDuration(node);
    slider.max = String(master);
    const v = Math.max(0, Math.min(master, Number(slider.value) || 0));
    slider.value = String(v);
    slider.title = `Preview starts at ${fmt(v)}`;
    updatePreviewPositionReadout(node, v);
    updateExportLengthReadout(node);
}
function previewButtonState(node, label, disabled = false) {
    const b = node?.__urnMixerPreviewButton;
    if (!b) return;
    b.textContent = label; b.disabled = !!disabled; b.style.opacity = disabled ? ".65" : "1";
}
function currentPreviewTime(node) {
    const p = node?.__urnMixerPreview;
    if (!p?.ctx || !Number.isFinite(p.playAt)) return null;
    const elapsed = Math.max(0, p.ctx.currentTime - p.playAt);
    return Math.max(0, Math.min(p.master, p.previewStart + elapsed));
}
function ensurePreviewAnimation(node) {
    if (node?.__urnMixerPreviewRAF) return;
    const tick = () => {
        if (!node?.__urnMixerPreview) { node.__urnMixerPreviewRAF = null; redrawAll(node); return; }
        const t = currentPreviewTime(node);
        const slider = node.__urnMixerPreviewSlider;
        if (slider && t != null) {
            slider.value = String(t);
            slider.title = `Preview position ${fmt(t)}`;
            updatePreviewPositionReadout(node, t);
        }
        redrawAll(node);
        node.__urnMixerPreviewRAF = window.requestAnimationFrame(tick);
    };
    node.__urnMixerPreviewRAF = window.requestAnimationFrame(tick);
}
async function stopMixerPreview(node, natural = false) {
    const p = node?.__urnMixerPreview;
    if (!p) { previewButtonState(node, "PREVIEW AUDIO"); return; }
    node.__urnMixerPreview = null;
    try { if (p.timer) clearTimeout(p.timer); } catch (_) {}
    try { if (node.__urnMixerPreviewRAF) cancelAnimationFrame(node.__urnMixerPreviewRAF); } catch (_) {}
    node.__urnMixerPreviewRAF = null;
    if (!natural) for (const src of p.sources || []) try { src.stop(); } catch (_) {}
    for (const src of p.sources || []) try { src.disconnect(); } catch (_) {}
    for (const gain of p.gains || []) try { gain.disconnect(); } catch (_) {}
    try { await p.ctx?.close?.(); } catch (_) {}
    previewButtonState(node, "PREVIEW AUDIO");
    redrawAll(node);
}
async function startMixerPreview(node) {
    if (node.__urnMixerPreview) { await stopMixerPreview(node); return; }
    const entries = allClips(node);
    if (!entries.length) {
        previewButtonState(node, "ADD AUDIO FILES FIRST");
        setTimeout(() => previewButtonState(node, "PREVIEW AUDIO"), 1300);
        return;
    }
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) { previewButtonState(node, "PREVIEW UNAVAILABLE"); return; }
    const master = masterDuration(node);
    updatePreviewControls(node);
    const previewStart = Math.max(0, Math.min(master, Number(node.__urnMixerPreviewSlider?.value) || 0));
    if (previewStart >= master - 0.001) {
        previewButtonState(node, "START IS AT END");
        setTimeout(() => previewButtonState(node, "PREVIEW AUDIO"), 1300);
        return;
    }
    const ctx = new AC();
    const token = Symbol("urnAudioMixerPreview");
    const state = { ctx, token, sources: [], gains: [], timer: null, playAt: NaN, previewStart, master };
    node.__urnMixerPreview = state;
    previewButtonState(node, "LOADING PREVIEW…", true);
    try {
        await ctx.resume();
        const decoded = await Promise.all(entries.map(async ({ track, clip }) => {
            const d = await decodeClip(clip);
            const buffer = await ctx.decodeAudioData(d.rawData.slice(0));
            return { track, clip, buffer };
        }));
        if (node.__urnMixerPreview?.token !== token) { try { await ctx.close(); } catch (_) {} return; }
        const playAt = ctx.currentTime + 0.04;
        state.playAt = playAt;
        let scheduled = 0;
        for (const { track, clip, buffer } of decoded) {
            const sourceTotal = Math.max(0, Number(buffer.duration || 0));
            if (sourceTotal <= 0) continue;
            clampClip(clip, sourceTotal);
            const position = Math.max(0, Number(clip.position) || 0);
            const start = Math.max(0, Math.min(Number(clip.start) || 0, sourceTotal));
            const end = Math.max(start, Math.min(Number(clip.end) > 0 ? Number(clip.end) : sourceTotal, sourceTotal));
            const selectedDuration = Math.max(0, end - start);
            if (selectedDuration <= 0) continue;
            const clipStart = position + start;
            const clipEnd = position + end;
            const overlapStart = Math.max(previewStart, clipStart);
            const overlapEnd = Math.min(master, clipEnd);
            if (overlapEnd <= overlapStart) continue;
            const localStart = overlapStart - clipStart;
            const sourceOffset = start + localStart;
            const playDuration = overlapEnd - overlapStart;
            const delay = overlapStart - previewStart;
            const fadeIn = Math.min(selectedDuration, Math.max(0, Number(clip.fadeIn) || 0));
            const fadeOut = Math.min(selectedDuration, Math.max(0, Number(clip.fadeOut) || 0));
            const volume = Math.max(0, Math.min(100, Number(track.volume ?? 100) || 0)) / 100;
            const src = ctx.createBufferSource();
            const gain = ctx.createGain();
            src.buffer = buffer; src.connect(gain); gain.connect(ctx.destination);
            const points = Math.max(64, Math.min(2048, Math.ceil(playDuration * 100)));
            const curve = new Float32Array(points);
            for (let i = 0; i < points; i++) {
                const rel = playDuration * i / Math.max(1, points - 1);
                curve[i] = envelopeValue(localStart + rel, selectedDuration, fadeIn, fadeOut) * volume;
            }
            const scheduledAt = playAt + delay;
            gain.gain.setValueCurveAtTime(curve, scheduledAt, playDuration);
            src.start(scheduledAt, sourceOffset, playDuration);
            state.sources.push(src); state.gains.push(gain); scheduled++;
        }
        if (!scheduled) throw new Error("No audible mixer clips overlap the selected preview start.");
        state.timer = setTimeout(() => {
            if (node.__urnMixerPreview?.token === token) stopMixerPreview(node, true);
        }, Math.max(1, (master - previewStart) * 1000) + 120);
        ensurePreviewAnimation(node);
        previewButtonState(node, "■ STOP PREVIEW");
    } catch (err) {
        console.warn("[URN Audio Mixer] preview failed", err);
        if (node.__urnMixerPreview?.token === token) {
            await stopMixerPreview(node);
            previewButtonState(node, "PREVIEW FAILED");
            setTimeout(() => previewButtonState(node, "PREVIEW AUDIO"), 1500);
        } else { try { await ctx.close(); } catch (_) {} }
    }
}

function waveformMetrics(canvas) {
    const r = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const pxW = Math.max(1, Math.round(r.width * dpr));
    const pxH = Math.max(1, Math.round(r.height * dpr));
    if (canvas.width !== pxW || canvas.height !== pxH) { canvas.width = pxW; canvas.height = pxH; }
    return { dpr, w: pxW / dpr, h: pxH / dpr };
}
function fadeCurve(ctx, x0, x1, top, bottom, fadeIn = true) {
    const width = Math.max(1, x1 - x0);
    const steps = Math.max(8, Math.min(120, Math.floor(width / 3)));
    ctx.beginPath();
    for (let i = 0; i <= steps; i++) {
        const p = i / steps;
        const shaped = 0.5 - 0.5 * Math.cos(Math.PI * p);
        const amplitude = fadeIn ? shaped : 1 - shaped;
        const x = x0 + width * p;
        const y = bottom - amplitude * (bottom - top);
        if (!i) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();
}
function drawTrack(node, track, canvas) {
    const trt = node.__urnMixerTrackRuntime?.get(track.id) || {};
    const { dpr, w, h } = waveformMetrics(canvas);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = "#101214"; ctx.fillRect(0, 0, w, h);

    const left = 8, right = w - 8, top = 25, bottom = h - 25;
    const mid = (top + bottom) / 2, spanPx = Math.max(1, right - left);
    // Use a stable display extent based on the full source footprints. The
    // audible/export master may shrink as Trim End moves, but the drawing scale
    // must stay put so the trim line visibly moves across the waveform.
    const master = displayMasterDuration(node);
    const tx = (timelineSec) => left + Math.max(0, Math.min(master, Number(timelineSec) || 0)) / master * spanPx;
    const fontPx = Math.max(13, Number(window.LiteGraph?.NODE_TEXT_SIZE || 14));
    ctx.font = `${fontPx}px sans-serif`; ctx.fillStyle = "rgba(235,235,235,.82)";
    ctx.fillText("0:00", left, fontPx + 1);
    const totalText = fmt(master); ctx.fillText(totalText, right - ctx.measureText(totalText).width, fontPx + 1);

    const selected = selectedClipForTrack(node, track);
    const geoms = new Map();
    for (const clip of track.clips || []) {
        const rt = node.__urnMixerRuntime?.get(clip.id) || { status: "Loading waveform…" };
        node.__urnMixerRuntime?.set(clip.id, rt);
        const sourceTotal = Math.max(0.001, Number(rt.duration || clip.sourceDuration || clip.end || 1));
        clampClip(clip, sourceTotal);
        const position = Math.max(0, Number(clip.position) || 0);
        const effectiveEnd = Number(clip.end) > 0 ? Math.min(clip.end, sourceTotal) : sourceTotal;
        const start = Math.max(0, Math.min(clip.start, effectiveEnd));
        const end = Math.max(start, effectiveEnd);
        const length = Math.max(0, end - start);
        const fadeIn = Math.min(length, Math.max(0, clip.fadeIn));
        const fadeOut = Math.min(length, Math.max(0, clip.fadeOut));
        const sourceTx = (sourceSec) => tx(position + sourceSec);
        const clipX0 = sourceTx(0), clipX1 = sourceTx(sourceTotal), clipWidth = Math.max(1, clipX1 - clipX0);
        const sx = sourceTx(start), ex = sourceTx(end), fix = sourceTx(start + fadeIn), fox = sourceTx(end - fadeOut);

        ctx.fillStyle = "rgba(110,165,215,.10)"; ctx.fillRect(clipX0, top, clipWidth, bottom - top);
        if (rt.peaks?.length) {
            ctx.strokeStyle = "rgba(165,180,194,.72)"; ctx.lineWidth = 1; ctx.beginPath();
            const amp = (bottom - top) * 0.43;
            for (let i = 0; i < rt.peaks.length; i++) {
                const x = clipX0 + i / Math.max(1, rt.peaks.length - 1) * clipWidth;
                const ph = Math.max(0.5, rt.peaks[i] * amp);
                ctx.moveTo(x, mid - ph); ctx.lineTo(x, mid + ph);
            }
            ctx.stroke();
        } else {
            ctx.fillStyle = "rgba(230,230,230,.65)"; ctx.textAlign = "center";
            ctx.fillText(rt.status || "Loading waveform…", (clipX0 + clipX1) / 2, mid + 4); ctx.textAlign = "left";
        }

        ctx.fillStyle = "rgba(0,0,0,.58)";
        ctx.fillRect(clipX0, top, Math.max(0, sx - clipX0), bottom - top);
        ctx.fillRect(ex, top, Math.max(0, clipX1 - ex), bottom - top);
        ctx.fillStyle = "rgba(110,165,215,.16)"; ctx.fillRect(sx, top, Math.max(1, ex - sx), bottom - top);

        ctx.fillStyle = "rgba(245,245,245,.96)"; ctx.strokeStyle = "rgba(245,245,245,.96)"; ctx.lineWidth = 2;
        for (const x of [sx, ex]) { ctx.beginPath(); ctx.moveTo(x, top - 1); ctx.lineTo(x, bottom + 1); ctx.stroke(); }
        ctx.lineWidth = 1.5;
        if (fix > sx + 1) fadeCurve(ctx, sx, fix, top + 2, bottom - 2, true);
        if (ex > fox + 1) fadeCurve(ctx, fox, ex, top + 2, bottom - 2, false);
        const diamond = (x) => { const y = top + 8; ctx.beginPath(); ctx.moveTo(x, y - 4); ctx.lineTo(x + 4, y); ctx.lineTo(x, y + 4); ctx.lineTo(x - 4, y); ctx.closePath(); ctx.fill(); };
        diamond(fix); diamond(fox);

        // Small clip label so multiple files on one track remain identifiable.
        if (clipWidth > 75) {
            ctx.save(); ctx.beginPath(); ctx.rect(clipX0 + 2, top + 1, Math.max(1, clipWidth - 4), 18); ctx.clip();
            ctx.font = `${Math.max(11, fontPx - 2)}px sans-serif`; ctx.fillStyle = "rgba(245,245,245,.88)";
            ctx.fillText(String(clip.displayName || clip.name || "Audio"), clipX0 + 5, top + 14); ctx.restore();
        }

        const isSelected = selected?.id === clip.id;
        ctx.save();
        ctx.strokeStyle = isSelected ? "#ff3b30" : "rgba(175,185,195,.38)";
        ctx.lineWidth = isSelected ? 2 : 1;
        ctx.strokeRect(Math.round(clipX0) + 0.5, top + 0.5, Math.max(1, Math.round(clipWidth) - 1), bottom - top - 1);
        ctx.restore();

        geoms.set(clip.id, { clip, left, right, spanPx, master, sourceTotal, position, clipX0, clipX1, sx, ex, fix, fox, start, end, length });
    }

    // Fade-handle guide for the active selected clip.
    if (trt.dragClipId && (trt.drag === "fadeIn" || trt.drag === "fadeOut")) {
        const g = geoms.get(trt.dragClipId);
        if (g) {
            const guideX = Math.round(trt.drag === "fadeIn" ? g.fix : g.fox) + 0.5;
            ctx.save(); ctx.strokeStyle = "#ff9800"; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(guideX, top); ctx.lineTo(guideX, bottom); ctx.stroke(); ctx.restore();
        }
    }

    const livePreviewTime = currentPreviewTime(node);
    const sliderPreviewTime = Number(node.__urnMixerPreviewSlider?.value);
    const previewTime = livePreviewTime != null ? livePreviewTime : (Number.isFinite(sliderPreviewTime) ? Math.max(0, Math.min(master, sliderPreviewTime)) : null);
    if (previewTime != null) {
        const playX = Math.round(tx(previewTime)) + 0.5;
        ctx.save(); ctx.strokeStyle = "#ff9800"; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(playX, top); ctx.lineTo(playX, bottom); ctx.stroke(); ctx.restore();
    }

    // Bottom readout follows the selected file.
    if (selected) {
        const g = geoms.get(selected.id);
        if (g) {
            ctx.font = `${fontPx}px sans-serif`; ctx.fillStyle = "rgba(245,245,245,.95)"; ctx.textAlign = "center";
            ctx.fillText(`${fmt(g.position + g.start)}  →  ${fmt(g.position + g.end)}   (${g.length.toFixed(2)}s)`, w / 2, h - 6); ctx.textAlign = "left";
        }
    }

    trt.canvas = canvas; trt.geoms = geoms; trt.left = left; trt.right = right; trt.spanPx = spanPx; trt.master = master;
    node.__urnMixerTrackRuntime?.set(track.id, trt);
}
function clipHandleAt(trt, x, y = null) {
    const geoms = trt?.geoms; if (!geoms?.size) return null;
    const selectedId = trt?.track ? selectedClipForTrack(trt.node, trt.track)?.id : null;
    const ordered = [...geoms.values()];
    if (selectedId) ordered.sort((a, b) => (a.clip.id === selectedId ? -1 : b.clip.id === selectedId ? 1 : 0));

    // Fade handles are the small diamonds at the top of the clip. Previously
    // hit-testing used X only, so when a fade-out diamond sat close to the trim
    // end line, clicking the end line could accidentally grab fadeOut instead.
    // Use the pointer Y position to make those controls unambiguous:
    //   - near the top/diamond row -> fade handles first
    //   - everywhere else -> trim start/end first
    const top = 25;
    const fadeZone = y != null && y <= top + 24;

    const groups = fadeZone
        ? [ [["fadeIn", "fix"], ["fadeOut", "fox"]], [["start", "sx"], ["end", "ex"]] ]
        : [ [["start", "sx"], ["end", "ex"]], [["fadeIn", "fix"], ["fadeOut", "fox"]] ];

    for (const group of groups) {
        let best = null, bestDist = 10;
        for (const g of ordered) {
            for (const [kind, key] of group) {
                const px = g[key];
                const d = Math.abs(px - x);
                if (d < bestDist) { bestDist = d; best = { clip: g.clip, geom: g, kind }; }
            }
        }
        if (best) return best;
    }
    return null;
}
function clipAtX(trt, x) {
    const geoms = [...(trt?.geoms?.values?.() || [])];
    if (!geoms.length) return null;
    const selectedId = trt?.track ? selectedClipForTrack(trt.node, trt.track)?.id : null;
    if (selectedId) {
        const sg = trt.geoms.get(selectedId); if (sg && x >= sg.clipX0 && x <= sg.clipX1) return sg;
    }
    for (let i = geoms.length - 1; i >= 0; i--) if (x >= geoms[i].clipX0 && x <= geoms[i].clipX1) return geoms[i];
    return null;
}
function xToSourceTimeFromGeom(g, x) {
    if (!g) return 0;
    const timeline = Math.max(0, Math.min(1, (x - g.left) / Math.max(1, g.spanPx))) * g.master;
    return Math.max(0, Math.min(g.sourceTotal, timeline - g.position));
}
function dragClip(clip, rt, kind, value) {
    const total = Math.max(0, rt?.duration || clip.end || 0);
    const start0 = Math.max(0, Math.min(clip.start || 0, total));
    const end0 = Number(clip.end) > 0 ? Math.max(start0, Math.min(clip.end, total)) : total;
    const minGap = 0.001;

    // Trim handles must remain independent from the fade handles. In older
    // builds, shortening a clip could clamp BOTH fades to the new clip length,
    // which made the two fade curves sweep across the clip and looked as if the
    // trim-end handle itself was dragging the fades. Preserve the opposite-side
    // fade and only shorten the fade adjacent to the trim edge when necessary.
    if (kind === "start") {
        const newStart = Math.max(0, Math.min(value, end0 - minGap));
        clip.start = newStart;
        const span = Math.max(0, end0 - newStart);
        clip.fadeOut = Math.min(Math.max(0, Number(clip.fadeOut) || 0), span);
        clip.fadeIn = Math.min(Math.max(0, Number(clip.fadeIn) || 0), Math.max(0, span - clip.fadeOut));
        return;
    }

    if (kind === "end") {
        const newEnd = Math.max(start0 + minGap, Math.min(total, value));
        clip.end = newEnd;
        const span = Math.max(0, newEnd - start0);
        clip.fadeIn = Math.min(Math.max(0, Number(clip.fadeIn) || 0), span);
        clip.fadeOut = Math.min(Math.max(0, Number(clip.fadeOut) || 0), Math.max(0, span - clip.fadeIn));
        return;
    }

    const span = Math.max(0, end0 - start0);
    if (kind === "fadeIn") {
        const maxFadeIn = Math.max(0, span - Math.max(0, Number(clip.fadeOut) || 0));
        clip.fadeIn = Math.max(0, Math.min(maxFadeIn, value - start0));
    } else if (kind === "fadeOut") {
        const maxFadeOut = Math.max(0, span - Math.max(0, Number(clip.fadeIn) || 0));
        clip.fadeOut = Math.max(0, Math.min(maxFadeOut, end0 - value));
    }
}
function dragClipPosition(clip, geom, trt, x) {
    if (!geom) return;
    // Freeze the seconds-per-pixel scale from pointer-down. The visible master
    // may grow while dragging; using a frozen scale keeps the clip attached to
    // the pointer instead of making it jump as the timeline rescales.
    const scaleMaster = Math.max(0.001, Number(trt.dragScaleMaster || geom.master || 1));
    const scaleSpanPx = Math.max(1, Number(trt.dragScaleSpanPx || geom.spanPx || 1));
    const deltaSec = (x - Number(trt.dragStartX || 0)) / scaleSpanPx * scaleMaster;
    clip.position = Math.max(0, Number(trt.dragStartPosition || 0) + deltaSec);
}

function makeButton(text) {
    const b = document.createElement("button"); b.type = "button"; b.textContent = text;
    Object.assign(b.style, { border: "1px solid #666", borderRadius: "5px", background: "rgba(255,255,255,.07)", color: "inherit", cursor: "pointer", height: "28px", padding: "0 10px", fontSize: "13px" });
    return b;
}
function resolveAutoPlacement(node, track) {
    // Newly-added clips are appended after the furthest end of the clips that
    // precede them in this track. Keep ordering deterministic even if browser
    // audio decoding completes out of order.
    let rightmost = 0;
    for (const clip of track.clips || []) {
        const duration = clipSourceDuration(node, clip);
        if (clip.autoPlacePending === true) {
            if (!(duration > 0)) break;
            clip.position = Math.max(0, rightmost);
            clip.autoPlacePending = false;
        }
        rightmost = Math.max(rightmost, clipTimelineEnd(node, clip));
    }
}

function decodeTrackClips(node, track, canvas) {
    for (const clip of track.clips || []) {
        const rt = node.__urnMixerRuntime.get(clip.id) || { status: "Loading waveform…" };
        node.__urnMixerRuntime.set(clip.id, rt);
        if (rt.decodeStarted) continue;
        rt.decodeStarted = true;
        decodeClip(clip).then((decoded) => {
            if (!track.clips?.some((c) => c.id === clip.id)) return;
            const runtime = node.__urnMixerRuntime.get(clip.id) || {};
            runtime.duration = decoded.duration; runtime.peaks = decoded.peaks; runtime.status = ""; runtime.decodeStarted = true;
            node.__urnMixerRuntime.set(clip.id, runtime);
            const applyDefaults = clip.fadeDefaultsPending === true;
            clip.sourceDuration = decoded.duration;
            if (applyDefaults) {
                clip.start = 0; clip.end = decoded.duration;
                const defaultFade = Math.max(0, decoded.duration * 0.05);
                clip.fadeIn = defaultFade; clip.fadeOut = defaultFade; clip.fadeDefaultsPending = false;
            }
            resolveAutoPlacement(node, track);
            clampClip(clip, decoded.duration);
            saveState(node); updatePreviewControls(node); redrawAll(node);
        }).catch((err) => {
            const runtime = node.__urnMixerRuntime.get(clip.id) || {};
            runtime.status = "Waveform unavailable"; runtime.decodeStarted = true; node.__urnMixerRuntime.set(clip.id, runtime);
            console.warn("[URN Audio Mixer] waveform decode failed", clip.displayName, err); drawTrack(node, track, canvas);
        });
    }
}

function makeTrackCard(node, track, index) {
    const card = document.createElement("div");
    Object.assign(card.style, { border: "1px solid #65686c", borderRadius: "9px", padding: "9px", marginBottom: "10px", background: "rgba(255,255,255,.025)", boxSizing: "border-box" });
    const header = document.createElement("div");
    Object.assign(header.style, { display: "flex", alignItems: "center", gap: "10px", minHeight: "34px" });
    const title = document.createElement("div"); title.textContent = track.name || `Track ${index + 1}`;
    Object.assign(title.style, { flex: "0 0 90px", fontSize: "15px", fontWeight: "700" });
    const pct = document.createElement("div"); pct.textContent = `${Math.round(track.volume)}%`;
    Object.assign(pct.style, { flex: "0 0 48px", textAlign: "right", fontSize: "14px", fontWeight: "600" });
    const speaker = document.createElement("div"); speaker.textContent = "🔊"; Object.assign(speaker.style, { flex: "0 0 22px", fontSize: "15px", textAlign: "center" });
    const volume = document.createElement("input"); volume.type = "range"; volume.min = "0"; volume.max = "100"; volume.step = "1"; volume.value = String(track.volume);
    Object.assign(volume.style, { flex: "1 1 220px", minWidth: "120px", accentColor: "#5d9cec" });
    for (const ev of ["pointerdown", "mousedown", "click"]) volume.addEventListener(ev, (e) => e.stopPropagation());
    volume.addEventListener("input", () => { if (node.__urnMixerPreview) stopMixerPreview(node); track.volume = Number(volume.value); pct.textContent = `${Math.round(track.volume)}%`; saveState(node); });

    const add = makeButton("Add Audio File");
    const picker = document.createElement("input"); picker.type = "file"; picker.multiple = true; picker.accept = "audio/*,.mp3,.wav,.flac,.ogg,.opus,.m4a,.aac,.aif,.aiff,.webm"; picker.style.display = "none";
    add.addEventListener("click", (e) => { stopEvent(e); picker.click(); });
    picker.addEventListener("change", async () => { await addFilesToTrack(node, track, picker.files); picker.value = ""; });

    const removeFile = makeButton("Remove File");
    const selected = selectedClipForTrack(node, track); removeFile.disabled = !selected; removeFile.style.opacity = selected ? "1" : ".45";
    removeFile.addEventListener("click", async (e) => {
        stopEvent(e);
        if (node.__urnMixerPreview) await stopMixerPreview(node);

        // Resolve the current track from node state rather than mutating the
        // card's captured object. This keeps removal local even after rerenders.
        const liveTrack = (node.__urnMixerTracks || []).find((t) => t.id === track.id);
        if (!liveTrack) return;
        const selectedId = node.__urnMixerSelectedClips?.get(liveTrack.id);
        if (!selectedId) return;
        const idx = (liveTrack.clips || []).findIndex((c) => c.id === selectedId);
        if (idx < 0) return;

        const removed = liveTrack.clips[idx];
        const nextClips = liveTrack.clips.filter((c) => c.id !== selectedId);
        const nextTrack = { ...liveTrack, clips: nextClips };
        node.__urnMixerTracks = (node.__urnMixerTracks || []).map((t) => t.id === liveTrack.id ? nextTrack : t);
        node.__urnMixerRuntime?.delete(removed.id);

        const next = nextClips[Math.min(idx, Math.max(0, nextClips.length - 1))] || null;
        if (next) node.__urnMixerSelectedClips.set(liveTrack.id, next.id);
        else node.__urnMixerSelectedClips.delete(liveTrack.id);

        saveState(node);
        render(node);
    });

    const removeTrack = makeButton("Remove Track");
    removeTrack.addEventListener("click", async (e) => {
        stopEvent(e);
        if (node.__urnMixerPreview) await stopMixerPreview(node);

        const liveTrack = (node.__urnMixerTracks || []).find((t) => t.id === track.id);
        if (!liveTrack) return;
        for (const clip of liveTrack.clips || []) node.__urnMixerRuntime?.delete(clip.id);
        node.__urnMixerTrackRuntime?.delete(liveTrack.id);
        node.__urnMixerSelectedClips?.delete(liveTrack.id);
        node.__urnMixerTracks = (node.__urnMixerTracks || []).filter((t) => t.id !== liveTrack.id);

        saveState(node);
        render(node);
    });
    header.append(title, pct, speaker, volume, add, picker, removeFile, removeTrack);

    const body = document.createElement("div"); Object.assign(body.style, { marginTop: "7px" });
    if (!track.clips?.length) {
        const empty = document.createElement("div"); empty.textContent = "No audio files in this track — use Add Audio File or drop files here.";
        Object.assign(empty.style, { border: "1px dashed #555", borderRadius: "6px", padding: "22px", textAlign: "center", opacity: ".65", fontSize: "13px" });
        body.append(empty);
    } else {
        const canvas = document.createElement("canvas");
        Object.assign(canvas.style, { width: "100%", height: "175px", display: "block", border: "1px solid #4f5358", borderRadius: "5px", background: "#101214", cursor: "default", boxSizing: "border-box" });
        body.append(canvas);
        const trt = node.__urnMixerTrackRuntime.get(track.id) || {};
        Object.assign(trt, { canvas, node, track, removeFileButton: removeFile, geoms: new Map() });
        node.__urnMixerTrackRuntime.set(track.id, trt);
        const px = (e) => e.clientX - canvas.getBoundingClientRect().left;
        const py = (e) => e.clientY - canvas.getBoundingClientRect().top;
        canvas.addEventListener("pointerdown", (e) => {
            if (node.__urnMixerPreview) stopMixerPreview(node);
            const runtime = node.__urnMixerTrackRuntime.get(track.id), x = px(e);
            const handle = clipHandleAt(runtime, x, py(e));
            const geom = handle?.geom || clipAtX(runtime, x);
            if (!geom) return;
            selectClip(node, track, geom.clip.id);
            runtime.dragClipId = geom.clip.id;
            runtime.drag = handle?.kind || "move";
            runtime.pointerId = e.pointerId;
            runtime.dragStartX = x; runtime.dragStartPosition = Number(geom.clip.position || 0);
            runtime.dragScaleMaster = Number(geom.master || masterDuration(node));
            runtime.dragScaleSpanPx = Number(geom.spanPx || 1);
            if (runtime.drag === "move") canvas.style.cursor = "grabbing";
            try { canvas.setPointerCapture(e.pointerId); } catch (_) {}
            drawTrack(node, track, canvas); stopEvent(e);
        });
        canvas.addEventListener("pointermove", (e) => {
            const runtime = node.__urnMixerTrackRuntime.get(track.id), x = px(e);
            if (runtime?.drag && runtime.dragClipId) {
                const clip = track.clips.find((c) => c.id === runtime.dragClipId), geom = runtime.geoms?.get(runtime.dragClipId);
                if (clip && geom) {
                    if (runtime.drag === "move") {
                        dragClipPosition(clip, geom, runtime, x);
                        saveState(node);
                        updatePreviewControls(node);
                        // Moving one clip can extend the shared virtual master,
                        // so every track must be redrawn at the new scale.
                        redrawAll(node);
                    } else {
                        dragClip(clip, node.__urnMixerRuntime.get(clip.id), runtime.drag, xToSourceTimeFromGeom(geom, x));
                        saveState(node); drawTrack(node, track, canvas);
                    }
                    stopEvent(e);
                }
            } else {
                const handle = clipHandleAt(runtime, x, py(e)), geom = clipAtX(runtime, x);
                canvas.style.cursor = handle ? "ew-resize" : (geom ? "grab" : "default");
            }
        });
        const endDrag = (e) => {
            const runtime = node.__urnMixerTrackRuntime.get(track.id); if (!runtime?.drag) return;
            runtime.drag = null; runtime.dragClipId = null; runtime.dragStartX = null; runtime.dragStartPosition = null;
            runtime.dragScaleMaster = null; runtime.dragScaleSpanPx = null;
            try { canvas.releasePointerCapture(e.pointerId); } catch (_) {}
            saveState(node); updatePreviewControls(node); redrawAll(node);
            const x = px(e), handle = clipHandleAt(runtime, x, py(e)), geom = clipAtX(runtime, x);
            canvas.style.cursor = handle ? "ew-resize" : (geom ? "grab" : "default");
        };
        canvas.addEventListener("pointerup", endDrag); canvas.addEventListener("pointercancel", endDrag);

        // IMPORTANT: this card/canvas is still detached from the DOM while
        // makeTrackCard() is running. Drawing here makes getBoundingClientRect()
        // report ~0x0, which leaves previously-decoded clips looking blank after
        // any re-render (Add Track / Remove File / Remove Track). Defer the first
        // draw until the card has actually been appended to the mixer list.
        window.requestAnimationFrame(() => {
            const liveRuntime = node.__urnMixerTrackRuntime?.get(track.id);
            if (!canvas.isConnected || liveRuntime?.canvas !== canvas) return;
            drawTrack(node, track, canvas);
            decodeTrackClips(node, track, canvas);
        });
    }

    const setDropStyle = (active) => { card.style.borderColor = active ? "#5d9cec" : "#65686c"; card.style.background = active ? "rgba(93,156,236,.08)" : "rgba(255,255,255,.025)"; };
    const dragEnter = (e) => { if (!Array.from(e.dataTransfer?.items || []).some((x) => x.kind === "file")) return; stopEvent(e); setDropStyle(true); };
    card.addEventListener("dragenter", dragEnter); card.addEventListener("dragover", dragEnter);
    card.addEventListener("dragleave", (e) => { stopEvent(e); setDropStyle(false); });
    card.addEventListener("drop", async (e) => { stopEvent(e); setDropStyle(false); await addFilesToTrack(node, track, e.dataTransfer?.files); });
    card.append(header, body);
    return card;
}

function dynamicHeight(node) {
    const tracks = node.__urnMixerTracks || [];
    return Math.max(340, 175 + tracks.length * 235);
}
function resizeNode(node) {
    const width = Math.max(920, node.size?.[0] || 0), height = dynamicHeight(node) + 65;
    try { node.setSize?.([width, height]); } catch (_) {}
    node.graph?.setDirtyCanvas?.(true, true);
}
function render(node) {
    const list = node.__urnMixerList; if (!list) return;
    list.replaceChildren();
    const tracks = node.__urnMixerTracks || [];
    const master = masterDuration(node);
    let changed = false;
    for (const track of tracks) for (const clip of track.clips || []) {
        const rt = node.__urnMixerRuntime?.get(clip.id); const duration = Number(rt?.duration || clip.sourceDuration || 0);
        if (duration > 0) { const before = clip.position; clampClip(clip, duration); if (before !== clip.position) changed = true; }
    }
    if (changed) saveState(node);
    if (!tracks.length) {
        const empty = document.createElement("div"); empty.textContent = "No tracks yet. Click Add Track or drop audio files above.";
        Object.assign(empty.style, { border: "1px dashed #666", borderRadius: "7px", padding: "18px", textAlign: "center", opacity: ".7", fontSize: "14px" });
        list.append(empty);
    } else tracks.forEach((track, i) => list.append(makeTrackCard(node, track, i)));
    updatePreviewControls(node);
    if (node.__urnMixerWrap) node.__urnMixerWrap.style.height = `${dynamicHeight(node)}px`;
    resizeNode(node);
}

async function uploadAudioFile(file) {
    const body = new FormData(); body.append("image", file); body.append("type", "input"); body.append("subfolder", "urn_audio_mixer");
    const resp = await api.fetchApi("/upload/image", { method: "POST", body });
    if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
    const data = await resp.json();
    return { name: String(data.name || file.name), subfolder: String(data.subfolder || "urn_audio_mixer"), type: String(data.type || "input") };
}
function newClipFromUpload(uploaded, displayName) {
    const clip = normalizedClip({ ...uploaded, displayName, sourceDuration: 0, position: 0, start: 0, end: 0, fadeIn: 0, fadeOut: 0, fadeDefaultsPending: true });
    clip.autoPlacePending = true;
    return clip;
}
async function addFilesToTrack(node, track, files) {
    if (node.__urnMixerPreview) await stopMixerPreview(node);
    const audioFiles = Array.from(files || []).filter(isAudioFile); if (!audioFiles.length) return false;
    for (const file of audioFiles) {
        try {
            const uploaded = await uploadAudioFile(file);
            const clip = newClipFromUpload(uploaded, file.name);
            track.clips.push(clip);
            node.__urnMixerSelectedClips?.set(track.id, clip.id);
        } catch (err) { console.error("[URN Audio Mixer] upload failed", file.name, err); }
    }
    saveState(node); render(node); return true;
}
async function addFilesAsTracks(node, files) {
    if (node.__urnMixerPreview) await stopMixerPreview(node);
    const audioFiles = Array.from(files || []).filter(isAudioFile);
    if (!audioFiles.length) {
        if (node.__urnMixerDropText) node.__urnMixerDropText.textContent = "No supported audio files found.";
        setTimeout(() => { if (node.__urnMixerDropText) node.__urnMixerDropText.textContent = "Drop audio files here — each file creates a new track"; }, 1600);
        return false;
    }
    if (node.__urnMixerDropText) node.__urnMixerDropText.textContent = `Adding ${audioFiles.length} track${audioFiles.length === 1 ? "" : "s"}…`;
    for (const file of audioFiles) {
        try {
            const uploaded = await uploadAudioFile(file);
            const clip = newClipFromUpload(uploaded, file.name);
            clip.autoPlacePending = false;
            const track = normalizedTrack({ name: `Track ${node.__urnMixerTracks.length + 1}`, volume: 100, clips: [clip] }, node.__urnMixerTracks.length + 1);
            node.__urnMixerTracks.push(track);
            node.__urnMixerSelectedClips?.set(track.id, clip.id);
        } catch (err) { console.error("[URN Audio Mixer] upload failed", file.name, err); }
    }
    saveState(node); render(node);
    if (node.__urnMixerDropText) node.__urnMixerDropText.textContent = "Drop audio files here — each file creates a new track";
    return true;
}
function addEmptyTrack(node) {
    node.__urnMixerTracks.push(normalizedTrack({ name: `Track ${node.__urnMixerTracks.length + 1}`, volume: 100, clips: [] }, node.__urnMixerTracks.length + 1));
    saveState(node); render(node);
}

function install(node) {
    if (!isOurNode(node) || node.__urnAudioMixerInstalled || typeof node.addDOMWidget !== "function") return;
    node.__urnAudioMixerInstalled = true;
    loadState(node); hideStateWidget(node); hideBackendWidget(node, PAD_START_WIDGET); hideBackendWidget(node, PAD_END_WIDGET);
    const wrap = document.createElement("div");
    Object.assign(wrap.style, { width: "100%", minWidth: "790px", padding: "5px 7px 7px", boxSizing: "border-box", userSelect: "none", overflow: "hidden" });
    const drop = document.createElement("div");
    Object.assign(drop.style, { border: "1px dashed #70757b", borderRadius: "7px", background: "rgba(255,255,255,.035)", height: "48px", display: "flex", alignItems: "center", justifyContent: "center", gap: "12px", marginBottom: "8px", boxSizing: "border-box" });
    const dropText = document.createElement("div"); dropText.textContent = "Drop audio files here — each file creates a new track";
    Object.assign(dropText.style, { fontSize: "14px", opacity: ".8" });
    const addTrack = makeButton("Add Track"); addTrack.addEventListener("click", (e) => { stopEvent(e); addEmptyTrack(node); });
    drop.append(dropText, addTrack);

    const previewPanel = document.createElement("div"); Object.assign(previewPanel.style, { width: "100%", boxSizing: "border-box", marginBottom: "8px" });
    const previewTopRow = document.createElement("div"); Object.assign(previewTopRow.style, { display: "flex", alignItems: "center", gap: "12px", width: "100%", marginBottom: "7px", boxSizing: "border-box" });
    const previewButton = makeButton("PREVIEW AUDIO");
    Object.assign(previewButton.style, { display: "block", flex: "0 1 34%", maxWidth: "40%", width: "auto", height: "38px", fontSize: "20px", fontWeight: "500", letterSpacing: ".3px", minWidth: "220px" });
    previewButton.addEventListener("click", (e) => { stopEvent(e); startMixerPreview(node); });

    const makePadControl = (labelText, widgetName) => {
        const group = document.createElement("label");
        Object.assign(group.style, { display: "flex", alignItems: "center", gap: "6px", whiteSpace: "nowrap", fontSize: "15px", flex: "0 0 auto" });
        const label = document.createElement("span"); label.textContent = labelText;
        const input = document.createElement("input");
        input.type = "number"; input.min = "0"; input.step = "1"; input.inputMode = "numeric";
        input.value = String(padValue(node, widgetName)); input.title = "Export padding in whole seconds (0 or greater)";
        Object.assign(input.style, { width: "58px", height: "31px", boxSizing: "border-box", fontSize: "15px", padding: "2px 5px", background: "#292929", color: "inherit", border: "1px solid #666", borderRadius: "5px" });
        for (const ev of ["pointerdown", "mousedown", "click"]) input.addEventListener(ev, (e) => e.stopPropagation());
        const commit = () => { const v = setPadValue(node, widgetName, input.value); input.value = String(v); updateExportLengthReadout(node); };
        input.addEventListener("input", commit); input.addEventListener("change", commit); input.addEventListener("blur", commit);
        group.append(label, input);
        return { group, input };
    };
    const padStart = makePadControl("PAD START (Secs)", PAD_START_WIDGET);
    const padEnd = makePadControl("PAD END (Secs)", PAD_END_WIDGET);

    const makeTimeReadout = (labelText) => {
        const group = document.createElement("div");
        Object.assign(group.style, { display: "flex", alignItems: "center", gap: "6px", flex: "0 0 auto", whiteSpace: "nowrap", lineHeight: "38px", userSelect: "none" });
        const label = document.createElement("span"); label.textContent = labelText;
        Object.assign(label.style, { color: "#ffd400", fontSize: "17px", fontWeight: "500" });
        const value = document.createElement("span"); value.textContent = "00:00:00";
        Object.assign(value.style, { color: "#ffd400", fontSize: "21px", fontWeight: "500", fontVariantNumeric: "tabular-nums" });
        group.append(label, value);
        return { group, value };
    };
    const playPosition = makeTimeReadout("Play Pos:");
    const exportLength = makeTimeReadout("Export Length:");
    previewTopRow.append(previewButton, padStart.group, padEnd.group, playPosition.group, exportLength.group);
    const previewSlider = document.createElement("input"); previewSlider.type = "range"; previewSlider.min = "0"; previewSlider.max = "1"; previewSlider.step = "0.01"; previewSlider.value = "0";
    Object.assign(previewSlider.style, { display: "block", width: "100%", height: "28px", margin: "0", cursor: "pointer", accentColor: "#ffb300", boxSizing: "border-box" });
    for (const ev of ["pointerdown", "mousedown", "click"]) previewSlider.addEventListener(ev, (e) => e.stopPropagation());
    previewSlider.addEventListener("input", () => { if (node.__urnMixerPreview) stopMixerPreview(node); updatePreviewControls(node); redrawAll(node); });
    previewPanel.append(previewTopRow, previewSlider);
    const list = document.createElement("div"); Object.assign(list.style, { width: "100%", boxSizing: "border-box" });
    wrap.append(drop, previewPanel, list);
    node.__urnMixerWrap = wrap; node.__urnMixerList = list; node.__urnMixerDropText = dropText; node.__urnMixerPreviewButton = previewButton; node.__urnMixerPreviewSlider = previewSlider; node.__urnMixerPreviewPosition = playPosition.value; node.__urnMixerExportLength = exportLength.value; node.__urnMixerPadStartInput = padStart.input; node.__urnMixerPadEndInput = padEnd.input;
    syncPadControls(node);
    updatePreviewPositionReadout(node, 0);
    updateExportLengthReadout(node);
    const dw = node.addDOMWidget(UI_WIDGET, UI_WIDGET, wrap, { serialize: false, getMinHeight: () => dynamicHeight(node) });
    dw.serialize = false; if (dw.options) dw.options.serialize = false;

    // Persist the complete mixer project in the workflow itself. The normal
    // hidden mix_state_json widget remains the execution payload; the node
    // property is a second durable copy used to restore tracks/clips after a
    // workflow close/reopen or full ComfyUI restart.
    const oldSerialize = node.onSerialize;
    node.onSerialize = function (data) {
        saveState(this);
        data = data || {};
        data.properties = data.properties || {};
        data.properties[STATE_PROPERTY] = String(widget(this, STATE_WIDGET)?.value || this.properties?.[STATE_PROPERTY] || "[]");
        return oldSerialize?.apply(this, arguments);
    };

    const oldConfigure = node.onConfigure;
    node.onConfigure = function (info) {
        const r = oldConfigure?.apply(this, arguments);
        const persisted = String(info?.properties?.[STATE_PROPERTY] || this.properties?.[STATE_PROPERTY] || "").trim();
        if (persisted) {
            this.properties = this.properties || {};
            this.properties[STATE_PROPERTY] = persisted;
            const stateWidget = widget(this, STATE_WIDGET);
            if (stateWidget) stateWidget.value = persisted;
            loadState(this);
            setTimeout(() => { syncPadControls(this); render(this); updatePreviewControls(this); redrawAll(this); }, 0);
        } else {
            setTimeout(() => syncPadControls(this), 0);
        }
        return r;
    };

    const setDropStyle = (active) => { drop.style.borderColor = active ? "#5d9cec" : "#70757b"; drop.style.background = active ? "rgba(93,156,236,.10)" : "rgba(255,255,255,.035)"; };
    const dragEnter = (e) => { if (!Array.from(e.dataTransfer?.items || []).some((x) => x.kind === "file")) return; stopEvent(e); setDropStyle(true); };
    drop.addEventListener("dragenter", dragEnter); drop.addEventListener("dragover", dragEnter);
    drop.addEventListener("dragleave", (e) => { stopEvent(e); setDropStyle(false); });
    drop.addEventListener("drop", async (e) => { stopEvent(e); setDropStyle(false); await addFilesAsTracks(node, e.dataTransfer?.files); });

    const oldDragDrop = node.onDragDrop;
    node.onDragDrop = async function (e) {
        const files = Array.from(e?.dataTransfer?.files || []).filter(isAudioFile);
        if (files.length) { await addFilesAsTracks(this, files); return true; }
        return oldDragDrop ? await oldDragDrop.apply(this, arguments) : false;
    };
    const oldDragOver = node.onDragOver;
    node.onDragOver = function (e) {
        const items = Array.from(e?.dataTransfer?.items || []); if (items.some((x) => x.kind === "file")) return true;
        return oldDragOver ? oldDragOver.apply(this, arguments) : false;
    };
    const oldRemoved = node.onRemoved;
    node.onRemoved = function () {
        try { stopMixerPreview(this); } catch (_) {}
        this.__urnMixerTracks = []; this.__urnMixerRuntime?.clear?.(); this.__urnMixerTrackRuntime?.clear?.(); this.__urnMixerSelectedClips?.clear?.();
        return oldRemoved?.apply(this, arguments);
    };
    render(node);
}
function scan() { for (const node of app.graph?._nodes || []) install(node); }
app.registerExtension({
    name: "URN.AudioNodes.AudioMixer",
    nodeCreated(node) { install(node); },
    loadedGraphNode(node) { install(node); },
    setup() { scan(); setTimeout(scan, 300); setTimeout(scan, 1200); },
});
