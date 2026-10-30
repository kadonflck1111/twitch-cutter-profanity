"""Воспроизводимое сравнение прежнего trim/concat и последовательного рендера.

python tools/benchmark_render.py --output-dir <temporary-folder>
Зависимость измерения памяти: psutil. Все исходники и результаты синтетические.
"""

import argparse
import json
import platform
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psutil

from media_utils import capture_process, filter_script_args, media_tool, probe_media, run_ffmpeg, video_encoder
from stream_cutter import export_cut_video


def measure(function):
    stop = threading.Event()
    peak = [0]
    parent = psutil.Process()

    def sample():
        while not stop.is_set():
            total = 0
            for child in parent.children(recursive=True):
                try:
                    if "ffmpeg" in child.name().lower():
                        memory = child.memory_info()
                        total += getattr(memory, "peak_wset", memory.rss)
                except psutil.Error:
                    pass
            peak[0] = max(peak[0], total)
            stop.wait(0.02)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    started = time.perf_counter()
    try:
        function()
    finally:
        elapsed = time.perf_counter() - started
        stop.set()
        sampler.join()
    return {"seconds": round(elapsed, 3), "peak_ffmpeg_mib": round(peak[0] / 1024 ** 2, 1)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--cuts", type=int, default=30)
    args = parser.parse_args()
    if not 2 <= args.cuts <= 100:
        parser.error("--cuts должен быть от 2 до 100")
    root = args.output_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    source = root / "benchmark_source.mp4"
    duration = args.cuts * 3
    intervals = [(i * 3 + 0.2, i * 3 + 1.4) for i in range(args.cuts)]
    expected = round(sum(end - start for start, end in intervals), 3)
    run_ffmpeg([
        "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=30:duration={duration}",
        "-f", "lavfi", "-i", f"sine=sample_rate=48000:duration={duration}",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20", "-g", "60", "-bf", "2",
        "-c:a", "aac", "-b:a", "192k", source,
    ])
    info = probe_media(source)
    detected_encoder = video_encoder()
    parts, inputs = [], []
    for i, (start, end) in enumerate(intervals):
        parts.extend([f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS[v{i}]",
                      f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[a{i}]"])
        inputs.append(f"[v{i}][a{i}]")
    graph = root / "legacy.fffilter"
    graph.write_text(";".join(parts) + f";{''.join(inputs)}concat=n={len(intervals)}:v=1:a=1[outv][outa]", encoding="utf-8")
    results = {"platform": platform.platform(), "input_seconds": duration, "kept_seconds": expected,
               "cuts": len(intervals), "resolution": "1280x720", "fps": 30,
               "ffmpeg": capture_process([media_tool("ffmpeg"), "-version"]).splitlines()[0],
               "baseline_note": "Original trim/concat graph; only removed FFmpeg 9 script option adapted.",
               "cases": {}}
    for name, encoder in [("legacy_cpu", None), ("sequential_cpu", "libx264"), ("sequential_gpu", detected_encoder)]:
        if name == "sequential_gpu" and encoder != "h264_nvenc":
            continue
        output = root / f"{name}.mp4"

        def render():
            if name == "legacy_cpu":
                run_ffmpeg(["-i", source, *filter_script_args(graph), "-map", "[outv]", "-map", "[outa]",
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                            "-c:a", "aac", "-b:a", "192k", output])
            elif not export_cut_video(source, intervals, output, print, metadata=info, encoder=encoder):
                raise RuntimeError(f"Не завершен экспорт {name}")

        print(f"Measuring {name}...", flush=True)
        measured = measure(render)
        metadata = probe_media(output)
        frames = int(capture_process([media_tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
                                      "-count_frames", "-show_entries", "stream=nb_read_frames",
                                      "-of", "csv=p=0", str(output)]).strip())
        if abs(metadata.duration - expected) > 0.08 or frames != round(expected * 30):
            raise AssertionError(f"{name}: duration={metadata.duration}, frames={frames}")
        measured.update({"duration": metadata.duration, "frames": frames})
        results["cases"][name] = measured
        print(json.dumps(measured), flush=True)
    (root / "benchmark.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Report: {root / 'benchmark.json'}")


if __name__ == "__main__":
    main()
