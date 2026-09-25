URN Audio Nodes - V9.86
=======================

CURRENT PACKAGE
---------------
This package contains the current URN Audio Nodes working set through V9.86
The per-node documentation is in the "Node Readmes" folder.

Included nodes
--------------
1. URN Audio Trim Fade
   Internal node ID: URN_MP3_Trim_Fade
   - Connected AUDIO trim/fade/pad utility with waveform editing and local Preview Changes.
   - Supports Length or End Sec trim modes, gain, normalization and optional silence padding.

2. URN Smart Seamless Audio Extender
   Internal node ID: URNSmartSeamlessAudioLoop
   - Finds an internal loop and extends audio to a requested duration.
   - Best Internal Loop and Preserve Full Input modes.
   - Connected audio_input takes priority over the audio_file picker.
   - A stale/missing picker file no longer blocks execution when a valid AUDIO cable is connected.

3. URN Audio Smart Splitter
   Internal node ID: URNSmartAudioChunker
   - Former visible name: URN Smart Audio Splitter.
   - Internal ID is unchanged for workflow compatibility.
   - Splits audio into safe-length chunks while avoiding cuts through words where possible.
   - Optional Mel-RoFormer vocal/music stems, vocal chunks, SRT/TXT sidecars and FLAC/MP3 chunk output.

4. URN Audio Mixer
   Internal node ID: URNAudioMixer
   - Multi-track visual timeline mixer.
   - Multiple clips per track with independent trim, fades, position and track volume.

5. URN Audio Channel
   Internal node ID: URNAudioChannel
   - Stereo channel levels, L/R swap, static pan and whole-clip auto-pan.

6. URN Audio Lyrics
   Internal node ID: URNAudioLyrics
   - Connected AUDIO lyric/transcription node.
   - Online lyric order after a confirmed song: LRCLIB -> Lyrics.ovh -> Whisper fallback.
   - Automatic filename-based title search is followed by a manual artist-search stage when rejected or unsuccessful.
   - Whisper is the final fallback after the manual stage is rejected or fails.
   - Stop Workflow is available from the interactive selection panel.
   - Music_Description combines filtered MusicBrainz genre/style tags with Librosa BPM and an optional conservative male/female lead-vocal label.
   - Album metadata is passed through from URN Load Audio when available; otherwise a confirmed Artist + Title triggers an automatic MusicBrainz album/release lookup with no extra validation prompt.
   - MusicBrainz tags are filtered by editable urn_musicbrainz_tag_blocklist.json.
   - The current upstream Load Audio filename is refreshed on every prompt so an old song name is not reused when the cable stays connected.

7. URN Text Edit
   Internal node ID: URNTextEdit
   - Pauses execution so a STRING can be reviewed/edited.
   - Save Changes writes the editor contents to a user-selected .txt file without resuming the workflow.
   - Load Existing loads a user-selected .txt file without resuming the workflow.
   - Accept Changes is the only action that resumes execution.

8. URN Audio Create Yue2 Lyrics
   Internal node ID: URNAudioCreateYue2Lyrics
   - Uses a ComfyUI-native text-generation CLIP/model connection to write new lyrics from Theme / Story.
   - Uses YuE2 ABC as a vocal timing/phrasing reference.
   - Outputs new_lyrics, unchanged abc passthrough and analysis.
   - Ollama is not required.

9. URN Audio Style Selector
   Internal node ID: URNAudioStyleSelector
   - Visual JSON-driven style/tag selector.
   - Incoming Generated_Styles appear grey in the green selected-style area.
   - User additions appear purple.
   - Available tags are shown on a black panel with white labels.
   - Tabs are generated dynamically from urn_audio_style_selector_styles.json.
   - Quick Custom adds a custom tag immediately and saves it under User Custom in the JSON file.
   - Preset tag lists are alphabetically sorted; BPM is kept in natural numeric order.


10. URN Tabbed Markdown
   Internal node ID: URNTabbedMarkdown
   - Display-only tabbed Markdown note with no inputs or outputs.
   - Add/remove tabs, double-click a tab to rename it, and double-click the note area to edit Markdown.
   - Tab names/content are stored with the workflow.


11. URN Save Audio with Lyrics
   Internal node ID: URNSaveAudioWithLyrics
   - Saves connected AUDIO as FLAC or MP3 using a user-controlled filename.
   - Embeds Lyrics/Artist/Title/Album using ID3 USLT/TPE1/TIT2/TALB for MP3 and Vorbis LYRICS/ARTIST/TITLE/ALBUM for FLAC.
   - Save Location can target a relative folder under ComfyUI/output or an absolute folder; outputs the saved path plus unchanged AUDIO passthrough.

12. URN Load Audio
   Internal node ID: URNLoadAudio
   - Loads audio through the normal ComfyUI input/upload flow.
   - Outputs AUDIO plus Embedded_Lyrics, Artist, Title, Album, Source_Filename and Metadata_JSON.
   - Reads URN-saved MP3 lyrics from ID3 USLT and FLAC lyrics from Vorbis Comment LYRICS.
   - Designed to feed embedded lyrics and song identity directly into URN Audio Lyrics.

Installation
------------
1. Copy the entire "URN Audio Nodes" folder into:
      ComfyUI\\custom_nodes\\
2. Remove/disable older standalone URN audio-node folders that register overlapping internal IDs.
3. Run install.bat once if the required Python packages are not already installed.
4. Restart ComfyUI.
5. Hard-refresh the browser (Ctrl+F5) after frontend changes.

Current package dependencies
----------------------------
faster-whisper==1.2.1
audio-separator[cpu]==0.44.5
librosa>=0.11.0
scipy>=1.13.0
soundfile>=0.13.1
mutagen>=1.47.0

