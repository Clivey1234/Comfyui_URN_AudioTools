from comfy_api.latest import ComfyExtension

from .smart_audio_chunker import URNSmartAudioChunker
from .legacy_v3_wrappers import URNMP3TrimFadeV3, URNSmartSeamlessAudioLoopV3
from .urn_audio_mixer import URNAudioMixer
from .urn_audio_channel import URNAudioChannel
from .urn_audio_lyrics import URNAudioLyrics
from .urn_text_edit import URNTextEdit
from .urn_audio_create_yue2_lyrics import URNAudioCreateYue2Lyrics
from .urn_audio_style_selector import URNAudioStyleSelector
from .urn_tabbed_markdown import URNTabbedMarkdown
from .urn_save_audio_with_lyrics import URNSaveAudioWithLyrics
from .urn_load_audio import URNLoadAudio


class URNAudioNodesExtension(ComfyExtension):
    async def get_node_list(self):
        return [
            URNMP3TrimFadeV3,
            URNSmartSeamlessAudioLoopV3,
            URNSmartAudioChunker,
            URNAudioMixer,
            URNAudioChannel,
            URNAudioLyrics,
            URNTextEdit,
            URNAudioCreateYue2Lyrics,
            URNAudioStyleSelector,
            URNTabbedMarkdown,
            URNSaveAudioWithLyrics,
            URNLoadAudio,
        ]


async def comfy_entrypoint() -> URNAudioNodesExtension:
    return URNAudioNodesExtension()


__all__ = [
    "comfy_entrypoint",
    "URNMP3TrimFadeV3",
    "URNSmartSeamlessAudioLoopV3",
    "URNSmartAudioChunker",
    "URNAudioMixer",
    "URNAudioChannel",
    "URNAudioLyrics",
    "URNTextEdit",
    "URNAudioCreateYue2Lyrics",
    "URNAudioStyleSelector",
    "URNTabbedMarkdown",
    "URNSaveAudioWithLyrics",
    "URNLoadAudio",
]
