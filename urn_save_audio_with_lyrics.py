import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import soundfile as sf
import torch
import folder_paths
from mutagen.flac import FLAC
from mutagen.id3 import ID3, ID3NoHeaderError, USLT, TPE1, TIT2, TALB

from comfy_api.latest import io


_AUDIO_EXTENSIONS = {
    ".mp3", ".flac", ".wav", ".wave", ".ogg", ".oga", ".opus",
    ".m4a", ".aac", ".wma", ".aiff", ".aif", ".alac",
}


def _audio_to_tensor(audio_input):
    if audio_input is None:
        raise ValueError("Connect an AUDIO input.")

    try:
        waveform = audio_input["waveform"]
        sample_rate = int(audio_input["sample_rate"])
    except (TypeError, KeyError, AttributeError, IndexError):
        raise ValueError("The connected value is not a valid ComfyUI AUDIO object.") from None

    if not isinstance(waveform, torch.Tensor):
        waveform = torch.as_tensor(waveform)

    waveform = waveform.detach().float().cpu()

    if waveform.ndim == 1:
        waveform = waveform.unsqueeze(0).unsqueeze(0)
    elif waveform.ndim == 2:
        waveform = waveform.unsqueeze(0)
    elif waveform.ndim != 3:
        raise ValueError(f"Unsupported AUDIO waveform shape: {tuple(waveform.shape)}")

    if sample_rate <= 0:
        raise ValueError("The connected audio has an invalid sample rate.")

    return waveform.contiguous(), sample_rate


def _user_filename_stem(filename: str) -> str:
    value = str(filename or "").strip().strip('"')
    if not value:
        raise ValueError("Enter a Filename for the saved audio.")

    # Filename is intentionally user-controlled. Save Location controls folders,
    # so path components are reduced to a safe basename here.
    value = value.replace("\\", "/").rsplit("/", 1)[-1].strip()
    stem = Path(value).stem.strip() if Path(value).suffix.lower() in _AUDIO_EXTENSIONS else value
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem)
    stem = stem.rstrip(" .")

    if not stem:
        raise ValueError("Filename does not contain a usable file name.")
    return stem


def _samples_for_soundfile(batch_item: torch.Tensor):
    # Comfy AUDIO is channels x samples for each batch item.
    x = batch_item.detach().float().cpu().clamp(-1.0, 1.0)
    if x.ndim != 2:
        raise ValueError(f"Unexpected batch audio shape: {tuple(x.shape)}")
    return x.transpose(0, 1).contiguous().numpy()


def _write_flac(path: str, batch_item: torch.Tensor, sample_rate: int):
    samples = _samples_for_soundfile(batch_item)
    sf.write(path, samples, sample_rate, format="FLAC", subtype="PCM_24")


def _write_mp3(path: str, batch_item: torch.Tensor, sample_rate: int):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg was not found in PATH. MP3 export requires FFmpeg.")

    samples = _samples_for_soundfile(batch_item)
    temp_root = folder_paths.get_temp_directory()
    os.makedirs(temp_root, exist_ok=True)

    fd, wav_path = tempfile.mkstemp(prefix="urn_save_audio_", suffix=".wav", dir=temp_root)
    os.close(fd)
    try:
        sf.write(wav_path, samples, sample_rate, format="WAV", subtype="PCM_24")
        cmd = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel", "error",
            "-i", wav_path,
            "-vn",
            "-codec:a", "libmp3lame",
            "-b:a", "320k",
            path,
        ]
        completed = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if completed.returncode != 0 or not os.path.isfile(path):
            detail = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(f"FFmpeg MP3 export failed: {detail or 'unknown error'}")
    finally:
        try:
            os.remove(wav_path)
        except OSError:
            pass



def _resolve_save_directory(save_location: str) -> str:
    """
    Absolute paths are used directly.
    Relative paths are created underneath ComfyUI/output.
    Blank values use ComfyUI/output.
    """
    raw = str(save_location or "").strip().strip('"')
    if not raw:
        target = folder_paths.get_output_directory()
    else:
        expanded = os.path.expandvars(os.path.expanduser(raw))
        if os.path.isabs(expanded):
            target = expanded
        else:
            target = os.path.join(folder_paths.get_output_directory(), expanded)

    target = os.path.abspath(target)
    os.makedirs(target, exist_ok=True)

    if not os.path.isdir(target):
        raise ValueError(f"Save location is not a directory: {target}")

    return target


