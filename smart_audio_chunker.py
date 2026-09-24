import gc
import hashlib
import importlib.util
import math
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
import unicodedata
import wave
import sys
from typing import List, Tuple

import av
import numpy as np
import torch
import soundfile as soundfile

import folder_paths
from comfy_api.latest import ComfyExtension, io

try:
    from comfy.utils import ProgressBar
except Exception:
    ProgressBar = None


NODE_TAG = "[URN Smart Audio Chunker]"


def _log(message: str):
    print(f"{NODE_TAG} {message}")


def _sanitize_filename(stem: str) -> str:
    """Return a Windows-safe ASCII source name.

    Source-derived folder/file stems are restricted to A-Z, a-z, 0-9 and
    underscore. Whitespace becomes underscore, accented Latin characters are
    transliterated where possible, and symbols/punctuation/emoji are removed.
    """
    stem = str(stem or "audio").strip()
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    stem = re.sub(r"\s+", "_", stem)
    stem = re.sub(r"[^A-Za-z0-9_]", "", stem)
    stem = re.sub(r"_+", "_", stem).strip("_")

    # Windows reserves these names even when they contain only alphanumerics.
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if stem.upper() in reserved:
        stem = f"audio_{stem}"

    return stem or "audio"


def _format_duration(seconds: float) -> str:
    total_ms = max(0, int(round(float(seconds) * 1000.0)))
    minutes = total_ms // 60000
    remainder = total_ms % 60000
    secs = remainder // 1000
    millis = remainder % 1000
    return f"{minutes:02d}.{secs:02d}.{millis:03d}"


def _format_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(float(seconds) * 1000.0)))
    minutes = total_ms // 60000
    remainder = total_ms % 60000
    secs = remainder // 1000
    millis = remainder % 1000
    return f"{minutes:02d}:{secs:02d}.{millis:03d}"


def _format_srt_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(float(seconds) * 1000.0)))
    hours = total_ms // 3600000
    remainder = total_ms % 3600000
    minutes = remainder // 60000
    remainder %= 60000
    secs = remainder // 1000
    millis = remainder % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

def _build_chunk_srt_and_text(
    transcript_words: List[Tuple[float, float, str, float]],
    chunk_start: float,
    chunk_end: float,
) -> Tuple[str, str]:
    """Build a zero-based SRT and plain transcript for one exported chunk.

    Words are assigned using the same midpoint rule as the combined chunk
    transcript, so sidecar text cannot drift into an adjacent audio chunk.
    Cues are split on sentence punctuation, longer pauses, excessive duration,
    or excessive text length.
    """
    duration = max(0.0, float(chunk_end) - float(chunk_start))
    selected = []
    for word_start, word_end, word_text, confidence in transcript_words:
        midpoint = (float(word_start) + float(word_end)) * 0.5
        if chunk_start <= midpoint < chunk_end or (abs(midpoint - chunk_end) < 1e-9 and chunk_end > chunk_start):
            selected.append((float(word_start), float(word_end), str(word_text or ""), float(confidence)))

    plain_text = "".join(item[2] for item in selected).strip()
    if not selected:
        return "", plain_text

    cues = []
    current = []
    cue_start = None
    previous_end = None

    def flush():
        nonlocal current, cue_start, previous_end
        if not current:
            return
        cue_text = "".join(item[2] for item in current).strip()
        if cue_text:
            raw_start = max(chunk_start, current[0][0])
            raw_end = min(chunk_end, current[-1][1])
            local_start = max(0.0, min(duration, raw_start - chunk_start))
            local_end = max(local_start + 0.001, min(duration, raw_end - chunk_start))
            if local_end > duration:
                local_end = duration
            if local_end <= local_start and duration > local_start:
                local_end = min(duration, local_start + 0.001)
            if local_end > local_start:
                cues.append((local_start, local_end, cue_text))
        current = []
        cue_start = None
        previous_end = None

    for item in selected:
        word_start, word_end, word_text, _confidence = item
        prospective_text = ("".join(x[2] for x in current) + word_text).strip()
        if current:
            pause = max(0.0, word_start - float(previous_end))
            cue_duration = max(0.0, word_end - float(cue_start))
            if pause >= 0.75 or cue_duration > 4.5 or len(prospective_text) > 84:
                flush()

        if not current:
            cue_start = word_start
        current.append(item)
        previous_end = word_end

        stripped = word_text.strip()
        if stripped.endswith((".", "!", "?")):
            flush()

    flush()

    blocks = []
    for index, (start, end, cue_text) in enumerate(cues, start=1):
        blocks.append(
            f"{index}\n"
            f"{_format_srt_timestamp(start)} --> {_format_srt_timestamp(end)}\n"
            f"{cue_text}"
        )
    srt_text = "\n\n".join(blocks).rstrip() + ("\n" if blocks else "")
    return srt_text, plain_text


def _f32_pcm(wav_tensor: torch.Tensor) -> torch.Tensor:
    if wav_tensor.dtype.is_floating_point:
        return wav_tensor.float()
    if wav_tensor.dtype == torch.int16:
        return wav_tensor.float() / (2 ** 15)
    if wav_tensor.dtype == torch.int32:
        return wav_tensor.float() / (2 ** 31)
    if wav_tensor.dtype == torch.uint8:
        return (wav_tensor.float() - 128.0) / 128.0
    raise ValueError(f"Unsupported decoded audio dtype: {wav_tensor.dtype}")


def _decode_audio(filepath: str) -> Tuple[torch.Tensor, int]:
    with av.open(filepath) as af:
        if not af.streams.audio:
            raise ValueError("No audio stream found in the selected file.")

        stream = af.streams.audio[0]
        sample_rate = int(stream.codec_context.sample_rate or 0)
        if sample_rate <= 0:
            raise ValueError("Could not determine the audio sample rate.")

        n_channels = int(stream.channels or 1)
        frames = []

        for frame in af.decode(streams=stream.index):
            buf = torch.from_numpy(frame.to_ndarray())
            if buf.ndim == 1:
                buf = buf.unsqueeze(0)
            if buf.shape[0] != n_channels:
                buf = buf.reshape(-1, n_channels).t()
            frames.append(_f32_pcm(buf))

        if not frames:
            raise ValueError("No audio frames could be decoded from the selected file.")

        waveform = torch.cat(frames, dim=1).contiguous().float()
        return waveform, sample_rate

def _timestamped_connected_name() -> str:
    return datetime.now().strftime("Audio_%d_%m_%Y__%H_%M")


def _audio_dict_to_waveform(audio_input) -> Tuple[torch.Tensor, int]:
    if not isinstance(audio_input, dict) or "waveform" not in audio_input or "sample_rate" not in audio_input:
        raise ValueError("Connected AUDIO input is invalid.")
    waveform = audio_input["waveform"]
    sample_rate = int(audio_input["sample_rate"])
    if not isinstance(waveform, torch.Tensor):
        waveform = torch.as_tensor(waveform)
    waveform = waveform.detach().float().cpu()
    if waveform.ndim == 3:
        waveform = waveform[0]
    elif waveform.ndim == 1:
        waveform = waveform.unsqueeze(0)
    elif waveform.ndim != 2:
        raise ValueError(f"Unsupported AUDIO waveform shape: {tuple(waveform.shape)}")
    return waveform.contiguous(), sample_rate


def _decode_mono_on_reference_grid(
    filepath: str,
    reference_sample_rate: int,
    reference_total_samples: int,
) -> np.ndarray:
    """Decode analysis audio and put it on the original audio sample/time grid.

    Vocal separators may output audio at a different sample rate. Cuts, however, are ultimately
    applied to the original source, which may use another rate such as 48 kHz.
    Resampling only the analysis signal onto the original grid lets all cut
    sample indices map directly back to the untouched source audio.
    """
    analysis_waveform, analysis_rate = _decode_audio(filepath)
    mono = analysis_waveform.mean(dim=0).detach().cpu().float()

    if reference_total_samples <= 0:
        return np.zeros(0, dtype=np.float32)

    if analysis_rate != reference_sample_rate:
        expected = max(1, int(round(mono.numel() * reference_sample_rate / float(analysis_rate))))
        mono = torch.nn.functional.interpolate(
            mono.view(1, 1, -1),
            size=expected,
            mode="linear",
            align_corners=False,
        ).view(-1)

    if mono.numel() < reference_total_samples:
        mono = torch.nn.functional.pad(mono, (0, reference_total_samples - mono.numel()))
    elif mono.numel() > reference_total_samples:
        mono = mono[:reference_total_samples]

    return mono.numpy().astype(np.float32, copy=False)


def _decode_waveform_on_reference_grid(
    filepath: str,
    reference_sample_rate: int,
    reference_total_samples: int,
) -> torch.Tensor:
    """Decode a stem and place all channels on the original source sample grid.

    This is used for exported vocal chunks. Once resampled/padded/cropped here,
    the exact same cut_samples[] indices used for the original source can be
    applied to the vocal stem, giving matching sample counts and durations.
    """
    stem_waveform, stem_rate = _decode_audio(filepath)
    stem_waveform = stem_waveform.detach().cpu().float()

    if reference_total_samples <= 0:
        return torch.zeros((stem_waveform.shape[0], 0), dtype=torch.float32)

    if stem_rate != reference_sample_rate:
        expected = max(1, int(round(stem_waveform.shape[-1] * reference_sample_rate / float(stem_rate))))
        stem_waveform = torch.nn.functional.interpolate(
            stem_waveform.unsqueeze(0),
            size=expected,
            mode="linear",
            align_corners=False,
        ).squeeze(0)

    if stem_waveform.shape[-1] < reference_total_samples:
        stem_waveform = torch.nn.functional.pad(
            stem_waveform,
            (0, reference_total_samples - stem_waveform.shape[-1]),
        )
    elif stem_waveform.shape[-1] > reference_total_samples:
        stem_waveform = stem_waveform[:, :reference_total_samples]

    return stem_waveform.contiguous()