Model behaviour
---------------
- Faster-Whisper models are cached locally after first download.
- Mel-RoFormer/audio-separator downloads its model when required if it is not already available locally.
- URN Audio Create Yue2 Lyrics does not bundle an LLM. Connect a ComfyUI-native text-generation model such as Qwen through the appropriate CLIP loader.

Editable support files
----------------------
urn_audio_style_selector_styles.json
- Top-level JSON keys become Style Selector tabs automatically.
- Quick Custom entries are stored under User Custom.
- Style/tag entries are sorted alphabetically except BPM, which uses numeric order.

urn_musicbrainz_tag_blocklist.json
- Editable MusicBrainz metadata blocklist.
- Unwanted community tags are removed before Music_Description is produced.
- Built-in defaults remain available if the JSON file is missing or malformed.

Recent accepted changes
-----------------------
V9.63
- Fixed stale upstream filename handling in URN Audio Lyrics. Changing the file selected in the same Load Audio node now refreshes the filename used for online lookup.

V9.65
- Added Quick Custom to URN Audio Style Selector and persistent User Custom JSON storage.

V9.66
- Added Save Changes and Load Existing to URN Text Edit. Neither resumes the workflow; Accept Changes remains the execution gate.

V9.68 / V9.69
- Changed the Style Selector available-tags panel to black and kept tag text white, including inactive/already-selected items.

V9.70
- Fixed URN Smart Seamless Audio Extender so connected audio_input always wins over a stale audio_file picker value during validation and execution.

V9.71
- Renamed the visible splitter node to URN Audio Smart Splitter while preserving internal ID URNSmartAudioChunker.
- Sorted Style Selector JSON tag lists alphabetically, with natural numeric sorting for BPM.

V9.72
- Added manual artist-search fallback to URN Audio Lyrics. Automatic search -> manual artist/title search -> Whisper final fallback.

V9.73
- Added editable MusicBrainz tag filtering through urn_musicbrainz_tag_blocklist.json before tags reach Music_Description.

V9.75
- Removed the redundant audio_length FLOAT input connector from URN Audio Trim Fade. The Length widget now exclusively controls duration in Length mode.

V9.76
- Added URN Tabbed Markdown, a display-only multi-tab Markdown note with persistent tab names/content and no inputs or outputs.

Compatibility notes
-------------------
- Existing workflows continue to use the same internal node IDs where names have changed.
- The obsolete LRCLIB Lyricsfile experiment from V9.67 is not part of this branch; V9.76 uses the accepted V9.66/V9.72 lyric behaviour.


V9.78 - URN TABBED MARKDOWN FORMATTING
---------------------------------------
- URN Tabbed Markdown now uses ComfyUI's native Markdown renderer when available.
- Markdown headings, bold/italic text, lists, tables, blockquotes, links and code now follow ComfyUI's normal Markdown formatting behaviour.
- The previous lightweight parser remains only as an older-frontend fallback.

V9.79 - URN TABBED MARKDOWN PERSISTENCE FIX
----------------------------------------------
- Fixed URN Tabbed Markdown tab names and Markdown contents not surviving workflow save/reload or browser refresh.
- The display DOM widget now participates in workflow serialization while remaining excluded from the backend prompt.
- Live editor text is copied into saved state during workflow serialization, even if the editor still has focus.
- Markdown edits now mark the workflow dirty immediately instead of waiting for a debounce/blur event.
- Added property-based serialization as a second compatibility path for current and older ComfyUI frontends.


V9.80
- URN Tabbed Markdown: tab labels are no longer truncated; full tab titles are displayed.


V9.84
- Added URN Save Audio with Lyrics.
- Saves using the original upstream source filename.
- MP3 lyrics use the standard ID3 USLT frame; FLAC lyrics use Vorbis Comment LYRICS.


V9.85
- URN Save Audio with Lyrics now includes a Save Location field.
- Relative paths are saved under ComfyUI/output; absolute folders are also supported.
- The original source filename is still preserved automatically.


V9.86
- Added URN Load Audio with AUDIO + embedded lyrics + Artist/Title/Album/filename/metadata outputs.
- URN Audio Lyrics now accepts optional Embedded Lyrics, Artist and Title inputs.
- Embedded lyrics are offered first in an interactive preview before any online lyric lookup.
- Choosing Ignore / Search Online continues through the accepted LRCLIB -> Lyrics.ovh -> Whisper fallback sequence.
- Connected Artist/Title metadata takes priority over filename parsing for online song identification.

V9.88: URN Audio Lyrics adds accepted Artist/Title outputs. URN Save Audio with Lyrics adds Artist/Title metadata inputs and a user-controlled Filename instead of inheriting the source filename.

V9.89: URN Audio Lyrics renames the visible Song control to Song Analysis and adds a separate Gender Vocal Determination toggle. Song Analysis keeps BPM/music-feature and Verse/Chorus processing; Gender Vocal Determination independently controls isolated-vocal male/female classification.

V9.91: URN Audio Lyrics adds Album input/output and automatic MusicBrainz album resolution when embedded Album metadata is unavailable. URN Save Audio with Lyrics adds Album metadata input and writes ID3 TALB / Vorbis ALBUM. Album lookup is automatic and does not add a user validation stage.

V9.92: Automatic Album lookup now hard-rejects Compilation, Various Artists, Live and Remix releases and rejects known artist mismatches. There is no compilation/Various Artists fallback; Album remains blank if no acceptable original-artist release is found.
V9.93: Fixed URN Audio Lyrics workflow-reload widget ordering. Gender Vocal Determination is now moved visually only after ComfyUI restores saved widget values, preventing Get Online Lyrics from being overwritten by the hidden filename value after restart. Named widget values are also restored when present for V9.90+ workflow compatibility.
