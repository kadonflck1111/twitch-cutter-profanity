"""Проверка реального FFmpeg: кадры, A/V синхронизация, форматы и отмена."""

import json
import shutil
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np

from censor_engine import build_ffmpeg_mute_filter, process_video_censorship
from media_utils import ProcessingCancelled, capture_process, filter_script_args, media_tool, probe_media, run_ffmpeg
from speech_engine import SpeechSegment, SpeechWord, decode_audio_chunk
from stream_cutter import export_cut_video


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "Нужен FFmpeg в PATH")
class MediaIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="cutter_tests_")
        cls.root = Path(cls.temp.name)
        cls.source = cls.root / "Видео 'тест'.mp4"
        run_ffmpeg([
            "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=6",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=6",
            "-c:v", "libx264", "-preset", "ultrafast", "-g", "60", "-bf", "2",
            "-c:a", "aac", cls.source,
        ])
        cls.wav = cls.root / "audio.wav"
        run_ffmpeg(["-i", cls.source, "-vn", "-c:a", "pcm_s16le", cls.wav])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_cut_has_expected_frames_duration_and_sync(self):
        output = self.root / "cut.mp4"
        logs = []
        intervals = [(0.5, 1.5), (3.1, 4.3), (5.2, 5.8)]
        self.assertTrue(export_cut_video(self.source, intervals, output, logs.append), "\n".join(logs))
        info = probe_media(output)
        self.assertAlmostEqual(info.duration, 2.8, delta=0.07)
        data = json.loads(capture_process([
            media_tool("ffprobe"), "-v", "error", "-count_frames", "-show_streams", "-of", "json", str(output),
        ]))
        video = next(s for s in data["streams"] if s["codec_type"] == "video")
        audio = next(s for s in data["streams"] if s["codec_type"] == "audio")
        self.assertEqual(int(video["nb_read_frames"]), 84)
        self.assertAlmostEqual(float(video.get("start_time", 0)), float(audio.get("start_time", 0)), delta=0.04)
        self.assertAlmostEqual(float(video["duration"]), float(audio["duration"]), delta=0.07)

    def test_audio_only_cut(self):
        output = self.root / "audio_cut.mp4"
        logs = []
        self.assertTrue(export_cut_video(self.wav, [(0.5, 1.5), (3, 4)], output, logs.append), "\n".join(logs))
        info = probe_media(output)
        self.assertTrue(info.has_audio)
        self.assertFalse(info.has_video)
        self.assertAlmostEqual(info.duration, 2, delta=0.04)

    def test_video_only_cut(self):
        source = self.root / "silent.mp4"
        output = self.root / "silent_cut.mp4"
        run_ffmpeg(["-i", self.source, "-an", "-c:v", "copy", source])
        self.assertTrue(export_cut_video(source, [(1, 2), (4, 5)], output))
        self.assertFalse(probe_media(output).has_audio)
        self.assertAlmostEqual(probe_media(output).duration, 2, delta=0.04)

    def test_full_length_fast_path_preserves_encoded_video(self):
        output = self.root / "copy.mp4"
        self.assertTrue(export_cut_video(self.source, [(0, 6)], output))
        hashes = []
        for path in (self.source, output):
            hashes.append(capture_process([media_tool("ffmpeg"), "-v", "error", "-i", str(path),
                                           "-map", "0:v:0", "-c", "copy", "-f", "hash", "-hash", "sha256", "-"]))
        self.assertEqual(*hashes)

    def test_mute_filter_silences_only_requested_time(self):
        output = self.root / "mute.wav"
        script = self.root / "mute.txt"
        script.write_text(f"[0:a]{build_ffmpeg_mute_filter([(1, 2), (4, 4.5)])}[out]", encoding="utf-8")
        run_ffmpeg(["-i", self.wav, *filter_script_args(script), "-map", "[out]", "-c:a", "pcm_s16le", output])
        with wave.open(str(output)) as stream:
            rate = stream.getframerate()
            audio = np.frombuffer(stream.readframes(stream.getnframes()), dtype="<i2")
        for start, end in ((1.02, 1.98), (4.02, 4.48)):
            self.assertEqual(np.max(np.abs(audio[round(start * rate):round(end * rate)])), 0)
        for start, end in ((0.2, 0.9), (2.1, 3.9), (4.6, 5.8)):
            self.assertGreater(np.max(np.abs(audio[round(start * rate):round(end * rate)])), 1000)

    def test_large_mute_filter_avoids_command_line_limit(self):
        script = self.root / "large_filter.txt"
        intervals = [(i * 0.01, i * 0.01 + 0.003) for i in range(4000)]
        script.write_text(f"[0:a]{build_ffmpeg_mute_filter(intervals)}[out]", encoding="utf-8")
        self.assertGreater(script.stat().st_size, 32768)
        run_ffmpeg(["-i", self.wav, *filter_script_args(script), "-map", "[out]", "-f", "null", "-"])

    def test_censor_webm_uses_supported_audio_codec(self):
        source = self.root / "source.webm"
        run_ffmpeg(["-i", self.source, "-t", "2", "-c:v", "libvpx-vp9", "-deadline", "realtime",
                    "-cpu-used", "8", "-c:a", "libopus", source])
        segment = SpeechSegment(0.5, 1.2, "блять", [SpeechWord(0.5, 1.2, "блять")])
        with patch("censor_engine.analyze_media", return_value=iter([segment])):
            success, output, count = process_video_censorship(source, object(), leave_end_sec=0)
        self.assertTrue(success)
        self.assertEqual(count, 1)
        self.assertEqual(probe_media(output).audio_codec, "opus")

    def test_decode_from_nonzero_offset_has_no_leading_silence(self):
        audio = decode_audio_chunk(self.source, 2, 1)
        self.assertEqual(len(audio), 16000)
        self.assertGreater(float(np.max(np.abs(audio[:800]))), 0.05)

    def test_cancellation_terminates_ffmpeg_quickly(self):
        stop = threading.Event()
        timer = threading.Timer(0.4, stop.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(ProcessingCancelled):
                run_ffmpeg(["-re", "-f", "lavfi", "-i", "color=size=320x240:rate=30:duration=30",
                            "-f", "null", "-"], stop_check=stop.is_set)
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 3)


if __name__ == "__main__":
    unittest.main()
