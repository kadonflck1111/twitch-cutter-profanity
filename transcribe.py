"""
Скрипт для транскрибации видео и аудиофайлов с использованием faster-whisper.
Поддерживает экспорт в форматы TXT, SRT (субтитры), VTT и JSON.
Может работать через аргументы командной строки или в интерактивном режиме.
"""

from __future__ import annotations

import sys
import argparse
import json
import time
from pathlib import Path
from typing import List, Tuple, Optional

from media_utils import SUPPORTED_EXTENSIONS, format_timestamp as media_timestamp
from speech_engine import ModelManager

# Обеспечиваем корректный вывод UTF-8 в консоли Windows
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

SUPPORTED_FORMATS = {"txt", "srt", "vtt", "json", "all"}


def format_timestamp(seconds: float, is_srt: bool = True) -> str:
    """Преобразует секунды в формат временной метки (ЧЧ:ММ:СС,мс для SRT или .мс для VTT)."""
    return media_timestamp(seconds, milliseconds=True, separator="," if is_srt else ".")


def get_optimal_device(requested_device: str = "auto") -> Tuple[str, str]:
    """Определяет оптимальное устройство (cuda/cpu) и тип квантования/вычислений."""
    import ctranslate2
    if requested_device == "auto":
        try:
            supported_cuda = ctranslate2.get_supported_compute_types("cuda")
            if "float16" in supported_cuda:
                return "cuda", "float16"
            elif "int8_float16" in supported_cuda:
                return "cuda", "int8_float16"
            elif len(supported_cuda) > 0:
                return "cuda", list(supported_cuda)[0]
        except Exception:
            pass
        return "cpu", "int8"
    elif requested_device == "cuda":
        return "cuda", "float16"
    else:
        return "cpu", "int8"


def save_transcription(
    segments: list,
    info,
    output_base_path: Path,
    formats: List[str]
):
    """Сохраняет результаты транскрибации в указанные форматы."""
    out_dir = output_base_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    base_name = output_base_path.name
    if not formats or set(formats) - SUPPORTED_FORMATS:
        raise ValueError("Форматы экспорта: txt, srt, vtt, json, all")

    # 1. Обычный текст TXT
    if "txt" in formats or "all" in formats:
        txt_path = out_dir / f"{base_name}.txt"
        full_text = " ".join(seg.text.strip() for seg in segments)
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(full_text + "\n")
        print(f" -> Сохранено: {txt_path.name}")

    # 2. Субтитры SRT
    if "srt" in formats or "all" in formats:
        srt_path = out_dir / f"{base_name}.srt"
        with open(srt_path, "w", encoding="utf-8") as f:
            for i, seg in enumerate(segments, start=1):
                start = format_timestamp(seg.start, is_srt=True)
                end = format_timestamp(seg.end, is_srt=True)
                f.write(f"{i}\n{start} --> {end}\n{seg.text.strip()}\n\n")
        print(f" -> Сохранено: {srt_path.name}")

    # 3. Субтитры WebVTT
    if "vtt" in formats or "all" in formats:
        vtt_path = out_dir / f"{base_name}.vtt"
        with open(vtt_path, "w", encoding="utf-8") as f:
            f.write("WEBVTT\n\n")
            for seg in segments:
                start = format_timestamp(seg.start, is_srt=False)
                end = format_timestamp(seg.end, is_srt=False)
                f.write(f"{start} --> {end}\n{seg.text.strip()}\n\n")
        print(f" -> Сохранено: {vtt_path.name}")

    # 4. Детализированный JSON
    if "json" in formats or "all" in formats:
        json_path = out_dir / f"{base_name}.json"
        data = {
            "language": info.language,
            "language_probability": round(info.language_probability, 4),
            "duration": round(info.duration, 2),
            "segments": [
                {
                    "id": seg.id,
                    "start": round(seg.start, 2),
                    "end": round(seg.end, 2),
                    "text": seg.text.strip()
                }
                for seg in segments
            ]
        }
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f" -> Сохранено: {json_path.name}")


def process_file(
    file_path: Path,
    model,
    language: Optional[str] = None,
    output_dir: Optional[Path] = None,
    formats: List[str] = None,
    beam_size: int = 5,
    vad_filter: bool = True,
    batched_pipeline=None,
):
    """Транскрибирует один медиафайл."""
    if formats is None:
        formats = ["txt", "srt"]

    print(f"\n[+] Обработка файла: {file_path.name}")
    start_time = time.time()

    options = {"batch_size": getattr(model, "_cutter_batch_size", 4)} if batched_pipeline and vad_filter else {}
    engine = batched_pipeline if batched_pipeline and vad_filter else model
    segments_generator, info = engine.transcribe(
        str(file_path),
        language=language,
        beam_size=beam_size,
        vad_filter=vad_filter,
        vad_parameters=dict(min_silence_duration_ms=500),
        **options,
    )

    lang_desc = info.language.upper()
    lang_prob = round(info.language_probability * 100, 1)
    print(f"    Определен язык: {lang_desc} ({lang_prob}%), длительность: {info.duration:.1f} сек.")
    print("    Идет распознавание...")

    segments = []
    for segment in segments_generator:
        segments.append(segment)
        # Отображение прогресса в консоли в реальном времени
        print(f"    [{format_timestamp(segment.start, is_srt=False)} -> {format_timestamp(segment.end, is_srt=False)}] {segment.text.strip()}")

    target_dir = output_dir if output_dir else file_path.parent
    output_base_path = target_dir / file_path.stem
    save_transcription(segments, info, output_base_path, formats)

    elapsed = time.time() - start_time
    print(f"Завершено за {elapsed:.1f} сек. ({info.duration / max(elapsed, 0.001):.1f}x от реального времени)")


