"""Сборка, самопроверка и упаковка NVIDIA-релиза для GitHub."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app_meta import APP_NAME, APP_VERSION  # noqa: E402

GITHUB_RELEASE_LIMIT = 2 * 1024 * 1024 * 1024
ARCHIVE_NAME = f"Twitch-Cutter-Profanity-v{APP_VERSION}-windows-x64-nvidia.zip"


def run(command, **kwargs):
    printable = subprocess.list2cmdline([str(item) for item in command])
    print(f"> {printable}", flush=True)
    return subprocess.run([str(item) for item in command], check=True, cwd=ROOT, **kwargs)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build(dist_dir, work_dir):
    if dist_dir.exists():
        shutil.rmtree(dist_dir)
    if work_dir.exists():
        shutil.rmtree(work_dir)
    env = os.environ.copy()
    env.pop("TWITCHCUTTER_CPU_ONLY", None)
    run([
        sys.executable, "-X", "utf8", "-m", "PyInstaller", "--clean", "--noconfirm",
        "--distpath", dist_dir, "--workpath", work_dir, "Twitch Cutter Profanity.spec",
    ], env=env)
    bundle = dist_dir / APP_NAME
    if not (bundle / f"{APP_NAME}.exe").is_file():
        raise RuntimeError(f"PyInstaller не создал {bundle}")
    return bundle


def prepare_bundle(bundle):
    for name in (
        "CHANGELOG.md", "CONTRIBUTING.md", "LICENSE", "README.md",
        "RELEASE_CHECKLIST.md", "SECURITY.md", "THIRD_PARTY_NOTICES.md",
    ):
        shutil.copy2(ROOT / name, bundle / name)
    shutil.copytree(ROOT / "docs", bundle / "docs", dirs_exist_ok=True)
    release_text = (
        f"{APP_NAME} {APP_VERSION}\n"
        "Windows 10/11 x64 · NVIDIA CUDA runtime included · CPU fallback available\n\n"
        f"Run: {APP_NAME}.exe\n"
        "Keep the executable and the _internal folder together.\n"
        "The first selected Whisper model is downloaded from Hugging Face.\n\n"
        "This build is not code-signed. Verify the SHA-256 checksum published with the archive.\n"
        "License details: LICENSE and THIRD_PARTY_NOTICES.md\n"
    )
    (bundle / "RELEASE.txt").write_text(release_text, encoding="utf-8")
    shortcuts = list(bundle.rglob("*.lnk"))
    if shortcuts:
        names = ", ".join(str(path.relative_to(bundle)) for path in shortcuts)
        raise RuntimeError(f"Удалите непереносимые ярлыки из сборки: {names}")


def self_test(bundle):
    exe = bundle / f"{APP_NAME}.exe"
    with tempfile.TemporaryDirectory(prefix="twitch-cutter-profanity-") as folder:
        env = os.environ.copy()
        env.pop("HF_HUB_OFFLINE", None)
        env.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        started = time.monotonic()
        run([exe, "--self-test", folder, "--model", "tiny", "--device", "cpu"],
            env=env, timeout=900)
        report_path = Path(folder) / "selftest.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if not report.get("ok") or report.get("version") != APP_VERSION:
            raise RuntimeError(f"Самопроверка не пройдена: {report_path}")
        print(f"Self-test OK in {time.monotonic() - started:.1f}s", flush=True)


def make_archive(bundle, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / ARCHIVE_NAME
    temporary_archive = output_dir / (ARCHIVE_NAME + ".tmp")
    checksum = output_dir / (ARCHIVE_NAME + ".sha256")
    archive.unlink(missing_ok=True)
    temporary_archive.unlink(missing_ok=True)
    checksum.unlink(missing_ok=True)
    files = sorted(path for path in bundle.rglob("*") if path.is_file())
    total = sum(path.stat().st_size for path in files)
    print(f"Packing {len(files)} files ({total / 1024 ** 3:.2f} GiB)...", flush=True)
    try:
        with zipfile.ZipFile(
            temporary_archive, "w", compression=zipfile.ZIP_DEFLATED,
            compresslevel=9, allowZip64=True,
        ) as packed:
            for index, path in enumerate(files, 1):
                relative = Path(APP_NAME) / path.relative_to(bundle)
                packed.write(path, relative.as_posix())
                if index % 50 == 0 or index == len(files):
                    print(f"  {index}/{len(files)}", flush=True)
        size = temporary_archive.stat().st_size
        if size >= GITHUB_RELEASE_LIMIT:
            raise RuntimeError(f"Архив {size / 1024 ** 3:.2f} ГиБ превышает лимит GitHub 2 ГиБ")
        temporary_archive.replace(archive)
    except Exception:
        temporary_archive.unlink(missing_ok=True)
        raise
    digest = sha256(archive)
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    print(f"Archive: {archive} ({size / 1024 ** 3:.2f} GiB)", flush=True)
    print(f"SHA-256: {digest}", flush=True)
    return archive


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, help="Упаковать существующую сборку без PyInstaller")
    parser.add_argument("--dist-dir", type=Path, default=ROOT / "_release_stage")
    parser.add_argument("--work-dir", type=Path, default=ROOT / "build_release")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "release")
    parser.add_argument("--self-test", action="store_true", help="Запустить frozen self-test перед упаковкой")
    args = parser.parse_args(argv)

    run([sys.executable, "tools/check_release.py", "--source-only"])
    bundle = args.bundle.resolve() if args.bundle else build(args.dist_dir.resolve(), args.work_dir.resolve())
    prepare_bundle(bundle)
    run([sys.executable, "tools/check_release.py", "--bundle", bundle])
    if args.self_test:
        self_test(bundle)
    archive = make_archive(bundle, args.output_dir.resolve())
    run([sys.executable, "tools/check_release.py", "--bundle", bundle, "--archive", archive])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
