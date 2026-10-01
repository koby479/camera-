import random
from app.core.dvrip import XMFrameParser


def fd(nal: bytes) -> bytes:
    return b"\x00\x00\x01\xfd" + len(nal).to_bytes(4, "little") + nal


def fc(nal: bytes) -> bytes:
    return b"\x00\x00\x01\xfc\x01\x19\x00\x00" + b"\x00" * 4 + len(nal).to_bytes(4, "little") + nal


def nal(n, tag):
    return b"\x00\x00\x00\x01\x02\x01" + bytes([tag]) + bytes(random.Random(n).randrange(4, 256) for _ in range(n))


def run(stream, step):
    p, out = XMFrameParser(), []
    for k in range(0, len(stream), step):
        out += p.feed(stream[k:k + step])
    return p, b"".join(out)


def test_plain_frames_unchanged():
    frames = [nal(300, 1), nal(50, 2), nal(70, 3)]
    stream = fc(frames[0]) + fd(frames[1]) + fd(frames[2])
    p, out = run(stream, 17)
    assert out == b"".join(frames) and p.embedded == 0 and p.dropped == 0


def test_frames_swallowed_by_previous_length():
    a, b, c, d = nal(400, 1), nal(80, 2), nal(90, 3), nal(60, 4)
    swallowing = fc(a + fd(b) + fd(c))          # I frame whose length also covers two P frames
    stream = swallowing + fd(d)
    for step in (1, 7, 4096):
        p, out = run(stream, step)
        assert out == a + b + c + d
        assert p.embedded == 2
