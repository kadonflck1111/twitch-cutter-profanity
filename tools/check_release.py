"""Проверка исходников и Windows-артефактов перед публикацией на GitHub."""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GITHUB_FILE_LIMIT = 100 * 1024 * 1024
GITHUB_RELEASE_LIMIT = 2 * 1024 * 1024 * 1024
TEXT_SUFFIXES = {
    ".css", ".html", ".ini", ".js", ".json", ".md", ".ps1", ".py",
    ".spec", ".toml", ".txt", ".yaml", ".yml",
}
IGNORED_DIRS = {
    ".git", ".idea", ".venv", ".vscode", "__pycache__", "_dist_previous",
    "_dist_stage", "_release_stage", "artifacts", "build", "build_icon",
    "build_release", "dist", "dist_icon", "release", "venv",
}


class CheckError(RuntimeError):
    pass


def _load_metadata():
    values = {}
    source = ROOT / "app_meta.py"
    exec(compile(source.read_text(encoding="utf-8"), str(source), "exec"), values)
    return values["APP_NAME"], values["APP_VERSION"]


def _repository_files():
    if (ROOT / ".git").exists():
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=ROOT, check=True, capture_output=True,
        )
        for raw in result.stdout.split(b"\0"):
            if raw:
                yield ROOT / raw.decode("utf-8", errors="surrogateescape")
        return
    for path in ROOT.rglob("*"):
        if path.is_file() and not any(part in IGNORED_DIRS for part in path.relative_to(ROOT).parts):
            yield path


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def check_source(tag=None):
    app_name, version = _load_metadata()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise CheckError(f"Некорректная SemVer-версия: {version}")
    if tag and tag != f"v{version}":
        raise CheckError(f"Тег {tag!r} не совпадает с версией v{version}")

    required = (
        ".editorconfig", ".gitattributes", ".gitignore", "CHANGELOG.md", "CONTRIBUTING.md",
        "LICENSE", "README.md", "RELEASE_CHECKLIST.md", "SECURITY.md", "THIRD_PARTY_NOTICES.md",
        "app_meta.py", "icon.ico", "icon.png", "requirements-build.txt",
        "requirements-dev.txt", "requirements-gpu.txt", "requirements.txt",
        "Twitch Cutter Profanity.spec",
        "docs/application.png",
        "packaging/windows_version_info.txt", "packaging/licenses/FFmpeg-GPL-3.0.txt",
        "tools/install_ffmpeg.ps1",
        ".github/workflows/ci.yml", ".github/workflows/release.yml",
    )
    missing = [name for name in required if not (ROOT / name).is_file()]
    if missing:
        raise CheckError("Отсутствуют обязательные файлы: " + ", ".join(missing))

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    version_info = (ROOT / "packaging/windows_version_info.txt").read_text(encoding="utf-8")
    index = (ROOT / "design/index.html").read_text(encoding="utf-8")
    expected_archive = f"Twitch-Cutter-Profanity-v{version}-windows-x64-nvidia.zip"
    if version not in readme or expected_archive not in readme or f"## [{version}]" not in changelog:
        raise CheckError("Версия не синхронизирована с README или CHANGELOG")
    for marker in (f"FileVersion', '{version}'", f"ProductVersion', '{version}'"):
        if marker not in version_info:
            raise CheckError("Версия не синхронизирована с Windows version info")
    if app_name not in index or f'id="systemVersion">{version}<' not in index:
        raise CheckError("Название или версия отсутствуют в design/index.html")

    files = sorted(set(_repository_files()))
    oversized = [path for path in files if path.stat().st_size >= GITHUB_FILE_LIMIT]
    if oversized:
        names = ", ".join(str(path.relative_to(ROOT)) for path in oversized)
        raise CheckError(f"Файлы репозитория превышают лимит GitHub 100 МБ: {names}")

    forbidden_names = (
        "Twitch" + "CutterMats",
        "D:" + "\\antigastra",
        "D:" + "\\TWITCHCUTTER",
    )
    private_key = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
    credential = re.compile(
        r"(?i)(?:api[_-]?key|client[_-]?secret|password)\s*[:=]\s*['\"][^'\"${}]{8,}['\"]"
    )
    findings = []
    for path in files:
        if path.suffix.lower() not in TEXT_SUFFIXES or path.stat().st_size > 5 * 1024 * 1024:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for value in forbidden_names:
            if value in text:
                findings.append(f"{path.relative_to(ROOT)}: запрещённая строка {value!r}")
        if private_key.search(text) or credential.search(text):
            findings.append(f"{path.relative_to(ROOT)}: возможный секрет")
    if findings:
        raise CheckError("\n".join(findings))

    try:
        from PIL import Image
    except ImportError as exc:
        raise CheckError("Для проверки иконок установите requirements-dev.txt") from exc
    with Image.open(ROOT / "icon.png") as image:
        if image.format != "PNG" or image.size != (800, 800):
            raise CheckError("icon.png должен быть PNG 800×800")
    with Image.open(ROOT / "icon.ico") as image:
        expected = {(16, 16), (20, 20), (24, 24), (32, 32), (40, 40),
                    (48, 48), (64, 64), (128, 128), (256, 256)}
        if set(image.ico.sizes()) != expected:
            raise CheckError("icon.ico не содержит все обязательные размеры")

    return {"application": app_name, "version": version, "files": len(files)}


