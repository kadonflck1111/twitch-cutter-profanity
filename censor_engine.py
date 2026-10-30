"""Точечное глушение слов с копированием исходного видеопотока."""

import math
import tempfile
import time
from pathlib import Path

from media_utils import (
    MediaError, atomic_output, audio_encode_args, check_cancel, filter_script_args,
    format_timestamp, merge_intervals, probe_media, run_ffmpeg, write_report,
)
from profanity_filter import censor_word_display, find_profanities_in_words
from speech_engine import analyze_media

INITIAL_PROMPT_CENSOR = (
    "Разговорная русская речь с матом, нецензурной лексикой и экспрессией: "
    "блять, блядь, пиздец, нахуй, ебать, хуй, хуйня, сука, мудак, говно, "
    "заебись, долбоеб, залупа, похуй, дохуя, впизду."
)


def format_simple_timestamp(seconds):
    return format_timestamp(seconds, milliseconds=True)


def build_ffmpeg_mute_filter(intervals, volume_level=0.0):
    """Один volume с деревом интервалов: O(log N) проверок вместо N фильтров.

    Короткие аудиофреймы ограничивают погрешность глушения несколькими мс.
    Фильтр передается через файл, поэтому лимит командной строки Windows не мешает.
    """
    if not math.isfinite(volume_level) or not 0 <= volume_level <= 1:
        raise ValueError("Громкость должна быть от 0 до 1")
    intervals = merge_intervals(intervals)
    if not intervals:
        return ""

    def expression(left, right):
        if right - left == 1:
            start, end = intervals[left]
            return f"if(between(t,{start:.6f},{end:.6f}),{volume_level:g},1)"
        middle = (left + right) // 2
        return (f"if(lt(t,{intervals[middle][0]:.6f}),"
                f"{expression(left, middle)},{expression(middle, right)})")

    return f"asetnsamples=n=256:p=0,volume='{expression(0, len(intervals))}':eval=frame"


def process_video_censorship(
    file_path: Path,
    model,
    batched_pipeline=None,
    custom_stopwords=None,
    include_soft=False,
    leave_end_sec=0.10,
    volume_level=0.0,
    output_dir=None,
    log_callback=None,
    status_callback=None,
    stop_check_callback=None,
    progress_callback=None,
    use_whitelist=True,
):
    log = log_callback or (lambda _: None)
    status = status_callback or (lambda _: None)
    file_path = Path(file_path).resolve()
    start_time = time.monotonic()
    status("Чтение аудиодорожки")
    info = probe_media(file_path, stop_check_callback)
    if not info.has_audio:
        raise MediaError("В выбранном файле нет аудиодорожки для цензуры")
    if not math.isfinite(leave_end_sec) or leave_end_sec < 0:
        raise ValueError("Длительность окончания слова должна быть неотрицательной")
    log(f"Анализ цензуры: {file_path.name}")
    status("Распознавание речи через Whisper")
    words = []
    for segment in analyze_media(
        file_path, info.duration, model, batched_pipeline, initial_prompt=INITIAL_PROMPT_CENSOR,
        stop_check=stop_check_callback, log_callback=log,
        progress_callback=(lambda value: progress_callback(1, value)) if progress_callback else None,
    ):
        words.extend(segment.words)
    check_cancel(stop_check_callback)
    status("Поиск слов для глушения")
    detected = find_profanities_in_words(
        words, custom_stopwords, include_soft, leave_end_sec, use_whitelist=use_whitelist,
    )
    del words
    count = len(detected)
    for item in detected:
        log(f"[{format_simple_timestamp(item['orig_start'])}] «{censor_word_display(item['word'])}» · "
            f"глушение {item['end'] - item['start']:.2f} сек.")
    if progress_callback:
        progress_callback(2, 1.0)
    log(f"Найдено слов и выражений: {count}")

    target_dir = Path(output_dir) if output_dir else file_path.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    output_path = None
    if detected:
        intervals = merge_intervals((item["start"], min(info.duration, item["end"])) for item in detected)
        mute_filter = build_ffmpeg_mute_filter(intervals, volume_level)
        output_path = target_dir / f"{file_path.stem}_clean{file_path.suffix}"
        status("Экспорт чистого файла через FFmpeg")
        try:
            with tempfile.TemporaryDirectory(prefix="censor_") as folder:
                script = Path(folder) / "mute.fffilter"
                script.write_text(f"[0:a:0]{mute_filter}[clean]", encoding="utf-8")
                args = ["-i", file_path, *filter_script_args(script)]
                if info.has_video:
                    args += ["-map", "0:v:0", "-c:v", "copy"]
                args += ["-map", "[clean]", "-map_metadata", "0", *audio_encode_args(file_path.suffix)]
                if file_path.suffix.lower() in {".mp4", ".mov", ".m4v", ".m4a"}:
                    args += ["-movflags", "+faststart"]
                with atomic_output(output_path) as temporary:
                    run_ffmpeg(
                        [*args, temporary], stop_check=stop_check_callback, duration=info.duration,
                        progress_callback=(lambda value: progress_callback(3, value)) if progress_callback else None,
                    )
        except MediaError as exc:
            log(f"[ОШИБКА FFmpeg]: {exc}")
            return False, None, count
        log(f"Сохранено: {output_path.name}")
    else:
        log("Стоп слова не найдены. Создан отчет, копия медиа не требуется.")
    check_cancel(stop_check_callback)
    lines = [f"Отчет цензуры: {file_path.name}", f"Дата: {time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"Длительность: {info.duration:.3f} сек.", f"Заглушено слов и выражений: {count}", ""]
    lines.extend(
        f"[{format_simple_timestamp(item['orig_start'])} - {format_simple_timestamp(item['orig_end'])}] "
        f"{item['word']} (глушение: {item['start']:.3f} - {item['end']:.3f})" for item in detected
    )
    write_report(target_dir / f"{file_path.stem}_отчет_цензуры.txt", "\n".join(lines) + "\n")
    elapsed = time.monotonic() - start_time
    status(f"Готово за {elapsed:.1f} сек.")
    log(f"Время обработки: {elapsed:.1f} сек. ({info.duration / max(elapsed, 0.001):.1f}x)")
    return True, output_path, count
