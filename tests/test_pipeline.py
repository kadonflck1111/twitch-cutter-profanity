"""Регрессии окон Whisper, повторов после OOM и передачи результатов в интерфейс."""

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from media_utils import ProcessingCancelled
from speech_engine import SpeechSegment, SpeechWord, _transcribe_chunk, analyze_media
from stream_cutter import compute_stream_cuts
from transcribe import save_transcription
from web_app import Bridge


class SpeechPipelineTests(unittest.TestCase):
    def test_overlapping_windows_keep_words_once_with_absolute_times(self):
        first = SpeechSegment(4, 6, words=[SpeechWord(4.6, 5.1, " Вместе"), SpeechWord(5.4, 6, " идем.")])
        second = SpeechSegment(0, 2, words=[SpeechWord(0.8, 1.3, " Вместе"), SpeechWord(1.4, 2, " идем.")])
        with patch("speech_engine.decode_audio_chunk", return_value=[0]) as decode, \
                patch("speech_engine._transcribe_chunk", side_effect=[[first], [second]]):
            segments = list(analyze_media(Path("speech.wav"), 10, object(), chunk_seconds=5))
        words = [word for segment in segments for word in segment.words]
        self.assertEqual([word.word for word in words], [" Вместе", " идем."])
        self.assertEqual([(word.start, word.end) for word in words], [(4.6, 5.1), (5.4, 6)])
        self.assertEqual([(call.args[1], call.args[2]) for call in decode.call_args_list], [(0, 6), (4, 6)])

    def test_oom_retry_discards_partial_attempt_and_reduces_batch(self):
        model = SimpleNamespace(_cutter_batch_size=8)
        segment = SpeechSegment(0, 1, "Речь")

        def failing_generator():
            yield segment
            raise RuntimeError("CUDA failed to allocate: out of memory")

        pipeline = SimpleNamespace(last_speech_timestamp=100,
                                   transcribe=Mock(side_effect=[(failing_generator(), None), (iter([segment]), None)]))
        result = _transcribe_chunk([0], model, pipeline, None, None, lambda _: None)
        self.assertEqual(result, [segment])
        self.assertEqual([call.kwargs["batch_size"] for call in pipeline.transcribe.call_args_list], [8, 4])
        self.assertEqual(model._cutter_batch_size, 4)
        self.assertEqual(pipeline.last_speech_timestamp, 0)

    def test_cancellation_during_inference_closes_generator(self):
        stop, closed = threading.Event(), threading.Event()

        def generate():
            try:
                stop.set()
                yield SpeechSegment(0, 1)
            finally:
                closed.set()

        pipeline = SimpleNamespace(last_speech_timestamp=0, transcribe=Mock(return_value=(generate(), None)))
        with self.assertRaises(ProcessingCancelled):
            _transcribe_chunk([0], object(), pipeline, None, stop.is_set, lambda _: None)
        self.assertTrue(closed.is_set())
        self.assertEqual(pipeline.transcribe.call_count, 1)

    def test_donation_does_not_remove_neighboring_sentences(self):
        words = [SpeechWord(0, 1, " Играем."), SpeechWord(1, 1.5, " Спасибо"),
                 SpeechWord(1.5, 1.7, " за"), SpeechWord(1.7, 2.2, " донат!"),
                 SpeechWord(2.2, 3, " Продолжаем.")]
        kept, removed = compute_stream_cuts([SpeechSegment(0, 3, words=words)], 3, None)
        self.assertEqual(kept, [(0, 1), (2.2, 3)])
        self.assertEqual([item["text"] for item in removed], ["Спасибо за донат!"])

    def test_dotted_transcription_name_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            segment = SimpleNamespace(id=1, start=59.9996, end=61, text="Текст")
            info = SimpleNamespace(language="ru", language_probability=1, duration=61)
            save_transcription([segment], info, Path(folder) / "stream.2026.10", ["srt"])
            path = Path(folder) / "stream.2026.10.srt"
            self.assertIn("00:01:00,000 --> 00:01:01,000", path.read_text(encoding="utf-8"))


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.mp4"
        self.source.touch()
        self.bridge = Bridge()
        self.bridge._get_model_and_pipeline = Mock(return_value=(object(), object()))

    def run_job(self, mode, options):
        self.assertTrue(self.bridge.start_processing(mode, str(self.source), "small", options))
        self.bridge._worker_thread.join(timeout=5)
        self.assertFalse(self.bridge._worker_thread.is_alive())
        return list(self.bridge._messages)

    def test_failed_export_is_reported_and_folder_is_not_opened(self):
        with patch("web_app.process_video_censorship", return_value=(False, None, 7)), \
                patch.object(self.bridge, "open_result_folder") as open_folder:
            messages = self.run_job("censor", {})
        done = next(message for message in messages if message["type"] == "done")
        self.assertEqual((done["succeeded"], done["failed"], done["hits"]), (0, 1, 0))
        self.assertEqual(done["outputs"], [])
        open_folder.assert_not_called()

    def test_zero_hits_have_report_without_fabricated_media(self):
        report = self.root / "source_отчет_цензуры.txt"
        report.write_text("0", encoding="utf-8")
        with patch("web_app.process_video_censorship", return_value=(True, None, 0)):
            messages = self.run_job("censor", {"openAfter": False})
        done = next(message for message in messages if message["type"] == "done")
        self.assertEqual(done["hits"], 0)
        self.assertEqual(done["outputs"], [str(report)])
        self.assertEqual(done["succeeded"], 1)

    def test_stream_options_and_custom_output_reach_engine(self):
        options = {"donations": False, "chat": False, "silence": False, "xml": True,
                   "mp4": False, "keepQuality": False, "saveNear": False,
                   "outputDir": str(self.root / "exports"), "openAfter": False}
        with patch("web_app.process_stream_cut", return_value=(True, None, None)) as process:
            self.run_job("stream", options)
        self.bridge._get_model_and_pipeline.assert_not_called()
        args = process.call_args.kwargs
        self.assertIsNone(args["model"])
        self.assertIsNone(args["min_silence_duration"])
        self.assertFalse(args["cut_donations"])
        self.assertFalse(args["cut_chat"])
        self.assertFalse(args["export_mp4"])
        self.assertFalse(args["high_quality"])
        self.assertEqual(args["output_dir"], self.root / "exports")

    def test_batch_continues_after_one_file_fails(self):
        second = self.root / "second.mp4"
        second.touch()
        with patch("web_app.process_video_censorship", side_effect=[RuntimeError("bad input"), (True, None, 0)]):
            self.assertTrue(self.bridge.start_processing("censor", str(self.root), "small", {"openAfter": False}))
            self.bridge._worker_thread.join(timeout=5)
        results = [m for m in self.bridge._messages if m["type"] == "file_result"]
        self.assertEqual([m["success"] for m in results], [False, True])
        done = next(m for m in self.bridge._messages if m["type"] == "done")
        self.assertEqual((done["succeeded"], done["failed"]), (1, 1))

    def test_stop_rejects_concurrent_job_and_emits_stopped(self):
        entered = threading.Event()

        def process(**kwargs):
            entered.set()
            self.bridge._stop_event.wait(timeout=5)
            raise ProcessingCancelled()

        with patch("web_app.process_video_censorship", side_effect=process):
            self.assertTrue(self.bridge.start_processing("censor", str(self.source), "small"))
            self.assertTrue(entered.wait(timeout=5))
            self.assertFalse(self.bridge.start_processing("censor", str(self.source), "small"))
            self.bridge.stop_processing()
            self.bridge._worker_thread.join(timeout=5)
        self.assertFalse(self.bridge._worker_thread.is_alive())
        self.assertEqual([m["type"] for m in self.bridge._messages][-1], "stopped")
        self.assertFalse(any(m["type"] == "done" for m in self.bridge._messages))

    def test_explorer_failure_does_not_turn_success_into_processing_error(self):
        with patch("web_app.process_video_censorship", return_value=(True, None, 0)), \
                patch.object(self.bridge, "open_result_folder", side_effect=OSError("Explorer unavailable")):
            messages = self.run_job("censor", {})
        self.assertFalse(any(m["type"] == "error" for m in messages))
        self.assertEqual(next(m for m in messages if m["type"] == "done")["succeeded"], 1)
        self.assertTrue(any(m.get("level") == "warn" for m in messages))


if __name__ == "__main__":
    unittest.main()
