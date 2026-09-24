# URN Audio Nodes for ComfyUI

A collection of custom ComfyUI nodes for audio editing, splitting, mixing, lyric handling, style selection, YuE2 workflows etc  
The package is designed to keep common audio tasks inside ComfyUI while providing interactive visual controls.  
Several nodes support optional transcription, stem-separation, and music-analysis features.

## Included Nodes
### URN Audio Lyrics (Automatically Retrieves lyrics and music tags online)
Automatically Retrieves lyrics for songs or transcribes them when online lyrics cannot be found. 
<img width="536" height="526" alt="image" src="https://github.com/user-attachments/assets/116a07a8-d5c3-478b-b671-0cde1f64435b" />

Automatically Retrieves lyrics for songs or transcribes them when online lyrics cannot be found.  
Supports automatic title detection, interactive song selection, manual artist/title search, and Whisper as the final fallback.  
It can also output duration, filtered MusicBrainz style information, BPM analysis, and the original audio.



### URN Audio Style Selector (gets online Genres for Yue2 etc)
Populates with online tags and allows you to add/remove/customise tags
<img width="640" height="540" alt="image" src="https://github.com/user-attachments/assets/05e26896-7004-47f8-b823-946538f64173" />

Visual style/tag selector designed for music-generation workflows such as YuE2.  
Incoming generated styles can be reviewed, removed, or supplemented with user-selected tags from configurable categories.  
Tabs and available styles are loaded from JSON, and custom user tags can be added and saved directly from the node.



### URN Audio Smart Splitter (Splits  audio into chunks while avoiding cutting through vocals or words)
<img width="355" height="605" alt="image" src="https://github.com/user-attachments/assets/1abb016e-6eb5-4f23-ad94-cec158a6a51e" />

Splits longer audio into manageable chunks while attempting to avoid cutting through vocals or words.  
Can use Whisper analysis and optional Mel-RoFormer vocal/music separation to improve cut placement.  
Supports vocal/music stem export, vocal chunks, transcript sidecars, and FLAC/MP3 chunk output.



### URN Audio Mixer (Mix tracks and audio samples)
<img width="640" height="512" alt="image" src="https://github.com/user-attachments/assets/4178afea-55d7-429e-b95b-1bdc04605cf8" />

A visual multi-track audio mixer for arranging and combining multiple clips inside ComfyUI.  
Clips can be positioned and adjusted independently with trim, fades, and track-level controls.  
Useful for assembling generated music, vocals, effects, or other audio elements into a final mix.



### URN Audio Channel
<img width="480" height="480" alt="image" src="https://github.com/user-attachments/assets/313a8933-c58d-4d85-9001-a6d968d8e1d2" />

Provides straightforward stereo-channel processing for connected audio.  
Includes left/right level control, channel swapping, static panning, and whole-clip auto-pan.  
Designed as a lightweight utility node for quick stereo adjustments.



### URN Audio Trim Fade
<img width="640" height="540" alt="image" src="https://github.com/user-attachments/assets/ad56ad6b-cae4-49ac-8e7f-7f752bb5cc37" />

Visual trim, fade, gain, normalization, and silence-padding utility for connected audio.  
Includes an interactive waveform editor with start/end and fade controls, plus local preview support.  
Can work by output length or end position and passes the processed audio back into the workflow.



### URN Text Edit

Interactive text-review node that pauses the workflow until the user accepts the current text.  
The text can be edited directly, saved to a `.txt` file, or replaced by loading an existing `.txt` file.  
Only **Accept Changes** resumes downstream workflow execution.



## Optional Models and Downloads

Some features require external models:

- **Faster-Whisper** models are downloaded and cached locally when transcription is first used.
- **Mel-RoFormer** is used by the Smart Audio Splitter for optional vocal/music separation and downloads its required model when needed.

Model files are not bundled with this repository.

## Editable Configuration Files

### `urn_audio_style_selector_styles.json`

Controls the tabs and tags displayed by **URN Audio Style Selector**.  
Each top-level JSON section automatically becomes a tab, so categories and tags can be expanded without editing the node code.

### `urn_musicbrainz_tag_blocklist.json`

Contains tags that should be excluded from MusicBrainz-derived music descriptions.  
The file can be edited directly to customise the filtering behaviour.


## Dependencies

The package uses several non-core Python dependencies, including:

- `faster-whisper`
- `audio-separator`
- `librosa`
- `scipy`
- `soundfile`

Use the included `install.bat` or install the versions listed in `requirements.txt` with the Python environment used by ComfyUI.


## Installation

1. Download or clone the repository.
2. Place the **`URN Audio Nodes`** folder inside:

   ```text
   ComfyUI/custom_nodes/
   ```

3. Run `install.bat` once to install the required Python dependencies.
4. Restart ComfyUI.
5. After updating frontend files, a browser hard refresh (`Ctrl+F5`) may be required.

> If you already manage dependencies manually, you can install them from `requirements.txt` using ComfyUI's Python environment.


### Manual installation with pip

If you do not want to use `install.bat`, open a Command Prompt in your ComfyUI installation folder and run:

```bat
python_embeded\python.exe -m pip install -r "ComfyUI\custom_nodes\URN Audio Nodes\requirements.txt"
```

If your ComfyUI installation does not use the Windows `python_embeded` layout, run the same requirements file using the Python environment that launches ComfyUI:

```bash
python -m pip install -r "ComfyUI/custom_nodes/URN Audio Nodes/requirements.txt"
```

> **First run:** some features may take a little longer the first time they are used because required models such as Faster-Whisper and Mel-RoFormer may need to be downloaded and cached locally. Later runs will reuse the downloaded model files.

## Workflow Compatibility

Where visible node names have changed, internal node IDs have been preserved where possible so existing workflows continue to load correctly.

## Documentation

More detailed per-node documentation is included in:

```text
URN Audio Nodes/Node Readmes/
```

These files can also be copied into ComfyUI Markdown/Note nodes and placed beside the matching node in a workflow.
