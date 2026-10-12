"""Application state shared by every page: the library, current item, clipboard, player."""
from __future__ import annotations

import itertools
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Signal

from ..audio.playback import Player
from ..core import ffmpeg
from ..core import tags as tagmod
from ..core.common import CancelToken, ProgressFn
from ..core.convert import ConvertItem
from ..core.document import Clip, Document
from ..core.settings import Favorites, Settings
from .stage import ViewState

MODES = ("convert", "edit", "tags", "audiobook")
_ids = itertools.count(1)


class LibraryItem:
    def __init__(self, path: Optional[Path], name: str) -> None:
        self.id = next(_ids)
        self.path = Path(path) if path else None
        self.name = name
        self.info: Optional[ffmpeg.MediaInfo] = None
        self.stream: Optional[ffmpeg.StreamInfo] = None
        self.duration = 0.0
        self.sr = 44100
        self.channels = 2
        self.tags = tagmod.TagSet()
        self.tags_orig = tagmod.TagSet()
        self.doc: Optional[Document] = None
        self.view = ViewState()
        self.status = "ready"            # ready | converting | done | error | skipped
        self.progress = 0.0
        self.message = ""
        self.output: Optional[Path] = None
        self.last_export: Optional[dict] = None

    # ---- construction helpers
    @classmethod
    def from_probe(cls, path: Path, info: ffmpeg.MediaInfo, tags: tagmod.TagSet,
                   stream: Optional[ffmpeg.StreamInfo] = None) -> "LibraryItem":
        item = cls(path, path.stem)
        item.info = info
        item.stream = stream or info.first_audio
        item.duration = item.stream.duration or info.duration
        item.sr = item.stream.sample_rate
        item.channels = 1 if item.stream.channels == 1 else 2
        item.tags = tags.copy()
        item.tags_orig = tags.copy()
        return item

    @classmethod
    def from_document(cls, doc: Document, name: str) -> "LibraryItem":
        item = cls(doc.path, name)
        item.doc = doc
        item.duration = doc.duration
        item.sr, item.channels = doc.sr, doc.channels
        return item

    # ---- derived
    @property
    def modified(self) -> bool:
        """Has audio that is not saved anywhere yet (edits, or a new/recorded/generated document)."""
        return self.doc is not None and (self.doc.modified or (self.path is None and self.doc.frames > 0))

    @property
    def tags_dirty(self) -> bool:
        a, b = self.tags, self.tags_orig
        return a.text() != b.text() or a.cover != b.cover

    @property
    def length(self) -> float:
        return self.doc.duration if self.doc is not None else self.duration

    @property
    def title(self) -> str:
        return self.name + (" *" if self.modified else "")

    def format_text(self) -> str:
        bits = ""
        lossless = ("pcm", "flac", "alac", "wavpack", "ape", "tta")
        codec = (self.doc.meta.get("codec") if self.doc is not None else None) or (self.stream.codec if self.stream else "")
        depth = (self.doc.meta.get("bits") if self.doc is not None else None) or (self.stream.bits if self.stream else 0)
        if depth and str(codec).startswith(lossless):
            bits = f" · {depth}-bit"
        sr, ch = (self.doc.sr, self.doc.channels) if self.doc is not None else (self.sr, self.channels)
        return f"{sr} Hz{bits} · {'Mono' if ch == 1 else 'Stereo'}"

    def to_convert_item(self) -> ConvertItem:
        """What the converter should encode: the edited document if there is one, else the file."""
        use_doc = self.doc is not None and (self.doc.modified or self.path is None)
        return ConvertItem(
            path=self.path, name=self.name, duration=self.length, sr=self.sr, channels=self.channels,
            stream_index=self.stream.index if self.stream is not None else None,
            tags=self.tags.copy(), doc=self.doc.snapshot() if use_doc else None, key=self)

    def release_document(self) -> None:
        """Drop a decoded but unedited document to free memory; it is re-decoded on demand."""
        if self.doc is not None and not self.doc.modified and self.path is not None:
            self.view.cursor = min(self.view.cursor, self.doc.frames)
            self.doc = None


def probe_path(path: Path) -> LibraryItem:
    """Probe a media file and read its tags (runs on a worker thread)."""
    info = ffmpeg.probe(path)
    if not info.audio:
        raise ffmpeg.FFmpegError(f"{path.name} contains no audio.")
    tags = tagmod.read_tags(path)
    return LibraryItem.from_probe(path, info, tags)


def decode_item(item: LibraryItem, progress: Optional[ProgressFn] = None,
                cancel: Optional[CancelToken] = None) -> Document:
    """Decode the item's file into an editable document (runs on a worker thread)."""
    src = ffmpeg.decode_to_source(item.path, stream=item.stream, info=item.info, progress=progress, cancel=cancel)
    src.peaks()
    meta = {}
    if item.stream is not None:
        meta = {"bits": item.stream.bits, "codec": item.stream.codec, "bit_rate": item.stream.bit_rate,
                "container": item.info.format_name if item.info else ""}
    return Document.from_source(src, name=item.name, path=item.path, meta=meta)


class AppState(QObject):
    itemsChanged = Signal()
    itemUpdated = Signal(object)
    currentChanged = Signal(object)
    modeChanged = Signal(str)

    def __init__(self, settings: Optional[Settings] = None, favorites: Optional[Favorites] = None,
                 player: Optional[Player] = None) -> None:
        super().__init__()
        self.settings = settings or Settings()
        self.favorites = favorites or Favorites()
        self.player = player or Player()
        self.items: list[LibraryItem] = []
        self.current: Optional[LibraryItem] = None
        self.mode = "edit"
        self.clipboard: Optional[Clip] = None

    # ---- library
    def add(self, item: LibraryItem, *, select: bool = False, index: Optional[int] = None) -> None:
        if index is None:
            self.items.append(item)
        else:
            self.items.insert(index, item)
        self.itemsChanged.emit()
        if select or self.current is None:
            self.set_current(item)

    def remove(self, item: LibraryItem) -> None:
        if item not in self.items:
            return
        idx = self.items.index(item)
        self.items.remove(item)
        if self.current is item:
            self.current = None
            nxt = self.items[min(idx, len(self.items) - 1)] if self.items else None
            self.itemsChanged.emit()
            self.set_current(nxt, force=True)
            return
        self.itemsChanged.emit()

    def move(self, item: LibraryItem, delta: int) -> None:
        if item not in self.items:
            return
        i = self.items.index(item)
        j = max(0, min(len(self.items) - 1, i + delta))
        if i != j:
            self.items.insert(j, self.items.pop(i))
            self.itemsChanged.emit()

    def reorder(self, items: list[LibraryItem]) -> None:
        self.items = items
        self.itemsChanged.emit()

    def set_current(self, item: Optional[LibraryItem], force: bool = False) -> None:
        if item is self.current and not force:
            return
        self.current = item
        self.currentChanged.emit(item)

    def touched(self, item: LibraryItem) -> None:
        self.itemUpdated.emit(item)

    def set_mode(self, mode: str) -> None:
        if mode in MODES and mode != self.mode:
            self.mode = mode
            self.modeChanged.emit(mode)

    def find(self, path: Path) -> Optional[LibraryItem]:
        for item in self.items:
            if item.path and item.path == path:
                return item
        return None

    def trim_memory(self) -> None:
        """Keep at most the current item's unedited document in memory plus the most recent other."""
        loaded = [i for i in self.items if i.doc is not None and not i.doc.modified and i.path is not None
                  and i is not self.current]
        for item in loaded[:-1]:
            item.release_document()
