import re
from collections import OrderedDict

from comfy_api.latest import io


NODE_TAG = "[URN Audio Create Yue2 Lyrics]"


def _log(message: str):
    print(f"{NODE_TAG} {message}")


def _header_value(abc: str, key: str) -> str:
    m = re.search(rf"(?mi)^\s*{re.escape(key)}\s*:\s*(.*?)\s*$", abc or "")
    return (m.group(1).strip() if m else "")


def _strip_abc_decorations(line: str) -> str:
    # Remove chord annotations, quoted text, comments, decorations and inline fields.
    s = re.sub(r'"(?:[^"\\]|\\.)*"', "", line)
    s = re.sub(r"%.*$", "", s)
    s = re.sub(r"![^!]*!", "", s)
    s = re.sub(r"\+[^^+]*\+", "", s)
    s = re.sub(r"\[[A-Za-z]:[^\]]*\]", "", s)
    return s


def _measure_note_summary(measure: str):
    """Return (note_attacks, rests, duration_tokens) for a single ABC measure.

    This is deliberately conservative: it is used to give the language model a
    singability/rhythm guide, not to re-notate the score.
    """
    s = _strip_abc_decorations(measure)

    # Remove grace-note groups and common bar/repeat syntax before token scanning.
    s = re.sub(r"\{[^}]*\}", "", s)
    s = s.replace("::", "|").replace(":|", "|").replace("|:", "|")

    token_re = re.compile(
        r"(?P<acc>\^{1,2}|_{1,2}|=)?"
        r"(?P<pitch>[A-Ga-gzZ])"
        r"(?P<oct>[,']*)"
        r"(?P<len>(?:\d+(?:/\d+)?)|(?:/+\d*)|(?:\d+/)?)?"
    )

    notes = []
    rests = 0
    for m in token_re.finditer(s):
        pitch = m.group("pitch")
        length = m.group("len") or "1"
        if pitch in "zZ":
            rests += 1
        else:
            notes.append((pitch + (m.group("oct") or ""), length))
    return notes, rests


def analyze_abc(abc: str) -> str:
    abc = str(abc or "")
    key = _header_value(abc, "K") or "unknown"
    meter = _header_value(abc, "M") or "unknown"
    tempo = _header_value(abc, "Q") or "unknown"
    unit = _header_value(abc, "L") or "unknown"

    sections = OrderedDict()
    current_section = "Song"
    current_voice = None
    sections[current_section] = []

    for raw in abc.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("%"):
            label = line.lstrip("%").strip()
            if label:
                current_section = label.title()
                sections.setdefault(current_section, [])
            continue
        if re.match(r"^V\s*:\s*", line, re.I):
            current_voice = re.sub(r"^V\s*:\s*", "", line, flags=re.I).strip().split()[0]
            continue
        # Ignore headers and non-vocal material.
        if re.match(r"^[A-Za-z]\s*:", line):
            continue
        if current_voice is None or current_voice.lower() != "vocal":
            continue

        measures = [m for m in line.split("|") if m.strip()]
        for measure in measures:
            notes, rests = _measure_note_summary(measure)
            if not notes and not rests:
                continue
            sections[current_section].append((notes, rests))

    lines = [
        f"Key: {key}",
        f"Meter: {meter}",
        f"Tempo: {tempo}",
        f"ABC unit length: {unit}",
        "",
        "VOCAL RHYTHM MAP",
        "Each measure below lists vocal note attacks. Treat note attacks as a strong guide to syllable capacity, not a rigid one-syllable-per-note law.",
    ]

    total_measures = 0
    total_attacks = 0
    for section, measures in sections.items():
        if not measures:
            continue
        lines.append(f"\n[{section}]")
        for idx, (notes, rests) in enumerate(measures, 1):
            total_measures += 1
            total_attacks += len(notes)
            if notes:
                pattern = " ".join(f"{p}:{d}" for p, d in notes)
                lines.append(
                    f"M{idx}: {len(notes)} note attacks, {rests} rest token(s) | {pattern}"
                )
            else:
                lines.append(f"M{idx}: vocal rest ({rests} rest token(s))")

    lines.append("")
    lines.append(f"Detected vocal measures: {total_measures}")
    lines.append(f"Detected vocal note attacks: {total_attacks}")
    return "\n".join(lines).strip()


def _section_order(abc: str):
    result = []
    for raw in (abc or "").splitlines():
        line = raw.strip()
        if line.startswith("%"):
            label = line.lstrip("%").strip()
            if label:
                result.append(label.title())
    return result


