"""
Движок умной нарезки стримов (Stream Highlight Cutter):
- Удаление только реальной тишины и затяжных пауз (настраиваемый порог, по умолч. от 1.5 сек)
- Естественные зазоры (padding 0.25 сек), сохраняющие вздохи, интонации и мычания
- Автоматическое вырезание отвлечений на донаты, сабки и чтение чата
- Экспорт готового таймлайна в DaVinci Resolve (.XML) и чистого видео (.MP4)
"""

import math
import re
import time
import tempfile
from pathlib import Path
from typing import List, Tuple, Optional, Callable, Dict, Any

from davinci_xml import create_davinci_xml
from media_utils import (
    MediaError, atomic_output, check_cancel, format_timestamp, merge_intervals,
    probe_media, run_ffmpeg, video_encode_args, video_encoder, write_report,
)
from speech_engine import SpeechSegment, analyze_media

# Базовые триггеры отвлечений на донаты, сабки и чат
DEFAULT_DONATION_TRIGGERS = [
    "спасибо за донат", "спасибо за подписк", "спасибо за саб",
    "спасибо за сотк", "спасибо за донатик", "спасибо за рейд",
    "спасибо за фолло", "спасибо за копеечк", "спасибо большое за",
    "спасибо огромное за", "читаю чат", "гляну в чат", "щас чат",
    "секунду чат", "донат пришел", "сообщение в донат", "за донат",
    "на дошик", "подарочные подписк", "скинул донат", "донат от",
    "привет из чата", "вопрос из чата", "спасибо за пятьдесят",
    "спасибо за сто", "спасибо за пятьсот", "спасибо за косарь",
    "спасибо за тысячу", "спасибо за балл", "донатер"
]

CHAT_TRIGGERS = ("читаю чат", "гляну в чат", "щас чат", "секунду чат", "привет из чата", "вопрос из чата")
DONATION_TRIGGERS = tuple(t for t in DEFAULT_DONATION_TRIGGERS if t not in CHAT_TRIGGERS)


def format_simple_timestamp(seconds: float) -> str:
    return format_timestamp(seconds)


def get_video_metadata(file_path: Path) -> Tuple[float, float, int, int]:
    """
    Получает (duration, fps, width, height) через ffprobe.
    """
    info = probe_media(file_path)
    return info.duration, info.fps, info.width, info.height


def is_distraction_segment(
    text: str, custom_triggers: Optional[List[str]] = None, *,
    cut_donations=True, cut_chat=True,
) -> Tuple[bool, str]:
    """
    Проверяет, является ли фраза стримера реакцией на донат, подписку или чат.
    """
    lower_text = " ".join(text.casefold().replace("ё", "е").split())

    triggers = list(DONATION_TRIGGERS if cut_donations else ())
    if cut_chat:
        triggers.extend(CHAT_TRIGGERS)
    if custom_triggers:
        triggers.extend(custom_triggers)

    for trig in triggers:
        trig_clean = " ".join(trig.casefold().replace("ё", "е").split())
        if trig_clean and trig_clean in lower_text:
            return True, trig_clean

    return False, ""


def _sentences(segments):
    """Whisper может вернуть несколько предложений одним сегментом на 30 секунд."""
    for segment in segments:
        if not getattr(segment, "words", None):
            yield segment
            continue
        words = []
        for word in segment.words:
            words.append(word)
            if re.search(r'[.!?…][»”"\']*$', word.word.rstrip()):
                yield SpeechSegment(words[0].start, words[-1].end,
                                    "".join(w.word for w in words).strip(), words)
                words = []
        if words:
            yield SpeechSegment(words[0].start, words[-1].end,
                                "".join(w.word for w in words).strip(), words)


