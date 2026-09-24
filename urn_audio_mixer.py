import hashlib
import json
import math
import os
from typing import Any, Dict, List, Tuple

import av
import torch
import torch.nn.functional as F

import folder_paths
from comfy_api.latest import io


NODE_TAG = "[URN Audio Mixer]"


def _log(message: str):
    print(f"{NODE_TAG} {message}")


def _f32_pcm(wav_tensor: torch.Tensor) -> torch.Tensor:
    if wav_tensor.dtype.is_floating_point:
        return wav_tensor.float()
    if wav_tensor.dtype == torch.int16:
        return wav_tensor.float() / (2 ** 15)
    if wav_tensor.dtype == torch.int32:
        return wav_tensor.float() / (2 ** 31)
    if wav_tensor.dtype == torch.uint8:
        return (wav_tensor.float() - 128.0) / 128.0
    return wav_tensor.float()


def _decode_audio(filepath: str) -> Tuple[torch.Tensor, int]:
    """Decode an audio file to [channels, samples] float32 on CPU."""
    with av.open(filepath) as container:
        audio_streams = container.streams.audio
        if not audio_streams:
            raise ValueError(f"No audio stream found in {os.path.basename(filepath)!r}.")

        stream = audio_streams[0]
        sample_rate = int(stream.codec_context.sample_rate or stream.rate or 0)
        if sample_rate <= 0:
            raise ValueError(f"Could not determine sample rate for {os.path.basename(filepath)!r}.")

        n_channels = int(stream.channels or 1)
        frames: List[torch.Tensor] = []
        for frame in container.decode(stream):
            arr = frame.to_ndarray()
            buf = torch.from_numpy(arr)
            if buf.ndim == 1:
                buf = buf.unsqueeze(0)
            if buf.shape[0] != n_channels and buf.ndim == 2 and buf.shape[1] == n_channels:
                buf = buf.t()
            elif buf.shape[0] != n_channels:
                buf = buf.reshape(-1, n_channels).t()
            frames.append(_f32_pcm(buf))

        if not frames:
            raise ValueError(f"No audio samples could be decoded from {os.path.basename(filepath)!r}.")

        return torch.cat(frames, dim=1).contiguous().float(), sample_rate


def _safe_input_path(clip: Dict[str, Any]) -> str:
    name = str(clip.get("name") or clip.get("serverName") or "").replace("\\", "/").strip()
    subfolder = str(clip.get("subfolder") or "").replace("\\", "/").strip("/")
    file_type = str(clip.get("type") or "input").lower()
    if file_type not in {"input", "temp", "output"}:
        file_type = "input"
    if not name:
        raise ValueError("Mixer clip is missing its uploaded filename.")

    roots = {
        "input": folder_paths.get_input_directory(),
        "temp": folder_paths.get_temp_directory(),
        "output": folder_paths.get_output_directory(),
    }
    root = os.path.abspath(roots[file_type])
    relative = os.path.normpath(os.path.join(subfolder, name)) if subfolder else os.path.normpath(name)
    full = os.path.abspath(os.path.join(root, relative))
    if os.path.commonpath([root, full]) != root:
        raise ValueError("Invalid mixer audio path.")
    if not os.path.isfile(full):
        display = clip.get("displayName") or name
        raise FileNotFoundError(f"Mixer audio file not found: {display}")
    return full


def _fade_curve(length: int, fade_in: bool, device, dtype) -> torch.Tensor:
    if length <= 0:
        return torch.empty(0, device=device, dtype=dtype)
    if length == 1:
        vals = torch.tensor([0.0 if fade_in else 1.0], device=device, dtype=dtype)
    else:
        x = torch.linspace(0.0, 1.0, steps=length, device=device, dtype=dtype)
        vals = 0.5 - 0.5 * torch.cos(math.pi * x)
        if not fade_in:
            vals = torch.flip(vals, dims=[0])
    return vals


