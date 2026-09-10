"""MP4 box layout for lifecycle tests whose ffprobe metadata is explicitly mocked.

This is not decodable media; real codec/pixel tests generate media with FFmpeg.
"""


def box(kind, payload=b""):
    return (8 + len(payload)).to_bytes(4, "big") + kind + payload


MP4_STUB = box(b"ftyp", b"isom\0\0\0\0isom") + box(b"moov") + box(b"mdat", b"encoded")
