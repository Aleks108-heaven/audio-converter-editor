"""Write assets/tonewright.ico from the procedurally drawn logo (run once; the result is committed)."""
from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QBuffer, QIODevice  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from tonewright.ui import icons  # noqa: E402


def main() -> None:
    app = QApplication([])
    sizes = (16, 24, 32, 48, 64, 128, 256)
    images = []
    for size in sizes:
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        icons.logo_pixmap(size, 1.0).save(buf, "PNG")
        images.append((size, bytes(buf.data())))
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for size, data in images:
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset + len(blobs))
        blobs += data
    out = ROOT / "tonewright" / "assets"
    out.mkdir(exist_ok=True)
    (out / "tonewright.ico").write_bytes(header + entries + blobs)
    print("wrote", out / "tonewright.ico")
    del app


if __name__ == "__main__":
    main()
