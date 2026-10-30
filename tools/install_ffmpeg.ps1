param(
    [string]$Destination = (Join-Path $env:TEMP 'twitch-cutter-ffmpeg-9.0.2')
)

$ErrorActionPreference = 'Stop'
$version = '9.0.2'
$url = "https://github.com/GyanD/codexffmpeg/releases/download/$version/ffmpeg-$version-full_build.zip"
# SHA-256 from the Microsoft WinGet manifest for Gyan.FFmpeg 9.0.2.
$expectedHash = '759D0A9831C436A0EB331AD36F236C06CB04AAA0005DA46571F0E9D3D9206F6B'
$archive = Join-Path $Destination "ffmpeg-$version-full_build.zip"
$extract = Join-Path $Destination 'files'

New-Item -ItemType Directory -Path $Destination -Force | Out-Null
$ffmpeg = Get-ChildItem -Path $extract -Recurse -File -Filter 'ffmpeg.exe' -ErrorAction SilentlyContinue | Select-Object -First 1

if (-not $ffmpeg) {
    if (-not (Test-Path -LiteralPath $archive)) {
        Write-Host "Downloading FFmpeg $version..."
        Invoke-WebRequest -Uri $url -OutFile $archive -UseBasicParsing
    }
    $actualHash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash
    if ($actualHash -ne $expectedHash) {
        Remove-Item -LiteralPath $archive -Force
        throw "FFmpeg archive checksum mismatch: $actualHash"
    }
    if (Test-Path -LiteralPath $extract) {
        Remove-Item -LiteralPath $extract -Recurse -Force
    }
    Expand-Archive -LiteralPath $archive -DestinationPath $extract -Force
    $ffmpeg = Get-ChildItem -Path $extract -Recurse -File -Filter 'ffmpeg.exe' | Select-Object -First 1
}

if (-not $ffmpeg) {
    throw 'ffmpeg.exe was not found after extraction'
}
$bin = $ffmpeg.Directory.FullName
if (-not (Test-Path -LiteralPath (Join-Path $bin 'ffprobe.exe'))) {
    throw 'ffprobe.exe is missing from the FFmpeg package'
}

$env:PATH = "$bin;$env:PATH"
if ($env:GITHUB_PATH) {
    Add-Content -LiteralPath $env:GITHUB_PATH -Value $bin -Encoding UTF8
}
Write-Host "FFmpeg $version ready: $bin"
& (Join-Path $bin 'ffmpeg.exe') -version | Select-Object -First 1