def compute_stream_cuts(
    segments: list,
    total_duration: float,
    min_silence_duration: float = 1.5,
    padding: float = 0.25,
    cut_donations: bool = True,
    custom_triggers: Optional[List[str]] = None,
    cut_chat: Optional[bool] = None,
) -> Tuple[List[Tuple[float, float]], List[Dict[str, Any]]]:
    """
    Вычисляет интервалы, которые нужно ОСТАВИТЬ (kept_intervals).
    Тишина и донаты вырезаются.
    """
    if not math.isfinite(total_duration) or total_duration <= 0:
        raise ValueError("Длительность должна быть положительной")
    if not math.isfinite(padding) or padding < 0:
        raise ValueError("Зазор должен быть неотрицательным")
    if min_silence_duration is not None and (
        not math.isfinite(min_silence_duration) or min_silence_duration <= 0
    ):
        raise ValueError("Порог паузы должен быть положительным")
    if cut_chat is None:
        cut_chat = cut_donations
    speech_intervals = []
    cut_distractions = []
    removals = []

    for seg in _sentences(segments):
        s_start = max(0.0, seg.start)
        s_end = min(total_duration, seg.end)
        if not math.isfinite(s_start) or not math.isfinite(s_end) or s_end <= s_start:
            continue
        s_text = seg.text.strip()

        if cut_donations or cut_chat or custom_triggers:
            is_distr, trig = is_distraction_segment(
                s_text, custom_triggers, cut_donations=cut_donations, cut_chat=cut_chat,
            )
            if is_distr:
                cut_distractions.append({
                    "type": "Донат / Чат",
                    "trigger": trig,
                    "text": s_text,
                    "start": s_start,
                    "end": s_end
                })
                removals.append((s_start, s_end))

        words = getattr(seg, "words", None)
        if words:
            speech_intervals.extend((max(0, w.start), min(total_duration, w.end)) for w in words)
        else:
            speech_intervals.append((s_start, s_end))

    speech = merge_intervals(speech_intervals)
    if min_silence_duration is not None:
        previous_end = 0.0
        # Сравниваем с порогом исходную паузу, а не уже сокращенную padding.
        for start, end in [*speech, (total_duration, total_duration)]:
            if start - previous_end > min_silence_duration:
                left = previous_end + padding if previous_end else 0.0
                right = start - padding if start < total_duration else total_duration
                if right > left:
                    removals.append((left, right))
            previous_end = max(previous_end, end)

    # Вычитаем донаты отдельно: объединение соседней речи больше не возвращает их.
    kept = []
    cursor = 0.0
    for start, end in merge_intervals(removals):
        if start > cursor:
            kept.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < total_duration:
        kept.append((cursor, total_duration))
    return kept, cut_distractions


def export_cut_video(
    file_path: Path,
    kept_intervals: List[Tuple[float, float]],
    output_path: Path,
    log_callback: Optional[Callable[[str], None]] = None,
    stop_check_callback=None,
    progress_callback=None,
    metadata=None,
    high_quality=True,
    encoder=None,
) -> bool:
    """
    Склеивает сохраненные фрагменты в готовый MP4 через FFmpeg.
    """
    def log(m):
        if log_callback: log_callback(m)

    try:
        info = metadata or probe_media(file_path, stop_check_callback)
        intervals = merge_intervals((s, min(e, info.duration)) for s, e in kept_intervals)
        if not intervals:
            raise MediaError("Нет фрагментов для экспорта")
        total = sum(e - s for s, e in intervals)
        check_cancel(stop_check_callback)
        whole_file = len(intervals) == 1 and intervals[0][0] == 0 and abs(total - info.duration) < 0.001
        copy_video = not info.has_video or info.video_codec in {"h264", "hevc", "av1", "mpeg4"}
        copy_audio = not info.has_audio or info.audio_codec in {"aac", "mp3", "alac", "ac3", "eac3"}
        if whole_file and copy_video and copy_audio:
            log("   Склеек нет: копирование потоков без перекодирования.")
            with atomic_output(output_path) as temporary:
                run_ffmpeg([
                    "-i", file_path, "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy",
                    "-movflags", "+faststart", temporary,
                ], stop_check=stop_check_callback, progress_callback=progress_callback, duration=total)
            return True

        selected_encoder = encoder or (video_encoder() if info.has_video else None)
        log(f"   Сборка {len(intervals)} фрагментов · {selected_encoder or 'AAC'}")
        with tempfile.TemporaryDirectory(prefix="cutter_") as folder:
            listing = Path(folder) / "cuts.ffconcat"
            escaped = Path(file_path).resolve().as_posix().replace("'", "'\\''")
            lines = ["ffconcat version 1.0"]
            for start, end in intervals:
                lines.extend([f"file '{escaped}'", f"inpoint {start:.6f}",
                              f"outpoint {end:.6f}", f"duration {end - start:.6f}"])
            listing.write_text("\n".join(lines) + "\n", encoding="utf-8")

            def render(current_encoder):
                # Concat demuxer читает диапазоны последовательно. Память не растет
                # с количеством склеек, в отличие от сотен параллельных trim/concat.
                args = ["-copyts", "-f", "concat", "-safe", "0", "-segment_time_metadata", "1",
                        "-i", listing]
                if info.has_video:
                    args += ["-map", "0:v:0", "-vf", "select=concatdec_select", "-fps_mode:v", "vfr"]
                    args += video_encode_args(current_encoder, high_quality)
                if info.has_audio:
                    args += ["-map", "0:a:0", "-af",
                             f"aselect=concatdec_select,aresample=async=1:first_pts=0,apad,atrim=duration={total:.6f}",
                             "-c:a", "aac", "-b:a", "192k"]
                with atomic_output(output_path) as temporary:
                    run_ffmpeg([*args, "-movflags", "+faststart", temporary],
                               stop_check=stop_check_callback, progress_callback=progress_callback, duration=total)

            try:
                render(selected_encoder)
            except MediaError:
                if selected_encoder != "h264_nvenc" or encoder:
                    raise
                log("   Аппаратный рендер не завершился. Повторяем через libx264.")
                render("libx264")
        return True
    except MediaError as exc:
        log(f"[ОШИБКА FFmpeg]: {exc}")
        return False