def _write_audio_metadata(path: str, fmt: str, lyrics: str, artist: str = "", title: str = "", album: str = ""):
    lyrics = str(lyrics or "").replace("\r\n", "\n").replace("\r", "\n")
    artist = str(artist or "").strip()
    title = str(title or "").strip()
    album = str(album or "").strip()

    if fmt == "mp3":
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()

        tags.delall("USLT")
        tags.delall("TPE1")
        tags.delall("TIT2")
        tags.delall("TALB")
        tags.add(
            USLT(
                encoding=1,      # UTF-16 for broad ID3v2.3 compatibility
                lang="eng",
                desc="",
                text=lyrics,
            )
        )
        if artist:
            tags.add(TPE1(encoding=1, text=[artist]))
        if title:
            tags.add(TIT2(encoding=1, text=[title]))
        if album:
            tags.add(TALB(encoding=1, text=[album]))
        tags.save(path, v2_version=3)
        return "ID3 USLT/TPE1/TIT2/TALB"

    audio = FLAC(path)
    audio["LYRICS"] = [lyrics]
    if artist:
        audio["ARTIST"] = [artist]
    if title:
        audio["TITLE"] = [title]
    if album:
        audio["ALBUM"] = [album]
    audio.save()
    return "Vorbis Comment LYRICS/ARTIST/TITLE/ALBUM"


class URNSaveAudioWithLyrics(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="URNSaveAudioWithLyrics",
            display_name="URN Save Audio with Lyrics",
            category="URN Audio Tools",
            description=(
                "Saves connected AUDIO as FLAC or MP3 using a user-selected filename. "
                "Choose a relative folder under ComfyUI/output or an absolute save directory. "
                "Lyrics, Artist, Title and Album are embedded in the relevant MP3/FLAC metadata fields."
            ),
            is_output_node=True,
            not_idempotent=True,
            inputs=[
                io.Audio.Input("audio"),
                io.String.Input(
                    "lyrics",
                    display_name="Lyrics",
                    force_input=True,
                    tooltip="Connect the lyrics text to embed in the saved audio file.",
                ),
                io.String.Input(
                    "artist",
                    display_name="Artist",
                    default="",
                    optional=True,
                    force_input=True,
                    tooltip="Optional artist text to embed in the audio metadata.",
                ),
                io.String.Input(
                    "title",
                    display_name="Title",
                    default="",
                    optional=True,
                    force_input=True,
                    tooltip="Optional title text to embed in the audio metadata.",
                ),
                io.String.Input(
                    "album",
                    display_name="Album",
                    default="",
                    optional=True,
                    force_input=True,
                    tooltip="Optional album text to embed in the audio metadata.",
                ),
                io.Combo.Input(
                    "format",
                    options=["flac", "mp3"],
                    default="flac",
                ),
                io.String.Input(
                    "save_location",
                    display_name="Save Location",
                    default="audio",
                    tooltip=(
                        "Folder to save into. Relative paths are created under ComfyUI/output "
                        "(for example: audio or music/finished). Absolute paths such as "
                        "D:\\Music\\Exports are also supported."
                    ),
                ),
                io.String.Input(
                    "filename",
                    display_name="Filename",
                    default="audio",
                    tooltip="User-controlled output filename. The selected FLAC/MP3 extension is added automatically.",
                ),
                io.String.Input("source_filename_hint", default=""),
            ],
            outputs=[
                io.Audio.Output("audio"),
                io.String.Output("saved_path"),
            ],
        )

    @classmethod
    def fingerprint_inputs(cls, audio=None, lyrics="", artist="", title="", album="", format="flac", save_location="audio", filename="audio", source_filename_hint="", **kwargs):
        # Save nodes should run on every queued workflow rather than being served from cache.
        return float("nan")

    @classmethod
    def execute(cls, audio=None, lyrics="", artist="", title="", album="", format="flac", save_location="audio", filename="audio", source_filename_hint="") -> io.NodeOutput:
        waveform, sample_rate = _audio_to_tensor(audio)
        fmt = str(format or "flac").strip().lower()
        if fmt not in {"flac", "mp3"}:
            raise ValueError("Format must be FLAC or MP3.")

        stem = _user_filename_stem(filename)
        output_dir = _resolve_save_directory(save_location)

        saved_paths = []
        batch_count = int(waveform.shape[0])

        for index in range(batch_count):
            name = stem if batch_count == 1 else f"{stem}_{index + 1:03d}"
            path = os.path.join(output_dir, f"{name}.{fmt}")

            if fmt == "flac":
                _write_flac(path, waveform[index], sample_rate)
            else:
                _write_mp3(path, waveform[index], sample_rate)

            field = _write_audio_metadata(path, fmt, lyrics, artist, title, album)
            saved_paths.append(path)
            print(f"[URN Save Audio with Lyrics] Saved '{path}' with lyrics/identity metadata in {field}.")

        return io.NodeOutput(audio, "\n".join(saved_paths))