def check_bundle(bundle):
    app_name, version = _load_metadata()
    bundle = Path(bundle).resolve()
    required = (
        f"{app_name}.exe", "CHANGELOG.md", "CONTRIBUTING.md", "LICENSE", "README.md",
        "RELEASE.txt", "RELEASE_CHECKLIST.md", "SECURITY.md", "THIRD_PARTY_NOTICES.md",
        "docs/application.png", "_internal/icon.ico", "_internal/design/index.html",
        "_internal/bin/ffmpeg.exe", "_internal/bin/ffprobe.exe",
        "_internal/licenses/ffmpeg/FFmpeg-GPL-3.0.txt",
    )
    missing = [name for name in required if not (bundle / name).is_file()]
    if missing:
        raise CheckError("В сборке отсутствуют файлы: " + ", ".join(missing))
    if list(bundle.rglob("*.lnk")):
        raise CheckError("Релиз не должен содержать непереносимые Windows-ярлыки")
    for package in ("cublas", "cudnn", "cuda_nvrtc"):
        dlls = list((bundle / "_internal" / "nvidia" / package / "bin").glob("*.dll"))
        if not dlls:
            raise CheckError(f"В NVIDIA-сборке отсутствуют DLL: {package}")
    html = (bundle / "_internal/design/index.html").read_text(encoding="utf-8")
    if app_name not in html or version not in html:
        raise CheckError("Название или версия отсутствуют в интерфейсе сборки")
    return {"bundle": str(bundle), "version": version}


def check_archive(archive):
    app_name, version = _load_metadata()
    archive = Path(archive).resolve()
    if archive.stat().st_size >= GITHUB_RELEASE_LIMIT:
        raise CheckError("Архив достиг лимита GitHub Release 2 ГиБ")
    expected_name = f"Twitch-Cutter-Profanity-v{version}-windows-x64-nvidia.zip"
    if archive.name != expected_name:
        raise CheckError(f"Некорректное имя архива: ожидалось {expected_name}")
    checksum = archive.with_name(archive.name + ".sha256")
    if not checksum.is_file() or _sha256(archive) not in checksum.read_text(encoding="ascii").lower():
        raise CheckError("SHA-256 отсутствует или не совпадает")
    prefix = app_name + "/"
    required = {
        prefix + f"{app_name}.exe", prefix + "LICENSE", prefix + "README.md",
        prefix + "RELEASE.txt", prefix + "THIRD_PARTY_NOTICES.md",
    }
    with zipfile.ZipFile(archive) as packed:
        names = set(packed.namelist())
        if packed.testzip() is not None:
            raise CheckError("ZIP-архив повреждён")
        if not required.issubset(names):
            raise CheckError("ZIP-архив не содержит обязательные файлы")
        if any(name.startswith(("/", "\\")) or ".." in Path(name).parts for name in names):
            raise CheckError("ZIP-архив содержит небезопасные пути")
    return {"archive": str(archive), "bytes": archive.stat().st_size, "sha256": _sha256(archive)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-only", action="store_true", help="Проверить только исходники")
    parser.add_argument("--tag", help="Проверить соответствие тега версии, например v0.1.0")
    parser.add_argument("--bundle", type=Path, help="Проверить распакованную Windows-сборку")
    parser.add_argument("--archive", type=Path, help="Проверить ZIP и SHA-256")
    args = parser.parse_args(argv)
    try:
        results = [check_source(args.tag)]
        if args.bundle:
            results.append(check_bundle(args.bundle))
        if args.archive:
            results.append(check_archive(args.archive))
    except (CheckError, OSError, subprocess.CalledProcessError, zipfile.BadZipFile) as exc:
        print(f"RELEASE CHECK FAILED: {exc}", file=sys.stderr)
        return 1
    for result in results:
        print("OK", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
