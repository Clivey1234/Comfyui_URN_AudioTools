import json
import sys
from audio_separator.separator import Separator


def main():
    source_path, output_dir, model_dir, model_filename = sys.argv[1:5]
    stem_mode = (sys.argv[5] if len(sys.argv) > 5 else "vocals").strip().lower()
    if stem_mode not in {"vocals", "instrumental", "both"}:
        raise ValueError(f"Unsupported stem mode: {stem_mode}")

    output_single_stem = {
        "vocals": "Vocals",
        "instrumental": "Instrumental",
        "both": None,
    }[stem_mode]

    separator = Separator(
        model_file_dir=model_dir,
        output_dir=output_dir,
        output_format="WAV",
        output_single_stem=output_single_stem,
        sample_rate=44100,
        normalization_threshold=0.9,
        amplification_threshold=0.0,
        use_autocast=False,
    )
    separator.load_model(model_filename=model_filename)
    outputs = separator.separate(
        source_path,
        {
            "Vocals": "urn_vocals",
            "Instrumental": "urn_instrumental",
        },
    )
    print("__URN_MEL_OUTPUTS__=" + json.dumps(outputs))


if __name__ == "__main__":
    main()