def _build_energy_profile(mono: np.ndarray, sample_rate: int, hop_seconds: float = 0.010):
    hop = max(1, int(round(sample_rate * hop_seconds)))
    n = int(mono.shape[0])
    if n == 0:
        return np.zeros(1, dtype=np.float32), hop

    block_count = int(math.ceil(n / hop))
    pad = block_count * hop - n
    if pad:
        padded = np.pad(mono, (0, pad), mode="constant")
    else:
        padded = mono

    blocks = padded.reshape(block_count, hop)
    # Mean absolute amplitude is enough for finding relatively quiet cut points
    # and avoids expensive spectral analysis.
    energy = np.mean(np.abs(blocks), axis=1, dtype=np.float64).astype(np.float32)

    # Light temporal smoothing to avoid choosing a one-sample dip inside a loud event.
    if energy.size >= 5:
        kernel = np.ones(5, dtype=np.float32) / 5.0
        energy = np.convolve(energy, kernel, mode="same").astype(np.float32)

    return energy, hop


def _is_time_protected(t: float, protected: List[Tuple[float, float]]) -> bool:
    for start, end in protected:
        if start <= t <= end:
            return True
        if start > t:
            break
    return False


def _protected_mask(times: np.ndarray, protected: List[Tuple[float, float]]) -> np.ndarray:
    mask = np.zeros(times.shape, dtype=bool)
    if not protected or times.size == 0:
        return mask
    lo = float(times[0])
    hi = float(times[-1])
    for start, end in protected:
        if end < lo:
            continue
        if start > hi:
            break
        mask |= (times >= start) & (times <= end)
    return mask


def _speech_risk_score(
    times: np.ndarray,
    speech_segments: List[Tuple[float, float, float]],
) -> np.ndarray:
    """Return a 0..1 speech-risk score at each candidate time.

    Faster-Whisper's segment.no_speech_prob is a confidence signal rather than
    a calibrated probability. We use it only for relative ranking: points
    inside a segment with low no_speech_prob are treated as more vocal-active,
    while points outside Whisper segments are treated as safer.
    """
    risk = np.zeros(times.shape, dtype=np.float64)
    if not speech_segments or times.size == 0:
        return risk

    lo = float(times[0])
    hi = float(times[-1])
    for start, end, no_speech_prob in speech_segments:
        if end < lo:
            continue
        if start > hi:
            break
        p = float(np.clip(no_speech_prob, 0.0, 1.0))
        segment_risk = 1.0 - p
        mask = (times >= start) & (times <= end)
        if np.any(mask):
            risk[mask] = np.maximum(risk[mask], segment_risk)
    return risk


def _refine_midgap_sample(
    midpoint: float,
    gap_start: float,
    gap_end: float,
    mono: np.ndarray,
    sample_rate: int,
) -> int:
    """Refine a safe gap midpoint by only a tiny amount.

    The midpoint remains the anchor. We search at most +/-5 ms and never
    outside the supplied safe corridor. In V14 the corridor can already have
    word-edge clearance removed from both sides, so this refinement cannot
    creep back toward a protected sung word.
    """
    n = int(mono.shape[0])
    center = int(round(midpoint * sample_rate))
    if n <= 1:
        return 0

    half_gap = max(0.0, (gap_end - gap_start) * 0.5)
    radius_seconds = min(0.005, half_gap * 0.5)
    radius = max(0, int(round(radius_seconds * sample_rate)))

    low_sample = max(1, int(math.ceil(gap_start * sample_rate)), center - radius)
    high_sample = min(n - 1, int(math.floor(gap_end * sample_rate)), center + radius)
    if high_sample <= low_sample:
        return max(1, min(n - 1, center))

    samples = np.arange(low_sample, high_sample + 1, dtype=np.int64)
    amp = np.abs(mono[samples]).astype(np.float64)
    amp_scale = float(np.percentile(amp, 90)) if amp.size else 0.0
    if amp_scale > 1e-12:
        amp_score = np.clip(amp / amp_scale, 0.0, 1.0)
    else:
        amp_score = np.zeros_like(amp)

    prev = mono[np.maximum(0, samples - 1)]
    curr = mono[samples]
    zero_cross = (prev == 0.0) | (curr == 0.0) | ((prev < 0.0) != (curr < 0.0))
    zero_penalty = np.where(zero_cross, 0.0, 0.15)

    radius_norm = max(1, radius)
    distance_score = np.abs(samples - center).astype(np.float64) / float(radius_norm)
    score = 0.65 * amp_score + 0.25 * distance_score + zero_penalty
    best = int(np.argmin(score))
    return int(samples[best])


def _word_edge_clearance_seconds(vocal_safety: str) -> float:
    """Preferred clearance from Whisper word edges for sung material."""
    return 0.150 if str(vocal_safety).lower() == "strong" else 0.100


def _vocal_energy_half_window_seconds(vocal_safety: str) -> float:
    """Window either side of a cut used to verify a quiet vocal corridor."""
    return 0.080 if str(vocal_safety).lower() == "strong" else 0.050


