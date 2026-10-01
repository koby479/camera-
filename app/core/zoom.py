"""Digital-zoom math, kept free of Qt so it can be unit-tested anywhere.

The view is a rectangle inside the full frame, in normalised 0..1 coordinates:
(x, y) is its top-left corner and its size is (1/zoom, 1/zoom).
"""
from __future__ import annotations

MIN_ZOOM = 1.0
MAX_ZOOM = 8.0
STEP = 1.25          # one mouse-wheel notch


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def fit_rect(src_w: float, src_h: float, dst_w: float, dst_h: float) -> tuple[float, float, float, float]:
    """Where a src_w x src_h picture lands inside dst_w x dst_h with aspect ratio kept and
    centred: returns (offset_x, offset_y, width, height)."""
    if src_w <= 0 or src_h <= 0 or dst_w <= 0 or dst_h <= 0:
        return 0.0, 0.0, 0.0, 0.0
    scale = min(dst_w / src_w, dst_h / src_h)
    w, h = src_w * scale, src_h * scale
    return (dst_w - w) / 2, (dst_h - h) / 2, w, h


class ZoomState:
    def __init__(self):
        self.zoom = MIN_ZOOM
        self.x = 0.0
        self.y = 0.0

    @property
    def active(self) -> bool:
        return self.zoom > MIN_ZOOM + 1e-6

    @property
    def size(self) -> float:
        return 1.0 / self.zoom

    def reset(self):
        self.zoom, self.x, self.y = MIN_ZOOM, 0.0, 0.0

    def _clamp_pos(self):
        s = self.size
        self.x = _clamp(self.x, 0.0, 1.0 - s)
        self.y = _clamp(self.y, 0.0, 1.0 - s)

    def zoom_at(self, new_zoom: float, u: float = 0.5, v: float = 0.5):
        """Change the zoom keeping the image point under the cursor fixed.
        (u, v) is the cursor position inside the visible view, 0..1."""
        u, v = _clamp(u, 0.0, 1.0), _clamp(v, 0.0, 1.0)
        px, py = self.x + u * self.size, self.y + v * self.size     # image point under the cursor
        self.zoom = _clamp(new_zoom, MIN_ZOOM, MAX_ZOOM)
        self.x, self.y = px - u * self.size, py - v * self.size
        self._clamp_pos()
        if not self.active:
            self.reset()

    def wheel(self, notches: float, u: float = 0.5, v: float = 0.5):
        """Positive notches = zoom in."""
        self.zoom_at(self.zoom * (STEP ** notches), u, v)

    def pan(self, du: float, dv: float):
        """Drag the picture by (du, dv), given as fractions of the visible view."""
        if not self.active:
            return
        self.x -= du * self.size
        self.y -= dv * self.size
        self._clamp_pos()

    def crop_rect(self, img_w: int, img_h: int) -> tuple[int, int, int, int]:
        """The visible region in pixels of an img_w x img_h frame: (x, y, w, h)."""
        w = max(1, min(img_w, round(img_w * self.size)))
        h = max(1, min(img_h, round(img_h * self.size)))
        x = int(_clamp(round(self.x * img_w), 0, img_w - w))
        y = int(_clamp(round(self.y * img_h), 0, img_h - h))
        return x, y, w, h