def process_stream_cut(
    file_path: Path,
    model=None,
    batched_pipeline=None,
    min_silence_duration: Optional[float] = 1.5,
    padding: float = 0.25,
    cut_donations: bool = True,
    custom_triggers: Optional[List[str]] = None,
    export_xml: bool = True,
    export_mp4: bool = True,
    output_dir: Optional[Path] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    status_callback: Optional[Callable[[str], None]] = None,
    stop_check_callback: Optional[Callable[[], bool]] = None,
    progress_callback=None,
    cut_chat: Optional[bool] = None,
    high_quality=True,
) -> Tuple[bool, Optional[Path], Optional[Path]]:
    """
    Основная функция нарезки стрима:
    1. Находит речь через Whisper
    2. Вычисляет затяжные паузы (отвлекания) и фразы донатов
    3. Создает DaVinci XML и/или готовый MP4
    """
    def log(m):
        if log_callback: log_callback(m)

    def set_status(s):
        if status_callback: status_callback(s)

    if not export_xml and not export_mp4:
        raise ValueError("Выберите хотя бы один формат экспорта")
    file_path = Path(file_path).resolve()
    start_time = time.monotonic()
    log(f"Нарезка: {file_path.name}")
    set_status("Чтение метаданных")
    info = probe_media(file_path, stop_check_callback)
    duration = info.duration
    if cut_chat is None:
        cut_chat = cut_donations
    recognize = cut_donations or cut_chat or bool(custom_triggers)
    if info.has_audio and (recognize or min_silence_duration is not None):
        set_status("Распознавание речи через Whisper" if recognize else "Поиск речи через VAD без Whisper")
        segments = list(analyze_media(
            file_path, duration, model, batched_pipeline, vad_only=not recognize,
            stop_check=stop_check_callback, log_callback=log,
            progress_callback=(lambda value: progress_callback(1, value)) if progress_callback else None,
        ))
        kept_intervals, cut_distractions = compute_stream_cuts(
            segments, duration, min_silence_duration, padding, cut_donations, custom_triggers, cut_chat,
        )
    else:
        kept_intervals, cut_distractions = [(0.0, duration)], []
        if not info.has_audio:
            log("В файле нет аудиодорожки: сохраняем видеоряд целиком.")
    check_cancel(stop_check_callback)
    if progress_callback:
        progress_callback(2, 1.0)
    total_kept = sum(e - s for s, e in kept_intervals)
    cut_sec = max(0, duration - total_kept)
    log(f"Оставлено {format_simple_timestamp(total_kept)} из {format_simple_timestamp(duration)}; фрагментов: {len(kept_intervals)}")
    target_dir = Path(output_dir) if output_dir else file_path.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    xml_path = mp4_path = None
    if export_xml:
        xml_path = target_dir / f"{file_path.stem}_нарезка_DaVinci.xml"
        set_status("Экспорт таймлайна DaVinci Resolve")
        create_davinci_xml(
            file_path, kept_intervals, duration, info.fps or 30, info.width, info.height, xml_path,
            has_video=info.has_video, has_audio=info.has_audio,
            sample_rate=info.sample_rate or 48000, channels=info.channels or 1,
        )
        log(f"Создан таймлайн: {xml_path.name}")
    if export_mp4 and kept_intervals:
        candidate = target_dir / f"{file_path.stem}_нарезка.mp4"
        set_status("Рендеринг готового видео")
        success = export_cut_video(
            file_path, kept_intervals, candidate, log_callback=log,
            stop_check_callback=stop_check_callback, metadata=info, high_quality=high_quality,
            progress_callback=(lambda value: progress_callback(3, value)) if progress_callback else None,
        )
        if not success:
            return False, xml_path, None
        mp4_path = candidate
        log(f"Сохранено: {mp4_path.name}")
    elif not kept_intervals:
        log("После удаления пауз и отвлечений не осталось фрагментов. MP4 не создается.")
    check_cancel(stop_check_callback)
    lines = [f"Отчет нарезки: {file_path.name}", f"Исходная длительность: {duration:.3f} сек.",
             f"Итоговая длительность: {total_kept:.3f} сек.", f"Удалено: {cut_sec:.3f} сек.",
             f"Фрагментов: {len(kept_intervals)}", "", "Удаленные отвлечения:"]
    lines.extend(f"[{d['start']:.3f} - {d['end']:.3f}] {d['text']}" for d in cut_distractions)
    write_report(target_dir / f"{file_path.stem}_отчет_нарезки.txt", "\n".join(lines) + "\n")
    elapsed = time.monotonic() - start_time
    set_status(f"Готово за {elapsed:.1f} сек.")
    log(f"Нарезка завершена за {elapsed:.1f} сек. ({duration / max(elapsed, 0.001):.1f}x)")
    return True, xml_path, mp4_path
