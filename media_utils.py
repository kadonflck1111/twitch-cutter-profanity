"""Общие операции с медиа: проверка потоков, отмена, прогресс и безопасный экспорт."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
from pathlib import Path

SUPPORTED_EXTENSIONS = frozenset({
    ".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".wmv", ".m4v",
    ".mp3", ".wav", ".aac", ".flac", ".ogg", ".m4a", ".wma", ".opus",
})
OUTPUT_SUFFIXES = ("_clean", "_нарезка")


class MediaError(RuntimeError):
    pass


class ProcessingCancelled(Exception):
    pass


def check_cancel(stop_check=None):
    if stop_check and stop_check():
        raise ProcessingCancelled("Обработка остановлена")


def subprocess_options():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def find_media_files(source: Path, *, skip_outputs=True):
    source = Path(source).expanduser().resolve()
    if source.is_file():
        if source.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise ValueError(f"Неподдерживаемый формат: {source.suffix}")
        return [source]
    if not source.is_dir():
        raise FileNotFoundError(f"Источник не найден: {source}")
    return [
        p for p in sorted(source.iterdir())
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
        and not (skip_outputs and p.stem.lower().endswith(OUTPUT_SUFFIXES))
    ]


@lru_cache(maxsize=2)
def media_tool(name):
    suffix = ".exe" if os.name == "nt" else ""
    roots = [Path(__file__).resolve().parent, Path(sys.executable).parent]
    if getattr(sys, "_MEIPASS", None):
        roots.insert(0, Path(sys._MEIPASS))
    for root in roots:
        for folder in (root, root / "bin"):
            candidate = folder / (name + suffix)
            if candidate.is_file():
                return str(candidate)
    executable = shutil.which(name)
    if executable:
        return executable
    raise MediaError(f"Не найден {name}. Установите FFmpeg и добавьте папку bin в PATH.")


def terminate_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def capture_process(command, stop_check=None, timeout=30, *, binary=False):
    """Короткие команды (ffprobe); communicate регулярно проверяет отмену."""
    check_cancel(stop_check)
    started = time.monotonic()
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        **subprocess_options(),
    )
    try:
        while True:
            check_cancel(stop_check)
            try:
                out, err = process.communicate(timeout=0.15)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() - started > timeout:
                    raise MediaError(f"Превышено время ожидания {Path(command[0]).name}")
        if process.returncode:
            raise MediaError(err.decode("utf-8", errors="replace")[-8000:].strip())
        return out if binary else out.decode("utf-8", errors="replace")
    finally:
        terminate_process(process)
        process.stdout.close()
        process.stderr.close()


def run_ffmpeg(arguments, *, stop_check=None, progress_callback=None, duration=0):
    """FFmpeg без блокировки UI и накопления многочасового stderr в памяти."""
    check_cancel(stop_check)
    command = [
        media_tool("ffmpeg"), "-hide_banner", "-nostdin", "-y",
        "-loglevel", "warning", "-nostats", "-progress", "pipe:1", *map(str, arguments),
    ]
    latest = [0.0]
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=errors, **subprocess_options(),
        )

        def read_progress():
            for line in iter(process.stdout.readline, b""):
                if line.startswith(b"out_time_us="):
                    try:
                        latest[0] = int(line.split(b"=", 1)[1]) / 1_000_000
                    except ValueError:
                        pass

        reader = threading.Thread(target=read_progress, daemon=True)
        reader.start()
        last_progress = -1.0
        try:
            while process.poll() is None:
                check_cancel(stop_check)
                progress = min(0.999, max(0.0, latest[0] / duration)) if duration else 0
                if progress_callback and progress != last_progress:
                    progress_callback(progress)
                    last_progress = progress
                try:
                    process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    pass
            check_cancel(stop_check)
            if process.returncode:
                errors.seek(0, os.SEEK_END)
                errors.seek(max(0, errors.tell() - 8000))
                detail = errors.read().decode("utf-8", errors="replace").strip()
                raise MediaError(detail or f"FFmpeg завершился с кодом {process.returncode}")
            if progress_callback:
                progress_callback(1.0)
        finally:
            terminate_process(process)
            reader.join(timeout=3)
            process.stdout.close()


@contextmanager
def atomic_output(target: Path):
    """Предыдущий результат заменяется только после успешного завершения операции."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{target.stem}.", suffix=target.suffix, dir=target.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        yield temporary
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def write_report(target, text):
    with atomic_output(target) as temporary:
        temporary.write_text(text, encoding="utf-8")


