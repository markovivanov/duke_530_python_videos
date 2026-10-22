#!/usr/bin/env python3
"""Generate synchronized tutorial narration with ElevenLabs."""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"
DEFAULT_VOICE_NAME = "Rachel"


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def extract_cues(source: str | Path, cue_field: str) -> list[dict[str, object]]:
    if isinstance(source, Path):
        source = source.read_text(encoding="utf-8")
    scene_pattern = re.compile(
        r'scenes\.push\(\{name:("(?:\\.|[^"\\])*")'
        rf',dur:[0-9.]+,{re.escape(cue_field)}:\[(.*?)\]\s*,build',
        re.DOTALL,
    )
    cue_pattern = re.compile(
        r'RECAP_BEAT|\[\s*([0-9.]+)\s*,\s*("(?:\\.|[^"\\])*")\s*\]'
    )
    recap_match = re.search(
        r'const RECAP_BEAT=\[\s*([0-9.]+)\s*,\s*'
        r'("(?:\\.|[^"\\])*")\s*\];',
        source,
    )
    recap = (
        (float(recap_match.group(1)), json.loads(recap_match.group(2)))
        if recap_match
        else None
    )

    cues: list[dict[str, object]] = []
    for scene_index, scene_match in enumerate(scene_pattern.finditer(source)):
        scene_name = json.loads(scene_match.group(1))
        for cue_index, cue_match in enumerate(cue_pattern.finditer(scene_match.group(2))):
            if cue_match.group(0) == "RECAP_BEAT":
                if recap is None:
                    raise RuntimeError("RECAP_BEAT is used but not defined")
                _, caption = recap
            else:
                caption = json.loads(cue_match.group(2))
            spoken = make_speakable(caption)
            cues.append(
                {
                    "key": f"{scene_index}:{cue_index}",
                    "scene": scene_name,
                    "caption": caption,
                    "spoken": spoken,
                }
            )
    if not cues:
        raise RuntimeError(f"No {cue_field} narration cues were found")
    return cues


_DIGIT_WORDS = {
    "0": "zero",
    "1": "one",
    "2": "two",
    "3": "three",
    "4": "four",
    "5": "five",
    "6": "six",
    "7": "seven",
    "8": "eight",
    "9": "nine",
}

_SMALL_NUMBER_WORDS = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)

_TENS_WORDS = {
    20: "twenty",
    30: "thirty",
    40: "forty",
    50: "fifty",
    60: "sixty",
    70: "seventy",
    80: "eighty",
    90: "ninety",
}


def integer_to_words(value: str) -> str:
    number = int(value)
    if number < 20:
        return _SMALL_NUMBER_WORDS[number]
    if number < 100:
        tens, ones = divmod(number, 10)
        return _TENS_WORDS[tens * 10] + (f" {_SMALL_NUMBER_WORDS[ones]}" if ones else "")
    if number < 1000:
        hundreds, remainder = divmod(number, 100)
        result = f"{_SMALL_NUMBER_WORDS[hundreds]} hundred"
        return result + (f" {integer_to_words(str(remainder))}" if remainder else "")
    return " ".join(_DIGIT_WORDS[digit] for digit in value)


def decimal_to_words(match: re.Match[str]) -> str:
    token = match.group(0)
    negative = token.startswith("-")
    if negative:
        token = token[1:]
    whole, *fractional_groups = token.split(".")
    spoken = integer_to_words(whole)
    for group in fractional_groups:
        spoken += " point " + " ".join(_DIGIT_WORDS[digit] for digit in group)
    return ("negative " if negative else "") + spoken


