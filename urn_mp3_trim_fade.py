import math

import torch
import torch.nn.functional as F


def _audio_dict_to_waveform(audio_input):
    # ComfyUI's normal Load Audio resolves to a plain dict, while VHS Load Video
    # returns a lazy mapping object that exposes the same waveform/sample_rate
    # keys only when they are accessed.  Do not require an actual dict here.
    if audio_input is None:
        raise ValueError("A valid AUDIO input must be connected.")

    try:
        waveform = audio_input["waveform"]
        sample_rate = int(audio_input["sample_rate"])
    except (TypeError, KeyError, AttributeError, IndexError):
        raise ValueError("A valid AUDIO input must be connected.") from None

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


def trim_or_pad(
    waveform: torch.Tensor,
    sample_rate: int,
    start_sec: float,
    requested_length: float,
    pad_if_short: bool,
) -> torch.Tensor:
    total = waveform.shape[-1]
    start = int(round(float(start_sec) * sample_rate))
    start = max(0, min(start, total))

    if requested_length is None or float(requested_length) <= 0:
        end = total
        desired = max(0, total - start)
    else:
        desired = max(0, int(round(float(requested_length) * sample_rate)))
        end = min(total, start + desired)

    out = waveform[..., start:end]

    if desired > 0 and pad_if_short and out.shape[-1] < desired:
        out = F.pad(out, (0, desired - out.shape[-1]))

    return out


def make_fade_curve(length: int, curve: str, direction: str, device, dtype):
    if length <= 0:
        return None

    if length == 1:
        vals = torch.tensor([1.0 if direction == "out" else 0.0], device=device, dtype=dtype)
    else:
        x = torch.linspace(0.0, 1.0, steps=length, device=device, dtype=dtype)
        if curve == "cosine":
            fade_in = 0.5 - 0.5 * torch.cos(math.pi * x)
        else:
            fade_in = x
        vals = fade_in if direction == "in" else torch.flip(fade_in, dims=[0])

    return vals.view(1, 1, -1)


def apply_fades(
    waveform: torch.Tensor,
    sample_rate: int,
    fade_in_sec: float,
    fade_out_sec: float,
    curve: str,
) -> torch.Tensor:
    n = waveform.shape[-1]
    if n <= 0:
        return waveform

    out = waveform.clone()
    fade_in = max(0, int(round(float(fade_in_sec) * sample_rate)))
    fade_out = max(0, int(round(float(fade_out_sec) * sample_rate)))
    fade_in = min(fade_in, n)
    fade_out = min(fade_out, n)

    if fade_in > 0:
        ramp = make_fade_curve(fade_in, curve, "in", out.device, out.dtype)
        out[..., :fade_in] = out[..., :fade_in] * ramp

    if fade_out > 0:
        ramp = make_fade_curve(fade_out, curve, "out", out.device, out.dtype)
        out[..., -fade_out:] = out[..., -fade_out:] * ramp

    return out.clamp(-1.0, 1.0)


def peak_normalize(waveform: torch.Tensor) -> torch.Tensor:
    """Peak-normalize the audible result to 0 dBFS without changing silence."""
    if waveform.numel() == 0:
        return waveform
    peak = waveform.abs().amax()
    if not torch.isfinite(peak) or float(peak) <= 0.0:
        return waveform
    return waveform / peak


def apply_gain_db(waveform: torch.Tensor, gain_db: float) -> torch.Tensor:
    """Apply scalar dB gain and protect the AUDIO payload from values outside [-1, 1]."""
    gain = math.pow(10.0, float(gain_db) / 20.0)
    return (waveform * gain).clamp(-1.0, 1.0)


class URN_MP3_Trim_Fade:
    """Backend for URN Audio Trim Fade.

    Audio is connector-only.  File selection/loading is intentionally handled by
    ComfyUI's normal Load Audio node so there is a single audio-loading path.
    """

    def load_trim_fade(
        self,
        audio_input,
        length=10.0,
        start_sec=0.0,
        fade_in_sec=3.0,
        fade_out_sec=3.0,
        curve="cosine",
        pad_if_short=True,
        gain_db=0.0,
        normalize=False,
        trim_mode="Length",
        end_sec=10.0,
    ):
        if audio_input is None:
            raise ValueError("Connect an AUDIO output (for example, ComfyUI Load Audio) to audio_input.")

        start_sec = max(0.0, float(start_sec))
        waveform, sample_rate = _audio_dict_to_waveform(audio_input)

        if str(trim_mode) == "End Sec":
            # end_sec is an absolute position in the source timeline.
            # Example: start=30, end=42 -> 12 seconds of audio.
            requested_end = max(0.0, float(end_sec))
            effective_length = max(0.0, requested_end - start_sec)
            total = waveform.shape[-1]
            start_sample = max(0, min(int(round(start_sec * sample_rate)), total))
            end_sample = max(start_sample, min(int(round(requested_end * sample_rate)), total))
            waveform = waveform[..., start_sample:end_sample]
        else:
            # Preserve existing dynamic-duration behaviour in Length mode,
            # including length=0 meaning "use all remaining audio".
            effective_length = float(length)
            waveform = trim_or_pad(waveform, sample_rate, start_sec, effective_length, False)

        # Process the real trimmed audio BEFORE adding requested silence padding.
        # This keeps fades/normalization/gain on audible material rather than padding.
        waveform = apply_fades(waveform, sample_rate, fade_in_sec, fade_out_sec, curve)

        if normalize:
            waveform = peak_normalize(waveform)

        if float(gain_db) != 0.0:
            waveform = apply_gain_db(waveform, gain_db)

        if pad_if_short and effective_length > 0.0:
            desired_samples = max(0, int(round(effective_length * sample_rate)))
            if waveform.shape[-1] < desired_samples:
                waveform = F.pad(waveform, (0, desired_samples - waveform.shape[-1]))

        duration = waveform.shape[-1] / float(sample_rate) if sample_rate else 0.0
        return ({"waveform": waveform, "sample_rate": sample_rate}, duration)

    @classmethod
    def IS_CHANGED(
        cls,
        audio_input,
        length=10.0,
        start_sec=0.0,
        fade_in_sec=3.0,
        fade_out_sec=3.0,
        curve="cosine",
        pad_if_short=True,
        gain_db=0.0,
        normalize=False,
        trim_mode="Length",
        end_sec=10.0,
    ):
        # The connected AUDIO input participates in ComfyUI's normal dependency
        # graph, so no file hash is required here.
        return float("NaN")

    @classmethod
    def VALIDATE_INPUTS(cls, audio_input=None, **kwargs):
        # Connected AUDIO values are not resolved yet during prompt validation
        # on current ComfyUI builds. Returning an error here would reject valid
        # linked inputs. The schema requires the AUDIO link, and the runtime
        # method still checks the resolved payload before processing.
        return True


NODE_CLASS_MAPPINGS = {
    "URN_MP3_Trim_Fade": URN_MP3_Trim_Fade,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "URN_MP3_Trim_Fade": "URN Audio Trim Fade",
}