def _number(value, default=0.0):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    fps: float = 0.0
    width: int = 0
    height: int = 0
    video_codec: str = ""
    audio_codec: str = ""
    sample_rate: int = 0
    channels: int = 0

    @property
    def has_video(self):
        return bool(self.video_codec)

    @property
    def has_audio(self):
        return bool(self.audio_codec)


def probe_media(path: Path, stop_check=None) -> MediaInfo:
    output = capture_process([
        media_tool("ffprobe"), "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", str(path),
    ], stop_check)
    try:
        data = json.loads(output)
    except ValueError as exc:
        raise MediaError(f"Не удалось прочитать метаданные: {path}") from exc
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    if not video and not audio:
        raise MediaError(f"В файле нет видео или звука: {path}")
    duration = _number(data.get("format", {}).get("duration"))
    if duration <= 0:
        duration = max((_number(s.get("duration")) for s in streams), default=0)
    if duration <= 0:
        raise MediaError(f"Не удалось определить длительность: {path}")
    fps = 0.0
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            fps = float(Fraction(video.get(key, "0/1")))
        except (ValueError, ZeroDivisionError):
            continue
        if fps > 0:
            break
    return MediaInfo(
        duration, fps, int(video.get("width", 0)), int(video.get("height", 0)),
        video.get("codec_name", ""), audio.get("codec_name", ""),
        int(_number(audio.get("sample_rate"))), int(audio.get("channels", 0)),
    )


@lru_cache(maxsize=1)
def ffmpeg_major():
    text = capture_process([media_tool("ffmpeg"), "-version"])
    match = re.search(r"ffmpeg version (?:n)?(\d+)", text)
    return int(match.group(1)) if match else 7


def filter_script_args(path):
    # Старые -filter_complex_script / -filter_script удалены в FFmpeg 9.
    option = "-/filter_complex" if ffmpeg_major() >= 7 else "-filter_complex_script"
    return [option, str(path)]


@lru_cache(maxsize=1)
def video_encoder():
    """Наличие энкодера в -encoders не означает, что драйвер способен его запустить."""
    try:
        capture_process([
            media_tool("ffmpeg"), "-v", "error", "-nostdin",
            "-f", "lavfi", "-i", "color=s=320x240:d=0.1",
            "-c:v", "h264_nvenc", "-f", "null", "-",
        ], timeout=15)
        return "h264_nvenc"
    except MediaError:
        return "libx264"


def video_encode_args(encoder, high_quality=True):
    quality = "20" if high_quality else "23"
    if encoder == "h264_nvenc":
        return ["-c:v", encoder, "-preset", "p4", "-rc", "vbr", "-cq", quality,
                "-b:v", "0", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", quality, "-pix_fmt", "yuv420p"]


def audio_encode_args(extension):
    codecs = {
        ".webm": ["libopus", "-b:a", "192k"],
        ".opus": ["libopus", "-b:a", "192k"],
        ".ogg": ["libvorbis", "-q:a", "5"],
        ".mp3": ["libmp3lame", "-b:a", "192k"],
        ".avi": ["libmp3lame", "-b:a", "192k"],
        ".wav": ["pcm_s16le"],
        ".flac": ["flac"],
        ".wma": ["wmav2", "-b:a", "192k"],
        ".wmv": ["wmav2", "-b:a", "192k"],
    }
    return ["-c:a", *codecs.get(extension.lower(), ["aac", "-b:a", "192k"])]


def merge_intervals(intervals, gap_tolerance=0.0):
    valid = sorted((max(0.0, float(s)), float(e)) for s, e in intervals
                   if math.isfinite(s) and math.isfinite(e) and e > max(0, s))
    merged = []
    for start, end in valid:
        if merged and start <= merged[-1][1] + gap_tolerance:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def format_timestamp(seconds, *, milliseconds=False, separator="."):
    ticks = max(0, round(seconds * (1000 if milliseconds else 1)))
    whole, millis = divmod(ticks, 1000) if milliseconds else (ticks, 0)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    result = f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return result + f"{separator}{millis:03d}" if milliseconds else result
