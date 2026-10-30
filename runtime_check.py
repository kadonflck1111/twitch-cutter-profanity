"""Самопроверка установленной сборки теми же движками, которые использует GUI."""

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

from app_meta import APP_NAME, APP_VERSION
from censor_engine import process_video_censorship
from media_utils import capture_process, media_tool, probe_media, run_ffmpeg, video_encoder, write_report
from speech_engine import MODEL_NAMES, ModelManager, analyze_media
from stream_cutter import export_cut_video, process_stream_cut


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", type=Path, required=True, metavar="OUTPUT_FOLDER")
    parser.add_argument("--speech-fixture", type=Path, help="Необязательная запись с русской речью")
    parser.add_argument("--model", choices=MODEL_NAMES, default="tiny")
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    args = parser.parse_args(argv)
    root = args.self_test.resolve()
    root.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    result = {"ok": False, "application": APP_NAME, "version": APP_VERSION,
              "executable": sys.executable, "frozen": bool(getattr(sys, "frozen", False)),
              "checks": {}, "log": []}
    log = result["log"].append
    try:
        checks = result["checks"]
        checks["ffmpeg"] = media_tool("ffmpeg")
        checks["ffprobe"] = media_tool("ffprobe")
        checks["ffmpeg_version"] = capture_process([media_tool("ffmpeg"), "-version"]).splitlines()[0]
        assets = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "design"
        for name in ("index.html", "styles.css", "app.js", "background.js", "icon.png",
                     "vendor/manrope-cyrillic.woff2", "vendor/manrope-latin.woff2",
                     "vendor/geist-mono-cyrillic.woff2", "vendor/geist-mono-latin.woff2",
                     "vendor/phosphor/regular/Phosphor.woff2", "vendor/phosphor/fill/Phosphor-Fill.woff2"):
            if not (assets / name).is_file():
                raise RuntimeError(f"Отсутствует ресурс: {name}")
        checks["ui_assets"] = "ok"
        release_root = assets.parent
        for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
            if not (release_root / name).is_file():
                raise RuntimeError(f"Отсутствует документ релиза: {name}")
        checks["legal_notices"] = "ok"
        source = root / "selftest_source.mp4"
        run_ffmpeg([
            "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=6",
            "-f", "lavfi", "-i", "sine=sample_rate=48000:duration=6",
            "-c:v", "libx264", "-preset", "ultrafast", "-g", "60", "-bf", "2", "-c:a", "aac", source,
        ])
        checks["video_encoder"] = video_encoder()
        output = root / "selftest_cut.mp4"
        if not export_cut_video(source, [(0.5, 1.5), (3.1, 4.3)], output, log):
            raise RuntimeError("Не прошла проверка рендера")
        info = probe_media(output)
        if abs(info.duration - 2.2) > 0.07 or not (info.has_audio and info.has_video):
            raise RuntimeError(f"Неверный результат рендера: {info}")
        checks["render_duration"] = info.duration

        manager = ModelManager()
        model, pipeline = manager.get(args.model, log, requested_device=args.device)
        checks["whisper"] = {"model": manager.name, "device": manager.device, "compute": manager.compute_type}
        if args.speech_fixture:
            speech = args.speech_fixture.resolve()
            duration = probe_media(speech).duration
            recognized = list(analyze_media(speech, duration, model, pipeline, log_callback=log, chunk_seconds=6))
            text = " ".join(segment.text for segment in recognized)
            if not text.strip():
                raise RuntimeError("Whisper не распознал речь в тестовой записи")
            checks["transcript"] = text
            words = [word for segment in recognized for word in segment.words]
            if not all(0 <= word.start < word.end <= duration for word in words):
                raise RuntimeError("Whisper вернул неверные таймкоды слов")
            regions = list(analyze_media(speech, duration, vad_only=True, chunk_seconds=6))
            if not regions:
                raise RuntimeError("VAD не обнаружил речь в тестовой записи")
            checks["vad_regions"] = len(regions)
            # Любое первое распознанное слово подходит для проверки полного пути цензуры.
            word = next(word.word for word in words if len(word.word.strip()) >= 3)
            success, clean, count = process_video_censorship(
                speech, model, pipeline, custom_stopwords={word}, output_dir=root,
                leave_end_sec=0, log_callback=log,
            )
            if not success or clean is None or count < 1 or not clean.is_file():
                raise RuntimeError("Не прошла проверка распознавания и цензуры")
            checks["censorship"] = {"count": count, "output": str(clean)}
            success, xml, mp4 = process_stream_cut(
                speech, cut_donations=False, cut_chat=False, output_dir=root, log_callback=log,
            )
            if not success or not xml or not mp4 or not xml.is_file() or not mp4.is_file():
                raise RuntimeError("Не прошла проверка нарезки с VAD")
            checks["stream_cut"] = {"xml": str(xml), "mp4": str(mp4)}
        result["ok"] = True
    except Exception:
        result["error"] = traceback.format_exc()
    finally:
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        write_report(root / "selftest.json", json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
