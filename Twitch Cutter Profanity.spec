# -*- mode: python ; coding: utf-8 -*-
"""Windows onedir: Edge WebView2, FFmpeg и CUDA runtime находятся рядом с EXE."""

import os
import shutil
from importlib.metadata import distribution
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_all, collect_data_files, collect_dynamic_libs, copy_metadata, get_package_paths,
)

root = Path(SPECPATH)
app_meta = {}
exec(compile((root / 'app_meta.py').read_text(encoding='utf-8'), 'app_meta.py', 'exec'), app_meta)
app_name = app_meta['APP_NAME']

datas = [
    (str(root / 'design'), 'design'),
    (str(root / 'icon.ico'), '.'),
    (str(root / 'LICENSE'), '.'),
    (str(root / 'THIRD_PARTY_NOTICES.md'), '.'),
    (str(root / 'CHANGELOG.md'), '.'),
    (str(root / 'packaging' / 'licenses' / 'FFmpeg-GPL-3.0.txt'), 'licenses/ffmpeg'),
]
binaries = []
hiddenimports = ['webview.platforms.edgechromium', 'webview.platforms.winforms']

fw_data, fw_binaries, fw_imports = collect_all('faster_whisper')
datas += fw_data
binaries += fw_binaries
hiddenimports += fw_imports
# Для инференса не нужны конвертеры моделей и их Torch/Transformers зависимости.
datas += collect_data_files('ctranslate2') + copy_metadata('ctranslate2')
binaries += collect_dynamic_libs('ctranslate2')
datas += collect_data_files('webview') + copy_metadata('pywebview')
binaries += collect_dynamic_libs('webview')

# Без этого замороженная сборка незаметно переходила на CPU даже при наличии GPU.
# TWITCHCUTTER_CPU_ONLY=1 позволяет собрать более компактный вариант без CUDA.
if os.environ.get('TWITCHCUTTER_CPU_ONLY') != '1':
    for package, project in (
        ('cublas', 'nvidia-cublas-cu12'),
        ('cudnn', 'nvidia-cudnn-cu12'),
        ('cuda_nvrtc', 'nvidia-cuda-nvrtc-cu12'),
    ):
        package_dir = Path(get_package_paths('nvidia.' + package)[1])
        dlls = [p for p in (package_dir / 'bin').glob('*.dll') if not p.name.endswith('.alt.dll')]
        if not dlls:
            raise RuntimeError(f'CUDA DLLs missing: install requirements-gpu.txt ({package})')
        binaries += [(str(dll), f'nvidia/{package}/bin') for dll in dlls]
        dist = distribution(project)
        datas += [(str(dist.locate_file(p)), f'licenses/{project}')
                  for p in dist.files or () if 'license' in p.name.lower()]

for tool in ('ffmpeg', 'ffprobe'):
    executable = shutil.which(tool)
    if not executable:
        raise RuntimeError(f'{tool} must be in PATH when building')
    binaries.append((executable, 'bin'))
    license_file = Path(executable).resolve().parents[1] / 'LICENSE'
    if license_file.exists() and tool == 'ffmpeg':
        datas.append((str(license_file), 'licenses/ffmpeg'))

a = Analysis(
    [str(root / 'web_app.py')],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['customtkinter', 'tkinter', 'torch', 'transformers', 'ctranslate2.converters', 'ctranslate2.specs'],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=app_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=[str(root / 'icon.ico')],
    version=str(root / 'packaging' / 'windows_version_info.txt'),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=app_name,
)