def _expand_word_regions(
    word_regions: List[Tuple[float, float]],
    margin: float,
    total_duration: float,
) -> List[Tuple[float, float]]:
    """Expand and merge word regions by a safety margin on both sides."""
    if not word_regions:
        return []
    expanded = []
    for start, end in word_regions:
        a = max(0.0, float(start) - float(margin))
        b = min(float(total_duration), float(end) + float(margin))
        if b > a:
            expanded.append((a, b))
    if not expanded:
        return []
    expanded.sort(key=lambda x: x[0])
    merged = [expanded[0]]
    for start, end in expanded[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def _window_mean_abs_at_times(
    mono: np.ndarray,
    sample_rate: int,
    times: np.ndarray,
    half_window_seconds: float,
) -> np.ndarray:
    """Mean absolute vocal/source energy around each proposed cut time."""
    if times.size == 0:
        return np.zeros(0, dtype=np.float64)
    n = int(mono.shape[0])
    if n <= 0:
        return np.zeros(times.shape, dtype=np.float64)
    half = max(1, int(round(float(half_window_seconds) * sample_rate)))
    out = np.zeros(times.shape, dtype=np.float64)
    for i, t in enumerate(times):
        c = int(round(float(t) * sample_rate))
        lo = max(0, c - half)
        hi = min(n, c + half + 1)
        if hi > lo:
            out[i] = float(np.mean(np.abs(mono[lo:hi]), dtype=np.float64))
    return out


def _word_gap_candidates(
    transcript_words: List[Tuple[float, float, str, float]],
    low_time: float,
    high_time: float,
    total_duration: float,
    min_length: float,
    edge_clearance_seconds: float,
    minimum_raw_gap_seconds: float,
):
    """Return word gaps that contain the requested protected safe corridor.

    A candidate must provide ``edge_clearance_seconds`` after the previous word
    and before the next word.  The returned safe_start/safe_end are therefore
    already clear of both Whisper word edges.
    """
    candidates = []
    if len(transcript_words) < 2:
        return candidates

    required_gap = max(float(minimum_raw_gap_seconds), 2.0 * float(edge_clearance_seconds))
    for i in range(len(transcript_words) - 1):
        _a_start, a_end, _a_text, a_conf = transcript_words[i]
        b_start, _b_end, _b_text, b_conf = transcript_words[i + 1]
        gap_start = float(a_end)
        gap_end = float(b_start)
        gap_length = gap_end - gap_start
        if gap_length + 1e-12 < required_gap:
            continue

        safe_start = gap_start + float(edge_clearance_seconds)
        safe_end = gap_end - float(edge_clearance_seconds)
        if safe_end < safe_start:
            continue
        midpoint = (safe_start + safe_end) * 0.5
        if midpoint < low_time or midpoint > high_time:
            continue

        adjacent_conf = float(np.clip((float(a_conf) + float(b_conf)) * 0.5, 0.0, 1.0))
        tail_ok = (total_duration - midpoint) >= min_length
        candidates.append(
            (midpoint, gap_start, gap_end, adjacent_conf, tail_ok, safe_start, safe_end, float(edge_clearance_seconds))
        )
    return candidates


def _widest_word_gap_candidate(
    transcript_words: List[Tuple[float, float, str, float]],
    low_time: float,
    high_time: float,
    total_duration: float,
    min_length: float,
):
    """Return the widest positive word gap in range, even if <100 ms.

    This is the final between-word fallback before waveform search.  It never
    intentionally chooses a time inside a Whisper word.
    """
    candidates = []
    for i in range(max(0, len(transcript_words) - 1)):
        _a_start, a_end, _a_text, a_conf = transcript_words[i]
        b_start, _b_end, _b_text, b_conf = transcript_words[i + 1]
        gap_start = float(a_end)
        gap_end = float(b_start)
        if gap_end <= gap_start:
            continue
        midpoint = (gap_start + gap_end) * 0.5
        if midpoint < low_time or midpoint > high_time:
            continue
        adjacent_conf = float(np.clip((float(a_conf) + float(b_conf)) * 0.5, 0.0, 1.0))
        tail_ok = (total_duration - midpoint) >= min_length
        candidates.append((midpoint, gap_start, gap_end, adjacent_conf, tail_ok))

    if not candidates:
        return None
    preferred = [g for g in candidates if g[4]]
    usable = preferred if preferred else candidates
    return max(usable, key=lambda g: (g[2] - g[1], g[3]))


def _fallback_cut_sample(
    mono: np.ndarray,
    sample_rate: int,
    energy: np.ndarray,
    hop_samples: int,
    total_duration: float,
    start_sample: int,
    target_length: float,
    min_length: float,
    max_length: float,
    word_regions: List[Tuple[float, float]],
    protected_word_regions: List[Tuple[float, float]],
    speech_segments: List[Tuple[float, float, float]],
    vocal_safety: str,
) -> Tuple[int, bool]:
    """Waveform/no-speech last resort. Returns (sample, forced_inside_word).

    V14 first avoids the expanded word-edge protection regions, then (only if
    necessary) relaxes those margins while still staying outside the actual
    Whisper words.  An inside-word cut remains an absolute last resort forced
    by Maximum Length.
    """
    hop_seconds = hop_samples / float(sample_rate)
    total_samples = int(mono.shape[0])
    start_time = start_sample / float(sample_rate)
    low_time = start_time + min_length
    high_time = min(start_time + max_length, total_duration)
    ideal_time = min(start_time + target_length, high_time)

    first_idx = int(math.ceil(low_time / hop_seconds))
    last_idx = int(math.floor(high_time / hop_seconds))
    if last_idx < first_idx:
        sample = max(start_sample + 1, min(total_samples - 1, int(round(high_time * sample_rate))))
        return sample, _is_time_protected(sample / float(sample_rate), word_regions)

    idxs = np.arange(first_idx, last_idx + 1, dtype=np.int64)
    times = np.clip(idxs.astype(np.float64) * hop_seconds, low_time, high_time)
    eidx = np.clip(idxs, 0, len(energy) - 1)
    local_energy = energy[eidx].astype(np.float64)

    actual_safe = ~_protected_mask(times, word_regions)
    margin_safe = ~_protected_mask(times, protected_word_regions)
    tail_ok = (total_duration - times) >= min_length

    forced_inside_word = False
    if np.any(margin_safe & tail_ok):
        usable = margin_safe & tail_ok
    elif np.any(margin_safe):
        usable = margin_safe
    elif np.any(actual_safe & tail_ok):
        # Relax only the added edge margin; still never enter a detected word.
        usable = actual_safe & tail_ok
    elif np.any(actual_safe):
        usable = actual_safe
    else:
        forced_inside_word = True
        usable = tail_ok if np.any(tail_ok) else np.ones(times.shape, dtype=bool)

    usable_times = times[usable]
    usable_energy = local_energy[usable]
    span = max(0.001, high_time - low_time)
    distance_score = np.abs(usable_times - ideal_time) / span

    e_min = float(np.min(usable_energy)) if usable_energy.size else 0.0
    e_hi = float(np.percentile(usable_energy, 90)) if usable_energy.size else 1.0
    if e_hi <= e_min + 1e-12:
        energy_score = np.zeros_like(usable_energy)
    else:
        energy_score = np.clip((usable_energy - e_min) / (e_hi - e_min), 0.0, 1.0)

    speech_risk = _speech_risk_score(usable_times, speech_segments) if speech_segments else np.zeros_like(energy_score)
    window_energy = _window_mean_abs_at_times(
        mono,
        sample_rate,
        usable_times,
        _vocal_energy_half_window_seconds(vocal_safety),
    )
    w_min = float(np.min(window_energy)) if window_energy.size else 0.0
    w_hi = float(np.percentile(window_energy, 90)) if window_energy.size else 1.0
    if w_hi <= w_min + 1e-12:
        window_score = np.zeros_like(window_energy)
    else:
        window_score = np.clip((window_energy - w_min) / (w_hi - w_min), 0.0, 1.0)

    if speech_segments:
        safety_score = 0.35 * energy_score + 0.35 * window_score + 0.30 * speech_risk
    else:
        safety_score = 0.45 * energy_score + 0.55 * window_score

    if str(vocal_safety).lower() == "strong":
        score = 0.30 * distance_score + 0.70 * safety_score
    else:
        score = 0.55 * distance_score + 0.45 * safety_score

    chosen_time = float(usable_times[int(np.argmin(score))])
    center = int(round(chosen_time * sample_rate))
    radius = max(1, int(round(sample_rate * 0.005)))
    lo = max(1, int(math.ceil(low_time * sample_rate)), center - radius)
    hi = min(total_samples - 1, int(math.floor(high_time * sample_rate)), center + radius)
    if hi <= lo:
        sample = max(start_sample + 1, min(total_samples - 1, center))
        return sample, forced_inside_word or _is_time_protected(sample / float(sample_rate), word_regions)

    region = np.abs(mono[lo:hi + 1])
    ordered = np.argsort(region)[: min(256, len(region))]
    # First preserve the expanded edge margin if possible.
    for idx in ordered:
        sample = lo + int(idx)
        t = sample / float(sample_rate)
        if not _is_time_protected(t, protected_word_regions):
            return sample, False
    # Then relax only the margin, never the actual word region.
    for idx in ordered:
        sample = lo + int(idx)
        t = sample / float(sample_rate)
        if not _is_time_protected(t, word_regions):
            return sample, False

    sample = max(start_sample + 1, min(total_samples - 1, center))
    return sample, True


def _score_gap_candidates(
    usable_gaps,
    ideal_time: float,
    low_time: float,
    high_time: float,
    mono: np.ndarray,
    sample_rate: int,
    speech_segments: List[Tuple[float, float, float]],
    vocal_safety: str,
):
    """Pick the best protected between-word gap."""
    mids = np.asarray([g[0] for g in usable_gaps], dtype=np.float64)
    span = max(0.001, high_time - low_time)
    distance_score = np.abs(mids - ideal_time) / span

    # Prefer wider remaining safe corridors, not just wider raw timestamp gaps.
    safe_widths = np.asarray([max(0.0, g[6] - g[5]) for g in usable_gaps], dtype=np.float64)
    width_hi = float(np.percentile(safe_widths, 90)) if safe_widths.size else 0.0
    if width_hi > 1e-9:
        corridor_penalty = 1.0 - np.clip(safe_widths / width_hi, 0.0, 1.0)
    else:
        corridor_penalty = np.ones_like(safe_widths)

    # V14 checks a genuine quiet window around the boundary instead of just the
    # exact sample/hop. When mono is the Mel-RoFormer stem, this specifically
    # measures residual vocal energy around the proposed cut.
    window_energy = _window_mean_abs_at_times(
        mono,
        sample_rate,
        mids,
        _vocal_energy_half_window_seconds(vocal_safety),
    )
    e_min = float(np.min(window_energy)) if window_energy.size else 0.0
    e_hi = float(np.percentile(window_energy, 90)) if window_energy.size else 1.0
    if e_hi <= e_min + 1e-12:
        window_score = np.zeros_like(window_energy)
    else:
        window_score = np.clip((window_energy - e_min) / (e_hi - e_min), 0.0, 1.0)

    speech_risk = _speech_risk_score(mids, speech_segments) if speech_segments else np.zeros_like(mids)
    confidence_penalty = 1.0 - np.asarray([g[3] for g in usable_gaps], dtype=np.float64)
    safety_score = (
        0.45 * window_score
        + 0.25 * speech_risk
        + 0.15 * confidence_penalty
        + 0.15 * corridor_penalty
    )

    if str(vocal_safety).lower() == "strong":
        score = 0.30 * distance_score + 0.70 * safety_score
    else:
        score = 0.55 * distance_score + 0.45 * safety_score
    return int(np.argmin(score))


def _find_cut_samples(
    mono: np.ndarray,
    sample_rate: int,
    total_duration: float,
    target_length: float,
    min_length: float,
    max_length: float,
    transcript_words: List[Tuple[float, float, str, float]],
    speech_segments: List[Tuple[float, float, float]],
    vocal_safety: str,
) -> Tuple[List[int], int, int, int, int, int, int]:
    """Find chunk boundaries with V14 word-edge and vocal-window protection.

    Priority for Strong safety:
      1) full 150 ms clearance on both word edges (>=300 ms raw gap),
      2) reduced 100 ms clearance on both edges (>=200 ms raw gap),
      3) >=100 ms actual between-word gap,
      4) widest positive between-word gap,
      5) waveform/no-speech fallback outside expanded word regions,
      6) inside-word cut only when Maximum Length makes it unavoidable.

    Normal uses 100 ms full clearance, then follows the same fallbacks.
    """
    energy, hop_samples = _build_energy_profile(mono, sample_rate)
    total_samples = int(mono.shape[0])
    word_regions = sorted([(float(w[0]), float(w[1])) for w in transcript_words], key=lambda x: x[0])
    preferred_clearance = _word_edge_clearance_seconds(vocal_safety)
    protected_word_regions = _expand_word_regions(word_regions, preferred_clearance, total_duration)

    cuts = [0]
    start_sample = 0
    full_clearance_count = 0
    reduced_clearance_count = 0
    narrow_gap_count = 0
    fallback_count = 0
    forced_inside_word_count = 0
    tiny_gap_count = 0

    while start_sample < total_samples:
        start_time = start_sample / float(sample_rate)
        remaining = total_duration - start_time

        if remaining <= max_length + (0.5 / sample_rate):
            cuts.append(total_samples)
            break

        low_time = start_time + min_length
        high_time = min(start_time + max_length, total_duration)
        ideal_time = min(start_time + target_length, high_time)

        # First demand the full configured edge clearance.
        gaps = _word_gap_candidates(
            transcript_words,
            low_time,
            high_time,
            total_duration,
            min_length,
            edge_clearance_seconds=preferred_clearance,
            minimum_raw_gap_seconds=2.0 * preferred_clearance,
        )
        tier = "full"

        # Strong mode gets a deliberate 100 ms-per-side relaxation before the
        # old >=100 ms gap fallback. This matches the requested 300/200/100 ms
        # safety hierarchy without making difficult songs impossible to split.
        if not gaps and preferred_clearance > 0.100 + 1e-9:
            gaps = _word_gap_candidates(
                transcript_words,
                low_time,
                high_time,
                total_duration,
                min_length,
                edge_clearance_seconds=0.100,
                minimum_raw_gap_seconds=0.200,
            )
            tier = "reduced"

        # Last structured gap tier: keep a real >=100 ms gap, but no extra word
        # clearance. This is safer than arbitrary waveform cutting.
        if not gaps:
            gaps = _word_gap_candidates(
                transcript_words,
                low_time,
                high_time,
                total_duration,
                min_length,
                edge_clearance_seconds=0.0,
                minimum_raw_gap_seconds=0.100,
            )
            tier = "narrow"

        preferred_gaps = [g for g in gaps if g[4]]
        usable_gaps = preferred_gaps if preferred_gaps else gaps
        cut_sample = None
        cut_forced = False

        if usable_gaps:
            best = _score_gap_candidates(
                usable_gaps,
                ideal_time,
                low_time,
                high_time,
                mono,
                sample_rate,
                speech_segments,
                vocal_safety,
            )
            midpoint, gap_start, gap_end, _adj_conf, _tail_ok, safe_start, safe_end, _clearance = usable_gaps[best]
            cut_sample = _refine_midgap_sample(midpoint, safe_start, safe_end, mono, sample_rate)
            if tier == "full":
                full_clearance_count += 1
            elif tier == "reduced":
                reduced_clearance_count += 1
            else:
                narrow_gap_count += 1
        else:
            tiny_gap = _widest_word_gap_candidate(
                transcript_words, low_time, high_time, total_duration, min_length
            )
            if tiny_gap is not None:
                midpoint, gap_start, gap_end, _adj_conf, _tail_ok = tiny_gap
                cut_sample = _refine_midgap_sample(midpoint, gap_start, gap_end, mono, sample_rate)
                tiny_gap_count += 1
            else:
                cut_sample, cut_forced = _fallback_cut_sample(
                    mono=mono,
                    sample_rate=sample_rate,
                    energy=energy,
                    hop_samples=hop_samples,
                    total_duration=total_duration,
                    start_sample=start_sample,
                    target_length=target_length,
                    min_length=min_length,
                    max_length=max_length,
                    word_regions=word_regions,
                    protected_word_regions=protected_word_regions,
                    speech_segments=speech_segments,
                    vocal_safety=vocal_safety,
                )
                fallback_count += 1

        cut_sample = max(start_sample + 1, min(total_samples - 1, int(cut_sample)))

        max_sample = min(total_samples - 1, start_sample + int(math.floor(max_length * sample_rate)))
        if cut_sample > max_sample:
            cut_sample = max_sample

        cut_time = cut_sample / float(sample_rate)
        if cut_forced or _is_time_protected(cut_time, word_regions):
            forced_inside_word_count += 1
            _log(
                f"WARNING: Maximum Length forced cut inside/at detected word region at {cut_time:.3f}s."
            )

        cuts.append(cut_sample)
        start_sample = cut_sample

    clean = [cuts[0]]
    for value in cuts[1:]:
        value = int(value)
        if value > clean[-1]:
            clean.append(value)
    if clean[-1] != total_samples:
        clean.append(total_samples)
    return (
        clean,
        full_clearance_count,
        reduced_clearance_count,
        narrow_gap_count,
        tiny_gap_count,
        fallback_count,
        forced_inside_word_count,
    )

def _write_wav(filepath: str, waveform: torch.Tensor, sample_rate: int):
    data = waveform.detach().cpu().float().numpy()
    if data.ndim == 1:
        data = data[None, :]
    data = np.clip(data, -1.0, 1.0)
    pcm = (data.T * 32767.0).round().astype(np.int16)

    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with wave.open(filepath, "wb") as wf:
        wf.setnchannels(int(pcm.shape[1]))
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcm.tobytes())


