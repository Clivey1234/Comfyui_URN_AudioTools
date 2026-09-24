import hashlib
import math
import os
from datetime import datetime
from typing import Optional, Tuple, List

import numpy as np
import torch

import folder_paths


AUDIO_EXTENSIONS = {
    ".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus", ".wma", ".mp4", ".mov", ".mkv", ".webm"
}


def _input_audio_files():
    root = folder_paths.get_input_directory()
    os.makedirs(root, exist_ok=True)
    found = []
    for base, _, files in os.walk(root):
        for name in files:
            if os.path.splitext(name)[1].lower() in AUDIO_EXTENSIONS:
                full = os.path.join(base, name)
                rel = os.path.relpath(full, root).replace("\\", "/")
                found.append(rel)
    return sorted(found) or [""]


def _load_comfy_audio(path: str) -> Tuple[torch.Tensor, int]:
    from comfy_extras.nodes_audio import load as comfy_audio_decode

    waveform, sample_rate = comfy_audio_decode(path)  # [channels, samples]
    if waveform.ndim == 1:
        waveform = waveform.unsqueeze(0)
    return waveform.unsqueeze(0).contiguous(), int(sample_rate)

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
    if waveform.ndim == 1:
        waveform = waveform.unsqueeze(0).unsqueeze(0)
    elif waveform.ndim == 2:
        waveform = waveform.unsqueeze(0)
    elif waveform.ndim != 3:
        raise ValueError(f"Unsupported AUDIO waveform shape: {tuple(waveform.shape)}")
    return waveform.contiguous(), sample_rate


def _resolve_audio_source(audio_file: str, audio_input=None) -> Tuple[torch.Tensor, int, str, bool]:
    # A connected AUDIO input always wins.  The local file picker is only a
    # fallback for standalone use of this node.  Do not inspect/validate the
    # picker value when connected audio is present; workflows may legitimately
    # contain an old picker filename that has since been deleted.
    if audio_input is not None:
        waveform, sample_rate = _audio_dict_to_waveform(audio_input)
        return waveform, sample_rate, _timestamped_connected_name(), True

    if not audio_file:
        raise ValueError("Choose an audio file or connect the AUDIO input.")
    if not folder_paths.exists_annotated_filepath(audio_file):
        raise ValueError(f"Audio file not found: {audio_file}. Choose another file or connect the AUDIO input.")

    path = folder_paths.get_annotated_filepath(audio_file)
    waveform, sample_rate = _load_comfy_audio(path)
    return waveform.float().contiguous(), sample_rate, os.path.splitext(os.path.basename(path))[0], False


def _analysis_signal(waveform: torch.Tensor) -> torch.Tensor:
    x = waveform.detach().float().cpu()
    if x.ndim == 3:
        x = x.mean(dim=0)
    if x.ndim == 2:
        x = x.mean(dim=0)
    return x.contiguous()


