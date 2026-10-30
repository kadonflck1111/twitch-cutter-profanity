"""FCP7 XML для DaVinci Resolve с корректными NTSC таймкодами и связями A/V."""

import math
import xml.etree.ElementTree as ET
from pathlib import Path

from media_utils import atomic_output, merge_intervals


def create_davinci_xml(
    media_path: Path,
    kept_intervals,
    total_duration: float,
    fps: float = 60.0,
    width: int = 1920,
    height: int = 1080,
    xml_output_path: Path = None,
    *,
    has_video=True,
    has_audio=True,
    sample_rate=48000,
    channels=2,
) -> Path:
    media_path = Path(media_path).resolve()
    xml_output_path = Path(xml_output_path) if xml_output_path else media_path.with_name(f"{media_path.stem}_davinci.xml")
    if not math.isfinite(fps) or fps <= 0 or not math.isfinite(total_duration) or total_duration <= 0:
        raise ValueError("Некорректная частота кадров или длительность")
    timebase = round(fps)
    ntsc = timebase in (24, 30, 60, 120) and abs(fps - timebase * 1000 / 1001) < 0.01
    actual_fps = timebase * 1000 / 1001 if ntsc else fps
    source_frames = round(total_duration * actual_fps)
    clips = []
    for start, end in merge_intervals(kept_intervals):
        in_frame = max(0, round(start * actual_fps))
        out_frame = min(source_frames, round(end * actual_fps))
        if out_frame > in_frame:
            clips.append((in_frame, out_frame))

    def text(parent, name, value):
        ET.SubElement(parent, name).text = str(value)

    def rate(parent):
        node = ET.SubElement(parent, "rate")
        text(node, "timebase", timebase)
        text(node, "ntsc", "TRUE" if ntsc else "FALSE")

    def video_format(parent):
        sample = ET.SubElement(ET.SubElement(parent, "format"), "samplecharacteristics")
        rate(sample)
        text(sample, "width", width)
        text(sample, "height", height)
        text(sample, "pixelaspectratio", "square")
        text(sample, "fielddominance", "none")

    xmeml = ET.Element("xmeml", version="4")
    sequence = ET.SubElement(xmeml, "sequence", id="Sequence-1")
    text(sequence, "name", f"Нарезка_{media_path.stem}")
    # Сумма фактических длин клипов, а не округление общей суммы секунд.
    text(sequence, "duration", sum(e - s for s, e in clips))
    rate(sequence)
    media = ET.SubElement(sequence, "media")
    tracks = []
    if has_video:
        video = ET.SubElement(media, "video")
        video_format(video)
        tracks.append(("video", 1, ET.SubElement(video, "track")))
    if has_audio:
        audio = ET.SubElement(media, "audio")
        text(audio, "numOutputChannels", channels)
        sample = ET.SubElement(ET.SubElement(audio, "format"), "samplecharacteristics")
        text(sample, "depth", 16)
        text(sample, "samplerate", sample_rate)
        for channel in range(1, channels + 1):
            tracks.append(("audio", channel, ET.SubElement(audio, "track")))

    file_defined = False
    cursor = 0
    for index, (in_frame, out_frame) in enumerate(clips, 1):
        length = out_frame - in_frame
        references = [(kind, channel, f"clipitem-{kind}-{channel}-{index}") for kind, channel, _ in tracks]
        for kind, channel, track in tracks:
            clip = ET.SubElement(track, "clipitem", id=f"clipitem-{kind}-{channel}-{index}")
            text(clip, "name", f"{media_path.name} [{index}]")
            text(clip, "enabled", "TRUE")
            text(clip, "duration", source_frames)
            rate(clip)
            for name, value in (("start", cursor), ("end", cursor + length), ("in", in_frame), ("out", out_frame)):
                text(clip, name, value)
            source = ET.SubElement(clip, "file", id="file-1")
            if not file_defined:
                text(source, "name", media_path.name)
                text(source, "pathurl", media_path.as_uri())
                text(source, "duration", source_frames)
                rate(source)
                source_media = ET.SubElement(source, "media")
                if has_video:
                    sample = ET.SubElement(ET.SubElement(source_media, "video"), "samplecharacteristics")
                    rate(sample)
                    text(sample, "width", width)
                    text(sample, "height", height)
                if has_audio:
                    source_audio = ET.SubElement(source_media, "audio")
                    sample = ET.SubElement(source_audio, "samplecharacteristics")
                    text(sample, "depth", 16)
                    text(sample, "samplerate", sample_rate)
                    text(source_audio, "channelcount", channels)
                file_defined = True
            source_track = ET.SubElement(clip, "sourcetrack")
            text(source_track, "mediatype", kind)
            text(source_track, "trackindex", channel)
            for linked_kind, linked_channel, reference in references:
                link = ET.SubElement(clip, "link")
                text(link, "linkclipref", reference)
                text(link, "mediatype", linked_kind)
                text(link, "trackindex", linked_channel)
                text(link, "clipindex", index)
                if linked_kind == "audio":
                    text(link, "groupindex", 1)
        cursor += length

    ET.indent(xmeml, space="  ")
    with atomic_output(xml_output_path) as temporary:
        with temporary.open("wb") as output:
            output.write(b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n')
            ET.ElementTree(xmeml).write(output, encoding="utf-8")
    return xml_output_path
