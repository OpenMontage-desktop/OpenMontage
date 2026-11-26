import tempfile
import unittest
from pathlib import Path

from lib.reference_style import (
    _cluster_cut_candidates,
    _parse_scene_candidates,
    _persistent_text_candidates,
    _source_entry,
    _summarize_frames,
    discover_media,
)


class ReferenceStyleTests(unittest.TestCase):
    def test_clusters_adjacent_scene_scores_and_keeps_strongest(self):
        events = _cluster_cut_candidates(
            [
                {"time_seconds": 1.0, "score": 0.41},
                {"time_seconds": 1.1, "score": 0.92},
                {"time_seconds": 2.0, "score": 0.62},
            ],
            0.2,
        )
        self.assertEqual(events, [
            {"time_seconds": 1.1, "score": 0.92},
            {"time_seconds": 2.0, "score": 0.62},
        ])

    def test_parses_scene_scores_from_ffmpeg_metadata(self):
        events = _parse_scene_candidates(
            "frame:0 pts:300 pts_time:0.25\nlavfi.scene_score=0.82\n"
            "frame:1 pts:600 pts_time:0.5\nlavfi.scene_score=0.41\n"
        )
        self.assertEqual(
            events,
            [
                {"time_seconds": 0.25, "score": 0.82},
                {"time_seconds": 0.5, "score": 0.41},
            ],
        )

    def test_visual_summary_ignores_cut_frames_for_motion_estimate(self):
        dark = bytes([12, 14, 18]) * (64 * 64)
        bright = bytes([240, 240, 240]) * (64 * 64)
        summary = _summarize_frames(
            [dark, bright, dark],
            [{"time_seconds": 0.25, "score": 0.9}, {"time_seconds": 0.5, "score": 0.9}],
        )
        self.assertEqual(summary["background_color"], "#080810")
        self.assertEqual(summary["motion_index"], 0.0)
        self.assertEqual(summary["motion_level"], "low")

    def test_persistent_text_is_reported_as_overlay_candidate(self):
        candidates = _persistent_text_candidates(
            [
                {"time_seconds": 1, "text": "OPEN MONTAGE", "confidence": 0.8, "box": [100, 800, 200, 30]},
                {"time_seconds": 2, "text": " open  montage ", "confidence": 0.9, "box": [102, 802, 200, 30]},
                {"time_seconds": 3, "text": "ONE OFF", "confidence": 0.95, "box": [20, 10, 80, 20]},
            ]
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["text"], "open montage")
        self.assertEqual(candidates[0]["occurrences"], 2)

    def test_discover_media_includes_images_and_videos_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "clip.MP4").touch()
            (root / "still.png").touch()
            (root / "notes.txt").touch()
            self.assertEqual(
                [path.name for path in discover_media(root)],
                ["clip.MP4", "still.png"],
            )

    def test_source_media_rejects_unsupported_extensions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "notes.txt"
            path.touch()
            with self.assertRaises(ValueError):
                _source_entry(path)


if __name__ == "__main__":
    unittest.main()
