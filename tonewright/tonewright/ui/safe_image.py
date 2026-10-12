"""Decode pictures that did not come from us (cover art inside an audio file, a file the user picked).

A tag can hold any bytes, so art is only decoded when it is a plain JPEG, PNG, GIF or BMP of a sensible size.
Rarer formats (TIFF, WebP, TGA, ICNS...) bring large third-party decoders with a history of memory bugs and are
not needed for cover art.
"""
from __future__ import annotations

import os
from typing import Union

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QImage, QImageReader, QPixmap

ALLOWED_FORMATS = frozenset({"jpeg", "png", "gif", "bmp"})
MAX_SIDE = 12000                 # pixels
MAX_MEGABYTES = 128              # decoded size Qt may allocate


def read_image(source: Union[bytes, bytearray, str, "os.PathLike[str]"]) -> QImage:
    """The picture, or a null QImage if it is not a plain, reasonably sized JPEG/PNG/GIF/BMP."""
    reader = QImageReader()
    buf = None
    if isinstance(source, (bytes, bytearray)):
        buf = QBuffer()
        buf.setData(bytes(source))
        buf.open(QIODevice.OpenModeFlag.ReadOnly)
        reader.setDevice(buf)
    else:
        reader.setFileName(os.fspath(source))
    reader.setAllocationLimit(MAX_MEGABYTES)
    fmt = bytes(reader.format()).decode("ascii", "ignore").lower()
    if fmt == "jpg":
        fmt = "jpeg"
    if fmt not in ALLOWED_FORMATS:
        return QImage()
    size = reader.size()
    if size.isValid() and (size.width() > MAX_SIDE or size.height() > MAX_SIDE):
        return QImage()
    image = reader.read()
    del buf
    return image


def read_pixmap(source: Union[bytes, bytearray, str, "os.PathLike[str]"]) -> QPixmap:
    image = read_image(source)
    return QPixmap() if image.isNull() else QPixmap.fromImage(image)
