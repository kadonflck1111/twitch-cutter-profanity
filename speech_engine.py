"""Whisper/VAD с ограниченным потреблением памяти и общим управлением CUDA."""

from __future__ import annotations

import gc
import os
import site
import sys
from dataclasses import dataclass, field
from pathlib import Path

from media_utils import capture_process, check_cancel, media_tool

SAMPLE_RATE = 16000
CHUNK_SECONDS = 300
OVERLAP_SECONDS = 1.0
MODEL_NAMES = ("tiny", "base", "small", "medium", "large-v3")
_dll_handles = []


def setup_runtime():
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    if os.name != "nt" or _dll_handles:
        return
    roots = {Path(__file__).resolve().parent, Path(sys.executable).parent}
    if getattr(sys, "_MEIPASS", None):
        roots.add(Path(sys._MEIPASS))
    # При сборке CUDA библиотеки находятся в _internal/nvidia, при запуске
    # исходников — в установленных nvidia-* пакетах Python.
    for folder in site.getsitepackages():
        roots.update(Path(folder).glob("nvidia/*/bin"))
    for root in tuple(roots):
        roots.update(root.glob("nvidia/*/bin"))
    for folder in sorted(roots):
        if folder.is_dir():
            try:
                # Handle обязательно хранить: иначе add_dll_directory сразу закрывается.
                _dll_handles.append(os.add_dll_directory(str(folder)))
            except OSError:
                continue
            os.environ["PATH"] = str(folder) + os.pathsep + os.environ.get("PATH", "")


setup_runtime()


class ModelManager:
    def __init__(self):
        self.model = None
        self.pipeline = None
        self.name = None
        self.device = "cpu"
        self.compute_type = "int8"
        self.requested_device = "auto"

    def get(self, name, log=lambda _: None, stop_check=None, *, requested_device="auto"):
        check_cancel(stop_check)
        if requested_device not in ("auto", "cpu", "cuda"):
            raise ValueError("Неизвестное устройство вычислений")
        if self.model is not None and self.name == name and self.requested_device == requested_device:
            return self.model, self.pipeline
        if name not in MODEL_NAMES:
            raise ValueError(f"Неизвестная модель Whisper: {name}")
        try:
            import ctranslate2
            import numpy as np
            from faster_whisper import BatchedInferencePipeline, WhisperModel
        except ImportError as exc:
            raise RuntimeError("Установите зависимости: python -m pip install -r requirements.txt") from exc

        # Освобождаем старую модель ДО загрузки новой, иначе large-v3 может не поместиться.
        self.pipeline = None
        self.model = None
        self.name = None
        gc.collect()
        device, compute = "cpu", "int8"
        try:
            if requested_device != "cpu" and "float16" in ctranslate2.get_supported_compute_types("cuda"):
                device, compute = "cuda", "float16"
        except (RuntimeError, ValueError):
            pass
        log(f"Загрузка Whisper {name} · {device.upper()} · {compute}")
        model = None
        try:
            model = WhisperModel(name, device=device, compute_type=compute)
            if device == "cuda":
                # cuDNN загружается только при первом encode, а не в конструкторе.
                # Проверяем сейчас, чтобы fallback на CPU работал и при отсутствии DLL.
                model.encode(np.zeros((model.model.n_mels, 3000), dtype=np.float32))
        except RuntimeError as exc:
            if device != "cuda":
                raise
            log(f"CUDA недоступна: {exc}. Используем CPU.")
            model = None
            gc.collect()
            device, compute = "cpu", "int8"
            model = WhisperModel(name, device=device, compute_type=compute)
        check_cancel(stop_check)
        model._cutter_batch_size = ({"large-v3": 4, "medium": 8}.get(name, 16)
                                   if device == "cuda" else 2)
        self.model = model
        self.pipeline = BatchedInferencePipeline(model=model)
        self.name, self.device, self.compute_type = name, device, compute
        self.requested_device = requested_device
        log(f"Модель готова · {device.upper()} · batch {model._cutter_batch_size}")
        return self.model, self.pipeline


@dataclass
class SpeechWord:
    start: float
    end: float
    word: str
    probability: float = 1.0


@dataclass
class SpeechSegment:
    start: float
    end: float
    text: str = ""
    words: list = field(default_factory=list)


