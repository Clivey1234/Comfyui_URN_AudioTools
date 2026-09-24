import json
import os

import folder_paths
from comfy_api.latest import io

from mutagen import File as MutagenFile
from mutagen.flac import FLAC
from mutagen.id3 import ID3, ID3NoHeaderError


NODE_TAG = "[URN Load Audio]"
_AUDIO_EXTENSIONS = {
    ".mp3", ".flac", ".wav", ".wave", ".ogg", ".oga", ".opus",
    ".m4a", ".aac", ".wma", ".aiff", ".aif", ".alac",
}


def _input_audio_files():
    root = folder_paths.get_input_directory()
    os.makedirs(root, exist_ok=True)
    try:
        names = folder_paths.get_filename_list("input")
    except Exception:
        names = os.listdir(root)
    result = []
    for name in names:
        try:
            ext = os.path.splitext(str(name))[1].lower()
            if ext in _AUDIO_EXTENSIONS:
                result.append(str(name))
        except Exception:
            continue
    return sorted(set(result), key=str.casefold)


def _first_text(value):
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        for item in value:
            text = _first_text(item)
            if text:
                return text
        return ""
    text = str(value).strip()
    return text


def _normalise_lyrics(value):
    text = _first_text(value)
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _safe_metadata_dict(tags):
    if not tags:
        return {}
    result = {}
    try:
        items = tags.items()
    except Exception:
        return result
    for key, value in items:
        try:
            if isinstance(value, (list, tuple)):
                cleaned = [str(v) for v in value]
                result[str(key)] = cleaned if len(cleaned) != 1 else cleaned[0]
            else:
                result[str(key)] = str(value)
        except Exception:
            continue
    return result


def _read_audio_metadata(path: str):
    lyrics = ""
    artist = ""
    title = ""
    album = ""
    metadata = {}
    ext = os.path.splitext(path)[1].lower()

    # Read the formats written by URN Save Audio with Lyrics explicitly first.
    if ext == ".mp3":
        try:
            tags = ID3(path)
            metadata.update(_safe_metadata_dict(tags))
            for frame in tags.getall("USLT"):
                value = _normalise_lyrics(getattr(frame, "text", ""))
                if value:
                    lyrics = value
                    break
            artist = _first_text(tags.get("TPE1"))
            title = _first_text(tags.get("TIT2"))
            album = _first_text(tags.get("TALB"))
        except ID3NoHeaderError:
            pass
        except Exception as exc:
            print(f"{NODE_TAG} ID3 metadata read warning: {exc}")

    elif ext == ".flac":
        try:
            tags = FLAC(path)
            metadata.update(_safe_metadata_dict(tags.tags))
            for key in ("lyrics", "LYRICS", "unsyncedlyrics", "UNSYNCEDLYRICS"):
                if key in tags:
                    value = _normalise_lyrics(tags.get(key))
                    if value:
                        lyrics = value
                        break
            artist = _first_text(tags.get("artist"))
            title = _first_text(tags.get("title"))
            album = _first_text(tags.get("album"))
        except Exception as exc:
            print(f"{NODE_TAG} FLAC metadata read warning: {exc}")

    # Generic Mutagen fallback for files created outside URN and other formats.
    try:
        easy = MutagenFile(path, easy=True)
        if easy is not None and getattr(easy, "tags", None):
            easy_tags = easy.tags
            if not artist:
                artist = _first_text(easy_tags.get("artist"))
            if not title:
                title = _first_text(easy_tags.get("title"))
            if not album:
                album = _first_text(easy_tags.get("album"))
            if not lyrics:
                for key in ("lyrics", "unsyncedlyrics", "lyric"):
                    try:
                        value = _normalise_lyrics(easy_tags.get(key))
                    except Exception:
                        value = ""
                    if value:
                        lyrics = value
                        break
            for key, value in _safe_metadata_dict(easy_tags).items():
                metadata.setdefault(key, value)
    except Exception as exc:
        print(f"{NODE_TAG} generic metadata read warning: {exc}")

    return lyrics, artist.strip(), title.strip(), album.strip(), metadata


def _load_audio(path: str):
    from comfy_extras.nodes_audio import load as comfy_audio_decode

    waveform, sample_rate = comfy_audio_decode(path)
    if waveform.ndim == 1:
        waveform = waveform.unsqueeze(0)
    return {
        "waveform": waveform.unsqueeze(0).contiguous(),
        "sample_rate": int(sample_rate),
    }


class URNLoadAudio(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        files = _input_audio_files()
        return io.Schema(
            node_id="URNLoadAudio",
            display_name="URN Load Audio",
            category="URN Audio Tools",
            description=(
                "Loads an audio file and exposes its embedded metadata. Lyrics written by "
                "URN Save Audio with Lyrics are read from ID3 USLT (MP3) or Vorbis Comment "
                "LYRICS (FLAC). Artist, title, album, source filename and a JSON metadata dump "
                "are also provided for downstream URN nodes."
            ),
            inputs=[
                io.Combo.Input(
                    "audio",
                    options=files,
                    upload=io.UploadType.audio,
                ),
            ],
            outputs=[
                io.Audio.Output("audio"),
                io.String.Output(display_name="Embedded_Lyrics"),
                io.String.Output("Artist"),
                io.String.Output("Title"),
                io.String.Output("Album"),
                io.String.Output(display_name="Source_Filename"),
                io.String.Output(display_name="Metadata_JSON"),
            ],
        )

    @classmethod
    def validate_inputs(cls, audio, **kwargs):
        if not audio:
            return "Select or upload an audio file."
        if not folder_paths.exists_annotated_filepath(audio):
            return f"Audio file not found: {audio}"
        ext = os.path.splitext(folder_paths.get_annotated_filepath(audio))[1].lower()
        if ext not in _AUDIO_EXTENSIONS:
            return f"Unsupported audio format: {ext or 'unknown'}"
        return True

    @classmethod
    def fingerprint_inputs(cls, audio, **kwargs):
        if not audio or not folder_paths.exists_annotated_filepath(audio):
            return float("nan")
        path = folder_paths.get_annotated_filepath(audio)
        try:
            stat = os.stat(path)
            return f"{os.path.abspath(path)}|{stat.st_size}|{stat.st_mtime_ns}"
        except Exception:
            return float("nan")

    @classmethod
    def execute(cls, audio) -> io.NodeOutput:
        if not audio or not folder_paths.exists_annotated_filepath(audio):
            raise ValueError("URN Load Audio requires a valid selected audio file.")

        path = folder_paths.get_annotated_filepath(audio)
        audio_out = _load_audio(path)
        lyrics, artist, title, album, metadata = _read_audio_metadata(path)
        filename = os.path.basename(path)

        metadata_summary = {
            "source_filename": filename,
            "artist": artist,
            "title": title,
            "album": album,
            "embedded_lyrics_found": bool(lyrics),
            "tags": metadata,
        }
        metadata_json = json.dumps(metadata_summary, ensure_ascii=False, indent=2)

        if lyrics:
            print(f"{NODE_TAG} Loaded '{filename}' with embedded lyrics ({len(lyrics)} characters).")
        else:
            print(f"{NODE_TAG} Loaded '{filename}'; no embedded lyrics were found.")

        return io.NodeOutput(
            audio_out,
            lyrics,
            artist,
            title,
            album,
            filename,
            metadata_json,
        )
