"""Регрессии таймкодов, правил вырезания и сообщений об ошибках."""

import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from censor_engine import format_simple_timestamp
from davinci_xml import create_davinci_xml
from media_utils import MediaInfo, ProcessingCancelled, atomic_output, find_media_files
from profanity_filter import calculate_gentle_mute_range, find_profanities_in_words, is_profane_word
from speech_engine import SpeechSegment, SpeechWord
from stream_cutter import compute_stream_cuts, process_stream_cut


class ProfanityTests(unittest.TestCase):
    def test_normal_word_after_profanity_is_not_muted(self):
        words = [SpeechWord(1, 1.4, " пиздец"), SpeechWord(1.5, 2, " сегодня")]
        found = find_profanities_in_words(words, leave_end_sec=0)
        self.assertEqual([(item["start"], item["end"]) for item in found], [(1, 1.4)])

    def test_bigram_does_not_span_a_long_pause(self):
        found = find_profanities_in_words([SpeechWord(0, 0.2, "на"), SpeechWord(3, 3.5, "хуй")])
        self.assertEqual(len(found), 1)
        self.assertGreaterEqual(found[0]["start"], 3)

    def test_short_words_never_mute_neighboring_audio(self):
        for duration in (0.01, 0.05, 0.15, 0.2, 0.5):
            with self.subTest(duration=duration):
                start, end = calculate_gentle_mute_range(1, 1 + duration)
                self.assertTrue(1 <= start < end <= 1 + duration)

    def test_strict_mode_mutes_whole_word(self):
        self.assertEqual(calculate_gentle_mute_range(1, 1.05, 0), (1, 1.05))

    def test_masked_and_normal_words(self):
        for word in ("х**", "б***ь", "п****ц", "заебись"):
            self.assertTrue(is_profane_word(word), word)
        for word in ("тебе", "себе", "ребята", "Херсон", "корабля", "мебель", "***"):
            self.assertFalse(is_profane_word(word), word)
        found = find_profanities_in_words([SpeechWord(0, 0.2, "на"), SpeechWord(0.2, 0.8, "Херсон")])
        self.assertEqual(found, [])

    def test_custom_stopwords_are_exact_and_override_whitelist(self):
        self.assertTrue(is_profane_word("тебе", {"тебе"}))
        self.assertFalse(is_profane_word("блинчики", {"блин"}))
        self.assertFalse(is_profane_word("сука"))
        self.assertTrue(is_profane_word("сука", include_soft=True))

    def test_timestamp_carries_minutes_and_hours(self):
        self.assertEqual(format_simple_timestamp(59.9996), "00:01:00.000")
        self.assertEqual(format_simple_timestamp(3599.9996), "01:00:00.000")


class CutTests(unittest.TestCase):
    def test_donation_is_not_merged_back_into_speech(self):
        segments = [SpeechSegment(0, 2, "Играем"), SpeechSegment(2, 2.8, "спасибо за донат"), SpeechSegment(2.8, 5, "Дальше")]
        kept, removed = compute_stream_cuts(segments, 5)
        self.assertEqual(kept, [(0, 2), (2.8, 5)])
        self.assertEqual(len(removed), 1)

    def test_all_donations_yield_empty_timeline(self):
        kept, _ = compute_stream_cuts([SpeechSegment(0, 5, "спасибо за донат")], 5)
        self.assertEqual(kept, [])

    def test_disabled_silence_preserves_both_edges(self):
        kept, _ = compute_stream_cuts([SpeechSegment(3, 4, "Речь")], 10, None, cut_donations=False)
        self.assertEqual(kept, [(0, 10)])

    def test_donations_and_chat_are_independent(self):
        segments = [SpeechSegment(1, 2, "читаю чат"), SpeechSegment(3, 4, "спасибо за донат")]
        kept, _ = compute_stream_cuts(segments, 5, None, cut_donations=False, cut_chat=True)
        self.assertEqual(kept, [(0, 1), (2, 5)])
        kept, _ = compute_stream_cuts(segments, 5, None, cut_donations=True, cut_chat=False)
        self.assertEqual(kept, [(0, 3), (4, 5)])

    def test_pause_threshold_is_checked_before_padding(self):
        kept, _ = compute_stream_cuts([SpeechSegment(0, 1), SpeechSegment(2.6, 4)], 4, 1.5, 0.25, False)
        self.assertEqual(kept, [(0, 1.25), (2.35, 4)])

    def test_word_timestamps_find_pause_inside_segment(self):
        segment = SpeechSegment(0, 5, "Начало конец", [SpeechWord(0, 1, "Начало"), SpeechWord(4, 5, "конец")])
        kept, _ = compute_stream_cuts([segment], 5, 1.5, 0.25, False)
        self.assertEqual(kept, [(0, 1.25), (3.75, 5)])

    def test_invalid_options_are_rejected(self):
        for threshold, pad in ((0, 0), (float("nan"), 0), (1.5, -1)):
            with self.assertRaises(ValueError):
                compute_stream_cuts([], 5, threshold, pad)

    def test_render_failure_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.mp4"
            info = MediaInfo(5, 30, 320, 240, "h264", "aac", 48000, 2)
            with patch("stream_cutter.probe_media", return_value=info), patch("stream_cutter.export_cut_video", return_value=False):
                result = process_stream_cut(source, min_silence_duration=None, cut_donations=False, cut_chat=False, export_xml=False)
            self.assertEqual(result, (False, None, None))
            self.assertFalse(list(Path(folder).glob("*_отчет_*.txt")))


class FileAndXmlTests(unittest.TestCase):
    def test_failed_export_preserves_previous_output(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "result.mp4"
            target.write_bytes(b"previous")
            with self.assertRaises(ProcessingCancelled):
                with atomic_output(target) as temporary:
                    temporary.write_bytes(b"partial")
                    raise ProcessingCancelled()
            self.assertEqual(target.read_bytes(), b"previous")
            self.assertEqual(list(Path(folder).iterdir()), [target])

    def test_batch_skips_both_generated_media_suffixes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ("source.MP4", "source_clean.mp4", "source_нарезка.mp4", "readme.txt"):
                (root / name).touch()
            self.assertEqual([p.name for p in find_media_files(root)], ["source.MP4"])

    def test_ntsc_xml_uses_real_frame_rate_and_consistent_durations(self):
        with tempfile.TemporaryDirectory() as folder:
            path = create_davinci_xml(Path(folder) / "файл с пробелами.mp4", [(0, 60), (70, 80)], 100, fps=30000 / 1001)
            tree = ET.parse(path)
            sequence = tree.getroot().find("sequence")
            self.assertEqual(sequence.findtext("rate/ntsc"), "TRUE")
            clips = sequence.findall("media/video/track/clipitem")
            self.assertEqual(int(clips[0].findtext("out")), 1798)
            self.assertEqual(int(sequence.findtext("duration")), int(clips[-1].findtext("end")))
            self.assertEqual(len(sequence.findall("media/audio/track")), 2)
            self.assertEqual(len(clips[0].findall("link")), 3)
            self.assertIn("%20", clips[0].findtext("file/pathurl"))


if __name__ == "__main__":
    unittest.main()