def make_speakable(text: str) -> str:
    """Expand code notation into deliberate speech without changing captions."""
    text = text.replace("…", "...")
    phrase_replacements = {
        "__init__": "dunder init",
        "TypeError": "type error",
        "AttributeError": "attribute error",
        "FileNotFoundError": "file not found error",
        "README": "read me",
        "HIHI": "hi hi",
        "True/False": "True or False",
        "yes/no": "yes or no",
    }
    for source, replacement in phrase_replacements.items():
        text = text.replace(source, replacement)

    # Read object attributes and filenames explicitly, then spell snake_case.
    text = re.sub(r"(?<=[A-Za-z_])\.(?=[A-Za-z_])", " dot ", text)
    text = re.sub(r"(?<=[A-Za-z0-9])_(?=[A-Za-z0-9])", " underscore ", text)

    # Expand compound units before their individual abbreviations.
    unit_patterns = (
        (r"\b(\d+(?:\.\d+)?)\s+km(?=\s+run\b)", r"\1 kilometer"),
        (r"\bkm\s*/\s*(?:hr|h)\b", "kilometers per hour"),
        (r"\bmi\s*/\s*(?:hr|h)\b", "miles per hour"),
        (r"\bmin\s*/\s*km\b", "minutes per kilometer"),
        (r"\bsec\s*/\s*km\b", "seconds per kilometer"),
        (r"\bper\s+km\b", "per kilometer"),
    )
    for pattern, replacement in unit_patterns:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

    abbreviation_patterns = (
        (r"\bkm\b", "kilometers"),
        (r"\bkph\b", "kilometers per hour"),
        (r"\bmph\b", "miles per hour"),
        (r"\bmi\b", "miles"),
        (r"\bkg\b", "kilograms"),
        (r"\blbs?\b", "pounds"),
        (r"\bft\b", "feet"),
        (r"\bcm\b", "centimeters"),
        (r"\bmm\b", "millimeters"),
        (r"\bhrs?\b", "hours"),
        (r"\bmins?\b", "minutes"),
        (r"\bsecs?\b", "seconds"),
        (r"\bint\b", "integer"),
        (r"\bstr\b", "S T R"),
        (r"\bbool\b", "Boolean"),
        (r"\bpwd\b", "P W D"),
        (r"\bls\b", "L S"),
        (r"\bcd\b", "C D"),
        (r"\bplt\b", "P L T"),
        (r"\bBMI\b", "B M I"),
        (r"\bbmi\b", "B M I"),
        (r"\bCSV\b", "C S V"),
        (r"\bcsv\b", "C S V"),
        (r"\bJSON\b", "J S O N"),
        (r"\bjson\b", "J S O N"),
        (r"\btxt\b", "text"),
        (r"\bpy\b", "P Y"),
        (r"\bmd\b", "M D"),
        (r"\bVS\b", "V S"),
    )
    for pattern, replacement in abbreviation_patterns:
        text = re.sub(pattern, replacement, text)

    # Unit suffixes inside names need their full meaning as well.
    text = re.sub(r"\bunderscore m\b", "underscore meters", text)
    text = text.replace("kilometre", "kilometer").replace("metre", "meter")

    # Preserve every written decimal place, including trailing zeroes.
    text = re.sub(r"(?<![\w.])-?\d+(?:\.\d+)+(?!\w)", decimal_to_words, text)

    # Read remaining path separators and common operators instead of pausing.
    text = re.sub(r"\s*/\s*", " slash ", text)
    text = re.sub(r"\s*=\s*", " equals ", text)
    text = re.sub(r"\s*\+\s*", " plus ", text)
    text = text.replace("%", " percent")
    return re.sub(r"[ \t]+", " ", text).strip()


def build_narration(cues: list[dict[str, object]]) -> tuple[str, list[tuple[int, int]]]:
    parts: list[str] = []
    spans: list[tuple[int, int]] = []
    cursor = 0
    for cue in cues:
        spoken = str(cue["spoken"]).strip()
        if parts:
            separator = "\n\n"
            parts.append(separator)
            cursor += len(separator)
        start = cursor
        parts.append(spoken)
        cursor += len(spoken)
        spans.append((start, cursor))
    return "".join(parts), spans


def audio_duration(path: Path, fallback: float) -> float:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return float(result.stdout.strip())
    except (FileNotFoundError, subprocess.CalledProcessError, ValueError):
        return fallback


