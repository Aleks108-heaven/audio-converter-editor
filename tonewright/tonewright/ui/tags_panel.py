"""The Tags page: edit titles, artists, cover art and more for one or many files."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QScrollArea, QSizePolicy,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..core import access
from ..core import tags as tagmod
from ..core.tags import TagSet
from . import icons, theme
from .state import AppState, LibraryItem
from .safe_image import read_image, read_pixmap
from .tasks import run_task
from .widgets import divider, make_button, role_label, section_label
from .write_problem import show_write_problem
from ..i18n import tr


def image_bytes_from_qimage(img: QImage, max_side: int = 1400) -> bytes:
    if max(img.width(), img.height()) > max_side:
        img = img.scaled(max_side, max_side, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG" if img.hasAlphaChannel() else "JPG", 92)
    return bytes(buf.data())


class CoverBox(QFrame):
    coverDropped = Signal(object)         # image bytes

    SIZE = 232

    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setAcceptDrops(True)
        self._pix: Optional[QPixmap] = None
        self._text = tr("No cover art\nDrop an image here")

    def set_cover(self, data: Optional[bytes], *, mixed: bool = False) -> None:
        self._pix = None
        if mixed:
            self._text = tr("Different covers\nChoose one to set them all")
        elif data:
            pm = read_pixmap(data)
            if not pm.isNull():
                self._pix = pm
        if self._pix is None and not mixed:
            self._text = tr("No cover art\nDrop an image here")
        self.update()

    def dragEnterEvent(self, ev: QDragEnterEvent) -> None:
        if ev.mimeData().hasUrls() or ev.mimeData().hasImage():
            ev.acceptProposedAction()

    def dropEvent(self, ev: QDropEvent) -> None:
        md = ev.mimeData()
        if md.hasUrls():
            for url in md.urls():
                path = Path(url.toLocalFile())
                if path.suffix.lower() in (".jpg", ".jpeg", ".png", ".bmp", ".gif"):
                    img = read_image(path)
                    if not img.isNull():
                        self.coverDropped.emit(image_bytes_from_qimage(img) if path.stat().st_size > 3_000_000
                                               else path.read_bytes())
                        return
        elif md.hasImage():
            img = QImage(md.imageData())
            if not img.isNull():
                self.coverDropped.emit(image_bytes_from_qimage(img))

    def paintEvent(self, ev) -> None:
        t = theme.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if self._pix is not None:
            path = QPainterPath()
            path.addRoundedRect(r, 14, 14)
            p.setClipPath(path)
            scaled = self._pix.scaled(int(r.width() * self.devicePixelRatioF()), int(r.height() * self.devicePixelRatioF()),
                                      Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
            scaled.setDevicePixelRatio(self.devicePixelRatioF())
            w, h = scaled.width() / self.devicePixelRatioF(), scaled.height() / self.devicePixelRatioF()
            p.drawPixmap(int(r.left() + (r.width() - w) / 2), int(r.top() + (r.height() - h) / 2), scaled)
            p.setClipping(False)
            p.setPen(QPen(theme.qcolor(t.line_strong), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r, 14, 14)
        else:
            p.setPen(QPen(theme.qcolor(t.line_strong), 1.5, Qt.PenStyle.DashLine))
            p.setBrush(theme.qcolor(t.bg2))
            p.drawRoundedRect(r, 14, 14)
            p.drawPixmap(int(r.center().x() - 16), int(r.center().y() - 46), icons.pixmap("image", t.text_faint, 32))
            p.setPen(theme.qcolor(t.text_dim))
            p.setFont(theme.ui_font(10))
            p.drawText(QRectF(r.left(), r.center().y() - 6, r.width(), 60), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                       self._text)


class RenameDialog(QDialog):
    PATTERNS = ["{artist} - {title}", "{track} - {title}", "{track} - {artist} - {title}", "{album} - {track} - {title}",
                "{title}", "{artist} - {album} - {track} - {title}"]

    def __init__(self, items: list[LibraryItem], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Rename files from tags"))
        self.setModal(True)
        self.resize(720, 520)
        self.items = items
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 18)
        root.setSpacing(10)
        head = QLabel(tr("Rename files from tags"))
        head.setProperty("role", "title")
        root.addWidget(head)
        desc = QLabel(tr("Tokens: {title} {artist} {album} {album_artist} {track} {disc} {year} {genre} {name}"))
        desc.setProperty("role", "description")
        root.addWidget(desc)
        self.pattern = QComboBox()
        self.pattern.setEditable(True)
        for p in self.PATTERNS:
            self.pattern.addItem(p)
        root.addWidget(self.pattern)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels([tr("CURRENT NAME"), tr("NEW NAME")])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        root.addWidget(self.table, 1)
        self.note = QLabel("")
        self.note.setProperty("role", "dim")
        root.addWidget(self.note)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = make_button(tr("Cancel"))
        cancel.clicked.connect(self.reject)
        self.ok = make_button(tr("Rename"), "primary")
        self.ok.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(self.ok)
        root.addLayout(buttons)
        self.pattern.currentTextChanged.connect(self._preview)
        self._preview()

    def plan(self) -> list[tuple[LibraryItem, Path]]:
        pattern = self.pattern.currentText().strip() or "{name}"
        out: list[tuple[LibraryItem, Path]] = []
        for item in self.items:
            if item.path is None:
                continue
            new = tagmod.name_from_pattern(pattern, item.tags, item.path.stem)
            out.append((item, item.path.with_name(new + item.path.suffix)))
        return out

    def _preview(self) -> None:
        plan = self.plan()
        self.table.setRowCount(len(plan))
        seen: dict[str, int] = {}
        problems = 0
        for item, new in plan:
            seen[str(new).lower()] = seen.get(str(new).lower(), 0) + 1
        for r, (item, new) in enumerate(plan):
            self.table.setItem(r, 0, QTableWidgetItem(item.path.name))
            cell = QTableWidgetItem(new.name)
            clash = seen[str(new).lower()] > 1 or (new.exists() and new != item.path)
            if clash:
                cell.setForeground(theme.qcolor(theme.current().bad))
                cell.setToolTip(tr("This name is already taken"))
                problems += 1
            elif new == item.path:
                cell.setForeground(theme.qcolor(theme.current().text_faint))
            self.table.setItem(r, 1, cell)
        self.ok.setEnabled(bool(plan) and not problems)
        self.note.setText(tr(f"{problems} name{'s' if problems != 1 else ''} would clash. Adjust the pattern.") if problems
                          else tr(f"{len(plan)} file{'s' if len(plan) != 1 else ''} will be renamed."))


FIELDS = [("title", "Title"), ("artist", "Artist"), ("album", "Album"), ("album_artist", "Album artist"),
          ("genre", "Genre"), ("composer", "Composer")]


class TagsPage(QWidget):
    renamed = Signal()

    def __init__(self, state: AppState) -> None:
        super().__init__()
        self.state = state
        self.items: list[LibraryItem] = []
        self._loading = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        root = QVBoxLayout(body)
        root.setContentsMargins(28, 22, 28, 22)
        root.setSpacing(14)

        self.title = QLabel(tr("Tags"))
        self.title.setProperty("role", "title")
        self.subtitle = QLabel("")
        self.subtitle.setProperty("role", "dim")
        self.subtitle.setWordWrap(True)
        root.addWidget(self.title)
        root.addWidget(self.subtitle)

        row = QHBoxLayout()
        row.setSpacing(28)
        row.setAlignment(Qt.AlignmentFlag.AlignTop)
        left = QVBoxLayout()
        left.setSpacing(10)
        self.cover = CoverBox()
        left.addWidget(self.cover)
        self.choose_btn = make_button(tr("Choose image…"), icon="image")
        self.paste_btn = make_button(tr("Paste image"), icon="paste")
        self.extract_btn = make_button(tr("Save image…"), icon="download")
        self.remove_btn = make_button(tr("Remove"), "ghost", "trash")
        grid = QGridLayout()
        grid.setSpacing(6)
        grid.addWidget(self.choose_btn, 0, 0)
        grid.addWidget(self.paste_btn, 0, 1)
        grid.addWidget(self.extract_btn, 1, 0)
        grid.addWidget(self.remove_btn, 1, 1)
        left.addLayout(grid)
        left.addStretch(1)
        row.addLayout(left)

        form = QGridLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        self.edits: dict[str, QLineEdit] = {}
        r = 0
        for key, label in FIELDS[:4]:
            form.addWidget(self._lab(label), r, 0)
            edit = QLineEdit()
            self.edits[key] = edit
            form.addWidget(edit, r, 1, 1, 5)
            r += 1
        form.addWidget(self._lab(tr("Year")), r, 0)
        self.edits["year"] = self._small("e.g. 2026", 90)
        form.addWidget(self.edits["year"], r, 1)
        form.addWidget(self._lab(tr("Genre")), r, 2, alignment=Qt.AlignmentFlag.AlignRight)
        self.edits["genre"] = QLineEdit()
        form.addWidget(self.edits["genre"], r, 3, 1, 3)
        r += 1
        form.addWidget(self._lab(tr("Track")), r, 0)
        self.edits["track"] = self._small("#", 96)
        form.addWidget(self.edits["track"], r, 1)
        form.addWidget(self._lab(tr("of")), r, 2, alignment=Qt.AlignmentFlag.AlignRight)
        self.edits["track_total"] = self._small("", 96)
        form.addWidget(self.edits["track_total"], r, 3)
        r += 1
        form.addWidget(self._lab(tr("Disc")), r, 0)
        self.edits["disc"] = self._small("#", 96)
        form.addWidget(self.edits["disc"], r, 1)
        form.addWidget(self._lab(tr("of")), r, 2, alignment=Qt.AlignmentFlag.AlignRight)
        self.edits["disc_total"] = self._small("", 96)
        form.addWidget(self.edits["disc_total"], r, 3)
        r += 1
        form.addWidget(self._lab(tr("Composer")), r, 0)
        self.edits["composer"] = QLineEdit()
        form.addWidget(self.edits["composer"], r, 1, 1, 5)
        r += 1
        form.addWidget(self._lab(tr("Comment")), r, 0, alignment=Qt.AlignmentFlag.AlignTop)
        self.comment = QPlainTextEdit()
        self.comment.setFixedHeight(84)
        form.addWidget(self.comment, r, 1, 1, 5)
        form.setColumnStretch(5, 1)
        for key, name in (("title", "Title"), ("artist", "Artist"), ("album", "Album"), ("album_artist", "Album artist"),
                          ("year", "Year"), ("genre", "Genre"), ("track", "Track number"), ("track_total", "Total tracks"),
                          ("disc", "Disc number"), ("disc_total", "Total discs"), ("composer", "Composer")):
            self.edits[key].setAccessibleName(tr(name))             # the labels sit beside the fields, not tied to them
        self.comment.setAccessibleName(tr("Comment"))
        row.addLayout(form, 1)
        root.addLayout(row)

        root.addWidget(divider())
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.number_btn = make_button(tr("Number the tracks"), icon="sliders")
        self.rename_btn = make_button(tr("Rename files from tags…"), icon="edit")
        self.revert_btn = make_button(tr("Revert"))
        self.save_btn = make_button(tr("Save tags to files"), "primary", "save")
        actions.addWidget(self.number_btn)
        actions.addWidget(self.rename_btn)
        actions.addStretch(1)
        actions.addWidget(self.revert_btn)
        actions.addWidget(self.save_btn)
        root.addLayout(actions)
        self.status = QLabel("")
        self.status.setProperty("role", "dim")
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        root.addStretch(1)

        for key, edit in self.edits.items():
            edit.textEdited.connect(lambda text, k=key: self._edited(k, text))
        self.comment.textChanged.connect(self._comment_edited)
        self.cover.coverDropped.connect(self._set_cover)
        self.choose_btn.clicked.connect(self._choose_image)
        self.paste_btn.clicked.connect(self._paste_image)
        self.extract_btn.clicked.connect(self._save_image)
        self.remove_btn.clicked.connect(lambda: self._set_cover(None))
        self.number_btn.clicked.connect(self._number)
        self.rename_btn.clicked.connect(self._rename)
        self.revert_btn.clicked.connect(self._revert)
        self.save_btn.clicked.connect(self.save)
        state.itemUpdated.connect(lambda item: self._maybe_refresh(item))
        self.set_items([])

    @staticmethod
    def _lab(text: str) -> QLabel:
        lab = QLabel(tr(text))
        lab.setProperty("role", "dim")
        return lab

    @staticmethod
    def _small(placeholder: str, width: int) -> QLineEdit:
        e = QLineEdit()
        e.setPlaceholderText(tr(placeholder))
        # never narrower than its own hint plus the field's padding: a translation or a larger system font would cut it off
        e.setFixedWidth(max(width, e.fontMetrics().horizontalAdvance(e.placeholderText()) + 36))
        return e

    # ------------------------------------------------------------ model <-> form
    def set_items(self, items: list[LibraryItem]) -> None:
        self.items = list(items)
        self.refresh()

    def _maybe_refresh(self, item: LibraryItem) -> None:
        pass

    def refresh(self) -> None:
        self._loading = True
        items = self.items
        has = bool(items)
        for w in list(self.edits.values()) + [self.comment, self.choose_btn, self.paste_btn, self.extract_btn,
                                              self.remove_btn, self.save_btn, self.revert_btn]:
            w.setEnabled(has)
        self.number_btn.setEnabled(len(items) > 1)
        self.rename_btn.setEnabled(has and any(i.path for i in items))
        if not has:
            self.title.setText(tr("Tags"))
            self.subtitle.setText(tr("Select one or more files in the library to edit their tags and cover art."))
            for e in self.edits.values():
                e.clear()
                e.setPlaceholderText("")
            self.comment.clear()
            self.cover.set_cover(None)
            self.status.setText("")
            self._loading = False
            return
        if len(items) == 1:
            self.title.setText(items[0].name)
            self.subtitle.setText(f"{items[0].format_text()}  ·  {items[0].path or tr('not saved yet')}")
        else:
            self.title.setText(tr(f"{len(items)} files selected"))
            self.subtitle.setText(tr("Fields that differ show “(multiple values)”. Whatever you type is applied to every selected file; "
                                  "fields you leave alone are not touched."))
        for key, edit in self.edits.items():
            values = {getattr(i.tags, key) for i in items}
            if len(values) == 1:
                edit.setText(next(iter(values)))
                edit.setPlaceholderText({"year": tr("e.g. 2026"), "track": "#", "disc": "#"}.get(key, ""))
            else:
                edit.clear()
                edit.setPlaceholderText(tr("(multiple values)"))
        comments = {i.tags.comment for i in items}
        self.comment.setPlainText(next(iter(comments)) if len(comments) == 1 else "")
        self.comment.setPlaceholderText("" if len(comments) == 1 else tr("(multiple values)"))
        covers = {i.tags.cover for i in items}
        self.cover.set_cover(next(iter(covers)) if len(covers) == 1 else None, mixed=len(covers) > 1)
        self._update_status()
        self._loading = False

    def _update_status(self) -> None:
        unwritable = [i for i in self.items if i.path and not tagmod.can_write(i.path)]
        dirty = [i for i in self.items if i.tags_dirty]
        parts = []
        if dirty:
            parts.append(tr(f"{len(dirty)} file{'s' if len(dirty) != 1 else ''} with unsaved tag changes. "
                         "They are used when converting even before you save."))
        if unwritable:
            parts.append(tr(f"Tags cannot be written into {', '.join(sorted({i.path.suffix for i in unwritable}))} files; "
                         "they are still applied when you convert."))
        self.status.setText("  ".join(parts))

    def _edited(self, key: str, text: str) -> None:
        if self._loading:
            return
        for item in self.items:
            setattr(item.tags, key, text.strip() if key in ("track", "track_total", "disc", "disc_total", "year") else text)
            self.state.touched(item)
        self._update_status()

    def _comment_edited(self) -> None:
        if self._loading:
            return
        text = self.comment.toPlainText()
        for item in self.items:
            item.tags.comment = text
            self.state.touched(item)
        self._update_status()

    def _set_cover(self, data: Optional[bytes]) -> None:
        for item in self.items:
            item.tags.cover = data
            self.state.touched(item)
        self.cover.set_cover(data)
        self._update_status()

    # ------------------------------------------------------------ cover actions
    def _choose_image(self) -> None:
        name, _ = QFileDialog.getOpenFileName(self, tr("Choose cover art"), self.state.settings.last_dir("image"),
                                              tr("Images (*.jpg *.jpeg *.png *.bmp *.gif)"))
        if not name:
            return
        self.state.settings.remember_dir("image", name)
        img = read_image(name)
        if img.isNull():
            QMessageBox.warning(self, tr("Cover art"), tr("That image could not be read. Use a JPEG, PNG, GIF or BMP picture."))
            return
        path = Path(name)
        data = path.read_bytes() if path.suffix.lower() in (".jpg", ".jpeg", ".png") and path.stat().st_size < 3_000_000 \
            else image_bytes_from_qimage(img)
        self._set_cover(data)

    def _paste_image(self) -> None:
        img = QApplication.clipboard().image()
        if img.isNull():
            QMessageBox.information(self, tr("Cover art"), tr("There is no image on the clipboard."))
            return
        self._set_cover(image_bytes_from_qimage(img))

    def _save_image(self) -> None:
        data = next((i.tags.cover for i in self.items if i.tags.cover), None)
        if not data:
            QMessageBox.information(self, tr("Cover art"), tr("There is no cover art to save."))
            return
        ext = ".png" if tagmod.sniff_image_mime(data) == "image/png" else ".jpg"
        name, _ = QFileDialog.getSaveFileName(self, tr("Save cover art"), str(Path(self.state.settings.last_dir("image")) / f"cover{ext}"),
                                              tr("Images (*.jpg *.png)"))
        if name:
            Path(name).write_bytes(data)

    # ------------------------------------------------------------ actions
    def _number(self) -> None:
        order = [i for i in self.state.items if i in self.items]
        for n, item in enumerate(order, start=1):
            item.tags.track = str(n)
            item.tags.track_total = str(len(order))
            self.state.touched(item)
        self.refresh()

    def _revert(self) -> None:
        for item in self.items:
            item.tags = item.tags_orig.copy()
            self.state.touched(item)
        self.refresh()

    def save(self) -> None:
        targets = [i for i in self.items if i.path and tagmod.can_write(i.path) and i.tags_dirty]
        if not targets:
            self.status.setText(tr("Nothing to save. Changed tags are kept in the app and used when converting."))
            return
        written: set[int] = set()                         # which of `targets` really were written
        errors: dict[int, str] = {}                       # which were not, and why
        problems: list[access.WriteProblem] = []

        def job(progress, cancel):
            for n, item in enumerate(targets):
                cancel.check()
                try:
                    tagmod.write_tags(item.path, item.tags, cover=item.tags.cover)
                    written.add(n)
                except OSError as exc:                    # say why: Windows Security refuses with just "Bad file descriptor"
                    problem = access.diagnose_modify(item.path, exc)
                    problems.append(problem)
                    errors[n] = access.one_line(problem, item.name)
                except Exception as exc:  # noqa: BLE001 - listed for the user
                    errors[n] = f"{item.name}: {exc}"
                progress((n + 1) / len(targets))

        run_task(self.window(), tr("Saving tags"), job, message=tr(f"Writing tags to {len(targets)} file(s)…"), delay_ms=400)
        for n in sorted(written):
            targets[n].tags_orig = targets[n].tags.copy()
            self.state.touched(targets[n])
        saved = len(written)
        blocked = next((p for p in problems if p.kind in access.BLOCKED_KINDS), None)
        if blocked is not None:                           # one sentence, not the same one per file
            why = access.one_line(blocked)
        else:
            why = "; ".join(text for _, text in sorted(errors.items())[:3])
        self.status.setText(tr(f"Saved tags to {saved} file{'s' if saved != 1 else ''}.")
                            + (tr(f"  {len(errors)} failed: ") + why if errors else ""))
        self.status.setProperty("role", "bad" if errors else "good")
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)
        if blocked is not None:
            show_write_problem(self.window(), tr("Save tags"), blocked)

    def _rename(self) -> None:
        dlg = RenameDialog(self.items, self.window())
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        done, failed = 0, []
        for item, new in dlg.plan():
            if new == item.path:
                continue
            try:
                os.rename(item.path, new)
            except OSError as exc:
                failed.append(f"{item.path.name}: {exc.strerror or exc}")
                continue
            item.path = new
            item.name = new.stem
            if item.doc is not None:
                item.doc.path, item.doc.name = new, new.stem
            self.state.touched(item)
            done += 1
        self.status.setText(tr(f"Renamed {done} file{'s' if done != 1 else ''}.") + (tr(f"  Failed: {'; '.join(failed)}") if failed else ""))
        self.renamed.emit()
        self.refresh()
