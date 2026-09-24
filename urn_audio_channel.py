import math

import torch

from comfy_api.latest import io


NODE_TAG = "[URN Audio Channel]"


def _audio_to_waveform(audio_input):
    """Return ComfyUI AUDIO as float32 [batch, channels, samples]."""
    if audio_input is None:
        raise ValueError("URN Audio Channel: connect a valid AUDIO input.")

    try:
        waveform = audio_input["waveform"]
        sample_rate = int(audio_input["sample_rate"])
    except (TypeError, KeyError, AttributeError, IndexError, ValueError):
        raise ValueError("URN Audio Channel: connect a valid AUDIO input.") from None

    if not isinstance(waveform, torch.Tensor):
        waveform = torch.as_tensor(waveform)
    waveform = waveform.detach().float()

    if waveform.ndim == 1:
        waveform = waveform.unsqueeze(0).unsqueeze(0)
    elif waveform.ndim == 2:
        waveform = waveform.unsqueeze(0)
    elif waveform.ndim != 3:
        raise ValueError(f"URN Audio Channel: unsupported AUDIO waveform shape {tuple(waveform.shape)}")

    if sample_rate <= 0:
        raise ValueError("URN Audio Channel: invalid sample rate.")

    return waveform.contiguous(), sample_rate


def _to_stereo(waveform: torch.Tensor) -> torch.Tensor:
    channels = int(waveform.shape[1])
    if channels <= 0:
        raise ValueError("URN Audio Channel: input has no audio channels.")
    if channels == 1:
        return waveform.repeat(1, 2, 1)
    # This node deliberately operates on a conventional L/R pair. For inputs
    # with more than two channels, use the first stereo pair.
    return waveform[:, :2, :].clone()


def _pan_gains(position: torch.Tensor):
    """Stereo balance gains for pan position(s) in [-1, 1].

    Centre leaves both channels unchanged. Moving toward one side smoothly
    attenuates the opposite channel to zero using a cosine law.
    """
    p = position.clamp(-1.0, 1.0)
    half_pi = math.pi * 0.5
    left = torch.where(p > 0, torch.cos(p * half_pi), torch.ones_like(p))
    right = torch.where(p < 0, torch.cos((-p) * half_pi), torch.ones_like(p))
    return left, right


def _auto_position(length: int, mode: str, curve: str, device, dtype):
    if length <= 0:
        return torch.empty(0, device=device, dtype=dtype)
    if length == 1:
        progress = torch.zeros(1, device=device, dtype=dtype)
    else:
        progress = torch.linspace(0.0, 1.0, steps=length, device=device, dtype=dtype)

    if str(curve) == "Cosine":
        progress = 0.5 - 0.5 * torch.cos(math.pi * progress)

    if str(mode) == "Right → Left":
        return 1.0 - 2.0 * progress
    return -1.0 + 2.0 * progress


def process_audio_channel(
    waveform: torch.Tensor,
    left_volume: float = 100.0,
    right_volume: float = 100.0,
    swap_lr: bool = False,
    pan: float = 0.0,
    auto_pan: str = "Off",
    pan_curve: str = "Cosine",
    prevent_clipping: bool = True,
) -> torch.Tensor:
    out = _to_stereo(waveform)

    if bool(swap_lr):
        out = out[:, [1, 0], :]

    left_scalar = max(0.0, float(left_volume)) / 100.0
    right_scalar = max(0.0, float(right_volume)) / 100.0
    out[:, 0, :] *= left_scalar
    out[:, 1, :] *= right_scalar

    mode = str(auto_pan)
    if mode in {"Left → Right", "Right → Left"}:
        positions = _auto_position(out.shape[-1], mode, str(pan_curve), out.device, out.dtype)
        lg, rg = _pan_gains(positions)
        out[:, 0, :] *= lg.view(1, -1)
        out[:, 1, :] *= rg.view(1, -1)
    else:
        p = torch.tensor(float(pan) / 100.0, device=out.device, dtype=out.dtype)
        lg, rg = _pan_gains(p)
        out[:, 0, :] *= lg
        out[:, 1, :] *= rg

    if bool(prevent_clipping) and out.numel():
        peak = out.abs().amax()
        if torch.isfinite(peak) and float(peak) > 1.0:
            out = out / peak

    return out.contiguous()


class URNAudioChannel(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="URNAudioChannel",
            display_name="URN Audio Channel",
            category="URN Audio Tools",
            description=(
                "Visual stereo channel utility with independent L/R level, channel swap, static pan, "
                "and whole-clip Left→Right / Right→Left auto-pan. Mono inputs are converted to stereo."
            ),
            inputs=[
                io.Audio.Input("audio_input"),
                io.Float.Input("left_volume", default=100.0, min=0.0, max=200.0, step=1.0),
                io.Float.Input("right_volume", default=100.0, min=0.0, max=200.0, step=1.0),
                io.Boolean.Input("swap_lr", default=False),
                io.Float.Input("pan", default=0.0, min=-100.0, max=100.0, step=1.0),
                io.Combo.Input(
                    "auto_pan",
                    options=["Off", "Left → Right", "Right → Left"],
                    default="Off",
                ),
                io.Combo.Input("pan_curve", options=["Cosine", "Linear"], default="Cosine"),
                io.Boolean.Input("prevent_clipping", default=True),
            ],
            outputs=[io.Audio.Output("audio")],
        )

    @classmethod
    def execute(
        cls,
        audio_input,
        left_volume=100.0,
        right_volume=100.0,
        swap_lr=False,
        pan=0.0,
        auto_pan="Off",
        pan_curve="Cosine",
        prevent_clipping=True,
    ) -> io.NodeOutput:
        waveform, sample_rate = _audio_to_waveform(audio_input)
        out = process_audio_channel(
            waveform,
            left_volume=left_volume,
            right_volume=right_volume,
            swap_lr=swap_lr,
            pan=pan,
            auto_pan=auto_pan,
            pan_curve=pan_curve,
            prevent_clipping=prevent_clipping,
        )
        return io.NodeOutput({"waveform": out, "sample_rate": sample_rate})
