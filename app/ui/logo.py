"""The program's logo, drawn with QPainter (so it is sharp at every size and needs no image file at run time).
tools/make_logo.py renders the same drawing into app/assets/logo.ico / logo.png for the EXE file and the shortcuts."""
from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QIcon, QImage, QLinearGradient, QPainter, QPixmap, QRadialGradient

SIZES = (16, 20, 24, 32, 48, 64, 128, 256)
BADGES = {"alert": "#ffb020", "error": "#ff4d4f"}


def render_logo(size: int, badge: str | None = None) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
    p.setPen(Qt.PenStyle.NoPen)
    s = float(size)

    bg = QLinearGradient(0, 0, 0, s)                          # rounded dark tile
    bg.setColorAt(0.0, QColor("#2a3f72"))
    bg.setColorAt(1.0, QColor("#0a1124"))
    p.setBrush(bg)
    p.drawRoundedRect(QRectF(s * 0.03, s * 0.03, s * 0.94, s * 0.94), s * 0.22, s * 0.22)

    c = QPointF(s * 0.5, s * 0.53)                            # the lens
    r = s * 0.31
    ring = QLinearGradient(c.x() - r, c.y() - r, c.x() + r, c.y() + r)
    ring.setColorAt(0.0, QColor("#8fbaff"))
    ring.setColorAt(1.0, QColor("#2a5ee0"))
    p.setBrush(ring)
    p.drawEllipse(c, r, r)
    glass = QRadialGradient(QPointF(c.x() - r * 0.25, c.y() - r * 0.30), r * 0.95)
    glass.setColorAt(0.0, QColor("#1f3668"))
    glass.setColorAt(0.65, QColor("#0a1330"))
    glass.setColorAt(1.0, QColor("#04081a"))
    p.setBrush(glass)
    p.drawEllipse(c, r * 0.72, r * 0.72)
    p.setBrush(QColor(86, 148, 255, 190))                     # the iris
    p.drawEllipse(c, r * 0.34, r * 0.34)
    if size >= 32:
        p.setBrush(QColor(255, 255, 255, 215))                # reflection
        p.drawEllipse(QPointF(c.x() - r * 0.30, c.y() - r * 0.32), r * 0.14, r * 0.14)
        p.setBrush(QColor(255, 255, 255, 70))
        p.drawEllipse(QPointF(c.x() + r * 0.22, c.y() + r * 0.26), r * 0.07, r * 0.07)

    p.setBrush(QColor("#ff4d4f"))                             # the "recording" dot
    p.drawEllipse(QPointF(s * 0.77, s * 0.23), s * 0.08, s * 0.08)

    if badge in BADGES:                                       # bottom-right marker (new events while in the tray)
        p.setBrush(QColor("#0a1124"))
        p.drawEllipse(QPointF(s * 0.76, s * 0.76), s * 0.17, s * 0.17)
        p.setBrush(QColor(BADGES[badge]))
        p.drawEllipse(QPointF(s * 0.76, s * 0.76), s * 0.125, s * 0.125)
    p.end()
    return img


def make_icon(badge: str | None = None) -> QIcon:
    icon = QIcon()
    for size in SIZES:
        icon.addPixmap(QPixmap.fromImage(render_logo(size, badge)))
    return icon
