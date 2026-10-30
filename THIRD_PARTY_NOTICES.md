# Third-party notices

Twitch Cutter Profanity is licensed under the MIT License. The application
uses and, in the Windows release, redistributes third-party components under
their own licenses. Those licenses are not replaced by the project license.

Authoritative license texts shipped with the application are stored in
`_internal/licenses` and `_internal/design/vendor/licenses`.

## Main components

| Component | License | Project |
| --- | --- | --- |
| faster-whisper 1.2.1 | MIT | https://github.com/SYSTRAN/faster-whisper |
| CTranslate2 4.8.3 | MIT | https://github.com/OpenNMT/CTranslate2 |
| pywebview 6.2.1 | BSD 3-Clause | https://github.com/r0x0r/pywebview |
| NumPy | BSD 3-Clause and bundled notices | https://numpy.org/ |
| PyInstaller | GPL-2.0-or-later with bootloader exception | https://pyinstaller.org/ |
| Phosphor Icons | MIT | https://phosphoricons.com/ |
| Manrope | SIL Open Font License 1.1 | https://github.com/sharanda/manrope |
| Geist Mono | SIL Open Font License 1.1 | https://github.com/vercel/geist-font |

## FFmpeg

The Windows package contains separate `ffmpeg` and `ffprobe` executables and
invokes them as external processes. The currently verified full build is
FFmpeg 9.0.2 from Gyan.dev and is distributed under GPL version 3 or later.
The exact license text is included at `_internal/licenses/ffmpeg/LICENSE`.

- Upstream source and releases: https://ffmpeg.org/download.html
- Windows build information and archives: https://www.gyan.dev/ffmpeg/builds/

Anyone rebuilding the application must verify the license and corresponding
source for the exact FFmpeg binaries placed on `PATH` at build time.

## NVIDIA runtime

The NVIDIA-enabled Windows package includes selected DLLs from these Python
packages:

- `nvidia-cublas-cu12==12.9.2.10`
- `nvidia-cudnn-cu12==9.27.0.42`
- `nvidia-cuda-nvrtc-cu12==12.9.86`

These components are distributed under NVIDIA proprietary license terms.
Copies of the package license texts are included under
`_internal/licenses/nvidia-*`. Redistributors are responsible for complying
with the NVIDIA terms applicable to their release.

## Downloaded speech models

Whisper models are not stored in this repository or release archive. They are
downloaded from Hugging Face on first use and remain subject to the license
and terms shown in the corresponding model repository.