def _build_prompt(abc: str, theme: str, source_lyrics: str, creativity: float,
                  syllable_fit: int, rhyme_strength: int) -> tuple[str, str]:
    analysis = analyze_abc(abc)
    section_order = _section_order(abc)
    source_lyrics = str(source_lyrics or "").strip()
    theme = str(theme or "").strip()

    creativity_desc = (
        "fairly literal and focused" if creativity < 0.35 else
        "balanced: coherent, vivid and original" if creativity < 0.75 else
        "highly imaginative while remaining singable and coherent"
    )

    prompt = f"""You are writing ORIGINAL song lyrics for YuE2 music generation.

STORY / THEME
{theme}

GOAL
Write a complete new set of lyrics that fits the vocal phrasing of the supplied ABC score. The ABC is the musical timing reference. Do not output ABC notation. Output lyrics only.

MUSICAL ANALYSIS
{analysis}

SECTION ORDER FOUND IN ABC
{', '.join(section_order) if section_order else 'Use the section flow implied by the ABC.'}

LYRIC-WRITING RULES
- Create completely new wording for the requested story/theme.
- Fit lyric syllables naturally to the vocal note attacks and note durations in each section.
- Syllable-fit strength: {int(syllable_fit)}/100. At high values, keep syllable counts close to available note attacks; at lower values, natural melisma and multiple syllables per note are acceptable.
- Rhyme strength: {int(rhyme_strength)}/100. Use rhyme where it sounds natural; never sacrifice clarity just to rhyme.
- Creativity: {float(creativity):.2f}/1.00 ({creativity_desc}).
- Long notes should preferably land on open, singable vowel sounds or words that can be comfortably sustained.
- Avoid cramming dense multisyllabic phrases into sparse/held-note measures.
- Repeated choruses should normally reuse the same chorus lyrics unless the story clearly benefits from a final-chorus variation.
- Preserve the broad section structure implied by the ABC comments (verse, chorus, bridge, etc.).
- Instrumental/rest-only areas do not need lyric lines.
- Make the chorus memorable and thematically central.
- Do not mention these instructions, syllable counts, ABC, measures, note names, or analysis in the output.
- Do not use Markdown fences.
- Output ONLY the finished lyrics, with simple section labels such as [Verse 1], [Chorus], [Bridge].
"""

    if source_lyrics:
        prompt += f"""
SOURCE LYRICS (STRUCTURAL REFERENCE ONLY)
The text below may be used ONLY to understand approximate line grouping, repeated sections, and where vocals occur. Do NOT copy, paraphrase, closely imitate, or preserve distinctive phrases from it. The new lyrics must stand on their own and be about the new story/theme.

{source_lyrics}
"""

    # The full ABC is useful to the model when the compact parser misses a tie,
    # pickup, unusual tuplet or other notation nuance.
    prompt += f"""
FULL ABC SCORE (MUSICAL REFERENCE ONLY)
{abc}
"""
    return prompt.strip(), analysis


def _tokenize(clip, prompt: str):
    # Current ComfyUI TextGenerate-compatible language models support these args.
    # Fallbacks keep the custom node usable on slightly older native LLM builds.
    try:
        return clip.tokenize(
            prompt,
            skip_template=False,
            min_length=1,
            thinking=False,
            system_prompt="You write original, singable song lyrics and follow musical constraints precisely.",
        )
    except TypeError:
        try:
            return clip.tokenize(prompt, skip_template=False, min_length=1, thinking=False)
        except TypeError:
            return clip.tokenize(prompt)


def _generate(clip, tokens, max_length: int, creativity: float, seed: int):
    # Map the user-facing creativity control onto conservative text sampling.
    temperature = max(0.2, min(1.4, 0.35 + (float(creativity) * 0.9)))
    kwargs = dict(
        do_sample=True,
        max_length=int(max_length),
        temperature=temperature,
        top_k=64,
        top_p=0.92,
        min_p=0.03,
        repetition_penalty=1.08,
        presence_penalty=0.0,
        seed=int(seed),
        mtp=True,
    )
    try:
        return clip.generate(tokens, **kwargs)
    except TypeError:
        kwargs.pop("mtp", None)
        try:
            return clip.generate(tokens, **kwargs)
        except TypeError:
            kwargs.pop("presence_penalty", None)
            return clip.generate(tokens, **kwargs)


