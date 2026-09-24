"""V3 ComfyUI wrappers for the two legacy URN audio node backends.

The processing functions remain in their original modules.  These wrappers let
all three nodes in URN Audio Tools register through one modern comfy_entrypoint,
while preserving the original node IDs used by existing workflows.
"""

import re

from comfy_api.latest import io

from .urn_mp3_trim_fade import URN_MP3_Trim_Fade
from .urn_smart_seamless_audio_loop import (
    URNSmartSeamlessAudioLoop,
    _input_audio_files,
)


class URNMP3TrimFadeV3(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="URN_MP3_Trim_Fade",
            display_name="URN Audio Trim Fade",
            category="URN Audio Tools",
            description=(
                "Trims/pads connected AUDIO with fades, gain, peak normalization, and Length/End Sec trim modes. "
                "Audio must come from a ComfyUI AUDIO connection such as Load Audio."
            ),
            inputs=[
                io.Audio.Input("audio_input"),
                io.Float.Input(
                    "length",
                    default=10.0,
                    min=0.0,
                    max=100000.0,
                    step=0.01,
                    tooltip="Output audio length in seconds. 0 = use all remaining audio.",
                ),
                io.Float.Input("start_sec", default=0.0, min=0.0, max=100000.0, step=0.01),
                io.Float.Input("fade_in_sec", default=3.0, min=0.0, max=3600.0, step=0.01),
                io.Float.Input("fade_out_sec", default=3.0, min=0.0, max=3600.0, step=0.01),
                io.Combo.Input("curve", options=["cosine", "linear"], default="cosine"),
                io.Boolean.Input("pad_if_short", default=True),
                io.Float.Input(
                    "gain_db",
                    default=0.0,
                    min=-60.0,
                    max=24.0,
                    step=0.1,
                    tooltip="Volume adjustment in dB, applied after optional normalization. 0 dB = unchanged.",
                ),
                io.Boolean.Input(
                    "normalize",
                    default=False,
                    tooltip="Peak-normalize the trimmed audio to 0 dBFS before applying gain_db.",
                ),
                io.Combo.Input(
                    "trim_mode",
                    options=["Length", "End Sec"],
                    default="Length",
                    tooltip="Length uses the Length duration. End Sec treats end_sec as an absolute source timestamp.",
                ),
                io.Float.Input(
                    "end_sec",
                    default=10.0,
                    min=0.0,
                    max=100000.0,
                    step=0.01,
                    tooltip="Absolute end timestamp when trim_mode is End Sec. Example: start_sec 30, end_sec 42 = 12 seconds.",
                ),
            ],
            outputs=[
                io.Audio.Output("audio"),
                io.Float.Output("audio_duration"),
            ],
        )

    @classmethod
    def execute(
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
    ) -> io.NodeOutput:
        result = URN_MP3_Trim_Fade().load_trim_fade(
            audio_input=audio_input,
            length=length,
            start_sec=start_sec,
            fade_in_sec=fade_in_sec,
            fade_out_sec=fade_out_sec,
            curve=curve,
            pad_if_short=pad_if_short,
            gain_db=gain_db,
            normalize=normalize,
            trim_mode=trim_mode,
            end_sec=end_sec,
        )
        return io.NodeOutput(*result)

    @classmethod
    def fingerprint_inputs(
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
        return URN_MP3_Trim_Fade.IS_CHANGED(
            audio_input=audio_input,
            length=length,
            start_sec=start_sec,
            fade_in_sec=fade_in_sec,
            fade_out_sec=fade_out_sec,
            curve=curve,
            pad_if_short=pad_if_short,
            gain_db=gain_db,
            normalize=normalize,
            trim_mode=trim_mode,
            end_sec=end_sec,
        )

    # Do not implement validate_inputs for audio_input here. ComfyUI validates
    # links before resolving the connected AUDIO payload, so custom validation
    # would incorrectly see None even when an AUDIO cable is connected. The
    # schema already makes audio_input required, and execute() performs the
    # final runtime sanity check.


class URNSmartSeamlessAudioLoopV3(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        files = _input_audio_files()
        return io.Schema(
            node_id="URNSmartSeamlessAudioLoop",
            display_name="URN Smart Seamless Audio Extender",
            category="URN Audio Tools",
            description=(
                "Advanced seamless audio extender with selectable internal-loop or full-input-preserving extension, Kaiser Fast or SoXR HQ resampling, Librosa phrase "
                "matching, SciPy seam alignment, confidence scoring, plus Basic V5."
            ),
            inputs=[
                io.Combo.Input("audio_file", options=files, default=files[0]),
                io.Audio.Input("audio_input", optional=True),
                io.Float.Input("output_length", default=60.0, min=1.0, max=3600.0, step=0.01),
                io.Combo.Input("mode", options=["Music", "Ambience", "Generic"], default="Music"),
                io.Combo.Input(
                    "analysis_engine",
                    options=["Advanced (Librosa)", "Advanced (SoXR HQ)", "Basic (V5)"],
                    default="Advanced (Librosa)",
                ),
                io.Combo.Input(
                    "loop_preference",
                    options=["Auto", "4 Bars", "8 Bars", "16 Bars", "Long Phrase"],
                    default="Auto",
                ),
                io.Combo.Input(
                    "extension_mode",
                    options=["Best Internal Loop", "Preserve Full Input"],
                    default="Best Internal Loop",
                ),
                io.Combo.Input(
                    "search_quality",
                    options=["Fast", "Balanced", "Thorough", "Maximum"],
                    default="Thorough",
                ),
                io.Float.Input("minimum_loop_seconds", default=8.0, min=0.5, max=120.0, step=0.1),
                io.Combo.Input(
                    "crossfade_mode",
                    options=["Auto (Recommended)", "Manual"],
                    default="Auto (Recommended)",
                ),
                io.Float.Input("crossfade_seconds", default=0.06, min=0.0, max=5.0, step=0.01),
                io.Float.Input("final_fade_seconds", default=0.50, min=0.0, max=10.0, step=0.01),
                io.Boolean.Input("peak_protect", default=True),
            ],
            outputs=[
                io.Audio.Output("audio"),
                io.Float.Output("detected_bpm"),
                io.String.Output("info"),
                io.Float.Output("total_dur_out"),
            ],
        )

    @classmethod
    def execute(
        cls,
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
    ) -> io.NodeOutput:
        result = URNSmartSeamlessAudioLoop().make_loop(
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
            audio_input,
        )

        info_text = str(result[2] or "")
        total_dur_out = float(result[3])
        requested_match = re.search(r"Minimum requested:\s*([0-9.]+)s", info_text)
        used_match = re.search(r"Minimum used:\s*([0-9.]+)s", info_text)
        loop_match = re.search(r"Loop:\s*([0-9.]+)s\s*->\s*([0-9.]+)s\s*\(([0-9.]+)s\)", info_text)
        requested_min = float(requested_match.group(1)) if requested_match else float(minimum_loop_seconds)
        used_min = float(used_match.group(1)) if used_match else float(minimum_loop_seconds)

        if loop_match:
            start_s = float(loop_match.group(1))
            end_s = float(loop_match.group(2))
            actual_loop_s = float(loop_match.group(3))
            if extension_mode == "Preserve Full Input":
                status_lines = [
                    f"Full input preserved: {end_s:.2f}s",
                    f"Loop section: {start_s:.2f}s -> {end_s:.2f}s ({actual_loop_s:.2f}s)",
                ]
                if used_min < requested_min - 1e-6:
                    status_lines.append(f"Minimum fallback: {requested_min:.2f}s -> {used_min:.2f}s")
                else:
                    status_lines.append(f"Minimum accepted: {used_min:.2f}s")
                status_lines.append(f"Total output: {total_dur_out:.2f}s")
                status = "\n".join(status_lines)
            elif used_min < requested_min - 1e-6:
                status = (
                    f"Loop found: {actual_loop_s:.2f}s\n"
                    f"Minimum fallback: {requested_min:.2f}s -> {used_min:.2f}s\n"
                    f"Total output: {total_dur_out:.2f}s"
                )
            else:
                status = (
                    f"Loop found: {actual_loop_s:.2f}s\n"
                    f"Minimum accepted: {used_min:.2f}s\n"
                    f"Total output: {total_dur_out:.2f}s"
                )
        else:
            status = f"No loop was needed. Total output: {total_dur_out:.2f}s"

        return io.NodeOutput(*result, ui={"urn_loop_status": [status]})

    @classmethod
    def fingerprint_inputs(cls, audio_file, audio_input=None, **kwargs):
        return URNSmartSeamlessAudioLoop.IS_CHANGED(audio_file, audio_input=audio_input, **kwargs)

    @classmethod
    def validate_inputs(cls, audio_file, audio_input=None, **kwargs):
        # IMPORTANT: ComfyUI performs custom validation before resolving linked
        # AUDIO payloads.  At that stage ``audio_input`` can appear as None even
        # when the socket is connected, so validating the fallback ``audio_file``
        # here can incorrectly reject a perfectly valid connected-audio workflow
        # because an old picker value no longer exists.
        #
        # Runtime source resolution in URNSmartSeamlessAudioLoop.make_loop() gives
        # connected AUDIO absolute priority and performs the appropriate fallback
        # file check only when no AUDIO payload is supplied.
        return True
