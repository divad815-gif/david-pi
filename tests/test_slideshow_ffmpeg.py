import json
import os
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from modules.secure_storage import PinnedStorageRoot
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

import app as portal
from modules import slideshow_worker


class SlideshowFfmpegTest(unittest.TestCase):
    def setUp(self):
        # Own this fixture instead of retaining test_app's already-cleaned root.
        directory = tempfile.TemporaryDirectory(prefix="david-pi-ffmpeg-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        values = {"DATA": root, "DATA_STORAGE": PinnedStorageRoot(root)}
        for constant, folder, storage in (
            ("ORIGINALS", "originals", "ORIGINAL_STORAGE"),
            ("PREVIEWS", "previews", "PREVIEW_STORAGE"),
            ("VIEWER_PREVIEWS", "viewer-previews", "VIEWER_PREVIEW_STORAGE"),
            ("THUMBS", "thumbs", "THUMB_STORAGE"),
            ("INCOMING", "incoming", "INCOMING_STORAGE"),
            ("QUARANTINE", "quarantine", "QUARANTINE_STORAGE"),
        ):
            path = root / folder
            path.mkdir(mode=0o700)
            values[constant] = path
            values[storage] = PinnedStorageRoot(path)
        self.enterContext(patch.multiple(portal, **values))

    def test_worker_runs_real_ffmpeg_into_anonymous_bounded_output(self):
        descriptor = slideshow_worker._anonymous_file(portal)
        try:
            settings = SimpleNamespace(
                ffmpeg_seconds=30,
                max_job_seconds=40,
                renew_seconds=20,
                max_target_bytes=32 * 1024 * 1024,
                min_free_bytes=1,
                terminate_seconds=2,
            )
            slideshow_worker._run_ffmpeg(
                [
                    "ffmpeg", "-nostdin", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=navy:s=320x180:d=1",
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=1",
                    "-shortest", "-c:v", "libx264", "-c:a", "aac",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                    "-f", "mp4", f"/proc/self/fd/{descriptor}",
                ],
                (descriptor,),
                descriptor,
                {},
                settings,
                portal,
                lambda: False,
                time.monotonic() + 40,
            )
            os.fsync(descriptor)
            metadata = os.fstat(descriptor)
            self.assertGreater(metadata.st_size, 1000)
            self.assertEqual(metadata.st_nlink, 0)
        finally:
            os.close(descriptor)

    def test_balanced_frame_trims_letterbox_but_preserves_dark_images(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "letterbox.jpg"
            output = root / "balanced.jpg"
            letterbox = Image.new("RGB", (800, 600), "black")
            letterbox.paste(Image.new("RGB", (800, 400), "#c95f4b"), (0, 100))
            letterbox.save(source, quality=95)
            crop = portal.create_balanced_frame(source, output)
            self.assertIsNotNone(crop)
            self.assertGreaterEqual(crop[1], 90)
            self.assertLessEqual(crop[3], 510)
            with Image.open(output) as balanced:
                self.assertEqual(balanced.size, (1280, 720))
                self.assertGreater(balanced.getpixel((640, 360))[0], 150)

            dark = Image.new("RGB", (800, 600), (8, 8, 8))
            self.assertIsNone(portal.detect_dark_border_crop(dark))

    def test_mixed_media_music_and_audio_handoff_render(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.jpg"
            last = root / "last.jpg"
            clip = root / "clip.mp4"
            output = root / "output.mp4"
            Image.new("RGB", (500, 900), "#ef9f78").save(first)
            Image.new("RGB", (900, 500), "#729a7b").save(last)
            portal.run_media_command([
                "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=navy:s=640x360:d=1.5",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1.5",
                "-shortest", "-c:v", "libx264", "-c:a", "aac", str(clip),
            ])
            items = [
                {"is_video": False, "source_duration": 0, "has_audio": False},
                {"is_video": True, "source_duration": 1.5, "has_audio": True},
                {"is_video": False, "source_duration": 0, "has_audio": False},
            ]
            visual, durations, starts = portal.slideshow_filter(items, 6, "mixed", "fill")
            audio = portal.slideshow_audio_filter(items, durations, starts, 6, 3)
            arguments = ["ffmpeg", "-y"]
            arguments.extend(["-loop", "1", "-t", f"{durations[0]:.3f}", "-i", str(first)])
            arguments.extend(["-i", str(clip)])
            arguments.extend(["-loop", "1", "-t", f"{durations[2]:.3f}", "-i", str(last)])
            arguments.extend([
                "-stream_loop", "-1", "-i", str(portal.MUSIC_LIBRARY / "jrpg-piano.mp3"),
                "-filter_complex", ";".join([*visual, *audio]),
                "-map", "[outv]", "-map", "[outa]", "-t", "6",
                "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                "-pix_fmt", "yuv420p", str(output),
            ])
            render = subprocess.run(arguments, capture_output=True, text=True, timeout=60, check=False)
            self.assertEqual(render.returncode, 0, render.stderr)
            details = json.loads(portal.run_media_command([
                "ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
                "-of", "json", str(output),
            ]).stdout)
            self.assertGreater(output.stat().st_size, 10000)
            self.assertAlmostEqual(float(details["format"]["duration"]), 6, delta=0.2)
            self.assertEqual(
                {stream["codec_type"] for stream in details["streams"]},
                {"video", "audio"},
            )

    def test_video_derivatives_render_through_proc_descriptors(self):
        media_id = "ffmpeg-descriptor-output"
        preview_path = portal.PREVIEWS / f"{media_id}.jpg"
        thumb_path = portal.THUMBS / f"{media_id}.jpg"
        playback_path = portal.PREVIEWS / f"{media_id}.mp4"
        artifacts = []
        try:
            with tempfile.TemporaryDirectory() as directory:
                clip = Path(directory) / "clip.mov"
                portal.run_media_command([
                    "ffmpeg", "-y", "-f", "lavfi", "-i",
                    "color=c=teal:s=320x240:d=1",
                    "-f", "lavfi", "-i", "sine=frequency=330:duration=1",
                    "-shortest", "-c:v", "libx264", "-c:a", "aac", str(clip),
                ])
                preview, thumb, playback, artifacts = portal.prepare_video(
                    clip, media_id, ".mov"
                )

            self.assertEqual(preview, preview_path.name)
            self.assertEqual(thumb, thumb_path.name)
            self.assertEqual(playback, playback_path.name)
            for path in (preview_path, thumb_path, playback_path):
                self.assertFalse(path.exists())
            self.assertEqual(
                [artifact["kind"] for artifact in artifacts],
                ["preview", "thumb", "playback"],
            )
            playback_descriptor, playback_metadata = (
                portal.DATA_STORAGE.open_regular_path(
                    artifacts[-1]["source_path"]
                )
            )
            try:
                self.assertGreater(playback_metadata.st_size, 0)
                details = json.loads(portal.run_media_command([
                    "ffprobe", "-v", "error", "-show_entries",
                    "format=duration:stream=codec_type", "-of", "json",
                    f"/proc/self/fd/{playback_descriptor}",
                ]).stdout)
            finally:
                os.close(playback_descriptor)
            self.assertGreater(float(details["format"]["duration"]), 0.5)
            self.assertIn(
                "video", {stream["codec_type"] for stream in details["streams"]}
            )
        finally:
            for artifact in artifacts:
                portal._unlink_staged_artifact(artifact["source_path"])


if __name__ == "__main__":
    unittest.main()