def normalize_audio(path: Path) -> None:
    """Master narration to a consistent web-friendly loudness when ffmpeg exists."""
    normalized_path = path.with_name(path.stem + ".normalized.mp3")
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(path),
                "-af",
                "loudnorm=I=-16:TP=-1.5:LRA=11",
                "-codec:a",
                "libmp3lame",
                "-b:a",
                "128k",
                str(normalized_path),
            ],
            check=True,
        )
        normalized_path.replace(path)
    except (FileNotFoundError, subprocess.CalledProcessError):
        if normalized_path.exists():
            normalized_path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="python-visualized.html")
    parser.add_argument("--cue-field", choices=("caps", "beats"), default="caps")
    parser.add_argument("--slug", default="episode1")
    parser.add_argument("--global-name", default="EPISODE1_NARRATION")
    parser.add_argument("--voice-id", default=DEFAULT_VOICE_ID)
    parser.add_argument("--voice-name", default=DEFAULT_VOICE_NAME)
    parser.add_argument("--model-id", default="eleven_multilingual_v2")
    args = parser.parse_args()

    env = read_env(ROOT / ".env")
    api_key = env.get("ELEVENLABS_API_KEY") or env.get("ELEVENLABS")
    if not api_key:
        print("Missing ELEVENLABS_API_KEY or ELEVENLABS in .env", file=sys.stderr)
        return 1

    source_path = ROOT / args.source
    output_dir = ROOT / "assets" / f"{args.slug}-narration"
    asset_base = f"{args.slug}-narration"
    cues = extract_cues(source_path.read_text(encoding="utf-8"), args.cue_field)
    narration, spans = build_narration(cues)
    payload = {
        "text": narration,
        "model_id": args.model_id,
        "voice_settings": {
            "stability": 0.48,
            "similarity_boost": 0.78,
            "style": 0.12,
            "use_speaker_boost": True,
            "speed": 1.0,
        },
    }
    query = urllib.parse.urlencode({"output_format": "mp3_44100_128"})
    url = (
        "https://api.elevenlabs.io/v1/text-to-speech/"
        f"{urllib.parse.quote(args.voice_id, safe='')}/with-timestamps?{query}"
    )
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "xi-api-key": api_key,
        },
        method="POST",
    )

    print(
        f"Generating {len(cues)} cues ({len(narration)} characters) "
        f"with {args.voice_name} / {args.model_id}..."
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(detail)
            detail = parsed.get("detail", {}).get("message", detail)
        except json.JSONDecodeError:
            pass
        print(f"ElevenLabs request failed ({error.code}): {detail}", file=sys.stderr)
        return 1

    alignment = result.get("alignment") or result.get("normalized_alignment")
    if not alignment:
        print("ElevenLabs response did not contain alignment data", file=sys.stderr)
        return 1

    characters = alignment["characters"]
    start_times = alignment["character_start_times_seconds"]
    end_times = alignment["character_end_times_seconds"]
    if len(characters) != len(narration):
        print(
            "Alignment length does not match the submitted narration text",
            file=sys.stderr,
        )
        return 1

    cue_starts: list[float] = []
    cue_spoken_ends: list[float] = []
    for span_start, span_end in spans:
        cue_starts.append(float(start_times[span_start]))
        cue_spoken_ends.append(float(end_times[span_end - 1]))

    output_dir.mkdir(parents=True, exist_ok=True)
    audio_path = output_dir / f"{asset_base}.mp3"
    audio_path.write_bytes(base64.b64decode(result["audio_base64"]))
    normalize_audio(audio_path)
    duration = audio_duration(audio_path, cue_spoken_ends[-1])

    manifest_cues: dict[str, dict[str, object]] = {}
    for index, cue in enumerate(cues):
        segment_end = cue_starts[index + 1] if index + 1 < len(cues) else duration
        spoken_end = cue_spoken_ends[index]
        if index == len(cues) - 1:
            # Re-encoding can trim a few milliseconds of MP3 padding. Keep the
            # final playback cutoff safely inside the normalized file.
            spoken_end = min(spoken_end, max(cue_starts[index], duration - 0.005))
        manifest_cues[str(cue["key"])] = {
            "start": round(cue_starts[index], 3),
            "spoken_end": round(spoken_end, 3),
            "end": round(segment_end, 3),
            "caption": cue["caption"],
            "spoken": cue["spoken"],
        }

    manifest = {
        "provider": "ElevenLabs",
        "model_id": args.model_id,
        "voice": {"id": args.voice_id, "name": args.voice_name},
        "audio": f"{asset_base}.mp3",
        "duration_seconds": round(duration, 3),
        "cues": manifest_cues,
    }
    manifest_path = output_dir / f"{asset_base}.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    browser_cues: dict[str, list[float]] = {}
    previous_spoken_end: float | None = None
    for key, value in manifest_cues.items():
        start = float(value["start"])
        safe_floor = (
            min(start, previous_spoken_end + 0.005)
            if previous_spoken_end is not None
            else 0.0
        )
        play_start = max(0.0, start - 0.12, safe_floor)
        browser_cues[key] = [
            round(play_start, 3),
            round(start, 3),
            float(value["spoken_end"]),
            float(value["end"]),
        ]
        previous_spoken_end = float(value["spoken_end"])

    browser_data = {
        "provider": manifest["provider"],
        "model_id": manifest["model_id"],
        "voice": manifest["voice"],
        "audio": f"assets/{asset_base}/{asset_base}.mp3",
        "cues": browser_cues,
    }
    browser_data_path = output_dir / f"{asset_base}-data.js"
    browser_data_path.write_text(
        f"window.{args.global_name}="
        + json.dumps(browser_data, ensure_ascii=False, separators=(",", ":"))
        + ";\n",
        encoding="utf-8",
    )
    print(f"Wrote {audio_path.relative_to(ROOT)} ({duration:.1f} seconds)")
    print(f"Wrote {manifest_path.relative_to(ROOT)}")
    print(f"Wrote {browser_data_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
