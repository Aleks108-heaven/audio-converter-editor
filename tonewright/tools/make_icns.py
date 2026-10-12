"""Write assets/tonewright.icns (the macOS app icon) from the procedurally drawn logo (run once; the result is committed).

An .icns file is a small container: 'icns', the total length, then one record per size, each a type code, its length
and a PNG. The logo is drawn a little smaller than the canvas, which is how macOS icons are laid out."""
from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QBuffer, QIODevice, Qt  # noqa: E402
from PySide6.QtGui import QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from tonewright.ui import icons  # noqa: E402

RECORDS = (("icp4", 16), ("icp5", 32), ("icp6", 64), ("ic07", 128), ("ic08", 256), ("ic09", 512), ("ic10", 1024))
CONTENT = 0.80          # the tile fills this fraction of the canvas


def png_bytes(size: int) -> bytes:
    canvas = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    canvas.fill(Qt.GlobalColor.transparent)
    inner = max(1, round(size * CONTENT))
    logo = icons.logo_pixmap(inner, 1.0).toImage()
    p = QPainter(canvas)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    offset = (size - inner) // 2
    p.drawImage(offset, offset, logo)
    p.end()
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    canvas.save(buf, "PNG")
    return bytes(buf.data())


def build_icns() -> bytes:
    body = b""
    for code, size in RECORDS:
        data = png_bytes(size)
        body += code.encode("ascii") + struct.pack(">I", 8 + len(data)) + data
    return b"icns" + struct.pack(">I", 8 + len(body)) + body


def main() -> None:
    app = QApplication([])
    out = ROOT / "tonewright" / "assets" / "tonewright.icns"
    out.write_bytes(build_icns())
    print("wrote", out, out.stat().st_size, "bytes")
    del app


if __name__ == "__main__":
    main()
