"""Analyze a reference video and produce a reusable montage style profile.

Requires FFmpeg/ffprobe on PATH. Tesseract is optional and is used only for
detecting persistent on-screen text; no model or network service is required.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import statistics
import subprocess
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


_VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v"}
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"}
_FRAME_WIDTH = 64
_FRAME_HEIGHT = 64
_FRAME_SIZE = _FRAME_WIDTH * _FRAME_HEIGHT * 3
_SCENE_LINE = re.compile(
    r"pts_time:([0-9.]+).*?lavfi\.scene_score=([0-9.]+)", re.DOTALL
)


class ReferenceStyleAnalysisError(RuntimeError):
    """Raised when a required video analysis step fails."""


def discover_media(directory: str | Path) -> list[Path]:
    """Return supported video and image files beneath a media directory."""
    root = Path(directory)
    if not root.is_dir():
        raise NotADirectoryError(f"Media directory does not exist: {root}")
    supported = _VIDEO_EXTENSIONS | _IMAGE_EXTENSIONS
    return sorted(
        (path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in supported),
        key=lambda path: str(path).lower(),
    )


def _run(
    command: list[str], *, timeout: int = 120, text: bool = True
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command, capture_output=True, text=text, timeout=timeout
        )
    except FileNotFoundError as exc:
        raise ReferenceStyleAnalysisError(
            f"Required media tool not found: {command[0]}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ReferenceStyleAnalysisError(
            f"Media analysis timed out running {command[0]}"
        ) from exc


def _probe_video(path: Path, ffprobe: str) -> dict[str, Any]:
    result = _run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate",
            "-of",
            "json",
            str(path),
        ]
    )
    if result.returncode:
        raise ReferenceStyleAnalysisError(
            f"Could not probe {path}: {result.stderr.strip()}"
        )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ReferenceStyleAnalysisError(f"Invalid ffprobe output for {path}") from exc

    video = next(
        (stream for stream in data.get("streams", []) if stream.get("codec_type") == "video"),
        None,
    )
    if not video:
        raise ValueError(f"Reference contains no video stream: {path}")
    try:
        duration = float(data.get("format", {}).get("duration", 0))
        width, height = int(video["width"]), int(video["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ReferenceStyleAnalysisError(f"Incomplete video metadata for {path}") from exc
    if duration <= 0 or width <= 0 or height <= 0:
        raise ValueError(f"Reference has invalid duration or dimensions: {path}")
    return {
        "duration_seconds": round(duration, 3),
        "width": width,
        "height": height,
        "fps": _parse_rate(video.get("r_frame_rate", "0/1")),
        "codec": video.get("codec_name", "unknown"),
    }


def _parse_rate(value: str) -> float:
    try:
        numerator, denominator = value.split("/", 1)
        return round(float(numerator) / float(denominator), 3) if float(denominator) else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def _detect_scene_changes(
    path: Path,
    ffmpeg: str,
    threshold: float,
    fps: float,
) -> list[dict[str, float]]:
    result = _run(
        [
            ffmpeg,
            "-hide_banner",
            "-i",
            str(path),
            "-vf",
            f"select='gt(scene,{threshold})',metadata=print",
            "-an",
            "-f",
            "null",
            "-",
        ]
    )
    if result.returncode:
        raise ReferenceStyleAnalysisError(
            f"Could not analyze scene changes in {path}: {result.stderr.strip()}"
        )
    return _cluster_cut_candidates(
        _parse_scene_candidates(result.stderr),
        max(0.18, 2 / fps) if fps else 0.18,
    )


def _parse_scene_candidates(log: str) -> list[dict[str, float]]:
    candidates = [
        {"time_seconds": float(match.group(1)), "score": float(match.group(2))}
        for match in _SCENE_LINE.finditer(log)
    ]
    return candidates


def _cluster_cut_candidates(
    candidates: list[dict[str, float]], cluster_gap_seconds: float
) -> list[dict[str, float]]:
    """Collapse adjacent high-scene-score frames into one cut candidate."""
    clusters: list[list[dict[str, float]]] = []
    for candidate in sorted(candidates, key=lambda item: item["time_seconds"]):
        if (
            not clusters
            or candidate["time_seconds"] - clusters[-1][-1]["time_seconds"]
            > cluster_gap_seconds
        ):
            clusters.append([candidate])
        else:
            clusters[-1].append(candidate)
    return [
        max(cluster, key=lambda item: item["score"])
        for cluster in clusters
    ]


def _extract_sample_frames(path: Path, ffmpeg: str) -> list[bytes]:
    vf = (
        "fps=4,"
        f"scale={_FRAME_WIDTH}:{_FRAME_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={_FRAME_WIDTH}:{_FRAME_HEIGHT}:(ow-iw)/2:(oh-ih)/2,"
        "format=rgb24"
    )
    result = _run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-vf",
            vf,
            "-an",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        text=False,
    )
    if result.returncode:
        raise ReferenceStyleAnalysisError(
            f"Could not sample frames from {path}: {result.stderr.strip()}"
        )
    raw = result.stdout
    if len(raw) < _FRAME_SIZE:
        raise ReferenceStyleAnalysisError(f"No sample frames could be decoded from {path}")
    return [
        raw[offset : offset + _FRAME_SIZE]
        for offset in range(0, len(raw) - _FRAME_SIZE + 1, _FRAME_SIZE)
    ]


def _color_hex(color: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*color)


def _summarize_frames(
    frames: list[bytes], cuts: list[dict[str, float]]
) -> dict[str, Any]:
    pixel_colors: Counter[tuple[int, int, int]] = Counter()
    luminance_values: list[float] = []
    saturation_values: list[float] = []
    pixels_per_frame = _FRAME_WIDTH * _FRAME_HEIGHT

    for frame in frames:
        for offset in range(0, len(frame), 3):
            red, green, blue = frame[offset : offset + 3]
            color = (red >> 3, green >> 3, blue >> 3)
            pixel_colors[color] += 1
            luminance_values.append((0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255)
            high, low = max(red, green, blue), min(red, green, blue)
            saturation_values.append((high - low) / high if high else 0.0)

    dominant = [
        tuple(round(channel * 255 / 31) for channel in color)
        for color, _ in pixel_colors.most_common(6)
    ]
    saturated = [
        tuple(round(channel * 255 / 31) for channel in color)
        for color, count in pixel_colors.most_common()
        if count >= max(2, len(frames) * pixels_per_frame // 1000)
        and max(color) > 0
        and (max(color) - min(color)) / max(color) >= 0.28
    ]
    background = min(dominant, key=lambda color: _luminance(color)) if dominant else (0, 0, 0)
    accent = saturated[0] if saturated else None

    changes = []
    for index, (previous, current) in enumerate(zip(frames, frames[1:]), start=1):
        sample_time = index / 4
        if any(abs(sample_time - cut["time_seconds"]) <= 0.2 for cut in cuts):
            continue
        difference = sum(abs(a - b) for a, b in zip(previous, current))
        changes.append(difference / (len(previous) * 255))
    motion_index = round(statistics.median(changes), 4) if changes else 0.0
    average_luminance = statistics.mean(luminance_values) if luminance_values else 0.0
    contrast = statistics.pstdev(luminance_values) if len(luminance_values) > 1 else 0.0

    return {
        "average_luminance": round(average_luminance, 3),
        "contrast_index": round(contrast, 3),
        "average_saturation": round(statistics.mean(saturation_values), 3) if saturation_values else 0,
        "dominant_colors": [_color_hex(color) for color in dominant],
        "background_color": _color_hex(background),
        "accent_color": _color_hex(accent) if accent else None,
        "motion_index": motion_index,
        "motion_level": "high" if motion_index >= 0.12 else "moderate" if motion_index >= 0.045 else "low",
    }


def _luminance(color: tuple[int, int, int]) -> float:
    return 0.2126 * color[0] + 0.7152 * color[1] + 0.0722 * color[2]


def _analyze_text_overlays(
    path: Path, ffmpeg: str, tesseract: str | None
) -> dict[str, Any]:
    if not tesseract:
        return {
            "detector": "tesseract OCR",
            "available": False,
            "text_candidates": [],
            "note": "Install Tesseract to detect persistent text overlays.",
        }

    hits: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="openmontage-style-") as temp_dir:
        pattern = str(Path(temp_dir) / "frame-%03d.png")
        result = _run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-vf",
                "fps=1,scale=960:-2",
                "-frames:v",
                "24",
                pattern,
            ]
        )
        if result.returncode:
            raise ReferenceStyleAnalysisError(
                f"Could not extract OCR samples from {path}: {result.stderr.strip()}"
            )
        for frame_path in sorted(Path(temp_dir).glob("frame-*.png")):
            ocr = _run([tesseract, str(frame_path), "stdout", "tsv"], timeout=30)
            if ocr.returncode:
                raise ReferenceStyleAnalysisError(
                    f"Tesseract failed for sample frame {frame_path.name}: "
                    f"{ocr.stderr.strip()}"
                )
            for row in ocr.stdout.splitlines()[1:]:
                columns = row.split("\t")
                if len(columns) < 12:
                    continue
                try:
                    confidence = float(columns[10])
                    text = columns[11].strip()
                    left, top, width, height = map(int, columns[6:10])
                except ValueError:
                    continue
                if text and confidence >= 55:
                    hits.append(
                        {
                            "time_seconds": int(frame_path.stem.rsplit("-", 1)[-1]),
                            "text": text,
                            "confidence": round(confidence / 100, 2),
                            "box": [left, top, width, height],
                        }
                    )

    candidates = _persistent_text_candidates(hits)
    return {
        "detector": "tesseract OCR",
        "available": True,
        "text_candidates": candidates,
        "note": "OCR finds text only; logos and non-text graphic overlays are not detected.",
    }


def _persistent_text_candidates(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for hit in hits:
        normalized = " ".join(hit["text"].lower().split())
        if normalized:
            grouped[normalized].append(hit)
    candidates = []
    for text, matches in grouped.items():
        repeated_frames = {
            match["time_seconds"]: match for match in matches
        }
        if len(repeated_frames) < 2:
            continue
        matches = list(repeated_frames.values())
        boxes = [match["box"] for match in matches]
        center_x = statistics.mean(box[0] + box[2] / 2 for box in boxes)
        center_y = statistics.mean(box[1] + box[3] / 2 for box in boxes)
        if any(
            abs(box[0] + box[2] / 2 - center_x) > 24
            or abs(box[1] + box[3] / 2 - center_y) > 24
            for box in boxes
        ):
            continue
        candidates.append(
            {
                "text": text,
                "occurrences": len(matches),
                "center": [round(center_x), round(center_y)],
                "confidence": round(statistics.mean(match["confidence"] for match in matches), 2),
            }
        )
    return sorted(candidates, key=lambda item: item["occurrences"], reverse=True)


def _source_entry(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix not in _VIDEO_EXTENSIONS | _IMAGE_EXTENSIONS:
        raise ValueError(f"Unsupported source media type: {path}")
    return {
        "path": str(path.resolve()),
        "media_type": "video" if suffix in _VIDEO_EXTENSIONS else "image",
        "size_bytes": path.stat().st_size,
    }


def _pace_for_duration(duration: float) -> str:
    return "fast" if duration < 1.5 else "moderate" if duration < 3.5 else "slow"


def analyze_reference_style(
    reference_video: str | Path,
    *,
    source_media: Iterable[str | Path] = (),
    output_path: str | Path | None = None,
    scene_threshold: float = 0.32,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    tesseract: str | None = "tesseract",
) -> dict[str, Any]:
    """Analyze a reference and return a reusable montage style profile.

    ``source_media`` accepts any existing video/image paths, including later
    user uploads; these are cataloged as editable source assets, not modified.
    """
    reference = Path(reference_video).expanduser()
    if not reference.is_file():
        raise FileNotFoundError(f"Reference video does not exist: {reference}")
    if not 0 < scene_threshold < 1:
        raise ValueError("scene_threshold must be greater than 0 and less than 1")
    tesseract_path = shutil.which(tesseract) if tesseract else None
    probe = _probe_video(reference, ffprobe)
    cuts = _detect_scene_changes(reference, ffmpeg, scene_threshold, probe["fps"])
    frames = _extract_sample_frames(reference, ffmpeg)
    visual = _summarize_frames(frames, cuts)
    intervals = [
        round(right["time_seconds"] - left["time_seconds"], 3)
        for left, right in zip(cuts, cuts[1:])
        if right["time_seconds"] > left["time_seconds"]
    ]
    shot_median = round(statistics.median(intervals), 3) if intervals else probe["duration_seconds"]
    shot_range = (
        [round(statistics.quantiles(intervals, n=4)[0], 3),
         round(statistics.quantiles(intervals, n=4)[2], 3)]
        if len(intervals) >= 2
        else [shot_median, shot_median]
    )

    sources = []
    seen = {reference.resolve()}
    for value in source_media:
        source = Path(value).expanduser()
        if not source.is_file():
            raise FileNotFoundError(f"Source media does not exist: {source}")
        resolved = source.resolve()
        if resolved not in seen:
            sources.append(_source_entry(source))
            seen.add(resolved)

    aspect_ratio = round(probe["width"] / probe["height"], 3)
    pace = _pace_for_duration(shot_median)
    overlays = _analyze_text_overlays(reference, ffmpeg, tesseract_path)
    profile = {
        "schema_version": 1,
        "reference": {"path": str(reference.resolve()), **probe},
        "source_media": sources,
        "analysis": {
            "scene_detector": "FFmpeg scene score",
            "scene_threshold": scene_threshold,
            "cut_candidates": [
                {"time_seconds": round(cut["time_seconds"], 3), "score": round(cut["score"], 3)}
                for cut in cuts
            ],
            "cut_count": len(cuts),
            "shot_duration_median_seconds": shot_median,
            "shot_duration_p25_p75_seconds": shot_range,
            "transition_profile": "hard-cut-led" if cuts else "no clear hard cuts detected",
            "transition_note": "Scene-score candidates identify abrupt changes; fades and dissolves may be undercounted.",
        },
        "style": {
            "pace": pace,
            "aspect_ratio": aspect_ratio,
            "lighting": "low-key" if visual["average_luminance"] < 0.35 else "balanced" if visual["average_luminance"] < 0.65 else "high-key",
            **visual,
        },
        "overlays": overlays,
        "montage_config": {
            "target_shot_duration_seconds": shot_median,
            "shot_duration_range_seconds": shot_range,
            "cut_style": "hard_cut",
            "transition_duration_seconds": 0.0,
            "frame_rate": round(probe["fps"]) if probe["fps"] else 30,
            "aspect_ratio": "9:16" if aspect_ratio < 0.8 else "16:9" if aspect_ratio > 1.2 else "1:1",
            "color_palette": {
                "background": visual["background_color"],
                "accent": visual["accent_color"],
                "dominant": visual["dominant_colors"],
            },
            "motion_level": visual["motion_level"],
            "overlay_text_candidates": [
                item["text"] for item in overlays["text_candidates"]
            ],
            "style_transfer_scope": "Match broad pacing, palette, and motion traits; use original footage and graphics.",
        },
    }
    if output_path:
        destination = Path(output_path).expanduser()
        if destination.resolve() in seen:
            raise ValueError("The profile output path must not overwrite input media.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    return profile


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference_video", type=Path)
    parser.add_argument("--source", type=Path, action="append", default=[], help="Editable source media; repeat as needed.")
    parser.add_argument("--sources-dir", type=Path, help="Recursively include supported source media from a directory.")
    parser.add_argument("--output", type=Path, default=Path("reference-style.json"))
    parser.add_argument("--scene-threshold", type=float, default=0.32)
    args = parser.parse_args(argv)
    sources = list(args.source)
    if args.sources_dir:
        sources.extend(discover_media(args.sources_dir))
    profile = analyze_reference_style(
        args.reference_video,
        source_media=sources,
        output_path=args.output,
        scene_threshold=args.scene_threshold,
    )
    print(json.dumps({"output": str(args.output.resolve()), "cuts": profile["analysis"]["cut_count"], "pace": profile["style"]["pace"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