def _clean_generated_text(text: str) -> str:
    text = str(text or "").strip()
    if "</think>" in text:
        _, _, text = text.partition("</think>")
        text = text.strip()
    if text.startswith("<think>") and "</think>" not in text:
        # A model hit max length while still thinking. Returning that as lyrics is
        # worse than failing loudly because YuE2 would sing the reasoning text.
        raise RuntimeError(
            "The language model returned only reasoning text. Increase max_tokens or use a non-thinking text model."
        )
    text = re.sub(r"^```(?:text|lyrics)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    # Strip common prefaces if the model ignored the output-only instruction.
    text = re.sub(r"^(?:here(?:'s| is)\s+(?:the\s+)?(?:finished\s+)?lyrics\s*:?\s*)", "", text, flags=re.I)
    return text.strip()


class URNAudioCreateYue2Lyrics(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="URNAudioCreateYue2Lyrics",
            display_name="URN Audio Create Yue2 Lyrics",
            category="URN Audio Tools",
            description=(
                "Writes new YuE2-ready lyrics from a story/theme while using a YuE2 ABC score as the vocal rhythm guide. "
                "Uses ComfyUI's native CLIP/text-generation model interface; no Ollama server is required."
            ),
            inputs=[
                io.Clip.Input(
                    "clip",
                    display_name="clip",
                    tooltip="Connect a ComfyUI native text-generation model (for example Qwen3/Qwen3.5 loaded as CLIP).",
                ),
                io.String.Input(
                    "abc",
                    display_name="abc",
                    force_input=True,
                    tooltip="Connect the STRING output from YuE2 Generate ABC.",
                ),
                io.String.Input(
                    "source_lyrics",
                    display_name="source_lyrics",
                    force_input=True,
                    optional=True,
                    tooltip="Optional original lyrics. Used only as a structural reference; the node instructs the model not to copy or paraphrase them.",
                ),
                io.String.Input(
                    "theme_story",
                    display_name="Theme / Story",
                    multiline=True,
                    default="A love/hate relationship between a cat and a dog.",
                ),
                io.Float.Input(
                    "creativity",
                    display_name="Creativity",
                    default=0.75,
                    min=0.0,
                    max=1.0,
                    step=0.05,
                ),
                io.Int.Input(
                    "syllable_fit",
                    display_name="Syllable Fit",
                    default=85,
                    min=0,
                    max=100,
                    step=1,
                ),
                io.Int.Input(
                    "rhyme_strength",
                    display_name="Rhyme Strength",
                    default=65,
                    min=0,
                    max=100,
                    step=1,
                ),
                io.Int.Input(
                    "max_tokens",
                    display_name="Max Tokens",
                    default=2048,
                    min=256,
                    max=8192,
                    step=128,
                    advanced=True,
                ),
                io.Int.Input(
                    "seed",
                    display_name="seed",
                    default=0,
                    min=0,
                    max=0xFFFFFFFFFFFFFFFF,
                ),
            ],
            outputs=[
                io.String.Output(display_name="new_lyrics"),
                io.String.Output(display_name="abc"),
                io.String.Output(display_name="analysis"),
            ],
            not_idempotent=True,
        )

    @classmethod
    def execute(
        cls,
        clip,
        abc,
        theme_story,
        creativity=0.75,
        syllable_fit=85,
        rhyme_strength=65,
        max_tokens=2048,
        seed=0,
        source_lyrics="",
    ) -> io.NodeOutput:
        abc = str(abc or "").strip()
        theme_story = str(theme_story or "").strip()
        if not abc:
            raise ValueError("ABC input is empty. Connect the output from YuE2 Generate ABC.")
        if not theme_story:
            raise ValueError("Theme / Story is empty. Describe what the new song should be about.")

        prompt, analysis = _build_prompt(
            abc=abc,
            theme=theme_story,
            source_lyrics=source_lyrics,
            creativity=creativity,
            syllable_fit=syllable_fit,
            rhyme_strength=rhyme_strength,
        )

        _log(
            f"Generating new lyrics | syllable_fit={syllable_fit} | rhyme={rhyme_strength} | "
            f"creativity={creativity:.2f} | max_tokens={max_tokens}"
        )
        tokens = _tokenize(clip, prompt)
        generated_ids = _generate(clip, tokens, max_tokens, creativity, seed)
        lyrics = _clean_generated_text(clip.decode(generated_ids))
        if not lyrics:
            raise RuntimeError("The text-generation model returned an empty lyric result.")

        _log(f"Complete: generated {len(lyrics)} characters of new lyrics.")
        # ABC is passed through unchanged to make wiring into YuE2 Generate Music easy.
        return io.NodeOutput(lyrics, abc, analysis)