def decode_audio_chunk(path, start, duration, stop_check=None):
    """Один decode в PCM через pipe; WAV на диск и повторный decode больше не нужны."""
    import numpy as np

    data = capture_process([
        media_tool("ffmpeg"), "-v", "error", "-nostdin", "-ss", f"{start:.6f}",
        "-i", str(path), "-t", f"{duration:.6f}", "-map", "0:a:0", "-vn",
        "-af", "aresample=async=1:first_pts=0", "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-f", "s16le", "pipe:1",
    ], stop_check, timeout=180, binary=True)
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


def _transcribe_chunk(audio, model, pipeline, prompt, stop_check, log):
    batch_size = getattr(model, "_cutter_batch_size", 4)
    while True:
        check_cancel(stop_check)
        generator = None
        try:
            options = dict(
                language="ru", beam_size=1, word_timestamps=True,
                initial_prompt=prompt, vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=300, speech_pad_ms=150),
                condition_on_previous_text=False,
            )
            if pipeline is not None:
                pipeline.last_speech_timestamp = 0.0
                options["batch_size"] = batch_size
            generator, _ = (pipeline or model).transcribe(audio, **options)
            segments = []
            for segment in generator:
                check_cancel(stop_check)
                segments.append(segment)
            return segments
        except RuntimeError as exc:
            message = str(exc).lower()
            if pipeline is None or batch_size <= 1 or not any(
                text in message for text in ("out of memory", "failed to allocate", "alloc_failed")
            ):
                raise
            batch_size = max(1, batch_size // 2)
            model._cutter_batch_size = batch_size
            log(f"Недостаточно видеопамяти. Повторяем фрагмент с batch {batch_size}.")
            gc.collect()
        finally:
            if generator is not None and hasattr(generator, "close"):
                generator.close()
            if pipeline is not None:
                pipeline.last_speech_timestamp = 0.0


def analyze_media(
    path, duration, model=None, pipeline=None, *, vad_only=False, initial_prompt=None,
    stop_check=None, log_callback=None, progress_callback=None, chunk_seconds=CHUNK_SECONDS,
):
    """Пятиминутные окна с контекстом по краям. Все таймкоды относятся к исходнику."""
    if not vad_only and model is None:
        raise RuntimeError("Модель Whisper не загружена")
    if chunk_seconds <= 2 * OVERLAP_SECONDS:
        raise ValueError("Слишком короткое окно анализа")
    log = log_callback or (lambda _: None)
    core_start = 0.0
    last_word = None
    while core_start < duration:
        check_cancel(stop_check)
        core_end = min(duration, core_start + chunk_seconds)
        start = max(0.0, core_start - OVERLAP_SECONDS)
        end = min(duration, core_end + OVERLAP_SECONDS)
        audio = decode_audio_chunk(path, start, end - start, stop_check)
        check_cancel(stop_check)
        if len(audio):
            if vad_only:
                from faster_whisper.vad import get_speech_timestamps

                regions = get_speech_timestamps(
                    audio, min_silence_duration_ms=150, speech_pad_ms=0,
                )
                for region in regions:
                    s = max(core_start, start + region["start"] / SAMPLE_RATE)
                    e = min(core_end, start + region["end"] / SAMPLE_RATE)
                    if e > s:
                        yield SpeechSegment(s, e)
            else:
                segments = _transcribe_chunk(audio, model, pipeline, initial_prompt, stop_check, log)
                for segment in segments:
                    words = []
                    for word in segment.words or []:
                        midpoint = start + (word.start + word.end) / 2
                        if not core_start <= midpoint < core_end:
                            continue
                        shifted = SpeechWord(
                            max(0.0, start + word.start), min(duration, start + word.end),
                            word.word, word.probability,
                        )
                        if shifted.end <= shifted.start:
                            continue
                        if (last_word and shifted.start < last_word.end
                                and shifted.word.strip().casefold() == last_word.word.strip().casefold()):
                            continue
                        words.append(shifted)
                        last_word = shifted
                    if words:
                        yield SpeechSegment(words[0].start, words[-1].end,
                                            "".join(w.word for w in words).strip(), words)
        del audio
        core_start = core_end
        if progress_callback:
            progress_callback(core_end / duration)
    check_cancel(stop_check)