def _block_rms(x: torch.Tensor, sample_rate: int, rate_hz: int = 200) -> np.ndarray:
    hop = max(1, int(round(sample_rate / rate_hz)))
    n = (x.numel() // hop) * hop
    if n < hop * 8:
        return np.zeros(8, dtype=np.float32)
    y = x[:n].view(-1, hop)
    rms = torch.sqrt(torch.mean(y * y, dim=1) + 1e-12)
    return rms.numpy().astype(np.float32, copy=False)


def _downsample_signed(x: torch.Tensor, sample_rate: int, rate_hz: int = 2000) -> np.ndarray:
    hop = max(1, int(round(sample_rate / rate_hz)))
    n = (x.numel() // hop) * hop
    if n < hop * 8:
        return np.zeros(8, dtype=np.float32)
    y = x[:n].view(-1, hop).mean(dim=1)
    return y.numpy().astype(np.float32, copy=False)


def _robust_normalize(a: np.ndarray) -> np.ndarray:
    a = np.asarray(a, dtype=np.float32)
    if a.size == 0:
        return a
    med = float(np.median(a))
    mad = float(np.median(np.abs(a - med))) + 1e-8
    return np.clip((a - med) / (mad * 4.0), -4.0, 4.0)


def _smooth(a: np.ndarray, width: int) -> np.ndarray:
    width = max(1, int(width))
    if width <= 1:
        return a.astype(np.float32, copy=False)
    k = np.ones(width, dtype=np.float32) / float(width)
    return np.convolve(a, k, mode="same").astype(np.float32, copy=False)


def _onset_envelope(env: np.ndarray, env_rate: int) -> np.ndarray:
    smooth = _smooth(env, max(1, int(0.055 * env_rate)))
    onset = np.maximum(0.0, np.diff(smooth, prepend=smooth[0]))
    onset = np.maximum(_robust_normalize(onset), 0.0)
    return onset.astype(np.float32, copy=False)


def _estimate_bpm_from_onset(onset: np.ndarray, rate_hz: int) -> Tuple[float, Optional[int], Optional[int]]:
    """Tempo estimate from spectral-flux onset autocorrelation."""
    if onset.size < rate_hz * 4:
        return 0.0, None, None

    onset = np.maximum(_robust_normalize(onset), 0.0)
    if float(np.max(onset)) < 1e-5:
        return 0.0, None, None

    centered = onset - float(np.mean(onset))
    min_lag = max(1, int(rate_hz * 60.0 / 190.0))
    max_lag = min(len(centered) - 2, int(rate_hz * 60.0 / 55.0))
    if max_lag <= min_lag:
        return 0.0, None, None

    def ac(lag: int) -> float:
        if lag <= 0 or lag >= len(centered) - 2:
            return 0.0
        a = centered[:-lag]
        b = centered[lag:]
        denom = math.sqrt(float(np.dot(a, a) * np.dot(b, b))) + 1e-9
        return float(np.dot(a, b)) / denom

    best_lag = None
    best_score = -1e30
    for lag in range(min_lag, max_lag + 1):
        bpm_here = 60.0 * rate_hz / lag
        score = ac(lag)
        if 2 * lag < len(centered):
            score += 0.45 * ac(2 * lag)
        # Resolve common half-time ambiguity toward the musically useful mid-tempo grid.
        if 80.0 <= bpm_here <= 160.0:
            score += 0.04
        if score > best_score:
            best_score = score
            best_lag = lag

    if best_lag is None:
        return 0.0, None, None

    bpm = 60.0 * rate_hz / best_lag
    phase_scores = [float(np.sum(onset[p::best_lag])) for p in range(best_lag)]
    phase = int(np.argmax(phase_scores)) if phase_scores else 0
    return float(bpm), int(best_lag), phase

def _log_spectral_features(x: torch.Tensor, sample_rate: int, frame_rate: int = 50, bands: int = 28) -> Tuple[np.ndarray, int, np.ndarray]:
    """Small coarse log-spectrum used to reject loop points with different harmony/timbre."""
    if x.numel() < 2048:
        return np.zeros((8, bands), dtype=np.float32), frame_rate, np.zeros(8, dtype=np.float32)

    n_fft = 1024 if sample_rate >= 16000 else 512
    hop = max(64, int(round(sample_rate / frame_rate)))
    win = torch.hann_window(n_fft, dtype=torch.float32)
    with torch.no_grad():
        stft = torch.stft(
            x.float(), n_fft=n_fft, hop_length=hop, win_length=n_fft,
            window=win, center=True, return_complex=True
        ).abs()  # [freq, frames]
        raw_mag = stft
        flux = torch.clamp(raw_mag[:, 1:] - raw_mag[:, :-1], min=0.0).sum(dim=0)
        flux = torch.cat([torch.zeros(1, dtype=flux.dtype), flux], dim=0)
        mag = torch.log1p(raw_mag)

        # Log-spaced frequency bands. Ignore the DC bin.
        max_bin = mag.shape[0] - 1
        edges = np.unique(np.round(np.geomspace(1, max(2, max_bin), bands + 1)).astype(int))
        if len(edges) < bands + 1:
            edges = np.round(np.linspace(1, max_bin, bands + 1)).astype(int)
        feats = []
        for i in range(bands):
            lo = int(edges[min(i, len(edges) - 2)])
            hi = int(edges[min(i + 1, len(edges) - 1)])
            hi = max(lo + 1, hi)
            feats.append(mag[lo:hi].mean(dim=0))
        feat = torch.stack(feats, dim=1)  # [frames, bands]
        # Remove broad loudness so spectral cost mainly reflects tonal/timbral shape.
        feat = feat - feat.mean(dim=1, keepdim=True)
        feat = feat / (feat.std(dim=1, keepdim=True) + 1e-5)
    return (feat.cpu().numpy().astype(np.float32, copy=False),
            int(round(sample_rate / hop)),
            flux.cpu().numpy().astype(np.float32, copy=False))


def _window(arr: np.ndarray, start: int, length: int) -> Optional[np.ndarray]:
    if start < 0 or start + length > len(arr):
        return None
    return arr[start:start + length]


def _center_window(arr: np.ndarray, center: int, radius: int) -> Optional[np.ndarray]:
    return _window(arr, center - radius, radius * 2)


def _norm_mse(a: Optional[np.ndarray], b: Optional[np.ndarray]) -> float:
    if a is None or b is None or len(a) == 0 or a.shape != b.shape:
        return 1e6
    aa = a.astype(np.float64, copy=False)
    bb = b.astype(np.float64, copy=False)
    scale = float(np.sqrt(np.mean(aa * aa) + np.mean(bb * bb))) + 1e-8
    return float(np.mean(((aa - bb) / scale) ** 2))


def _corr_cost(a: Optional[np.ndarray], b: Optional[np.ndarray]) -> float:
    if a is None or b is None or len(a) == 0 or a.shape != b.shape:
        return 2.0
    aa = a.astype(np.float64, copy=False).reshape(-1)
    bb = b.astype(np.float64, copy=False).reshape(-1)
    aa = aa - float(np.mean(aa))
    bb = bb - float(np.mean(bb))
    denom = math.sqrt(float(np.dot(aa, aa) * np.dot(bb, bb))) + 1e-9
    corr = float(np.dot(aa, bb)) / denom
    return 1.0 - max(-1.0, min(1.0, corr))


def _spectral_cost(a: Optional[np.ndarray], b: Optional[np.ndarray]) -> float:
    if a is None or b is None or a.shape != b.shape or a.size == 0:
        return 4.0
    d = a.astype(np.float64, copy=False) - b.astype(np.float64, copy=False)
    return float(np.sqrt(np.mean(d * d)))


def _candidate_indices(lo: int, hi: int, count: int) -> np.ndarray:
    if hi <= lo:
        return np.array([lo], dtype=np.int32)
    count = max(2, min(count, hi - lo + 1))
    return np.unique(np.linspace(lo, hi, count).round().astype(np.int32))


def _music_candidate_pairs(
    s_lo: int, s_hi: int, e_lo: int, e_hi: int,
    beat_lag: int, beat_phase: int, min_loop_frames: int,
    quality: str,
) -> List[Tuple[int, int]]:
    # Whole-bar/phrase candidates are the biggest V2 improvement for music.
    beats = []
    first_k = math.ceil((s_lo - beat_phase) / beat_lag)
    last_k = math.floor((s_hi - beat_phase) / beat_lag)
    for k in range(first_k, last_k + 1):
        p = beat_phase + k * beat_lag
        if s_lo <= p <= s_hi:
            beats.append(p)

    phrase_beats = [4, 8, 12, 16, 24, 32, 48, 64]
    pairs = []
    for s in beats:
        for nbeats in phrase_beats:
            e = s + nbeats * beat_lag
            if e_lo <= e <= e_hi and (e - s) >= min_loop_frames:
                pairs.append((s, e))

    # Allow any whole-beat length as fallback, but penalize it later unless it is bar-like.
    if len(pairs) < 8:
        e_first_k = math.ceil((e_lo - beat_phase) / beat_lag)
        e_last_k = math.floor((e_hi - beat_phase) / beat_lag)
        ends = [beat_phase + k * beat_lag for k in range(e_first_k, e_last_k + 1)]
        for s in beats:
            for e in ends:
                if e > s and (e - s) >= min_loop_frames:
                    pairs.append((s, e))

    # Reduce huge candidate sets deterministically.
    limits = {"Fast": 180, "Balanced": 550, "Thorough": 1400}
    limit = limits.get(quality, 550)
    if len(pairs) > limit:
        idx = np.linspace(0, len(pairs) - 1, limit).round().astype(int)
        pairs = [pairs[i] for i in np.unique(idx)]
    return pairs


def _fine_align_boundaries(
    signed: np.ndarray,
    signed_rate: int,
    start_sample: int,
    end_sample: int,
    sample_rate: int,
    max_shift_ms: float = 36.0,
) -> Tuple[int, int, float]:
    """Shift BOTH loop boundaries together to find the cleanest splice.

    Moving start and end by the same amount preserves the musical loop period.
    V2 moved only the start, which could slightly change phrase length.
    """
    s0 = int(round(start_sample * signed_rate / sample_rate))
    e0 = int(round(end_sample * signed_rate / sample_rate))
    max_shift = max(1, int(max_shift_ms * 0.001 * signed_rate))
    short = max(6, int(0.018 * signed_rate))
    med = max(12, int(0.060 * signed_rate))

    best_shift = 0
    best_cost = float("inf")
    for sh in range(-max_shift, max_shift + 1):
        s = s0 + sh
        e = e0 + sh
        if s - med < 1 or e + med >= len(signed):
            continue

        # What leaves the loop versus what enters it after the wrap.
        out_short = _window(signed, e - short, short)
        in_short = _window(signed, s, short)
        out_med = _window(signed, e - med, med)
        in_med = _window(signed, s, med)
        if out_short is None or in_short is None or out_med is None or in_med is None:
            continue

        # Small endpoint / slope terms strongly suppress clicks; correlation
        # keeps the surrounding waveform phase/timbre compatible.
        amp_jump = abs(float(signed[e - 1]) - float(signed[s]))
        slope_out = float(signed[e - 1] - signed[e - 2])
        slope_in = float(signed[s + 1] - signed[s])
        slope_jump = abs(slope_out - slope_in)
        corr = _corr_cost(out_med, in_med)
        local = _corr_cost(out_short, in_short)
        zero_pref = 0.20 * (abs(float(signed[e - 1])) + abs(float(signed[s])))
        cost = 2.0 * amp_jump + 0.8 * slope_jump + 0.9 * local + 0.45 * corr + zero_pref

        if cost < best_cost:
            best_cost = cost
            best_shift = sh

    shift_samples = int(round(best_shift * sample_rate / signed_rate))
    new_start = max(0, start_sample + shift_samples)
    new_end = max(new_start + 1, end_sample + shift_samples)
    return new_start, new_end, float(best_cost if math.isfinite(best_cost) else 0.0)


def _find_loop_points_basic(
    mono: torch.Tensor,
    sample_rate: int,
    mode: str,
    min_loop_seconds: float,
    search_quality: str,
) -> Tuple[int, int, float, float, float]:
    duration = mono.numel() / sample_rate
    if duration < 1.0:
        return 0, mono.numel(), 0.0, 0.0, 0.0

    env_rate = 200
    signed_rate = 2000
    env = _block_rms(mono, sample_rate, env_rate)
    env_n = _robust_normalize(env)
    onset = _onset_envelope(env, env_rate)
    signed = _downsample_signed(mono, sample_rate, signed_rate)
    spec, spec_rate, spectral_onset = _log_spectral_features(mono, sample_rate)

    if mode == "Music":
        bpm, beat_lag_spec, beat_phase_spec = _estimate_bpm_from_onset(spectral_onset, spec_rate)
        if beat_lag_spec and beat_phase_spec is not None:
            beat_lag = max(1, int(round(beat_lag_spec * env_rate / spec_rate)))
            beat_phase = int(round(beat_phase_spec * env_rate / spec_rate)) % beat_lag
        else:
            beat_lag, beat_phase = None, None
    else:
        bpm, beat_lag, beat_phase = 0.0, None, None

    # Leave enough material either side of every boundary for context comparison.
    margin_s = min(2.0, max(0.6, duration * 0.04))
    if duration < 8.0:
        start_lo_s, start_hi_s = max(margin_s, duration * 0.06), duration * 0.42
        end_lo_s, end_hi_s = duration * 0.58, min(duration - margin_s, duration * 0.96)
    else:
        start_lo_s, start_hi_s = max(margin_s, duration * 0.10), duration * 0.48
        end_lo_s, end_hi_s = duration * 0.56, min(duration - margin_s, duration * 0.94)

    min_loop_seconds = max(0.5, min(float(min_loop_seconds), duration * 0.80))
    min_loop_frames = int(round(min_loop_seconds * env_rate))

    s_lo = max(1, int(start_lo_s * env_rate))
    s_hi = min(len(env) - 2, int(start_hi_s * env_rate))
    e_lo = max(2, int(end_lo_s * env_rate))
    e_hi = min(len(env) - 2, int(end_hi_s * env_rate))

    if mode == "Music" and beat_lag and beat_phase is not None:
        pairs = _music_candidate_pairs(s_lo, s_hi, e_lo, e_hi, beat_lag, beat_phase, min_loop_frames, search_quality)
    else:
        quality_counts = {"Fast": (24, 30), "Balanced": (42, 54), "Thorough": (72, 88)}
        sc, ec = quality_counts.get(search_quality, quality_counts["Balanced"])
        starts = _candidate_indices(s_lo, s_hi, sc)
        ends = _candidate_indices(e_lo, e_hi, ec)
        pairs = [(int(s), int(e)) for s in starts for e in ends if (e - s) >= min_loop_frames]

    # Context windows are homologous: around S is compared with around E.
    # That is what V1 got wrong.
    if mode == "Music" and beat_lag:
        context_s = max(0.7, min(2.4, (beat_lag / env_rate) * 2.0))
    elif mode == "Ambience":
        context_s = 1.4
    else:
        context_s = 1.0
    env_r = max(12, int(context_s * env_rate))
    onset_r = max(8, int(min(context_s, 1.5) * env_rate))
    spec_r = max(4, int(context_s * spec_rate))
    sig_r = max(20, int(0.055 * signed_rate))

    best = None
    best_score = float("inf")

    for s, e in pairs:
        if e <= s:
            continue
        loop_s = (e - s) / env_rate

        env_cost = _norm_mse(_center_window(env_n, s, env_r), _center_window(env_n, e, env_r))
        onset_cost = _norm_mse(_center_window(onset, s, onset_r), _center_window(onset, e, onset_r))

        ss = int(round(s * spec_rate / env_rate))
        ee = int(round(e * spec_rate / env_rate))
        spec_cost = _spectral_cost(_center_window(spec, ss, spec_r), _center_window(spec, ee, spec_r))

        # V3 explicitly scores the material that will actually overlap at the seam:
        # natural audio AFTER loop_end versus the restarted audio AFTER loop_start.
        seam_env_n = max(8, int(0.28 * env_rate))
        seam_spec_n = max(4, int(0.28 * spec_rate))
        fwd_env_cost = _norm_mse(_window(env_n, s, seam_env_n), _window(env_n, e, seam_env_n))
        fwd_onset_cost = _norm_mse(_window(onset, s, seam_env_n), _window(onset, e, seam_env_n))
        fwd_spec_cost = _spectral_cost(_window(spec, ss, seam_spec_n), _window(spec, ee, seam_spec_n))

        si = int(round(s * signed_rate / env_rate))
        ei = int(round(e * signed_rate / env_rate))
        wave_cost = _corr_cost(_center_window(signed, si, sig_r), _center_window(signed, ei, sig_r))

        local_w = max(4, int(0.12 * env_rate))
        ea = _center_window(env, e, local_w)
        eb = _center_window(env, s, local_w)
        if ea is None or eb is None:
            level_cost = 1.0
        else:
            ra = float(np.mean(ea)) + 1e-8
            rb = float(np.mean(eb)) + 1e-8
            level_cost = abs(math.log(ra / rb))

        long_pref = 0.12 * (min_loop_seconds / max(loop_s, min_loop_seconds))

        if mode == "Music":
            # Loop length in whole beats, with strong preference for 4-beat/bar multiples.
            if beat_lag:
                nbeats = max(1, int(round((e - s) / beat_lag)))
                bar_penalty = 0.0 if nbeats % 4 == 0 else (0.12 if nbeats % 3 == 0 else 0.35)
            else:
                bar_penalty = 0.25
            score = (
                0.55 * env_cost +
                0.65 * onset_cost +
                0.90 * spec_cost +
                0.40 * wave_cost +
                0.55 * level_cost +
                1.10 * fwd_env_cost +
                1.25 * fwd_onset_cost +
                1.50 * fwd_spec_cost +
                bar_penalty + long_pref
            )
        elif mode == "Ambience":
            score = (
                0.85 * env_cost + 0.30 * onset_cost + 0.55 * spec_cost + 0.50 * wave_cost +
                0.85 * level_cost + 1.20 * fwd_env_cost + 0.35 * fwd_onset_cost + 0.80 * fwd_spec_cost + long_pref
            )
        else:
            score = (
                0.75 * env_cost + 0.45 * onset_cost + 0.70 * spec_cost + 0.50 * wave_cost +
                0.70 * level_cost + 1.05 * fwd_env_cost + 0.75 * fwd_onset_cost + 1.05 * fwd_spec_cost + long_pref
            )

        if score < best_score:
            best_score = score
            best = (s, e)

    if best is None:
        s_sec = duration * 0.25
        e_sec = duration * 0.85
    else:
        s_sec = best[0] / env_rate
        e_sec = best[1] / env_rate

    start_sample = max(0, min(mono.numel() - 2, int(round(s_sec * sample_rate))))
    end_sample = max(start_sample + 1, min(mono.numel(), int(round(e_sec * sample_rate))))

    # V3 shifts BOTH boundaries together, preserving the exact phrase length.
    aligned_start, aligned_end, phase_cost = _fine_align_boundaries(
        signed, signed_rate, start_sample, end_sample, sample_rate
    )
    if aligned_end <= mono.numel() - 2 and aligned_start < aligned_end - int(0.5 * sample_rate):
        start_sample, end_sample = aligned_start, aligned_end

    return start_sample, end_sample, float(bpm), float(best_score if math.isfinite(best_score) else 0.0), float(phase_cost)



def _require_advanced_audio_libs():
    try:
        import librosa
        from scipy import signal
    except Exception as exc:
        raise RuntimeError(
            "Advanced (Librosa) analysis needs librosa and scipy. "
            "Install this node's requirements.txt (or run install.bat), restart ComfyUI, then try again. "
            f"Original import error: {exc}"
        ) from exc
    return librosa, signal


def _safe_cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    aa = np.asarray(a, dtype=np.float64).reshape(-1)
    bb = np.asarray(b, dtype=np.float64).reshape(-1)
    if aa.size == 0 or aa.shape != bb.shape:
        return 0.0
    aa = aa - float(np.mean(aa))
    bb = bb - float(np.mean(bb))
    denom = math.sqrt(float(np.dot(aa, aa) * np.dot(bb, bb))) + 1e-12
    return float(np.clip(np.dot(aa, bb) / denom, -1.0, 1.0))


def _zscore_rows(a: np.ndarray) -> np.ndarray:
    a = np.asarray(a, dtype=np.float32)
    if a.ndim == 1:
        a = a[None, :]
    mu = np.mean(a, axis=1, keepdims=True)
    sd = np.std(a, axis=1, keepdims=True) + 1e-6
    return ((a - mu) / sd).astype(np.float32, copy=False)


def _l2_columns(a: np.ndarray) -> np.ndarray:
    a = np.asarray(a, dtype=np.float32)
    denom = np.sqrt(np.sum(a * a, axis=0, keepdims=True)) + 1e-8
    return (a / denom).astype(np.float32, copy=False)


def _beat_sync_feature(feature: np.ndarray, beat_frames: np.ndarray, total_frames: int) -> np.ndarray:
    """Average a frame feature inside each beat interval.

    Column k describes the audio AFTER boundary beat_frames[k] and before the
    next beat. This makes boundary i and boundary j directly comparable.
    """
    feature = np.asarray(feature, dtype=np.float32)
    if feature.ndim == 1:
        feature = feature[None, :]
    frames = np.asarray(beat_frames, dtype=np.int64)
    if len(frames) < 2:
        return np.zeros((feature.shape[0], 0), dtype=np.float32)
    out = []
    last_frame = min(int(total_frames), feature.shape[1])
    for i in range(len(frames) - 1):
        lo = int(max(0, min(frames[i], last_frame - 1)))
        hi = int(max(lo + 1, min(frames[i + 1], last_frame)))
        if hi <= lo:
            out.append(feature[:, lo])
        else:
            out.append(np.mean(feature[:, lo:hi], axis=1))
    return np.stack(out, axis=1).astype(np.float32, copy=False)


def _detect_meter_and_phase(beat_strength: np.ndarray) -> Tuple[int, int, float]:
    """Infer a useful bar grid from beat accents; prefer 4/4 unless 3/4 is clearly stronger."""
    x = np.asarray(beat_strength, dtype=np.float64)
    if x.size < 12:
        return 4, 0, 0.0
    x = (x - np.median(x)) / (np.std(x) + 1e-8)
    candidates = []
    for meter in (4, 3):
        for phase in range(meter):
            accent = x[phase::meter]
            others = np.concatenate([x[p::meter] for p in range(meter) if p != phase])
            if accent.size < 2 or others.size < 2:
                score = -1e9
            else:
                score = float(np.mean(accent) - 0.35 * np.mean(others))
            # Most material this node is aimed at is 4/4. A small prior stops
            # a weak three-beat pattern from accidentally winning.
            if meter == 4:
                score += 0.08
            candidates.append((score, meter, phase))
    score, meter, phase = max(candidates, key=lambda z: z[0])
    return int(meter), int(phase), float(score)


def _sequence_cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Mean per-column cosine similarity mapped to 0..1."""
    if a.shape != b.shape or a.size == 0:
        return 0.0
    aa = np.asarray(a, dtype=np.float64)
    bb = np.asarray(b, dtype=np.float64)
    dots = np.sum(aa * bb, axis=0)
    den = np.sqrt(np.sum(aa * aa, axis=0) * np.sum(bb * bb, axis=0)) + 1e-10
    c = np.clip(dots / den, -1.0, 1.0)
    return float(np.mean((c + 1.0) * 0.5))


def _sequence_corr_similarity(a: np.ndarray, b: np.ndarray) -> float:
    if a.shape != b.shape or a.size == 0:
        return 0.0
    c = _safe_cosine_similarity(a, b)
    return float(np.clip((c + 1.0) * 0.5, 0.0, 1.0))


def _sequence_distance_similarity(a: np.ndarray, b: np.ndarray, scale: float = 1.0) -> float:
    if a.shape != b.shape or a.size == 0:
        return 0.0
    d = float(np.sqrt(np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2)))
    return float(math.exp(-d / max(1e-6, scale)))


def _advanced_feature_bundle(mono: torch.Tensor, sample_rate: int, search_quality: str, resample_type: str = "kaiser_fast"):
    librosa, _ = _require_advanced_audio_libs()
    y = mono.detach().float().cpu().numpy().astype(np.float32, copy=False)
    if y.ndim != 1:
        y = np.reshape(y, (-1,))

    # 22.05 kHz gives reliable beat/chroma analysis while keeping a several-minute
    # track quick enough for an interactive ComfyUI node.
    analysis_sr = 22050
    if sample_rate != analysis_sr:
        y_a = librosa.resample(y, orig_sr=sample_rate, target_sr=analysis_sr, res_type=resample_type)
    else:
        y_a = y

    # Avoid pathological all-zero tracks upsetting feature normalisation.
    peak = float(np.max(np.abs(y_a))) if y_a.size else 0.0
    if peak > 1e-8:
        y_a = y_a / peak

    hop = 512
    # Compute one shared STFT and derive all high-level features from it. This is
    # dramatically faster than running CQT + HPSS + separate transforms while
    # retaining the phrase/harmony information V6 needs.
    D = librosa.stft(y_a, n_fft=2048, hop_length=hop, win_length=2048)
    mag = np.abs(D).astype(np.float32, copy=False)
    power = mag * mag
    db = librosa.power_to_db(power + 1e-10, ref=np.max)
    onset_env = librosa.onset.onset_strength(S=db, sr=analysis_sr, hop_length=hop)
    tempo, beat_frames = librosa.beat.beat_track(
        onset_envelope=onset_env,
        sr=analysis_sr,
        hop_length=hop,
        trim=False,
        units="frames",
    )
    tempo_arr = np.asarray(tempo).reshape(-1)
    bpm = float(tempo_arr[0]) if tempo_arr.size else 0.0
    beat_frames = np.asarray(beat_frames, dtype=np.int64)

    if len(beat_frames) < 10:
        raise RuntimeError("Advanced analysis could not find a stable beat grid in this audio.")

    # Include one beat boundary after the final detected beat when possible so
    # the last detected interval can participate in phrase matching.
    median_beat_frames = int(max(1, round(np.median(np.diff(beat_frames)))))
    total_frames = max(1, 1 + len(onset_env))
    extra = int(beat_frames[-1] + median_beat_frames)
    if extra < total_frames:
        beat_frames = np.concatenate([beat_frames, [extra]])

    # STFT chroma + CENS provide complementary harmonic views. CENS is robust to
    # dynamics, while STFT chroma keeps local chord changes sharp.
    chroma_stft = librosa.feature.chroma_stft(S=power, sr=analysis_sr, hop_length=hop)
    chroma_cens = librosa.feature.chroma_cens(C=chroma_stft, sr=analysis_sr, hop_length=hop)
    chroma = 0.68 * _l2_columns(chroma_stft) + 0.32 * _l2_columns(chroma_cens)

    mfcc = librosa.feature.mfcc(S=db, sr=analysis_sr, n_mfcc=16)
    contrast = librosa.feature.spectral_contrast(S=mag, sr=analysis_sr)
    rms = librosa.feature.rms(S=mag, frame_length=2048, hop_length=hop)

    n_frames = min(chroma.shape[1], mfcc.shape[1], contrast.shape[1], rms.shape[1], len(onset_env))
    # Keep only beat boundaries that land inside every feature stream.
    beat_frames = beat_frames[beat_frames < n_frames]
    if len(beat_frames) < 10:
        raise RuntimeError("Advanced analysis found too few usable beat intervals after feature extraction.")

    # Every sync array has one column per complete beat interval.
    chroma_b = _beat_sync_feature(chroma[:, :n_frames], beat_frames, n_frames)
    mfcc_b = _zscore_rows(_beat_sync_feature(mfcc[:, :n_frames], beat_frames, n_frames))
    contrast_b = _zscore_rows(_beat_sync_feature(contrast[:, :n_frames], beat_frames, n_frames))
    onset_b = _zscore_rows(_beat_sync_feature(onset_env[:n_frames], beat_frames, n_frames))
    rms_b_raw = _beat_sync_feature(np.log1p(20.0 * rms[:, :n_frames]), beat_frames, n_frames)
    rms_b = _zscore_rows(rms_b_raw)

    intervals = min(chroma_b.shape[1], mfcc_b.shape[1], contrast_b.shape[1], onset_b.shape[1], rms_b.shape[1])
    if intervals < 8:
        raise RuntimeError("Advanced analysis did not produce enough beat-synchronous feature intervals.")
    chroma_b = chroma_b[:, :intervals]
    mfcc_b = mfcc_b[:, :intervals]
    contrast_b = contrast_b[:, :intervals]
    onset_b = onset_b[:, :intervals]
    rms_b = rms_b[:, :intervals]
    rms_b_raw = rms_b_raw[:, :intervals]
    beat_frames = beat_frames[:intervals + 1]

    beat_strength = []
    for bf in beat_frames[:-1]:
        idx = int(min(max(0, bf), len(onset_env) - 1))
        beat_strength.append(float(onset_env[idx]))
    meter, bar_phase, meter_score = _detect_meter_and_phase(np.asarray(beat_strength, dtype=np.float32))

    beat_times = librosa.frames_to_time(beat_frames, sr=analysis_sr, hop_length=hop)
    return {
        "bpm": bpm,
        "meter": meter,
        "bar_phase": bar_phase,
        "meter_score": meter_score,
        "beat_times": np.asarray(beat_times, dtype=np.float64),
        "chroma": chroma_b,
        "mfcc": mfcc_b,
        "contrast": contrast_b,
        "onset": onset_b,
        "rms": rms_b,
        "rms_raw": rms_b_raw,
        "analysis_sr": analysis_sr,
    }


def _phrase_match_components(bundle, i: int, j: int, ctx_beats: int):
    n = bundle["chroma"].shape[1]
    if i < 0 or j < 0 or i + ctx_beats > n or j + ctx_beats > n:
        return None

    sl_i = slice(i, i + ctx_beats)
    sl_j = slice(j, j + ctx_beats)
    harmonic = _sequence_cosine_similarity(bundle["chroma"][:, sl_i], bundle["chroma"][:, sl_j])
    rhythm = _sequence_corr_similarity(bundle["onset"][:, sl_i], bundle["onset"][:, sl_j])
    timbre_mfcc = _sequence_distance_similarity(bundle["mfcc"][:, sl_i], bundle["mfcc"][:, sl_j], scale=1.45)
    timbre_contrast = _sequence_distance_similarity(bundle["contrast"][:, sl_i], bundle["contrast"][:, sl_j], scale=1.35)
    timbre = 0.65 * timbre_mfcc + 0.35 * timbre_contrast
    level = _sequence_distance_similarity(bundle["rms"][:, sl_i], bundle["rms"][:, sl_j], scale=1.25)

    # A small look-behind term helps identify the same structural boundary, but
    # it is deliberately weak because two choruses can have different lead-ins.
    back = min(4, i, j)
    if back >= 2:
        pre_h = _sequence_cosine_similarity(
            bundle["chroma"][:, i - back:i], bundle["chroma"][:, j - back:j]
        )
        pre_r = _sequence_corr_similarity(
            bundle["onset"][:, i - back:i], bundle["onset"][:, j - back:j]
        )
        pre = 0.65 * pre_h + 0.35 * pre_r
    else:
        pre = 0.5

    structural = (
        0.40 * harmonic +
        0.24 * rhythm +
        0.20 * timbre +
        0.10 * level +
        0.06 * pre
    )
    return {
        "structural": float(np.clip(structural, 0.0, 1.0)),
        "harmonic": float(np.clip(harmonic, 0.0, 1.0)),
        "rhythm": float(np.clip(rhythm, 0.0, 1.0)),
        "timbre": float(np.clip(timbre, 0.0, 1.0)),
        "level": float(np.clip(level, 0.0, 1.0)),
        "pre": float(np.clip(pre, 0.0, 1.0)),
    }


def _preference_bonus(loop_preference: str, beats: int, meter: int, loop_seconds: float, duration: float) -> float:
    bars = beats / max(1, meter)
    fixed = {
        "4 Bars": 4.0,
        "8 Bars": 8.0,
        "16 Bars": 16.0,
    }
    if loop_preference in fixed:
        target = fixed[loop_preference]
        # Soft target: exact length gets +0.14; one bar away is still usable.
        return 0.14 * math.exp(-abs(bars - target) / 1.8)
    if loop_preference == "Long Phrase":
        frac = min(1.0, loop_seconds / max(1.0, duration * 0.55))
        return 0.10 * frac
    # Auto: slightly favour a useful phrase length without overriding similarity.
    return 0.04 * min(1.0, bars / 12.0)


def _candidate_pairs_advanced(bundle, duration: float, minimum_loop_seconds: float, loop_preference: str, search_quality: str):
    bt = bundle["beat_times"]
    meter = bundle["meter"]
    phase = bundle["bar_phase"]
    n_intervals = bundle["chroma"].shape[1]
    ctx_map = {"Fast": 4, "Balanced": 8, "Thorough": 12, "Maximum": 16}
    ctx = ctx_map.get(search_quality, 12)
    ctx = min(ctx, max(4, n_intervals // 5))

    # Keep boundaries away from the first instant and leave enough material after
    # the later boundary for phrase recurrence and the crossfade handle.
    start_min_t = max(0.5, duration * 0.03)
    end_max_t = max(start_min_t + minimum_loop_seconds, duration - max(0.75, duration * 0.02))

    starts = [i for i in range(0, n_intervals - ctx) if bt[i] >= start_min_t and (i - phase) % meter == 0]
    ends = [j for j in range(1, n_intervals - ctx) if bt[j] <= end_max_t and (j - phase) % meter == 0]

    # If accent-phase inference was poor, do not let it eliminate all candidates.
    if len(starts) < 2 or len(ends) < 3:
        starts = list(range(0, n_intervals - ctx))
        ends = list(range(1, n_intervals - ctx))

    fixed_bars = {"4 Bars": 4, "8 Bars": 8, "16 Bars": 16}
    target_beats = fixed_bars.get(loop_preference, None)
    if target_beats is not None:
        target_beats *= meter

    pairs = []
    for i in starts:
        for j in ends:
            if j <= i:
                continue
            loop_s = float(bt[j] - bt[i])
            if loop_s < minimum_loop_seconds:
                continue
            if loop_s > duration * 0.88:
                continue
            nbeats = j - i
            if nbeats < meter:
                continue
            # A named bar preference means that phrase length. Both boundaries
            # are already constrained to the same inferred bar phase, so the
            # beat-count difference should be exact.
            if target_beats is not None and nbeats != target_beats:
                continue
            comp = _phrase_match_components(bundle, i, j, ctx)
            if comp is None:
                continue
            bonus = _preference_bonus(loop_preference, nbeats, meter, loop_s, duration)
            selection = comp["structural"] + bonus
            pairs.append({
                "i": i,
                "j": j,
                "seconds": loop_s,
                "beats": nbeats,
                "bars": nbeats / meter,
                "selection": selection,
                **comp,
            })

    pairs.sort(key=lambda c: c["selection"], reverse=True)
    stage2_limits = {"Fast": 8, "Balanced": 16, "Thorough": 28, "Maximum": 48}
    return pairs[:stage2_limits.get(search_quality, 28)], ctx



def _end_to_start_components(bundle, start_i: int, ctx_beats: int):
    """Score how well the physical end of the source can return to start_i.

    The final ctx_beats of the analysed source are compared with the ctx_beats
    immediately after the proposed loop start.  This is intentionally different
    from the normal internal-loop comparison because Preserve Full Input has no
    audio after the physical source end.
    """
    n = bundle["chroma"].shape[1]
    if start_i < 0 or start_i + ctx_beats > n or n < ctx_beats:
        return None

    head = slice(start_i, start_i + ctx_beats)
    tail = slice(n - ctx_beats, n)
    harmonic = _sequence_cosine_similarity(bundle["chroma"][:, tail], bundle["chroma"][:, head])
    rhythm = _sequence_corr_similarity(bundle["onset"][:, tail], bundle["onset"][:, head])
    timbre_mfcc = _sequence_distance_similarity(bundle["mfcc"][:, tail], bundle["mfcc"][:, head], scale=1.45)
    timbre_contrast = _sequence_distance_similarity(bundle["contrast"][:, tail], bundle["contrast"][:, head], scale=1.35)
    timbre = 0.65 * timbre_mfcc + 0.35 * timbre_contrast
    level = _sequence_distance_similarity(bundle["rms"][:, tail], bundle["rms"][:, head], scale=1.25)

    structural = (
        0.42 * harmonic +
        0.26 * rhythm +
        0.20 * timbre +
        0.12 * level
    )
    return {
        "structural": float(np.clip(structural, 0.0, 1.0)),
        "harmonic": float(np.clip(harmonic, 0.0, 1.0)),
        "rhythm": float(np.clip(rhythm, 0.0, 1.0)),
        "timbre": float(np.clip(timbre, 0.0, 1.0)),
        "level": float(np.clip(level, 0.0, 1.0)),
        "pre": 0.5,
    }


def _candidate_starts_preserve_full(bundle, duration: float, minimum_loop_seconds: float, loop_preference: str, search_quality: str):
    """Generate end-anchored loop starts for Preserve Full Input mode."""
    bt = bundle["beat_times"]
    meter = max(1, int(bundle["meter"]))
    phase = int(bundle["bar_phase"])
    n_intervals = bundle["chroma"].shape[1]
    ctx_map = {"Fast": 4, "Balanced": 8, "Thorough": 12, "Maximum": 16}
    ctx = ctx_map.get(search_quality, 12)
    ctx = min(ctx, max(4, n_intervals // 5))

    start_min_t = max(0.35, duration * 0.02)
    latest_start_t = duration - float(minimum_loop_seconds)
    if latest_start_t <= start_min_t:
        return [], ctx

    starts = [
        i for i in range(0, max(0, n_intervals - ctx + 1))
        if bt[i] >= start_min_t
        and bt[i] <= latest_start_t
        and (i - phase) % meter == 0
    ]
    if not starts:
        starts = [
            i for i in range(0, max(0, n_intervals - ctx + 1))
            if bt[i] >= start_min_t and bt[i] <= latest_start_t
        ]

    fixed_bars = {"4 Bars": 4, "8 Bars": 8, "16 Bars": 16}
    target_beats = fixed_bars.get(loop_preference)
    if target_beats is not None:
        target_beats *= meter

    pairs = []
    for i in starts:
        loop_s = float(duration - bt[i])
        if loop_s < minimum_loop_seconds:
            continue
        # Avoid near-whole-file loops, while still allowing substantially longer
        # end-anchored phrases than the old internal-loop search when needed.
        if loop_s > duration * 0.96:
            continue
        nbeats = max(1, n_intervals - i)
        if target_beats is not None and nbeats != target_beats:
            continue
        comp = _end_to_start_components(bundle, i, ctx)
        if comp is None:
            continue
        bonus = _preference_bonus(loop_preference, nbeats, meter, loop_s, duration)
        # A tiny preference for a later start makes two otherwise equal seams feel
        # more like an extension of the supplied clip rather than restarting it.
        late_bonus = 0.12 * min(1.0, float(bt[i]) / max(1e-6, duration))
        selection = comp["structural"] + bonus + late_bonus
        pairs.append({
            "i": i,
            "seconds": loop_s,
            "beats": nbeats,
            "bars": nbeats / meter,
            "selection": selection,
            **comp,
        })

    pairs.sort(key=lambda c: c["selection"], reverse=True)
    stage2_limits = {"Fast": 8, "Balanced": 16, "Thorough": 28, "Maximum": 48}
    return pairs[:stage2_limits.get(search_quality, 28)], ctx

def _fft_similarity(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) != len(b) or len(a) < 16:
        return 0.0
    win = np.hanning(len(a)).astype(np.float64)
    A = np.log1p(np.abs(np.fft.rfft(a.astype(np.float64) * win)))
    B = np.log1p(np.abs(np.fft.rfft(b.astype(np.float64) * win)))
    return float(np.clip((_safe_cosine_similarity(A, B) + 1.0) * 0.5, 0.0, 1.0))


def _stage2_seam_test(mono_np: np.ndarray, sample_rate: int, start_sample: int, end_sample: int, mode: str):
    """Test the actual post-end -> post-start splice for top structural candidates.

    SciPy cross-correlation suggests phase offsets, then a small deterministic
    shift search chooses the cleanest local match. We deliberately keep the
    correction under ~30 ms so musical timing is not perceptibly stretched.
    """
    _, signal = _require_advanced_audio_libs()
    y = np.asarray(mono_np, dtype=np.float32)
    max_shift = max(1, int(round(0.030 * sample_rate)))
    probe = max(256, int(round((0.11 if mode == "Music" else 0.22) * sample_rate)))
    if start_sample + probe >= len(y) or end_sample + probe >= len(y):
        return end_sample, 0.0, 0.03

    a0 = y[end_sample:end_sample + probe].astype(np.float64)
    b0 = y[start_sample:start_sample + probe].astype(np.float64)
    a0 -= np.mean(a0)
    b0 -= np.mean(b0)
    corr = signal.correlate(a0, b0, mode="full", method="fft")
    lags = signal.correlation_lags(len(a0), len(b0), mode="full")
    mask = (lags >= -max_shift) & (lags <= max_shift)
    suggested = int(lags[mask][int(np.argmax(corr[mask]))]) if np.any(mask) else 0

    # Search near both signs of the correlation suggestion plus an even grid;
    # direct scoring below decides the correct convention safely.
    step = max(1, int(round(0.002 * sample_rate)))
    shifts = set(range(-max_shift, max_shift + 1, step))
    for d in range(-3 * step, 3 * step + 1, step):
        shifts.add(int(np.clip(suggested + d, -max_shift, max_shift)))
        shifts.add(int(np.clip(-suggested + d, -max_shift, max_shift)))

    best = None
    for sh in sorted(shifts):
        e = end_sample + sh
        if e <= start_sample + 1 or e + probe >= len(y):
            continue
        a = y[e:e + probe]
        b = y[start_sample:start_sample + probe]
        corr_sim = float(np.clip((_safe_cosine_similarity(a, b) + 1.0) * 0.5, 0.0, 1.0))
        spec_sim = _fft_similarity(a, b)
        ra = math.sqrt(float(np.mean(a.astype(np.float64) ** 2)) + 1e-12)
        rb = math.sqrt(float(np.mean(b.astype(np.float64) ** 2)) + 1e-12)
        level_sim = math.exp(-abs(math.log((ra + 1e-9) / (rb + 1e-9))))

        # Endpoint/slope continuity is weak evidence here because the real splice
        # is crossfaded, but it helps break ties between otherwise equal phrases.
        amp_scale = max(1e-4, 0.5 * (ra + rb))
        amp_jump = abs(float(y[e]) - float(y[start_sample])) / amp_scale
        if e + 1 < len(y) and start_sample + 1 < len(y):
            slope_jump = abs(float((y[e + 1] - y[e]) - (y[start_sample + 1] - y[start_sample]))) / amp_scale
        else:
            slope_jump = 1.0
        edge_sim = math.exp(-0.45 * amp_jump - 0.12 * slope_jump)

        seam = 0.42 * spec_sim + 0.30 * corr_sim + 0.20 * level_sim + 0.08 * edge_sim
        if best is None or seam > best[0]:
            best = (float(seam), int(e))

    if best is None:
        best = (0.0, int(end_sample))

    seam = float(np.clip(best[0], 0.0, 1.0))
    # A highly similar repeated phrase can tolerate a little more overlap. If the
    # two performances differ, a short anti-click splice avoids audible phasing.
    if mode == "Music":
        if seam >= 0.90:
            crossfade = 0.085
        elif seam >= 0.80:
            crossfade = 0.060
        elif seam >= 0.68:
            crossfade = 0.040
        else:
            crossfade = 0.024
    elif mode == "Ambience":
        crossfade = 0.55 + 0.45 * seam
    else:
        crossfade = 0.10 + 0.08 * seam
    return best[1], seam, float(crossfade)



def _stage2_end_seam_test(mono_np: np.ndarray, sample_rate: int, start_sample: int, mode: str):
    """Fine-test the physical source-end -> proposed loop-start splice."""
    y = np.asarray(mono_np, dtype=np.float32)
    max_shift = max(1, int(round(0.030 * sample_rate)))
    probe = max(256, int(round((0.13 if mode == "Music" else 0.25) * sample_rate)))
    if len(y) < probe + 2 or start_sample + probe >= len(y):
        return start_sample, 0.0, 0.03

    tail = y[-probe:]
    step = max(1, int(round(0.002 * sample_rate)))
    best = None
    for sh in range(-max_shift, max_shift + 1, step):
        s = start_sample + sh
        if s < 0 or s + probe >= len(y):
            continue
        head = y[s:s + probe]
        corr_sim = float(np.clip((_safe_cosine_similarity(tail, head) + 1.0) * 0.5, 0.0, 1.0))
        spec_sim = _fft_similarity(tail, head)
        ra = math.sqrt(float(np.mean(tail.astype(np.float64) ** 2)) + 1e-12)
        rb = math.sqrt(float(np.mean(head.astype(np.float64) ** 2)) + 1e-12)
        level_sim = math.exp(-abs(math.log((ra + 1e-9) / (rb + 1e-9))))
        amp_scale = max(1e-4, 0.5 * (ra + rb))
        amp_jump = abs(float(y[-1]) - float(y[s])) / amp_scale
        if s + 1 < len(y) and len(y) >= 2:
            slope_jump = abs(float((y[s + 1] - y[s]) - (y[-1] - y[-2]))) / amp_scale
        else:
            slope_jump = 1.0
        edge_sim = math.exp(-0.45 * amp_jump - 0.12 * slope_jump)
        seam = 0.42 * spec_sim + 0.30 * corr_sim + 0.20 * level_sim + 0.08 * edge_sim
        if best is None or seam > best[0]:
            best = (float(seam), int(s))

    if best is None:
        best = (0.0, int(start_sample))
    seam = float(np.clip(best[0], 0.0, 1.0))
    if mode == "Music":
        if seam >= 0.90:
            crossfade = 0.085
        elif seam >= 0.80:
            crossfade = 0.060
        elif seam >= 0.68:
            crossfade = 0.040
        else:
            crossfade = 0.024
    elif mode == "Ambience":
        crossfade = 0.55 + 0.45 * seam
    else:
        crossfade = 0.10 + 0.08 * seam
    return best[1], seam, float(crossfade)


def _find_loop_points_advanced_preserve_full(
    mono: torch.Tensor,
    sample_rate: int,
    mode: str,
    min_loop_seconds: float,
    search_quality: str,
    loop_preference: str,
    resample_type: str = "kaiser_fast",
):
    """Find a loop start whose loop-out is anchored to the physical source end."""
    if mode != "Music":
        s, _, bpm, score, phase = _find_loop_points_basic(
            mono, sample_rate, mode, min_loop_seconds,
            search_quality if search_quality != "Maximum" else "Thorough"
        )
        end = int(mono.numel())
        max_start = max(0, end - int(max(0.5, float(min_loop_seconds)) * sample_rate))
        s = min(int(s), max_start)
        confidence = float(np.clip(100.0 * math.exp(-0.18 * max(0.0, score)), 15.0, 85.0))
        return {
            "start": s, "end": end, "bpm": bpm, "confidence": confidence,
            "structural": 0.0, "harmonic": 0.0, "rhythm": 0.0, "timbre": 0.0,
            "seam": float(math.exp(-0.35 * max(0.0, phase))), "bars": 0.0,
            "meter": 0, "auto_crossfade": _auto_overlap_seconds(mode, bpm, (end - s) / sample_rate),
            "stage1_candidates": 0,
            "requested_min_loop_seconds": float(min_loop_seconds),
            "used_min_loop_seconds": float(min_loop_seconds),
            "minimum_fallback_steps": 0,
            "preserve_full_input": True,
        }

    duration = mono.numel() / sample_rate
    bundle = _advanced_feature_bundle(mono, sample_rate, search_quality, resample_type=resample_type)
    requested_min_loop = max(0.5, float(min_loop_seconds))
    trial_min_loop = requested_min_loop
    candidates = []
    ctx = 0
    attempted_minimums = []

    while True:
        attempted_minimums.append(float(trial_min_loop))
        candidates, ctx = _candidate_starts_preserve_full(
            bundle, duration, float(trial_min_loop), loop_preference, search_quality
        )
        if candidates:
            break
        if trial_min_loop <= 0.5 + 1e-9:
            break
        next_trial = trial_min_loop - 1.0
        trial_min_loop = 0.5 if next_trial < 0.5 else next_trial

    if not candidates:
        attempted = ", ".join(f"{v:.2f}s" for v in attempted_minimums)
        raise RuntimeError(
            "Preserve Full Input could not find a musical return point to the physical end even after reducing "
            f"Minimum Loop Seconds from {requested_min_loop:.2f}s down to {attempted_minimums[-1]:.2f}s. "
            f"Tried: {attempted}. Try Loop Preference = Auto or Best Internal Loop."
        )

    used_min_loop = float(trial_min_loop)
    mono_np = mono.detach().float().cpu().numpy().astype(np.float32, copy=False)
    bt = bundle["beat_times"]
    tested = []
    for c in candidates:
        start_sample = int(round(float(bt[c["i"]]) * sample_rate))
        start_sample = max(0, min(len(mono_np) - 2, start_sample))
        aligned_start, seam, auto_cf = _stage2_end_seam_test(mono_np, sample_rate, start_sample, mode)
        loop_seconds = (len(mono_np) - aligned_start) / sample_rate
        if loop_seconds + 1e-9 < used_min_loop:
            continue
        # Preserve Full Input should feel like an extension, so among musically
        # credible seams we deliberately favour a later return point.  The
        # proximity term is bounded so it cannot rescue a poor structural seam.
        latest_start = max(1.0, duration - used_min_loop)
        proximity = float(np.clip((aligned_start / sample_rate) / latest_start, 0.0, 1.0))
        final_quality = 0.70 * c["structural"] + 0.22 * seam + 0.08 * proximity
        tested.append((final_quality, seam, auto_cf, aligned_start, c))

    if not tested:
        raise RuntimeError("Preserve Full Input found candidates, but none survived final source-end seam alignment.")

    tested.sort(key=lambda z: z[0], reverse=True)
    final_quality, seam, auto_cf, start_sample, best = tested[0]
    end_sample = int(len(mono_np))
    runner = tested[1][0] if len(tested) > 1 else max(0.0, final_quality - 0.08)
    margin = max(0.0, final_quality - runner)
    confidence01 = np.clip(0.90 * final_quality + 0.10 * min(1.0, margin / 0.08), 0.0, 1.0)
    confidence = float(round(100.0 * confidence01, 1))

    return {
        "start": int(start_sample),
        "end": int(end_sample),
        "bpm": float(bundle["bpm"]),
        "confidence": confidence,
        "structural": float(best["structural"]),
        "harmonic": float(best["harmonic"]),
        "rhythm": float(best["rhythm"]),
        "timbre": float(best["timbre"]),
        "seam": float(seam),
        "bars": float(best["bars"]),
        "meter": int(bundle["meter"]),
        "auto_crossfade": float(auto_cf),
        "stage1_candidates": len(candidates),
        "context_beats": int(ctx),
        "requested_min_loop_seconds": float(requested_min_loop),
        "used_min_loop_seconds": float(used_min_loop),
        "minimum_fallback_steps": int(max(0, len(attempted_minimums) - 1)),
        "preserve_full_input": True,
    }

def _find_loop_points_advanced(
    mono: torch.Tensor,
    sample_rate: int,
    mode: str,
    min_loop_seconds: float,
    search_quality: str,
    loop_preference: str,
    resample_type: str = "kaiser_fast",
):
    if mode != "Music":
        # Librosa's structural phrase finder is specifically valuable for music.
        # Ambience/Generic still use the mature V5 boundary logic.
        s, e, bpm, score, phase = _find_loop_points_basic(
            mono, sample_rate, mode, min_loop_seconds, search_quality if search_quality != "Maximum" else "Thorough"
        )
        confidence = float(np.clip(100.0 * math.exp(-0.18 * max(0.0, score)), 15.0, 85.0))
        return {
            "start": s, "end": e, "bpm": bpm, "confidence": confidence,
            "structural": 0.0, "harmonic": 0.0, "rhythm": 0.0, "timbre": 0.0,
            "seam": float(math.exp(-0.35 * max(0.0, phase))), "bars": 0.0,
            "meter": 0, "auto_crossfade": _auto_overlap_seconds(mode, bpm, (e - s) / sample_rate),
            "stage1_candidates": 0,
        }

    duration = mono.numel() / sample_rate
    bundle = _advanced_feature_bundle(mono, sample_rate, search_quality, resample_type=resample_type)

    # V10 minimum-loop fallback: keep the musical analysis unchanged, but if the
    # requested minimum produces no valid candidates, retry at 1-second lower
    # thresholds until a valid candidate exists. This is deliberately different
    # from the rejected V9 adaptive-context experiment: phrase context, beat/bar
    # rules, loop preference and seam scoring remain untouched.
    requested_min_loop = max(0.5, float(min_loop_seconds))
    trial_min_loop = requested_min_loop
    candidates = []
    ctx = 0
    attempted_minimums = []

    while True:
        attempted_minimums.append(float(trial_min_loop))
        candidates, ctx = _candidate_pairs_advanced(
            bundle, duration, float(trial_min_loop), loop_preference, search_quality
        )
        if candidates:
            break
        if trial_min_loop <= 0.5 + 1e-9:
            break
        next_trial = trial_min_loop - 1.0
        trial_min_loop = 0.5 if next_trial < 0.5 else next_trial

    if not candidates:
        attempted = ", ".join(f"{v:.2f}s" for v in attempted_minimums)
        raise RuntimeError(
            "Advanced phrase analysis could not find a repeated musical boundary even after reducing "
            f"Minimum Loop Seconds from {requested_min_loop:.2f}s down to {attempted_minimums[-1]:.2f}s. "
            f"Tried: {attempted}. Try Loop Preference = Auto or use Basic (V5) analysis."
        )

    used_min_loop = float(trial_min_loop)

    mono_np = mono.detach().float().cpu().numpy().astype(np.float32, copy=False)
    bt = bundle["beat_times"]
    tested = []
    for c in candidates:
        start_sample = int(round(float(bt[c["i"]]) * sample_rate))
        end_sample = int(round(float(bt[c["j"]]) * sample_rate))
        start_sample = max(0, min(len(mono_np) - 2, start_sample))
        end_sample = max(start_sample + 1, min(len(mono_np) - 2, end_sample))
        aligned_end, seam, auto_cf = _stage2_seam_test(mono_np, sample_rate, start_sample, end_sample, mode)
        # Structural identity is intentionally more important than raw waveform
        # similarity: the whole point of V6 is to avoid a mathematically smooth
        # splice at the wrong musical phrase.
        final_quality = 0.78 * c["structural"] + 0.22 * seam
        tested.append((final_quality, seam, auto_cf, start_sample, aligned_end, c))

    tested.sort(key=lambda z: z[0], reverse=True)
    final_quality, seam, auto_cf, start_sample, end_sample, best = tested[0]
    if end_sample <= start_sample + int(0.5 * sample_rate):
        raise RuntimeError("Advanced analysis selected an invalid loop after seam alignment.")

    # Confidence rewards an intrinsically strong match and also a clear win over
    # the next candidate. Ambiguous songs should not report fake certainty.
    runner = tested[1][0] if len(tested) > 1 else max(0.0, final_quality - 0.08)
    margin = max(0.0, final_quality - runner)
    confidence01 = np.clip(0.90 * final_quality + 0.10 * min(1.0, margin / 0.08), 0.0, 1.0)
    confidence = float(round(100.0 * confidence01, 1))

    return {
        "start": int(start_sample),
        "end": int(end_sample),
        "bpm": float(bundle["bpm"]),
        "confidence": confidence,
        "structural": float(best["structural"]),
        "harmonic": float(best["harmonic"]),
        "rhythm": float(best["rhythm"]),
        "timbre": float(best["timbre"]),
        "seam": float(seam),
        "bars": float(best["bars"]),
        "meter": int(bundle["meter"]),
        "auto_crossfade": float(auto_cf),
        "stage1_candidates": len(candidates),
        "context_beats": int(ctx),
        "requested_min_loop_seconds": float(requested_min_loop),
        "used_min_loop_seconds": float(used_min_loop),
        "minimum_fallback_steps": int(max(0, len(attempted_minimums) - 1)),
    }

def _auto_overlap_seconds(mode: str, bpm: float, segment_seconds: float) -> float:
    """Choose a transparent seam window.

    V2's ~half-beat music crossfade was often audible as a deliberate dissolve.
    V3 uses a short splice for music and relies on better boundary matching.
    """
    if mode == "Music":
        if bpm > 0.0:
            beat = 60.0 / bpm
            sec = max(0.045, min(0.120, beat * 0.16))
        else:
            sec = 0.080
    elif mode == "Ambience":
        sec = 0.90
    else:
        sec = 0.18
    return min(sec, max(0.02, segment_seconds * 0.08))


def _append_crossfade(base: torch.Tensor, addition: torch.Tensor, overlap: int, mode: str) -> torch.Tensor:
    if base.shape[-1] == 0:
        return addition
    if addition.shape[-1] == 0:
        return base
    overlap = int(max(0, min(overlap, base.shape[-1], addition.shape[-1])))
    if overlap <= 0:
        return torch.cat([base, addition], dim=-1)

    # For matched musical material, constant-sum fades avoid the +3 dB swell
    # that equal-power fades can produce when both sides are highly correlated.
    t = torch.linspace(0.0, 1.0, overlap, device=base.device, dtype=base.dtype)
    if mode == "Ambience":
        theta = t * (math.pi / 2.0)
        fade_out = torch.cos(theta)
        fade_in = torch.sin(theta)
    else:
        fade_in = 0.5 - 0.5 * torch.cos(t * math.pi)
        fade_out = 1.0 - fade_in
    fade_out = fade_out.view(1, 1, -1)
    fade_in = fade_in.view(1, 1, -1)
    mixed = base[..., -overlap:] * fade_out + addition[..., :overlap] * fade_in
    return torch.cat([base[..., :-overlap], mixed, addition[..., overlap:]], dim=-1)


def _final_fade(waveform: torch.Tensor, sample_rate: int, seconds: float) -> torch.Tensor:
    n = int(round(max(0.0, seconds) * sample_rate))
    n = min(n, waveform.shape[-1])
    if n <= 1:
        return waveform
    out = waveform.clone()
    # Cosine fade to avoid a sharp derivative change at fade start.
    t = torch.linspace(0.0, math.pi / 2.0, n, device=out.device, dtype=out.dtype)
    fade = torch.cos(t).view(1, 1, -1)
    out[..., -n:] *= fade
    return out


def _peak_protect(waveform: torch.Tensor, ceiling: float = 0.99) -> torch.Tensor:
    peak = float(waveform.detach().abs().max().cpu()) if waveform.numel() else 0.0
    if peak > ceiling and peak > 0.0:
        waveform = waveform * (ceiling / peak)
    return waveform


class URNSmartSeamlessAudioLoop:
    @classmethod
    def INPUT_TYPES(cls):
        files = _input_audio_files()
        return {
            "required": {
                "audio_file": (files, {"default": files[0]}),
                "output_length": ("FLOAT", {"default": 60.0, "min": 1.0, "max": 3600.0, "step": 0.01}),
                "mode": (["Music", "Ambience", "Generic"], {"default": "Music"}),
                "analysis_engine": (["Advanced (Librosa)", "Advanced (SoXR HQ)", "Basic (V5)"], {"default": "Advanced (Librosa)"}),
                "loop_preference": (["Auto", "4 Bars", "8 Bars", "16 Bars", "Long Phrase"], {"default": "Auto"}),
                "extension_mode": (["Best Internal Loop", "Preserve Full Input"], {"default": "Best Internal Loop"}),
                "search_quality": (["Fast", "Balanced", "Thorough", "Maximum"], {"default": "Thorough"}),
                "minimum_loop_seconds": ("FLOAT", {"default": 8.0, "min": 0.5, "max": 120.0, "step": 0.1}),
                "crossfade_mode": (["Auto (Recommended)", "Manual"], {"default": "Auto (Recommended)"}),
                "crossfade_seconds": ("FLOAT", {"default": 0.06, "min": 0.0, "max": 5.0, "step": 0.01}),
                "final_fade_seconds": ("FLOAT", {"default": 0.50, "min": 0.0, "max": 10.0, "step": 0.01}),
                "peak_protect": ("BOOLEAN", {"default": True}),
            }
        }

    # User-facing outputs: rendered audio, detected tempo, diagnostic info,
    # and the actual rendered duration in seconds. Loop boundaries/confidence
    # remain available in the status/info text but are no longer connectors.
    RETURN_TYPES = ("AUDIO", "FLOAT", "STRING", "FLOAT")
    RETURN_NAMES = ("audio", "detected_bpm", "info", "total_dur_out")
    FUNCTION = "make_loop"
    CATEGORY = "URN Audio Tools"
    DESCRIPTION = (
        "Advanced seamless audio extender with selectable internal-loop or full-input-preserving extension, Kaiser Fast or SoXR HQ resampling, Librosa phrase "
        "matching, SciPy seam alignment, confidence scoring, plus Basic V5."
    )

    @classmethod
    def IS_CHANGED(cls, audio_file, audio_input=None, **kwargs):
        if audio_input is not None:
            return float("nan")
        try:
            path = folder_paths.get_annotated_filepath(audio_file)
            h = hashlib.sha256()
            with open(path, "rb") as f:
                while True:
                    block = f.read(1024 * 1024)
                    if not block:
                        break
                    h.update(block)
            h.update(repr(sorted(kwargs.items())).encode("utf-8", errors="ignore"))
            return h.hexdigest()
        except Exception:
            return float("nan")

    @classmethod
    def VALIDATE_INPUTS(cls, audio_file, audio_input=None, **kwargs):
        if audio_input is not None:
            return True
        if not audio_file:
            return "Choose an audio file."
        if not folder_paths.exists_annotated_filepath(audio_file):
            return f"Audio file not found: {audio_file}"
        return True

    def make_loop(
        self,
        audio_file,
        output_length,
        mode,
        analysis_engine,
        loop_preference,
        extension_mode,
        search_quality,
        minimum_loop_seconds,
        crossfade_mode,
        crossfade_seconds,
        final_fade_seconds,
        peak_protect,
        audio_input=None,
    ):
        waveform, sample_rate, source_name, used_connected = _resolve_audio_source(audio_file, audio_input)

        source_samples = waveform.shape[-1]
        source_seconds = source_samples / sample_rate
        target_samples = max(1, int(round(float(output_length) * sample_rate)))

        if source_samples < 2:
            raise ValueError("Audio file is empty or too short to loop.")

        if target_samples <= source_samples:
            out = waveform[..., :target_samples].clone()
            out = _final_fade(out, sample_rate, final_fade_seconds)
            if peak_protect:
                out = _peak_protect(out)
            source_label = f"connected input ({source_name})" if used_connected else source_name
            info = (
                f"V6 | Source {source_seconds:.3f}s -> Output {target_samples / sample_rate:.3f}s. "
                f"Input: {source_label}. Target is not longer than the source, so no loop was needed."
            )
            total_dur_out = out.shape[-1] / sample_rate
            return ({"waveform": out, "sample_rate": sample_rate}, 0.0, info, float(total_dur_out))

        mono = _analysis_signal(waveform)
        advanced = analysis_engine in ("Advanced (Librosa)", "Advanced (SoXR HQ)")
        if advanced:
            resample_type = "soxr_hq" if analysis_engine == "Advanced (SoXR HQ)" else "kaiser_fast"
            finder = _find_loop_points_advanced_preserve_full if extension_mode == "Preserve Full Input" else _find_loop_points_advanced
            result = finder(
                mono,
                sample_rate,
                mode,
                float(minimum_loop_seconds),
                search_quality,
                loop_preference,
                resample_type=resample_type,
            )
            loop_start = int(result["start"])
            loop_end = int(result["end"])
            bpm = float(result["bpm"])
            confidence = float(result["confidence"])
        else:
            basic_quality = "Thorough" if search_quality == "Maximum" else search_quality
            loop_start, loop_end, bpm, score, phase_cost = _find_loop_points_basic(
                mono, sample_rate, mode, minimum_loop_seconds, basic_quality
            )
            if extension_mode == "Preserve Full Input":
                max_start = max(0, source_samples - int(max(0.5, float(minimum_loop_seconds)) * sample_rate))
                loop_start = min(int(loop_start), max_start)
                loop_end = int(source_samples)
            confidence = float(np.clip(100.0 * math.exp(-0.18 * max(0.0, score)), 10.0, 82.0))
            result = {
                "structural": 0.0,
                "harmonic": 0.0,
                "rhythm": 0.0,
                "timbre": 0.0,
                "seam": float(math.exp(-0.35 * max(0.0, phase_cost))),
                "bars": 0.0,
                "meter": 0,
                "auto_crossfade": _auto_overlap_seconds(mode, bpm, (loop_end - loop_start) / sample_rate),
                "stage1_candidates": 0,
                "requested_min_loop_seconds": float(minimum_loop_seconds),
                "used_min_loop_seconds": float(minimum_loop_seconds),
                "minimum_fallback_steps": 0,
            }

        core_segment = waveform[..., loop_start:loop_end]
        if core_segment.shape[-1] < 2:
            raise ValueError("Could not find a usable loop segment.")

        segment_seconds = core_segment.shape[-1] / sample_rate
        if crossfade_mode == "Auto (Recommended)":
            if advanced:
                chosen_crossfade = float(result.get("auto_crossfade", _auto_overlap_seconds(mode, bpm, segment_seconds)))
            else:
                chosen_crossfade = _auto_overlap_seconds(mode, bpm, segment_seconds)
        else:
            chosen_crossfade = max(0.0, float(crossfade_seconds))

        requested_overlap = int(round(chosen_crossfade * sample_rate))
        if extension_mode == "Preserve Full Input":
            # Play the complete original source once.  Every subsequent repeat
            # crossfades the physical source end back to the selected loop start.
            overlap = min(requested_overlap, max(0, core_segment.shape[-1] // 6), source_samples)
            repeat_with_handle = core_segment
            out = waveform.clone()
        else:
            max_handle = max(0, source_samples - loop_end)
            overlap = min(requested_overlap, max_handle, max(0, core_segment.shape[-1] // 6))
            if overlap > 0:
                repeat_with_handle = waveform[..., loop_start:loop_end + overlap]
                out = waveform[..., :loop_end + overlap].clone()
            else:
                repeat_with_handle = core_segment
                out = waveform[..., :loop_end].clone()

        safety = 0
        while out.shape[-1] < target_samples:
            before = out.shape[-1]
            out = _append_crossfade(out, repeat_with_handle, overlap, mode)
            safety += 1
            if out.shape[-1] <= before or safety > 10000:
                raise RuntimeError("Loop construction stalled; try Manual crossfade with a shorter value.")

        out = out[..., :target_samples].contiguous()
        out = _final_fade(out, sample_rate, final_fade_seconds)
        if peak_protect:
            out = _peak_protect(out)

        start_s = loop_start / sample_rate
        end_s = loop_end / sample_rate
        loop_s = (loop_end - loop_start) / sample_rate
        bpm_text = f"{bpm:.2f}" if bpm > 0.0 else "n/a"

        if advanced and mode == "Music":
            meter = int(result.get("meter", 0))
            meter_text = f"{meter}/4" if meter else "n/a"
            source_label = f"connected input ({source_name})" if used_connected else source_name
            info = (
                f"V13 Advanced | Engine: {analysis_engine} | Extension: {extension_mode} | Mode: {mode} | Source: {source_seconds:.3f}s | Input: {source_label} | Output: {target_samples / sample_rate:.3f}s | "
                f"Loop: {start_s:.3f}s -> {end_s:.3f}s ({loop_s:.3f}s) | "
                f"Minimum requested: {result.get('requested_min_loop_seconds', minimum_loop_seconds):.2f}s | "
                f"Minimum used: {result.get('used_min_loop_seconds', minimum_loop_seconds):.2f}s | "
                f"Approx bars: {result.get('bars', 0.0):.2f} | Grid: {meter_text} | BPM: {bpm_text} | "
                f"Crossfade: {overlap / sample_rate:.3f}s ({crossfade_mode}) | Confidence: {confidence:.1f}% | "
                f"Structural: {100.0 * result.get('structural', 0.0):.1f}% | "
                f"Harmony: {100.0 * result.get('harmonic', 0.0):.1f}% | "
                f"Rhythm: {100.0 * result.get('rhythm', 0.0):.1f}% | "
                f"Timbre: {100.0 * result.get('timbre', 0.0):.1f}% | "
                f"Rendered seam: {100.0 * result.get('seam', 0.0):.1f}%"
            )
        else:
            engine_text = "Advanced/V5 fallback" if advanced else "Basic V5"
            source_label = f"connected input ({source_name})" if used_connected else source_name
            info = (
                f"V13 {engine_text} | Engine: {analysis_engine} | Extension: {extension_mode} | Mode: {mode} | Source: {source_seconds:.3f}s | Input: {source_label} | "
                f"Output: {target_samples / sample_rate:.3f}s | Loop: {start_s:.3f}s -> {end_s:.3f}s ({loop_s:.3f}s) | "
                f"Minimum requested: {result.get('requested_min_loop_seconds', minimum_loop_seconds):.2f}s | "
                f"Minimum used: {result.get('used_min_loop_seconds', minimum_loop_seconds):.2f}s | "
                f"Crossfade: {overlap / sample_rate:.3f}s ({crossfade_mode}) | BPM: {bpm_text} | "
                f"Confidence: {confidence:.1f}%"
            )

        total_dur_out = out.shape[-1] / sample_rate
        return (
            {"waveform": out, "sample_rate": sample_rate},
            float(bpm),
            info,
            float(total_dur_out),
        )


NODE_CLASS_MAPPINGS = {
    "URNSmartSeamlessAudioLoop": URNSmartSeamlessAudioLoop,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "URNSmartSeamlessAudioLoop": "URN Smart Seamless Audio Extender",
}