def _process_clip(waveform: torch.Tensor, sample_rate: int, clip: Dict[str, Any]) -> torch.Tensor:
    total_samples = waveform.shape[-1]
    total_seconds = total_samples / float(sample_rate) if sample_rate else 0.0

    start = max(0.0, min(float(clip.get("start", 0.0) or 0.0), total_seconds))
    raw_end = clip.get("end", total_seconds)
    try:
        end = float(raw_end)
    except (TypeError, ValueError):
        end = total_seconds
    if end <= 0:
        end = total_seconds
    end = max(start, min(end, total_seconds))

    a = max(0, min(total_samples, int(round(start * sample_rate))))
    z = max(a, min(total_samples, int(round(end * sample_rate))))
    out = waveform[:, a:z].clone()
    n = out.shape[-1]
    if n <= 0:
        return out

    duration = n / float(sample_rate)
    fade_in_sec = max(0.0, min(float(clip.get("fadeIn", 0.0) or 0.0), duration))
    fade_out_sec = max(0.0, min(float(clip.get("fadeOut", 0.0) or 0.0), duration))
    fade_in_n = min(n, int(round(fade_in_sec * sample_rate)))
    fade_out_n = min(n, int(round(fade_out_sec * sample_rate)))

    if fade_in_n > 0:
        out[:, :fade_in_n] *= _fade_curve(fade_in_n, True, out.device, out.dtype).view(1, -1)
    if fade_out_n > 0:
        out[:, -fade_out_n:] *= _fade_curve(fade_out_n, False, out.device, out.dtype).view(1, -1)
    return out


def _resample_linear(waveform: torch.Tensor, source_rate: int, target_rate: int) -> torch.Tensor:
    if source_rate == target_rate or waveform.shape[-1] == 0:
        return waveform
    new_len = max(1, int(round(waveform.shape[-1] * target_rate / float(source_rate))))
    return F.interpolate(waveform.unsqueeze(0), size=new_len, mode="linear", align_corners=False).squeeze(0)


def _match_channels(waveform: torch.Tensor, channels: int) -> torch.Tensor:
    current = waveform.shape[0]
    if current == channels:
        return waveform
    if current == 1:
        return waveform.repeat(channels, 1)
    if current > channels:
        return waveform[:channels]
    return torch.cat([waveform, waveform[-1:].repeat(channels - current, 1)], dim=0)


def _legacy_clip_to_track(clip: Dict[str, Any], index: int) -> Dict[str, Any]:
    migrated = dict(clip)
    volume = migrated.pop("volume", 100.0)
    return {
        "id": f"legacy_track_{index}",
        "name": f"Track {index}",
        "volume": volume,
        "clips": [migrated],
    }