def _write_flac(filepath: str, waveform: torch.Tensor, sample_rate: int):
    """Write lossless 24-bit FLAC for persistent URN Audio Tools audio files."""
    data = waveform.detach().cpu().float().numpy()
    if data.ndim == 1:
        data = data[None, :]
    data = np.clip(data, -1.0, 1.0)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    # soundfile expects frames x channels. PCM_24 keeps substantially more
    # precision than the legacy 16-bit WAV exporter while remaining lossless.
    soundfile.write(filepath, data.T, int(sample_rate), format="FLAC", subtype="PCM_24")


MEL_ROFORMER_MODEL = "mel_band_roformer_kim_ft2_bleedless_unwa.ckpt"


def _mel_roformer_settings_match(output_root: str, clean_stem: str) -> bool:
    settings_path = os.path.join(output_root, f"{clean_stem}_mel_roformer_settings.txt")
    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = {}
            for line in f:
                if "=" in line:
                    key, value = line.strip().split("=", 1)
                    settings[key.strip()] = value.strip()
        return (
            settings.get("separator") == "mel_roformer"
            and settings.get("model") == MEL_ROFORMER_MODEL
        )
    except Exception:
        return False


def _export_mel_roformer_stems(
    source_path: str,
    output_root: str,
    clean_stem: str,
    want_vocals: bool = True,
    want_instrumental: bool = False,
) -> Tuple[List[str], str]:
    """Create requested MelBand RoFormer vocal/instrumental stems.

    When both stems are requested the separator runs only once and writes both
    model outputs. Persistent URN files are always converted to lossless FLAC.
    """
    want_vocals = bool(want_vocals)
    want_instrumental = bool(want_instrumental)
    if not want_vocals and not want_instrumental:
        return [], ""

    if importlib.util.find_spec("audio_separator") is None:
        raise RuntimeError(
            "Mel-RoFormer is selected but audio-separator is not installed. "
            "Run install.bat from this node folder once, restart ComfyUI, then try again."
        )

    model_cache = os.path.join(folder_paths.models_dir, "mel_roformer")
    os.makedirs(model_cache, exist_ok=True)
    os.makedirs(output_root, exist_ok=True)
    temp_root = tempfile.mkdtemp(prefix="urn_mel_roformer_", dir=output_root)
    runner_path = os.path.join(os.path.dirname(__file__), "mel_roformer_runner.py")

    model_path = os.path.join(model_cache, MEL_ROFORMER_MODEL)
    if os.path.isfile(model_path) and os.path.getsize(model_path) > 0:
        _log(f"Mel-RoFormer model found locally — reusing {MEL_ROFORMER_MODEL}.")
    else:
        _log(f"Mel-RoFormer model not found locally — first run will download {MEL_ROFORMER_MODEL}.")

    stem_mode = "both" if want_vocals and want_instrumental else ("vocals" if want_vocals else "instrumental")
    requested_label = "vocal + instrumental" if stem_mode == "both" else stem_mode
    device_order = ["cuda", "cpu"] if torch.cuda.is_available() else ["cpu"]
    errors = []
    try:
        for device in device_order:
            env = os.environ.copy()
            if device == "cpu":
                env["CUDA_VISIBLE_DEVICES"] = ""
            _log(f"Mel-RoFormer {requested_label} separation starting on {device}.")
            completed = subprocess.run(
                [
                    sys.executable,
                    runner_path,
                    source_path,
                    temp_root,
                    model_cache,
                    MEL_ROFORMER_MODEL,
                    stem_mode,
                ],
                env=env,
                capture_output=True,
                text=True,
            )
            if completed.stdout:
                for line in completed.stdout.splitlines():
                    if line.strip():
                        _log(f"Mel-RoFormer: {line.strip()}")
            if completed.returncode == 0:
                candidates = []
                for root, _dirs, files in os.walk(temp_root):
                    for filename in files:
                        if filename.lower().endswith(".wav"):
                            candidates.append(os.path.join(root, filename))

                def pick_candidate(kind: str):
                    kind = kind.lower()
                    preferred_tokens = (
                        ("urn_vocals", "vocals")
                        if kind == "vocals"
                        else ("urn_instrumental", "instrumental", "no_vocals", "karaoke")
                    )
                    matches = [
                        path for path in candidates
                        if any(token in os.path.basename(path).lower() for token in preferred_tokens)
                    ]
                    return matches[0] if matches else None

                final_paths = []
                if want_vocals:
                    vocal_source = pick_candidate("vocals")
                    if vocal_source is None:
                        raise RuntimeError(
                            "Mel-RoFormer completed but the temporary vocal audio could not be identified."
                        )
                    final_vocals = os.path.join(output_root, f"{clean_stem}_vocals.flac")
                    vocal_waveform, vocal_sample_rate = _decode_audio(vocal_source)
                    _write_flac(final_vocals, vocal_waveform, vocal_sample_rate)
                    final_paths.append(final_vocals)
                    _log(f"Mel-RoFormer vocal stem saved to {final_vocals}")

                if want_instrumental:
                    instrumental_source = pick_candidate("instrumental")
                    if instrumental_source is None:
                        # For a single-stem instrumental request the temporary
                        # folder should contain exactly one output. This fallback
                        # keeps compatibility with models that label the complement
                        # differently while still avoiding accidental vocal reuse.
                        non_vocal = [
                            path for path in candidates
                            if "vocal" not in os.path.basename(path).lower()
                        ]
                        if len(non_vocal) == 1:
                            instrumental_source = non_vocal[0]
                    if instrumental_source is None:
                        raise RuntimeError(
                            "Mel-RoFormer completed but the temporary instrumental audio could not be identified."
                        )
                    final_instrumental = os.path.join(output_root, f"{clean_stem}_instrumental.flac")
                    inst_waveform, inst_sample_rate = _decode_audio(instrumental_source)
                    _write_flac(final_instrumental, inst_waveform, inst_sample_rate)
                    final_paths.append(final_instrumental)
                    _log(f"Mel-RoFormer instrumental stem saved to {final_instrumental}")

                settings_path = os.path.join(output_root, f"{clean_stem}_mel_roformer_settings.txt")
                with open(settings_path, "w", encoding="utf-8", newline="\n") as f:
                    f.write("separator=mel_roformer\n")
                    f.write(f"model={MEL_ROFORMER_MODEL}\n")

                return final_paths, device

            error_text = (completed.stderr or "").strip()
            errors.append(
                f"{device}: exit code {completed.returncode}"
                + (f" | {error_text}" if error_text else "")
            )
            if device == "cuda":
                _log("Mel-RoFormer CUDA separation failed; retrying on CPU.")

        raise RuntimeError("Mel-RoFormer separation failed (" + "; ".join(errors) + ").")
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def _export_mel_roformer_vocals(
    source_path: str,
    output_root: str,
    clean_stem: str,
) -> Tuple[List[str], str]:
    """Backward-compatible helper for callers that only need vocals."""
    return _export_mel_roformer_stems(
        source_path=source_path,
        output_root=output_root,
        clean_stem=clean_stem,
        want_vocals=True,
        want_instrumental=False,
    )

