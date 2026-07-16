"""Контрольный лист: все состояния точки на разных фонах. python preview.py"""

import sys

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter
from PySide6.QtWidgets import QApplication

import dot as D

CELL = 96
PAD = 26
LABEL = 30

STATES = [
    ("покой", dict(state=D.IDLE, hover=False)),
    ("наведён", dict(state=D.IDLE, hover=True)),
    ("раскрыт наполовину", dict(state=D.REC, level=0.3, peak=0.4, disc_r=19.5)),
    ("тихо", dict(state=D.REC, level=0.12, peak=0.2)),
    ("речь", dict(state=D.REC, level=0.55, peak=0.7)),
    ("громко", dict(state=D.REC, level=0.95, peak=0.95)),
    ("думает", dict(state=D.WORK, phase=1.9)),
    ("грузится", dict(state=D.LOADING, phase=3.1)),
    ("ошибка", dict(state=D.ERR,)),
]

BACKDROPS = [
    ("тёмный редактор", QColor(0x1E, 0x1E, 0x1E), None),
    ("белый чат", QColor(0xFF, 0xFF, 0xFF), None),
    ("серый UI", QColor(0x8A, 0x8F, 0x94), None),
    ("обои", QColor(0x2C, 0x4A, 0x3E), (QColor(0x6E, 0x8B, 0x5A), QColor(0x1A, 0x22, 0x2E))),
]


def render(widget, spec) -> QImage:
    widget.state, widget.hover = D.IDLE, False
    widget.level = widget.peak = widget.phase = 0.0
    for k, v in spec.items():
        setattr(widget, k, v)
    if "disc_r" not in spec:  # без анимации — сразу конечный радиус
        widget.disc_r = widget.target_disc()
    img = QImage(D.WIN, D.WIN, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    widget.render(img, QPoint(0, 0), renderFlags=widget.RenderFlag.DrawChildren)
    return img


def main() -> None:
    app = QApplication(sys.argv)
    w = D.Dot(live=False)

    width = PAD + len(STATES) * (CELL + PAD)
    height = LABEL + len(BACKDROPS) * (CELL + PAD) + PAD
    sheet = QImage(width, height, QImage.Format_ARGB32)
    sheet.fill(QColor(0x0E, 0x0C, 0x0A))
    p = QPainter(sheet)
    p.setRenderHint(QPainter.Antialiasing, True)

    f = QFont("Segoe UI", 8)
    p.setFont(f)
    for i, (name, _) in enumerate(STATES):
        p.setPen(QColor(0x9A, 0x92, 0x86))
        x = PAD + i * (CELL + PAD)
        p.drawText(QRectF(x, 6, CELL, 16), Qt.AlignCenter, name)

    for row, (bg_name, base, grad) in enumerate(BACKDROPS):
        y = LABEL + row * (CELL + PAD)
        for col, (name, spec) in enumerate(STATES):
            x = PAD + col * (CELL + PAD)
            cell = QRectF(x, y, CELL, CELL)
            if grad:
                g = QLinearGradient(x, y, x + CELL, y + CELL)
                g.setColorAt(0.0, grad[0])
                g.setColorAt(1.0, grad[1])
                p.fillRect(cell, g)
            else:
                p.fillRect(cell, base)
            img = render(w, spec)
            p.drawImage(QPointF(x + (CELL - D.WIN) / 2, y + (CELL - D.WIN) / 2), img)
        p.setPen(QColor(0x6E, 0x66, 0x5C))
        p.drawText(QRectF(0, y + CELL + 3, width - 6, 16), Qt.AlignRight, bg_name)

    p.end()
    sheet.save("preview.png")
    print(f"preview.png {width}x{height}")

    # И крупный план — на нём видно кромку и края рисок.
    big = render(w, dict(state=D.REC, level=0.62, peak=0.8))
    zoom = QImage(D.WIN * 8, D.WIN * 8, QImage.Format_ARGB32)
    zoom.fill(QColor(0xF2, 0xEE, 0xE8))
    pz = QPainter(zoom)
    pz.drawImage(QRectF(0, 0, D.WIN * 8, D.WIN * 8), big)
    pz.end()
    zoom.save("preview-zoom.png")
    print(f"preview-zoom.png {D.WIN * 8}x{D.WIN * 8}")


if __name__ == "__main__":
    main()
