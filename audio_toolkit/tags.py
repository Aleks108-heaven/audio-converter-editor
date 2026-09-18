"""Read/write audio metadata tags and batch-rename files from those tags.

Uses mutagen's "easy" interface, which normalizes tag field names across
containers (ID3 for MP3, Vorbis comments for FLAC/OGG, MP4 atoms for M4A,
ASF for WMA). AMR and MP2 have no widely-supported tag format and will
raise AudioToolError.
"""

from __future__ import annotations

from pathlib import Path

import mutagen

from .core import AudioToolError

TAG_FIELDS = ["title", "artist", "album", "genre", "date", "tracknumber"]

# mutagen's easy=True interface is unreliable for plain WAV and unsupported for
# AMR/MP2, so we only advertise tagging for containers it handles well.
TAGGABLE_FORMATS = {"mp3", "flac", "m4a", "wma", "ogg"}


def _open_easy(path: Path) -> mutagen.FileType:
    fmt = path.suffix.lower().lstrip(".")
    if fmt not in TAGGABLE_FORMATS:
        raise AudioToolError(
            f"'.{fmt}' files don't support tagging. Supported: {', '.join(sorted(TAGGABLE_FORMATS))}"
        )
    try:
        audio = mutagen.File(path, easy=True)
    except Exception as exc:  # noqa: BLE001 - mutagen raises varied types per format
        raise AudioToolError(f"Could not read tags from '{path}': {exc}") from exc
    if audio is None:
        raise AudioToolError(f"Could not recognize '{path}' as a taggable audio file.")
    return audio


def get_tags(path: str | Path) -> dict[str, str]:
    path = Path(path)
    if not path.is_file():
        raise AudioToolError(f"Input file not found: {path}")
    audio = _open_easy(path)
    return {field: audio[field][0] for field in TAG_FIELDS if field in audio and audio[field]}


def set_tags(path: str | Path, **fields: str) -> None:
    path = Path(path)
    if not path.is_file():
        raise AudioToolError(f"Input file not found: {path}")
    audio = _open_easy(path)

    for field, value in fields.items():
        if field not in TAG_FIELDS:
            raise AudioToolError(f"Unknown tag field '{field}'. Supported: {', '.join(TAG_FIELDS)}")
        try:
            if value:
                audio[field] = value
            elif field in audio:
                del audio[field]
        except Exception as exc:  # noqa: BLE001
            raise AudioToolError(f"Could not set tag '{field}' on '{path}': {exc}") from exc

    try:
        audio.save()
    except Exception as exc:  # noqa: BLE001
        raise AudioToolError(f"Could not save tags to '{path}': {exc}") from exc


def rename_from_tags(paths: list[str | Path], pattern: str, out_dir: str | Path | None = None) -> list[Path]:
    """Rename files using a pattern like '{artist} - {title}'. Missing fields fall
    back to '(original stem)' text so the operation never fails on missing tags."""
    results = []
    for path in paths:
        path = Path(path)
        tags = {}
        try:
            tags = get_tags(path)
        except AudioToolError:
            pass  # untagged or unsupported format: fall back to placeholders below

        values = {field: tags.get(field, f"unknown_{field}") for field in TAG_FIELDS}
        values["original"] = path.stem
        try:
            new_stem = pattern.format(**values)
        except KeyError as exc:
            raise AudioToolError(f"Unknown placeholder {exc} in rename pattern.") from exc

        new_stem = "".join(c for c in new_stem if c not in '<>:"/\\|?*').strip() or path.stem
        target_dir = Path(out_dir) if out_dir else path.parent
        target_dir.mkdir(parents=True, exist_ok=True)
        new_path = target_dir / f"{new_stem}{path.suffix}"
        path.rename(new_path)
        results.append(new_path)
    return results