def _write_mp3(filepath: str, waveform: torch.Tensor, sample_rate: int):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(
            "MP3 output was selected but ffmpeg was not found on PATH. "
            "Use FLAC output or make ffmpeg available to ComfyUI."
        )

    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    fd, temp_wav = tempfile.mkstemp(prefix="urn_audio_chunk_", suffix=".wav")
    os.close(fd)
    try:
        _write_wav(temp_wav, waveform, sample_rate)
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            temp_wav,
            "-codec:a",
            "libmp3lame",
            "-q:a",
            "2",
            filepath,
        ]
        completed = subprocess.run(cmd, capture_output=True, text=True)
        if completed.returncode != 0:
            raise RuntimeError(f"ffmpeg MP3 export failed: {completed.stderr.strip()}")
    finally:
        try:
            os.remove(temp_wav)
        except OSError:
            pass


def _analyse_words(
    source_path: str,
    model_name: str,
    requested_device: str,
    total_duration: float,
    beam_size: int,
    patience: float,
    pbar=None,
):
    """Return raw Whisper word timing regions plus transcript data.

    word_regions: [(start, end), ...] using raw Whisper word timings
    transcript_words: [(start, end, text, confidence), ...] using raw Whisper timings
    transcript_segments: [(start, end, text), ...] for readable full-song output
    speech_segments: [(start, end, no_speech_prob), ...] for cut ranking
    """
    try:
        from faster_whisper import WhisperModel
        from faster_whisper.utils import download_model
    except Exception as exc:
        return [], [], [], [], "energy fallback", f"faster-whisper unavailable: {exc}"

    model_root = os.path.join(folder_paths.models_dir, "faster_whisper")
    os.makedirs(model_root, exist_ok=True)

    # Resolve the selected model from the local Hugging Face cache first.
    # local_files_only=True guarantees that this first check does not contact
    # huggingface.co. Only if no complete local snapshot exists do we permit a
    # one-time download. Once resolved, WhisperModel receives the local folder
    # path directly, so device fallback (CUDA -> CPU) cannot cause another
    # network check.
    model_cache_note = ""
    try:
        model_path = download_model(
            model_name,
            cache_dir=model_root,
            local_files_only=True,
        )
        model_cache_note = "Whisper model source: local cache (offline; no network check)."
        _log(f"Model '{model_name}' found locally — offline load, no network check.")
    except Exception as local_exc:
        _log(f"Model '{model_name}' not found locally — downloading from Hugging Face.")
        try:
            model_path = download_model(
                model_name,
                cache_dir=model_root,
                local_files_only=False,
            )
            model_cache_note = "Whisper model source: downloaded from Hugging Face (future runs use local cache only)."
        except Exception as download_exc:
            return [], [], [], [], "energy fallback", (
                f"Whisper model '{model_name}' was not available locally and could not be downloaded: {download_exc}"
            )

    def run(device: str):
        compute_type = "float16" if device == "cuda" else "int8"
        _log(f"Loading Faster-Whisper model '{model_name}' on {device} ({compute_type}).")
        model = WhisperModel(
            model_path,
            device=device,
            compute_type=compute_type,
        )
        word_regions = []
        transcript_words = []
        transcript_segments = []
        speech_segments = []
        segments, _info = model.transcribe(
            source_path,
            language="en",
            task="transcribe",
            beam_size=int(beam_size),
            patience=float(patience),
            word_timestamps=True,
            vad_filter=False,
            condition_on_previous_text=False,
        )

        last_progress = 0
        for segment in segments:
            seg_start = max(0.0, float(segment.start))
            seg_end = min(total_duration, float(segment.end))
            seg_text = str(segment.text or "").strip()
            no_speech_prob = float(getattr(segment, "no_speech_prob", 0.0) or 0.0)
            no_speech_prob = float(np.clip(no_speech_prob, 0.0, 1.0))
            if seg_end > seg_start:
                speech_segments.append((seg_start, seg_end, no_speech_prob))
            if seg_text and seg_end > seg_start:
                transcript_segments.append((seg_start, seg_end, seg_text))

            if pbar is not None and total_duration > 0:
                frac = min(1.0, max(0.0, float(segment.end) / total_duration))
                progress = 8 + int(frac * 52)
                if progress > last_progress:
                    try:
                        pbar.update_absolute(progress, 100)
                    except Exception:
                        pass
                    last_progress = progress

            if not segment.words:
                continue
            for word in segment.words:
                if word.start is None or word.end is None:
                    continue
                raw_start = max(0.0, float(word.start))
                raw_end = min(total_duration, float(word.end))
                word_text = str(word.word or "")
                if raw_end <= raw_start:
                    continue

                confidence = getattr(word, "probability", None)
                try:
                    confidence = float(confidence) if confidence is not None else 1.0
                except Exception:
                    confidence = 1.0
                confidence = float(np.clip(confidence, 0.0, 1.0))
                transcript_words.append((raw_start, raw_end, word_text, confidence))

                # Keep the raw Whisper word region. V1 FIXED7 no longer pads
                # word boundaries; confidence is used to rank the trustworthiness
                # of between-word midpoint candidates instead.
                word_regions.append((raw_start, raw_end))

        del model
        gc.collect()
        return word_regions, transcript_words, transcript_segments, speech_segments

    req = str(requested_device).strip().lower()
    if req == "cpu":
        try:
            word_regions, words, segments, speech_segments = run("cpu")
            return word_regions, words, segments, speech_segments, "Whisper CPU", model_cache_note
        except Exception as exc:
            return [], [], [], [], "energy fallback", f"Whisper CPU analysis failed: {exc}"

    if req == "cuda":
        try:
            word_regions, words, segments, speech_segments = run("cuda")
            return word_regions, words, segments, speech_segments, "Whisper CUDA", model_cache_note
        except Exception as exc:
            raise RuntimeError(
                "Faster-Whisper CUDA analysis failed. Select Analysis Device = Auto or CPU "
                f"to allow CPU fallback. Original error: {exc}"
            ) from exc

    # Auto: CUDA first, then CPU, then waveform-only fallback.
    cuda_error = ""
    try:
        word_regions, words, segments, speech_segments = run("cuda")
        return word_regions, words, segments, speech_segments, "Whisper CUDA", model_cache_note
    except Exception as exc:
        cuda_error = str(exc)
        _log(f"CUDA Whisper unavailable; falling back to CPU. {cuda_error}")
        gc.collect()

    try:
        word_regions, words, segments, speech_segments = run("cpu")
        fallback_note = f"{model_cache_note} CUDA fallback reason: {cuda_error}".strip()
        return word_regions, words, segments, speech_segments, "Whisper CPU", fallback_note
    except Exception as exc:
        return [], [], [], [], "energy fallback", f"CUDA failed: {cuda_error} | CPU failed: {exc}"