def find_files(input_path: Path) -> List[Path]:
    """Находит все поддерживаемые медиафайлы по пути или в папке."""
    if input_path.is_file():
        if input_path.suffix.lower() in SUPPORTED_EXTENSIONS:
            return [input_path]
        else:
            print(f"Предупреждение: расширение {input_path.suffix} не входит в список типовых, но пробуем обработать.")
            return [input_path]
    elif input_path.is_dir():
        files = []
        for p in sorted(input_path.iterdir()):
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS:
                files.append(p)
        return files
    return []


def interactive_mode():
    """Интерактивный ввод параметров, если скрипт запущен без аргументов."""
    print("=" * 60)
    print("        Транскрибация видео/аудио через faster-whisper")
    print("=" * 60)

    while True:
        raw_path = input("\nВведите путь к видеофайлу или папке: ").strip().strip('"').strip("'")
        if not raw_path:
            print("Путь не может быть пустым.")
            continue
        p = Path(raw_path)
        if not p.exists():
            print(f"Путь '{p}' не найден. Попробуйте снова.")
            continue
        files = find_files(p)
        if not files:
            print(f"В '{p}' не найдено медиафайлов поддерживаемых форматов.")
            continue
        break

    print(f"\nНайдено файлов для обработки: {len(files)}")
    for f in files[:5]:
        print(f" - {f.name}")
    if len(files) > 5:
        print(f"   ... и ещё {len(files) - 5} файлов")

    print("\nВыберите модель Whisper:")
    print("1) tiny    (самая быстрая, минимальная точность)")
    print("2) base    (быстрая, хорошая для английского)")
    print("3) small   (рекомендуется: отличный баланс скорости и качества)")
    print("4) medium  (высокая точность, требует больше ресурсов)")
    print("5) large-v3(максимальная точность)")
    choice = input("Номер модели [по умолчанию 3 (small)]: ").strip()
    model_map = {"1": "tiny", "2": "base", "3": "small", "4": "medium", "5": "large-v3"}
    model_name = model_map.get(choice, "small")

    lang_input = input("\nУкажите язык (например ru, en) или Enter для автоопределения: ").strip()
    language = lang_input.lower() if lang_input else None

    formats_input = input("\nФорматы экспорта через запятую (txt, srt, vtt, json или all) [по умолчанию: txt, srt]: ").strip()
    if not formats_input:
        formats = ["txt", "srt"]
    elif formats_input.lower() == "all":
        formats = ["all"]
    else:
        formats = [f.strip().lower() for f in formats_input.split(",")]

    return files, model_name, language, formats


def main():
    parser = argparse.ArgumentParser(
        description="Транскрибация видео и аудиофайлов с помощью Whisper (faster-whisper)"
    )
    parser.add_argument("path", nargs="?", help="Путь к видео/аудиофайлу или папке с файлами")
    parser.add_argument(
        "-m", "--model",
        default="small",
        choices=["tiny", "base", "small", "medium", "large-v3"],
        help="Размер модели Whisper (по умолчанию: small)"
    )
    parser.add_argument(
        "-l", "--language",
        default=None,
        help="Язык аудио (код ISO-639-1, например 'ru', 'en'). Если не указан - автоопределение."
    )
    parser.add_argument(
        "-o", "--output-dir",
        default=None,
        help="Папка для сохранения результатов (по умолчанию: рядом с исходным файлом)"
    )
    parser.add_argument(
        "-f", "--format",
        default="txt,srt",
        help="Форматы экспорта через запятую: txt, srt, vtt, json или all (по умолчанию: txt,srt)"
    )
    parser.add_argument(
        "-d", "--device",
        default="auto",
        choices=["auto", "cuda", "cpu"],
        help="Устройство вычислений: auto, cuda, cpu (по умолчанию: auto)"
    )
    parser.add_argument(
        "--no-vad",
        action="store_true",
        help="Отключить фильтр голосовой активности (VAD)"
    )

    args = parser.parse_args()

    # Если путь не указан в CLI - запускаем интерактивный диалог
    if not args.path:
        files, model_name, language, formats = interactive_mode()
        output_dir = None
        device_choice = "auto"
        vad_filter = True
    else:
        p = Path(args.path)
        if not p.exists():
            print(f"Ошибка: путь '{args.path}' не существует.")
            sys.exit(1)
        files = find_files(p)
        if not files:
            print(f"Ошибка: в '{args.path}' не найдены медиафайлы.")
            sys.exit(1)
        model_name = args.model
        language = args.language
        output_dir = Path(args.output_dir) if args.output_dir else None
        formats = [x.strip().lower() for x in args.format.split(",")]
        device_choice = args.device
        vad_filter = not args.no_vad

    if not formats or set(formats) - SUPPORTED_FORMATS:
        parser.error("Форматы экспорта: txt, srt, vtt, json, all")
    print("(При первом запуске модель автоматически скачивается с Hugging Face)")
    manager = ModelManager()
    model, pipeline = manager.get(model_name, print, requested_device=device_choice)

    print("Модель готова к работе.")

    total_files = len(files)
    failures = 0
    for idx, f in enumerate(files, start=1):
        print(f"\n==================== [{idx}/{total_files}] ====================")
        try:
            process_file(
                file_path=f,
                model=model,
                language=language,
                output_dir=output_dir,
                formats=formats,
                vad_filter=vad_filter,
                batched_pipeline=pipeline,
            )
        except Exception as err:
            failures += 1
            print(f"Ошибка при обработке {f.name}: {err}")

    print(f"\nГотово: {total_files - failures} из {total_files}. Ошибок: {failures}.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
