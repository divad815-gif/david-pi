import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from PIL import Image

import app as portal


class SlideshowFfmpegTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