class URNSmartAudioChunker(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        input_dir = folder_paths.get_input_directory()
        os.makedirs(input_dir, exist_ok=True)
        files = folder_paths.filter_files_content_types(os.listdir(input_dir), ["audio"])
        files = [f for f in files if os.path.splitext(f)[1].lower() in {".mp3", ".wav", ".flac"}]
        files = sorted(files)

        return io.Schema(
            node_id="URNSmartAudioChunker",
            display_name="URN Audio Smart Splitter",
            category="URN Audio Tools",
            description=(
                "Loads MP3/WAV/FLAC audio and prefers protected quiet corridors between detected words near the target. "
                "and automatically exports all chunks. FLAC is the default lossless output. "
                "The transcript output is standard SRT and is also saved as an .srt file. "
                "Mel-RoFormer (Kim FT2 Bleedless) can export both isolated vocals and an instrumental/music-without-vocals stem into dedicated subfolders. "
                "The isolated vocal can drive Whisper/cut safety; V14 adds word-edge clearance and vocal-window energy checks before selecting a cut, and can export sample-aligned vocal chunks for lip-sync."
            ),
            is_output_node=True,
            inputs=[
                io.Combo.Input(
                    "audio",
                    options=files,
                    upload=io.UploadType.audio,
                ),
                io.Audio.Input("audio_input", optional=True),
                io.Float.Input(
                    "target_length",
                    default=9.0,
                    min=5.0,
                    max=60.0,
                    step=0.001,
                ),
                io.Float.Input(
                    "minimum_length",
                    default=6.0,
                    min=5.0,
                    max=60.0,
                    step=0.001,
                ),
                io.Float.Input(
                    "maximum_length",
                    default=12.0,
                    min=5.0,
                    max=60.0,
                    step=0.001,
                ),
                io.Combo.Input(
                    "vocal_safety",
                    options=["Strong", "Normal"],
                    default="Strong",
                ),
                io.Combo.Input(
                    "whisper_model",
                    options=["tiny", "base", "small", "medium", "large-v3"],
                    default="medium",
                ),
                io.Combo.Input(
                    "analysis_device",
                    options=["Auto", "CUDA", "CPU"],
                    default="CUDA",
                ),
                io.Int.Input(
                    "beam_size",
                    default=18,
                    min=1,
                    max=20,
                    step=1,
                ),
                io.Float.Input(
                    "patience",
                    default=3.0,
                    min=1.0,
                    max=5.0,
                    step=0.1,
                ),
                io.Combo.Input(
                    "output_format",
                    options=["FLAC", "MP3"],
                    default="FLAC",
                ),
                io.Boolean.Input(
                    "export_stems",
                    display_name="export_vocal_stem",
                    default=True,
                    tooltip="Save the full Mel-RoFormer isolated vocal stem beside the chunks.",
                ),
                io.Boolean.Input(
                    "use_vocal_stem_for_analysis",
                    default=True,
                ),
                io.Boolean.Input(
                    "export_vocal_chunks",
                    default=True,
                ),
                io.Boolean.Input(
                    "export_music_stem",
                    default=True,
                    tooltip="Save the full Mel-RoFormer instrumental/music stem (music without vocals) in the stems folder.",
                ),
            ],
            outputs=[
                io.String.Output(display_name="output_folder"),
                io.String.Output(display_name="debug_text"),
                io.String.Output(display_name="transcript_srt"),
            ],
        )

    @classmethod
    def validate_inputs(
        cls,
        audio,
        target_length,
        minimum_length,
        maximum_length,
        beam_size,
        patience,
        audio_input=None,
        **kwargs,
    ):
        if audio_input is None and not audio:
            return "Select or upload an MP3, WAV or FLAC file, or connect an AUDIO input."
        if audio_input is None:
            if not folder_paths.exists_annotated_filepath(audio):
                return f"Invalid audio file: {audio}"

            ext = os.path.splitext(folder_paths.get_annotated_filepath(audio))[1].lower()
            if ext not in {".mp3", ".wav", ".flac"}:
                return "Accepts MP3, WAV and FLAC input only."

        if minimum_length < 5.0:
            return "Minimum Length cannot be lower than 5 seconds."
        if maximum_length > 60.0:
            return "Maximum Length cannot be higher than 60 seconds."
        if minimum_length > maximum_length:
            return "Minimum Length cannot be greater than Maximum Length."
        if target_length < minimum_length or target_length > maximum_length:
            return "Target Length must be between Minimum Length and Maximum Length."
        if int(beam_size) < 1 or int(beam_size) > 20:
            return "Beam Size must be between 1 and 20."
        if float(patience) < 1.0 or float(patience) > 5.0:
            return "Patience must be between 1.0 and 5.0."
        return True

    @classmethod
    def fingerprint_inputs(cls, audio, audio_input=None, **kwargs):
        if audio_input is not None:
            return float("nan")
        if not audio or not folder_paths.exists_annotated_filepath(audio):
            return float("nan")
        path = folder_paths.get_annotated_filepath(audio)
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1024 * 1024), b""):
                h.update(block)
        return h.digest().hex()

    @classmethod
    def execute(
        cls,
        audio,
        target_length,
        minimum_length,
        maximum_length,
        vocal_safety,
        whisper_model,
        analysis_device,
        beam_size,
        patience,
        output_format,
        export_stems,
        use_vocal_stem_for_analysis,
        export_vocal_chunks,
        export_music_stem=True,
        audio_input=None,
    ) -> io.NodeOutput:
        # Runtime guards also protect workflows loaded with manually edited values.
        target_length = float(target_length)
        minimum_length = float(minimum_length)
        maximum_length = float(maximum_length)
        beam_size = int(beam_size)
        patience = float(patience)
        if minimum_length < 5.0:
            raise ValueError("Minimum Length cannot be lower than 5 seconds.")
        if maximum_length > 60.0:
            raise ValueError("Maximum Length cannot be higher than 60 seconds.")
        if minimum_length > maximum_length:
            raise ValueError("Minimum Length cannot be greater than Maximum Length.")
        if not (minimum_length <= target_length <= maximum_length):
            raise ValueError("Target Length must be between Minimum Length and Maximum Length.")
        if beam_size < 1 or beam_size > 20:
            raise ValueError("Beam Size must be between 1 and 20.")
        if patience < 1.0 or patience > 5.0:
            raise ValueError("Patience must be between 1.0 and 5.0.")
        export_stems = bool(export_stems)
        use_vocal_stem_for_analysis = bool(use_vocal_stem_for_analysis)
        export_vocal_chunks = bool(export_vocal_chunks)
        export_music_stem = bool(export_music_stem)
        stem_feature_requested = export_stems or export_music_stem or use_vocal_stem_for_analysis or export_vocal_chunks
        if stem_feature_requested and importlib.util.find_spec("audio_separator") is None:
            raise RuntimeError(
                "Mel-RoFormer stem separation requires audio-separator. "
                "Run install.bat from this node folder once, restart ComfyUI, then try again."
            )

        connected_input_temp = None
        if audio_input is not None:
            waveform_input, sample_rate_input = _audio_dict_to_waveform(audio_input)
            source_stem = _timestamped_connected_name()
            clean_stem = _sanitize_filename(source_stem)
            output_root = os.path.join(folder_paths.get_output_directory(), "audio_chunks", clean_stem)
            os.makedirs(output_root, exist_ok=True)
            source_path = os.path.join(output_root, f"{clean_stem}.flac")
            _write_flac(source_path, waveform_input, sample_rate_input)
            connected_input_temp = source_path
        else:
            source_path = folder_paths.get_annotated_filepath(audio)
            ext_in = os.path.splitext(source_path)[1].lower()
            if ext_in not in {".mp3", ".wav", ".flac"}:
                raise ValueError("Accepts MP3, WAV and FLAC input only.")
            source_stem = os.path.splitext(os.path.basename(source_path))[0]
            clean_stem = _sanitize_filename(source_stem)

        output_root = os.path.join(folder_paths.get_output_directory(), "audio_chunks", clean_stem)
        os.makedirs(output_root, exist_ok=True)

        # Keep each output type in its own subfolder so the song folder stays tidy.
        chunks_dir = os.path.join(output_root, "chunks")
        vocal_chunks_dir = os.path.join(output_root, "vocal_chunks")
        chunk_srts_dir = os.path.join(output_root, "chunk_srts")
        chunk_text_dir = os.path.join(output_root, "chunk_text")
        stems_dir = os.path.join(output_root, "stems")
        transcripts_dir = os.path.join(output_root, "transcripts")
        for directory in (chunks_dir, vocal_chunks_dir, chunk_srts_dir, chunk_text_dir, stems_dir, transcripts_dir):
            os.makedirs(directory, exist_ok=True)

        # V26 stored the reusable Mel-RoFormer vocal stem and its settings marker
        # directly in output_root. Migrate those known files once so upgrading
        # does not force an unnecessary separation pass.
        legacy_vocals = os.path.join(output_root, f"{clean_stem}_vocals.wav")
        legacy_settings = os.path.join(output_root, f"{clean_stem}_mel_roformer_settings.txt")
        old_stems_wav = os.path.join(stems_dir, f"{clean_stem}_vocals.wav")
        migrated_vocals = os.path.join(stems_dir, f"{clean_stem}_vocals.flac")
        migrated_settings = os.path.join(stems_dir, f"{clean_stem}_mel_roformer_settings.txt")

        # Upgrade reusable V14-and-earlier WAV stems to lossless FLAC once, so
        # existing songs do not need another expensive Mel-RoFormer pass.
        old_vocal_source = None
        if os.path.isfile(old_stems_wav):
            old_vocal_source = old_stems_wav
        elif os.path.isfile(legacy_vocals):
            old_vocal_source = legacy_vocals
        if old_vocal_source and not os.path.exists(migrated_vocals):
            try:
                old_waveform, old_sample_rate = _decode_audio(old_vocal_source)
                _write_flac(migrated_vocals, old_waveform, old_sample_rate)
                os.remove(old_vocal_source)
            except Exception as exc:
                _log(f"Could not migrate legacy vocal WAV to FLAC: {exc}")
        if os.path.isfile(legacy_settings) and not os.path.exists(migrated_settings):
            try:
                shutil.move(legacy_settings, migrated_settings)
            except OSError:
                pass

        pbar = ProgressBar(100) if ProgressBar is not None else None
        if pbar is not None:
            try:
                pbar.update_absolute(2, 100)
            except Exception:
                pass

        _log(f"Loading '{os.path.basename(source_path)}'." + (" (from connected AUDIO input)" if audio_input is not None else ""))
        waveform, sample_rate = _decode_audio(source_path)
        total_samples = int(waveform.shape[-1])
        total_duration = total_samples / float(sample_rate)
        if total_samples <= 0:
            raise ValueError("Selected audio file is empty.")

        if pbar is not None:
            try:
                pbar.update_absolute(7, 100)
            except Exception:
                pass

        analysis_source_path = source_path
        analysis_source_label = "original source audio"
        precreated_stem_paths = []
        stem_device = ""
        vocals_path = os.path.join(stems_dir, f"{clean_stem}_vocals.flac")
        instrumental_path = os.path.join(stems_dir, f"{clean_stem}_instrumental.flac")

        # Resolve every requested Mel-RoFormer output up front. This lets vocal
        # analysis/chunks and the user-facing vocal + music stem exports share
        # one separator pass whenever more than one stem is needed.
        want_vocals = export_stems or use_vocal_stem_for_analysis or export_vocal_chunks
        want_instrumental = export_music_stem
        if want_vocals or want_instrumental:
            settings_match = _mel_roformer_settings_match(stems_dir, clean_stem)
            vocals_exist = (
                settings_match
                and os.path.isfile(vocals_path)
                and os.path.getsize(vocals_path) > 0
            )
            instrumental_exists = (
                settings_match
                and os.path.isfile(instrumental_path)
                and os.path.getsize(instrumental_path) > 0
            )

            need_vocals = want_vocals and not vocals_exist
            need_instrumental = want_instrumental and not instrumental_exists

            if need_vocals or need_instrumental:
                reasons = []
                if need_vocals:
                    reasons.append("vocal stem")
                if need_instrumental:
                    reasons.append("instrumental/music stem")
                _log("Running Mel-RoFormer for " + " and ".join(reasons) + ".")
                _generated_paths, stem_device = _export_mel_roformer_stems(
                    source_path=source_path,
                    output_root=stems_dir,
                    clean_stem=clean_stem,
                    want_vocals=need_vocals,
                    want_instrumental=need_instrumental,
                )
            else:
                _log("Existing requested Mel-RoFormer stem files found — reusing them.")

            if want_vocals:
                if not (os.path.isfile(vocals_path) and os.path.getsize(vocals_path) > 0):
                    raise RuntimeError("Mel-RoFormer vocal stem was not available after separation.")
                precreated_stem_paths.append(vocals_path)
            if want_instrumental:
                if not (os.path.isfile(instrumental_path) and os.path.getsize(instrumental_path) > 0):
                    raise RuntimeError("Mel-RoFormer instrumental stem was not available after separation.")
                precreated_stem_paths.append(instrumental_path)

        if use_vocal_stem_for_analysis:
            analysis_source_path = vocals_path
            analysis_source_label = f"Mel-RoFormer vocal stem ({os.path.basename(vocals_path)})"

        _log(
            f"Duration {total_duration:.3f}s at {sample_rate} Hz. "
            f"Analysing vocal boundaries from {analysis_source_label}."
        )
        word_regions, transcript_words, transcript_segments, speech_segments, analysis_used, analysis_note = _analyse_words(
            analysis_source_path,
            whisper_model,
            analysis_device,
            total_duration,
            beam_size,
            patience,
            pbar=pbar,
        )
        word_regions.sort(key=lambda x: x[0])
        transcript_words.sort(key=lambda x: x[0])
        transcript_segments.sort(key=lambda x: x[0])
        speech_segments.sort(key=lambda x: x[0])

        if word_regions:
            _log(f"Detected {len(word_regions)} Whisper word regions using {analysis_used}.")
        else:
            _log(f"No usable word timings. Using waveform energy analysis ({analysis_note}).")

        if pbar is not None:
            try:
                pbar.update_absolute(64, 100)
            except Exception:
                pass

        if use_vocal_stem_for_analysis:
            _log(
                "Cut safety waveform: Mel-RoFormer vocal stem, resampled onto original source time grid."
            )
            cut_analysis_mono = _decode_mono_on_reference_grid(
                vocals_path,
                reference_sample_rate=sample_rate,
                reference_total_samples=total_samples,
            )
            cut_analysis_label = "Mel-RoFormer vocal stem"
        else:
            cut_analysis_mono = waveform.mean(dim=0).detach().cpu().numpy().astype(np.float32, copy=False)
            cut_analysis_label = "original source audio"

        cut_samples, full_clearance_cut_count, reduced_clearance_cut_count, narrow_gap_cut_count, tiny_gap_cut_count, fallback_cut_count, forced_inside_word_count = _find_cut_samples(
            mono=cut_analysis_mono,
            sample_rate=sample_rate,
            total_duration=total_duration,
            target_length=target_length,
            min_length=minimum_length,
            max_length=maximum_length,
            transcript_words=transcript_words,
            speech_segments=speech_segments,
            vocal_safety=vocal_safety,
        )

        chunk_count = max(0, len(cut_samples) - 1)
        if chunk_count == 0:
            raise RuntimeError("Could not create any audio chunks.")

        if pbar is not None:
            try:
                pbar.update_absolute(70, 100)
            except Exception:
                pass

        # Match both the current filename layout and the older V1 layout so
        # rerunning a source cleans up chunks created by either version.
        pattern = re.compile(
            rf"^{re.escape(clean_stem)}_(?:chunk_\d{{3,}}_\d{{2,}}\.\d{{2}}\.\d{{3}}|\d{{2,}}\.\d{{2}}\.\d{{3}}_chunk_\d{{3,}})\.(?:wav|mp3)$",
            re.IGNORECASE,
        )
        for filename in os.listdir(chunks_dir):
            if pattern.match(filename):
                try:
                    os.remove(os.path.join(chunks_dir, filename))
                except OSError:
                    pass

        report_lines = [
            f"Source: {os.path.basename(source_path)}",
            f"Duration: {total_duration:.3f}s",
            f"Sample rate: {sample_rate} Hz",
            f"Analysis: {analysis_used}",
            f"Whisper model: {whisper_model}",
            "Whisper language: English (forced)",
            f"Whisper decoding: beam_size={beam_size}, patience={patience:.1f}",
            f"Vocal safety: {vocal_safety}",
            f"Export vocal stem: {'True' if export_stems else 'False'}",
            f"Export music stem: {'True' if export_music_stem else 'False'}",
            "Vocal separator: Mel-RoFormer",
            f"Use vocal stem for analysis: {'True' if use_vocal_stem_for_analysis else 'False'}",
            f"Export vocal chunks: {'True' if export_vocal_chunks else 'False'}",
            f"Whisper analysis audio: {analysis_source_label}",
            f"Whisper word regions: {len(word_regions)}",
            f"Transcript words: {len(transcript_words)}",
            f"Transcript segments: {len(transcript_segments)}",
            f"Whisper speech/no-speech segments: {len(speech_segments)}",
            "V14 word-edge protected cutting: enabled",
            f"Preferred word-edge clearance: {_word_edge_clearance_seconds(vocal_safety):.3f}s each side",
            f"Quiet vocal-energy window: +/-{_vocal_energy_half_window_seconds(vocal_safety):.3f}s",
            "Structured fallback tiers: full clearance -> reduced clearance -> >=0.100s gap -> widest positive gap -> waveform",
            "Mid-gap local refinement: enabled (maximum +/-0.005s, never outside selected safe corridor)",
            f"Cut safety waveform: {cut_analysis_label}",
            "No-speech cut ranking: enabled",
            "Confidence-aware gap ranking: enabled (no timing padding)",
            f"Full-clearance cuts used: {full_clearance_cut_count}",
            f"Reduced-clearance cuts used: {reduced_clearance_cut_count}",
            f">=0.100s gap fallbacks used: {narrow_gap_cut_count}",
            f"<0.100s positive-gap fallbacks used: {tiny_gap_cut_count}",
            f"Waveform/no-speech fallbacks used: {fallback_cut_count}",
            f"Forced inside-word cuts: {forced_inside_word_count}",
            f"Chunks created: {chunk_count}",
            f"Output: {output_root}",
            f"Main chunks folder: {chunks_dir}",
            f"Vocal chunks folder: {vocal_chunks_dir}",
            f"Per-chunk SRT folder: {chunk_srts_dir}",
            f"Per-chunk TXT folder: {chunk_text_dir}",
            f"Stems folder: {stems_dir}",
            f"Full transcripts folder: {transcripts_dir}",
        ]
        if transcript_words:
            avg_word_conf = sum(w[3] for w in transcript_words) / len(transcript_words)
            low_conf_words = sum(1 for w in transcript_words if w[3] < 0.50)
            report_lines.append(f"Average word confidence: {avg_word_conf:.3f}")
            report_lines.append(f"Low-confidence words (<0.50): {low_conf_words}")
        if speech_segments:
            avg_no_speech = sum(seg[2] for seg in speech_segments) / len(speech_segments)
            report_lines.append(f"Average no-speech score: {avg_no_speech:.3f}")
        if analysis_note:
            report_lines.append(f"Analysis note: {analysis_note}")
        report_lines.append("")

        # Full-song transcript output is standard SubRip (SRT) so it can be
        # connected directly to downstream text/file nodes or saved beside a video.
        if transcript_segments:
            srt_blocks = []
            for index, (start, end, text_value) in enumerate(transcript_segments, start=1):
                clean_text = str(text_value or "").strip()
                if not clean_text:
                    continue
                srt_blocks.append(
                    f"{index}\n"
                    f"{_format_srt_timestamp(start)} --> {_format_srt_timestamp(end)}\n"
                    f"{clean_text}"
                )
            transcript_text = "\n\n".join(srt_blocks).rstrip() + ("\n" if srt_blocks else "")
        else:
            transcript_text = ""

        transcript_filename = f"{clean_stem}_transcript.txt"
        transcript_filepath = os.path.join(transcripts_dir, transcript_filename)
        srt_filename = f"{clean_stem}_transcript.srt"
        srt_filepath = os.path.join(transcripts_dir, srt_filename)
        chunk_transcript_lines = [
            f"Source: {os.path.basename(source_path)}",
            f"Analysis: {analysis_used}",
            f"Whisper model: {whisper_model}",
            "Whisper language: English (forced)",
            f"Whisper decoding: beam_size={beam_size}, patience={patience:.1f}",
            "",
            "Chunk | Start --> End | Filename | Transcript",
        ]

        # Old workflows can retain a serialized WAV widget value from versions
        # before FLAC became the default.  Never use an unknown value as a file
        # extension: V15 could accidentally write MP3 bytes into a .wav file.
        requested_format = str(output_format).strip().upper()
        if requested_format == "MP3":
            ext = "mp3"
        else:
            # FLAC is the lossless default and also the migration target for
            # legacy values such as WAV or any unexpected/stale workflow value.
            ext = "flac"
            if requested_format != "FLAC":
                _log(
                    f"Legacy/unsupported output_format '{requested_format or '<empty>'}' "
                    "was mapped to FLAC."
                )

        # Remove stale per-chunk subtitle/text sidecars from earlier runs of the
        # same source. This matters when chunk durations/counts change.
        chunk_srt_pattern = re.compile(
            rf"^{re.escape(clean_stem)}_chunk_\d{{3,}}_\d{{2,}}\.\d{{2}}\.\d{{3}}\.srt$",
            re.IGNORECASE,
        )
        chunk_txt_pattern = re.compile(
            rf"^{re.escape(clean_stem)}_chunk_\d{{3,}}_\d{{2,}}\.\d{{2}}\.\d{{3}}\.txt$",
            re.IGNORECASE,
        )
        for existing_name in os.listdir(chunk_srts_dir):
            if chunk_srt_pattern.match(existing_name):
                try:
                    os.remove(os.path.join(chunk_srts_dir, existing_name))
                except OSError:
                    pass
        for existing_name in os.listdir(chunk_text_dir):
            if chunk_txt_pattern.match(existing_name):
                try:
                    os.remove(os.path.join(chunk_text_dir, existing_name))
                except OSError:
                    pass

        # Remove stale audio chunks from previous runs/formats so switching
        # from legacy WAV to FLAC does not leave duplicate chunk sets behind.
        main_chunk_pattern = re.compile(
            rf"^{re.escape(clean_stem)}_chunk_\d{{3,}}_\d{{2,}}\.\d{{2}}\.\d{{3}}\.(?:wav|flac|mp3)$",
            re.IGNORECASE,
        )
        for existing_name in os.listdir(chunks_dir):
            if main_chunk_pattern.match(existing_name):
                try:
                    os.remove(os.path.join(chunks_dir, existing_name))
                except OSError:
                    pass

        vocal_chunk_waveform = None
        if export_vocal_chunks:
            if not os.path.isfile(vocals_path):
                raise RuntimeError("Export Vocal Chunks is enabled but the Mel-RoFormer vocal stem is missing.")

            vocal_chunk_pattern = re.compile(
                rf"^{re.escape(clean_stem)}_vocals_chunk_\d{{3,}}_\d{{2,}}\.\d{{2}}\.\d{{3}}\.(?:wav|flac)$",
                re.IGNORECASE,
            )
            for existing_name in os.listdir(vocal_chunks_dir):
                if vocal_chunk_pattern.match(existing_name):
                    try:
                        os.remove(os.path.join(vocal_chunks_dir, existing_name))
                    except OSError:
                        pass

            _log(
                "Preparing vocal stem on the original source sample grid so vocal and main chunks use identical cut indices."
            )
            vocal_chunk_waveform = _decode_waveform_on_reference_grid(
                vocals_path,
                reference_sample_rate=sample_rate,
                reference_total_samples=total_samples,
            )

        for i in range(chunk_count):
            start = int(cut_samples[i])
            end = int(cut_samples[i + 1])
            chunk = waveform[:, start:end].contiguous()
            duration = (end - start) / float(sample_rate)
            duration_tag = _format_duration(duration)
            filename = f"{clean_stem}_chunk_{i + 1:03d}_{duration_tag}.{ext}"
            filepath = os.path.join(chunks_dir, filename)

            if ext == "flac":
                _write_flac(filepath, chunk, sample_rate)
            else:
                _write_mp3(filepath, chunk, sample_rate)

            if export_vocal_chunks and vocal_chunk_waveform is not None:
                vocal_chunk = vocal_chunk_waveform[:, start:end].contiguous()
                vocal_filename = f"{clean_stem}_vocals_chunk_{i + 1:03d}_{duration_tag}.flac"
                vocal_filepath = os.path.join(vocal_chunks_dir, vocal_filename)
                _write_flac(vocal_filepath, vocal_chunk, sample_rate)

                # This should always hold because both chunks use the exact same
                # cut_samples[] indices. Keep the runtime check so a future
                # refactor cannot silently break lip-sync pairing.
                if vocal_chunk.shape[-1] != chunk.shape[-1]:
                    raise RuntimeError(
                        f"Vocal chunk {i + 1:03d} sample count does not match the main chunk."
                    )

            start_t = start / float(sample_rate)
            end_t = end / float(sample_rate)
            report_lines.append(
                f"{i + 1:03d}: {start_t:09.3f} -> {end_t:09.3f} | "
                f"{duration:.3f}s | {filename}"
            )

            # Create chunk-local transcript sidecars using the same midpoint
            # word-assignment rule as the combined transcript. The SRT clock is
            # reset so each individual audio/video chunk begins at 00:00:00,000.
            chunk_srt_text, chunk_text = _build_chunk_srt_and_text(
                transcript_words,
                start_t,
                end_t,
            )
            chunk_base = os.path.splitext(filename)[0]
            chunk_srt_filepath = os.path.join(chunk_srts_dir, f"{chunk_base}.srt")
            chunk_txt_filepath = os.path.join(chunk_text_dir, f"{chunk_base}.txt")
            with open(chunk_srt_filepath, "w", encoding="utf-8", newline="\n") as csf:
                csf.write(chunk_srt_text)
            with open(chunk_txt_filepath, "w", encoding="utf-8", newline="\n") as ctf:
                ctf.write(chunk_text.rstrip() + ("\n" if chunk_text else ""))

            chunk_transcript_lines.append(
                f"{i + 1:03d} | {_format_timestamp(start_t)} --> {_format_timestamp(end_t)} | "
                f"{filename} | {chunk_text}"
            )

            if pbar is not None:
                try:
                    progress = 70 + int(((i + 1) / chunk_count) * 30)
                    pbar.update_absolute(progress, 100)
                except Exception:
                    pass

        with open(transcript_filepath, "w", encoding="utf-8", newline="\n") as tf:
            tf.write("\n".join(chunk_transcript_lines).rstrip() + "\n")

        with open(srt_filepath, "w", encoding="utf-8", newline="\n") as sf:
            sf.write(transcript_text)

        stem_paths = list(precreated_stem_paths)

        if export_stems or export_music_stem or use_vocal_stem_for_analysis or export_vocal_chunks:
            report_lines.append("")
            if stem_device:
                report_lines.append(f"Stem separation device: {stem_device}")
            else:
                report_lines.append("Stem source: existing cached Mel-RoFormer output")
            report_lines.append("Stem separator: Mel-RoFormer")
            report_lines.append(f"Mel-RoFormer model: {MEL_ROFORMER_MODEL}")
            report_lines.append(f"Mel-RoFormer model cache: {os.path.join(folder_paths.models_dir, 'mel_roformer')}")
            report_lines.append(f"Export vocal stem: {'True' if export_stems else 'False'}")
            report_lines.append(f"Export music stem: {'True' if export_music_stem else 'False'}")
            if export_stems:
                report_lines.append(f"Vocal stem: {vocals_path}")
            if export_music_stem:
                report_lines.append(f"Instrumental/music stem: {instrumental_path}")
            for stem_path in stem_paths:
                report_lines.append(f"Prepared stem: {stem_path}")

        report_lines.append("")
        report_lines.append(f"Chunk transcript file: {transcript_filepath}")
        report_lines.append(f"SRT subtitle file: {srt_filepath}")
        report_lines.append(f"Per-chunk SRT files: {chunk_count} (zero-based timestamps)")
        report_lines.append(f"Per-chunk transcript TXT files: {chunk_count}")
        if export_vocal_chunks:
            report_lines.append(
                f"Vocal chunks: {chunk_count} FLAC files, sample-aligned to the corresponding main chunks"
            )
        debug_text = "\n".join(report_lines)
        _log(f"Finished: {chunk_count} chunks saved to {chunks_dir}")
        if export_vocal_chunks:
            _log(f"Finished: {chunk_count} sample-aligned vocal chunks saved to {vocal_chunks_dir}")
        _log(f"Chunk transcript saved to {transcript_filepath}")
        _log(f"SRT transcript saved to {srt_filepath}")
        _log(f"Saved {chunk_count} per-chunk SRT files to {chunk_srts_dir} and transcript TXT files to {chunk_text_dir}.")

        # Remove only known loose V26 outputs from the song root after the new
        # subfolder outputs have been written successfully. User-created files
        # with unrelated names are left untouched.
        legacy_patterns = [
            re.compile(rf"^{re.escape(clean_stem)}_chunk_\d{{3,}}_\d{{2,}}\.\d{{2}}\.\d{{3}}\.(?:wav|flac|mp3|srt|txt)$", re.IGNORECASE),
            re.compile(rf"^{re.escape(clean_stem)}_vocals_chunk_\d{{3,}}_\d{{2,}}\.\d{{2}}\.\d{{3}}\.(?:wav|flac)$", re.IGNORECASE),
            re.compile(rf"^{re.escape(clean_stem)}_transcript\.(?:srt|txt)$", re.IGNORECASE),
        ]
        for legacy_name in os.listdir(output_root):
            legacy_path = os.path.join(output_root, legacy_name)
            if not os.path.isfile(legacy_path):
                continue
            if any(rx.match(legacy_name) for rx in legacy_patterns):
                try:
                    os.remove(legacy_path)
                except OSError:
                    pass

        ui_payload = {"urn_srt": [transcript_text]}
        if audio_input is not None:
            # Expose the materialised connected AUDIO source to ComfyUI's standard
            # AUDIO_UI widget. This keeps the on-node player in sync with the
            # source that was actually processed instead of the fallback picker.
            preview_subfolder = os.path.relpath(output_root, folder_paths.get_output_directory()).replace("\\", "/")
            ui_payload["audio"] = [{
                "filename": os.path.basename(source_path),
                "subfolder": preview_subfolder,
                "type": "output",
            }]
        return io.NodeOutput(output_root, debug_text, transcript_text, ui=ui_payload)


class URNSmartAudioChunkerExtension(ComfyExtension):
    async def get_node_list(self) -> list[type[io.ComfyNode]]:
        return [URNSmartAudioChunker]


async def comfy_entrypoint() -> URNSmartAudioChunkerExtension:
    return URNSmartAudioChunkerExtension()
