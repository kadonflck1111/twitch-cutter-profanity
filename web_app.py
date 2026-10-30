"""Настольный интерфейс Twitch Cutter Profanity и фоновая обработка медиа."""

import json
import math
import os
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import webview

from app_meta import APP_NAME, APP_VERSION
from censor_engine import process_video_censorship
from media_utils import ProcessingCancelled, check_cancel, find_media_files, format_timestamp, probe_media
from speech_engine import MODEL_NAMES, ModelManager
from stream_cutter import process_stream_cut

VIDEO_AUDIO_TYPES = (
    "Video and audio files (*.mp4;*.mkv;*.avi;*.mov;*.webm;*.flv;*.wmv;*.m4v;"
    "*.mp3;*.wav;*.m4a;*.aac;*.flac;*.ogg;*.opus;*.wma)",
    "All files (*.*)",
)


def _app_dir():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


class Bridge:
    def __init__(self):
        # pywebview рекурсивно публикует все публичные атрибуты js_api.
        # Внутренние окна, потоки и модели не должны попадать в JS-мост.
        self._window = None
        self._stop_event = threading.Event()
        self._closed = threading.Event()
        self._worker_thread = None
        self._models = ModelManager()
        self._maximized = False
        self._last_output_dir = None
        self._start_lock = threading.Lock()
        self._message_lock = threading.Lock()
        self._messages = deque()

    def _attach(self, window):
        self._window = window
        window.events.closing += self._on_closing
        threading.Thread(target=self._message_pump, daemon=True).start()

    def _emit(self, payload):
        # Один переход Python -> WebView на пачку, а не на каждое найденное слово.
        with self._message_lock:
            if len(self._messages) < 2048 or payload.get("type") != "log":
                self._messages.append(payload)

    def _message_pump(self):
        while not self._closed.wait(0.1):
            with self._message_lock:
                messages = [self._messages.popleft() for _ in range(min(256, len(self._messages)))]
            if messages and self._window:
                try:
                    self._window.evaluate_js("window.onBackendMessages && window.onBackendMessages("
                                            + json.dumps(messages, ensure_ascii=True) + ");")
                except Exception:
                    if not self._closed.is_set():
                        self._stop_event.set()

    @staticmethod
    def _file_info(path, *, probe=False):
        size = path.stat().st_size
        size_text = f"{size / 1024 ** 3:.2f} GB" if size >= 1024 ** 3 else f"{size / 1024 ** 2:.1f} MB"
        result = {"name": path.name, "path": str(path), "ext": path.suffix[1:].upper(),
                  "size": size_text, "duration": "После анализа", "res": "Исходное качество"}
        if probe:
            try:
                info = probe_media(path)
                result["duration"] = format_timestamp(info.duration)
                result["res"] = (f"{info.width}×{info.height} · {info.fps:.3f} fps" if info.has_video
                                 else f"Аудио · {info.sample_rate} Hz · {info.channels} канал(а)")
            except RuntimeError as exc:
                result["res"] = str(exc)
        return result

    def inspect_path(self, raw_path):
        path = Path(str(raw_path)).expanduser().resolve()
        files = find_media_files(path)
        if path.is_file():
            return {"type": "file", "path": str(path), "file": self._file_info(path, probe=True)}
        return {"type": "batch", "path": str(path), "files": [
            {**self._file_info(p), "dur": "После анализа"} for p in files
        ]}

    def choose_file(self):
        if self._worker_thread and self._worker_thread.is_alive():
            return None
        result = self._window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=False, file_types=VIDEO_AUDIO_TYPES)
        return self.inspect_path(result[0]) if result else None

    def choose_folder(self):
        result = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        return self.inspect_path(result[0]) if result else None

    def choose_output_folder(self):
        result = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        return str(Path(result[0]).resolve()) if result else None

    def get_system_info(self):
        hub = os.environ.get("HF_HUB_CACHE") or str(Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub")
        return {"version": APP_VERSION,
                "device": self._models.device.upper() if self._models.model else "Авто: CUDA / CPU",
                "compute": self._models.compute_type if self._models.model else "После загрузки модели",
                "modelCache": hub}

    def _get_model_and_pipeline(self, model_name):
        if self._models.name != model_name:
            self._emit({"type": "status", "status": "loading", "title": "Загрузка модели", "desc": f"Whisper {model_name}"})
        result = self._models.get(model_name, lambda text: self._emit({"type": "log", "message": text}), self._stop_event.is_set)
        self._emit({"type": "system", **self.get_system_info()})
        return result

    def start_processing(self, mode, source, model_name, options=None):
        with self._start_lock:
            if self._closed.is_set() or (self._worker_thread and self._worker_thread.is_alive()):
                return False
            if mode not in ("censor", "stream") or model_name not in MODEL_NAMES:
                raise ValueError("Некорректный режим или модель")
            files = find_media_files(Path(str(source)))
            if not files:
                raise ValueError("В источнике не найдено поддерживаемых файлов")
            options = dict(options or {})
            for key, default, low, high in (("minPause", 1.5, 0.1, 60), ("pad", 0.25, 0, 10)):
                value = float(options.get(key, default))
                if not math.isfinite(value) or not low <= value <= high:
                    raise ValueError(f"Некорректное значение {key}")
                options[key] = value
            if mode == "stream" and not (options.get("xml", True) or options.get("mp4", True)):
                raise ValueError("Выберите хотя бы один формат экспорта")
            if not options.get("saveNear", True) and not options.get("outputDir"):
                raise ValueError("Выберите папку для результатов")
            self._stop_event.clear()
            self._worker_thread = threading.Thread(target=self._worker, args=(mode, files, model_name, options), daemon=True)
            self._worker_thread.start()
            return True

    def stop_processing(self):
        self._emit({"type": "status", "status": "stopping", "title": "Остановка", "desc": "Отменяем текущую операцию"})
        self._stop_event.set()
        return True

    def _worker(self, mode, files, model_name, options):
        started = time.monotonic()
        total = len(files)
        successes = hits_total = 0
        failures, outputs = [], []
        output_dir = Path(options["outputDir"]) if not options.get("saveNear", True) else None
        try:
            for index, file_path in enumerate(files):
                check_cancel(self._stop_event.is_set)
                file_progress = 0.0
                last_sent = 0.0

                def log_callback(message):
                    self._emit({"type": "log", "level": "info", "message": str(message)})

                def status_callback(message):
                    if not self._stop_event.is_set():
                        self._emit({"type": "status", "status": "processing", "title": "Обработка файла", "desc": str(message)})

                def progress_callback(stage, value):
                    nonlocal file_progress, last_sent
                    spans = {0: (0, 0.05), 1: (0.05, 0.65), 2: (0.7, 0.05), 3: (0.75, 0.24), 4: (1, 0)}
                    base, span = spans[stage]
                    file_progress = max(file_progress, base + span * value)
                    now = time.monotonic()
                    if value not in (0, 1) and now - last_sent < 0.15:
                        return
                    last_sent = now
                    self._emit({"type": "progress", "file_index": index, "total": total,
                                "progress": (index + file_progress) / total, "file_progress": file_progress,
                                "stage": stage, "hits": hits_total, "file_name": file_path.name})

                progress_callback(0, 0)
                try:
                    needs_model = mode == "censor" or options.get("donations", True) or options.get("chat", True)
                    model, pipeline = self._get_model_and_pipeline(model_name) if needs_model else (None, None)
                    check_cancel(self._stop_event.is_set)
                    common = dict(file_path=file_path, model=model, batched_pipeline=pipeline, output_dir=output_dir,
                                  log_callback=log_callback, status_callback=status_callback,
                                  stop_check_callback=self._stop_event.is_set, progress_callback=progress_callback)
                    if mode == "censor":
                        stopwords = {w.strip().lower() for w in str(options.get("stopwords", "")).split(",") if w.strip()}
                        success, media, count = process_video_censorship(
                            **common, custom_stopwords=stopwords, include_soft=bool(options.get("rude", False)),
                            leave_end_sec=0.10 if options.get("soft", True) else 0.0,
                            use_whitelist=bool(options.get("whitelist", True)),
                        )
                        paths = [media] if media else []
                        hits_total += count if success else 0
                    else:
                        success, xml, mp4 = process_stream_cut(
                            **common, min_silence_duration=options["minPause"] if options.get("silence", True) else None,
                            padding=options["pad"], cut_donations=bool(options.get("donations", True)),
                            cut_chat=bool(options.get("chat", True)), export_xml=bool(options.get("xml", True)),
                            export_mp4=bool(options.get("mp4", True)), high_quality=bool(options.get("keepQuality", True)),
                        )
                        paths = [p for p in (xml, mp4) if p]
                    check_cancel(self._stop_event.is_set)
                    if not success:
                        raise RuntimeError("Экспорт не завершен. Подробности FFmpeg записаны в журнал.")
                    successes += 1
                    if mode == "stream":
                        hits_total += 1
                    self._last_output_dir = output_dir or file_path.parent
                    report = self._last_output_dir / f"{file_path.stem}_отчет_{'цензуры' if mode == 'censor' else 'нарезки'}.txt"
                    outputs.extend(str(p) for p in paths)
                    if report.exists():
                        outputs.append(str(report))
                    self._emit({"type": "file_result", "file_index": index, "success": True,
                                "outputs": [str(p) for p in paths], "report": str(report)})
                except ProcessingCancelled:
                    raise
                except Exception as exc:
                    failures.append({"file": file_path.name, "message": str(exc)})
                    self._emit({"type": "log", "level": "err", "message": f"{file_path.name}: {exc}"})
                    self._emit({"type": "file_result", "file_index": index, "success": False, "message": str(exc)})
                progress_callback(4, 1)
            check_cancel(self._stop_event.is_set)
            self._emit({"type": "done", "hits": hits_total, "total": total, "succeeded": successes,
                        "failed": len(failures), "failures": failures, "outputs": outputs,
                        "output_dir": str(self._last_output_dir) if successes else None,
                        "elapsed": time.monotonic() - started,
                        "message": f"Обработано: {successes} из {total}. Ошибок: {len(failures)}."})
            if successes and options.get("openAfter", True):
                try:
                    self.open_result_folder()
                except OSError as exc:
                    self._emit({"type": "log", "level": "warn", "message": f"Результаты сохранены, но папка не открылась: {exc}"})
        except ProcessingCancelled:
            self._emit({"type": "stopped", "completed": successes, "outputs": outputs})
        except Exception as exc:
            self._emit({"type": "error", "message": str(exc)})

    def minimize(self):
        if self._window:
            self._window.minimize()

    def toggle_maximize(self):
        if self._window:
            self._window.restore() if self._maximized else self._window.maximize()
            self._maximized = not self._maximized
        return self._maximized

    def _on_closing(self):
        self._stop_event.set()
        self._closed.set()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=5)

    def close(self):
        if self._window:
            self._window.destroy()

    def open_result_folder(self, path=None):
        target = Path(path) if path else self._last_output_dir
        if target is None:
            return False
        if target.is_file():
            target = target.parent
        if not target.is_dir():
            return False
        if sys.platform == "win32":
            os.startfile(str(target))
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(target)])
        return True


def main():
    if "--self-test" in sys.argv[1:]:
        from runtime_check import main as self_test
        sys.exit(self_test())
    bridge = Bridge()
    index = _app_dir() / "design" / "index.html"
    if not index.exists():
        raise FileNotFoundError(f"Не найден интерфейс: {index}")
    window = webview.create_window(
        APP_NAME, url=index.as_uri(), js_api=bridge,
        width=1280, height=860, min_size=(980, 700), resizable=True,
        frameless=True, easy_drag=False, background_color="#000000",
    )
    bridge._attach(window)
    webview.start(
        gui="edgechromium" if sys.platform == "win32" else None,
        debug=False, icon=str(_app_dir() / "icon.ico"),
    )


if __name__ == "__main__":
    main()