def _parse_state(mix_state_json: str) -> List[Dict[str, Any]]:
    try:
        state = json.loads(mix_state_json or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError(f"Mixer state is invalid JSON: {exc}") from None

    if isinstance(state, dict):
        items = state.get("tracks", [])
    elif isinstance(state, list):
        items = state
    else:
        raise ValueError("Mixer state must contain tracks.")

    if not isinstance(items, list):
        raise ValueError("Mixer state must contain a list of tracks.")

    # V9.11 and earlier stored one file directly as one top-level 'track'.
    if items and all(isinstance(x, dict) and not isinstance(x.get("clips"), list) for x in items):
        return [_legacy_clip_to_track(x, i + 1) for i, x in enumerate(items)]

    out = []
    for i, track in enumerate(items, start=1):
        if not isinstance(track, dict):
            continue
        clips = track.get("clips", [])
        if not isinstance(clips, list):
            clips = []
        t = dict(track)
        t["clips"] = [c for c in clips if isinstance(c, dict)]
        out.append(t)
    return out


class URNAudioMixer(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="URNAudioMixer",
            display_name="URN Audio Mixer",
            category="URN Audio Tools",
            description=(
                "A simple multi-track audio mixer. Each track can contain multiple movable audio clips, "
                "with track-level volume plus per-clip trim and fade controls."
            ),
            inputs=[
                io.String.Input("mix_state_json", default="[]"),
                io.Int.Input("pad_start_sec", default=0, min=0, max=86400, step=1),
                io.Int.Input("pad_end_sec", default=0, min=0, max=86400, step=1),
            ],
            outputs=[io.Audio.Output("audio")],
        )

    @classmethod
    def execute(cls, mix_state_json="[]", pad_start_sec=0, pad_end_sec=0) -> io.NodeOutput:
        tracks = _parse_state(mix_state_json)
        clips_to_decode = []
        for ti, track in enumerate(tracks, start=1):
            for ci, clip in enumerate(track.get("clips", []), start=1):
                clips_to_decode.append((track, clip, ti, ci))
        if not clips_to_decode:
            raise ValueError("URN Audio Mixer: add at least one audio file before running.")

        decoded = []
        earliest_used_start = math.inf
        latest_used_end = 0.0
        for track, clip, ti, ci in clips_to_decode:
            path = _safe_input_path(clip)
            waveform, sample_rate = _decode_audio(path)
            source_duration = waveform.shape[-1] / float(sample_rate) if sample_rate else 0.0
            position = max(0.0, float(clip.get("position", 0.0) or 0.0))

            # The virtual/master timeline ends at the furthest USED clip end,
            # not the furthest original source-file end. Respect the clip's
            # trim end so the exported AUDIO contains no unnecessary silent
            # tail after the final clip across all tracks.
            trim_start = max(0.0, min(float(clip.get("start", 0.0) or 0.0), source_duration))
            raw_end = clip.get("end", source_duration)
            try:
                trim_end = float(raw_end)
            except (TypeError, ValueError):
                trim_end = source_duration
            if trim_end <= 0:
                trim_end = source_duration
            trim_end = max(trim_start, min(trim_end, source_duration))
            if trim_end > trim_start:
                audible_start = position + trim_start
                audible_end = position + trim_end
                earliest_used_start = min(earliest_used_start, audible_start)
                latest_used_end = max(latest_used_end, audible_end)

            decoded.append((track, clip, waveform, sample_rate, source_duration, ti, ci))

        if not math.isfinite(earliest_used_start) or latest_used_end <= earliest_used_start:
            raise ValueError("URN Audio Mixer: no usable audio samples were decoded.")

        # Export only the USED span: start at the earliest trimmed/audible clip
        # across all tracks and end at the furthest trimmed clip end. Optional
        # non-negative integer padding adds silence before/after that used span.
        pad_start = max(0, int(pad_start_sec or 0))
        pad_end = max(0, int(pad_end_sec or 0))
        used_duration = max(0.0, latest_used_end - earliest_used_start)
        export_duration = float(pad_start) + used_duration + float(pad_end)

        target_rate = max(rate for _, _, _, rate, _, _, _ in decoded)
        target_channels = max(wave.shape[0] for _, _, wave, _, _, _, _ in decoded)
        master_samples = max(1, int(round(export_duration * target_rate)))
        mix = torch.zeros((target_channels, master_samples), dtype=torch.float32)

        for track, clip, waveform, rate, source_duration, ti, ci in decoded:
            processed = _process_clip(waveform, rate, clip)
            if processed.shape[-1] <= 0:
                _log(f"Track {ti} clip {ci} produced no samples after trimming; skipping.")
                continue
            processed = _resample_linear(processed, rate, target_rate)
            processed = _match_channels(processed, target_channels)

            position = max(0.0, float(clip.get("position", 0.0) or 0.0))
            trim_start = max(0.0, min(float(clip.get("start", 0.0) or 0.0), source_duration))
            audible_start = position + trim_start
            # Rebase the project timeline so export time 0 is the earliest
            # used clip start, then prepend any requested start padding.
            export_start = float(pad_start) + (audible_start - earliest_used_start)
            offset_samples = max(0, min(master_samples, int(round(export_start * target_rate))))

            track_volume = max(0.0, min(100.0, float(track.get("volume", 100.0) or 0.0))) / 100.0
            if track_volume != 1.0:
                processed = processed * track_volume

            if offset_samples >= master_samples:
                continue
            usable = min(processed.shape[-1], master_samples - offset_samples)
            if usable > 0:
                mix[:, offset_samples:offset_samples + usable] += processed[:, :usable]

        peak = mix.abs().amax() if mix.numel() else torch.tensor(0.0)
        if torch.isfinite(peak) and float(peak) > 1.0:
            mix /= peak

        return io.NodeOutput({"waveform": mix.unsqueeze(0).contiguous(), "sample_rate": int(target_rate)})

    @classmethod
    def fingerprint_inputs(cls, mix_state_json="[]", pad_start_sec=0, pad_end_sec=0):
        h = hashlib.sha256((mix_state_json or "[]").encode("utf-8", "replace"))
        h.update(f"|pad_start={max(0, int(pad_start_sec or 0))}|pad_end={max(0, int(pad_end_sec or 0))}".encode())
        try:
            for track in _parse_state(mix_state_json):
                for clip in track.get("clips", []):
                    try:
                        path = _safe_input_path(clip)
                        st = os.stat(path)
                        h.update(path.encode("utf-8", "replace"))
                        h.update(str(st.st_size).encode())
                        h.update(str(st.st_mtime_ns).encode())
                    except Exception:
                        continue
        except Exception:
            pass
        return h.hexdigest()
