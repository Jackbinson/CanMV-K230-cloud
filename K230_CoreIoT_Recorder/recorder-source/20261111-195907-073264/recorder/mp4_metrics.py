"""Read finalized MP4 video sample counts without loading the video into RAM."""
import os


def _u32(data):
    if len(data) != 4:
        raise ValueError("Truncated MP4 integer")
    return (data[0] << 24) | (data[1] << 16) | (data[2] << 8) | data[3]


def _u64(data):
    return (_u32(data[:4]) << 32) | _u32(data[4:])


def _boxes(stream, start, end):
    position = start
    while position < end:
        if end - position < 8:
            raise ValueError("Truncated MP4 box")
        stream.seek(position)
        header = stream.read(8)
        size = _u32(header[:4])
        kind = header[4:8]
        header_size = 8
        if size == 1:
            size = _u64(stream.read(8))
            header_size = 16
        elif size == 0:
            size = end - position
        if size < header_size or position + size > end:
            raise ValueError("Invalid MP4 box bounds")
        yield kind, position + header_size, position + size
        position += size


def _table(stream, start, end, result):
    for kind, content, limit in _boxes(stream, start, end):
        if kind == b"stsz":
            if limit - content < 12:
                raise ValueError("Truncated stsz")
            stream.seek(content + 8)
            result["video_frames"] = _u32(stream.read(4))
        elif kind == b"stts":
            stream.seek(content + 4)
            count = _u32(stream.read(4))
            if count > 100000 or content + 8 + count * 8 > limit:
                raise ValueError("Invalid stts table")
            samples = 0
            ticks = 0
            for unused in range(count):
                row = stream.read(8)
                n = _u32(row[:4])
                delta = _u32(row[4:])
                samples += n
                ticks += n * delta
            result["timed_samples"] = samples
            result["duration_ticks"] = ticks


def _media(stream, start, end):
    result = {}
    for kind, content, limit in _boxes(stream, start, end):
        if kind == b"hdlr":
            stream.seek(content + 8)
            result["handler"] = stream.read(4)
        elif kind == b"mdhd":
            stream.seek(content)
            version = stream.read(1)[0]
            if version not in (0, 1):
                raise ValueError("Unsupported mdhd version")
            stream.seek(content + (20 if version == 1 else 12))
            result["time_scale"] = _u32(stream.read(4))
        elif kind == b"minf":
            for subkind, subcontent, sublimit in _boxes(stream, content, limit):
                if subkind == b"stbl":
                    _table(stream, subcontent, sublimit, result)
    return result


def read_video_metrics(path, expected_frames=None):
    """Read video sample tables; verify against the writer's picture count.

    A sample table alone cannot distinguish pictures from codec-only packets.
    The recorder must exclude HEADER packets before muxing and pass its count.
    """
    size = os.stat(path)[6]
    tracks = []
    with open(path, "rb") as stream:
        for kind, content, limit in _boxes(stream, 0, size):
            if kind != b"moov":
                continue
            for subkind, subcontent, sublimit in _boxes(stream, content, limit):
                if subkind != b"trak":
                    continue
                for trackkind, trackcontent, tracklimit in _boxes(stream, subcontent, sublimit):
                    if trackkind == b"mdia":
                        track = _media(stream, trackcontent, tracklimit)
                        if track.get("handler") == b"vide":
                            tracks.append(track)
    if len(tracks) != 1:
        raise ValueError("Expected one finalized MP4 video track")
    track = tracks[0]
    frames = track.get("video_frames", 0)
    scale = track.get("time_scale", 0)
    ticks = track.get("duration_ticks", 0)
    if frames <= 0 or scale <= 0 or ticks <= 0:
        raise ValueError("MP4 video sample table missing or empty")
    if frames != track.get("timed_samples"):
        raise ValueError("MP4 sample counts do not agree")
    if expected_frames is not None and frames != expected_frames:
        raise ValueError("MP4 samples do not match successfully written pictures")
    seconds = ticks / scale
    return {"video_frames": frames, "video_duration_s": seconds,
            "fps_media_timeline": frames / seconds, "file_bytes": size}
