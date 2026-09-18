"""Run one operation across a queue of files, continuing past per-file
errors and reporting a per-file result at the end."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .core import AudioToolError


@dataclass
class JobResult:
    label: str
    success: bool
    error: str | None = None


@dataclass
class BatchResult:
    results: list[JobResult] = field(default_factory=list)

    @property
    def succeeded(self) -> list[JobResult]:
        return [r for r in self.results if r.success]

    @property
    def failed(self) -> list[JobResult]:
        return [r for r in self.results if not r.success]


def run_batch(
    jobs: list[tuple[str, Callable[[], None]]],
    on_progress: Callable[[JobResult], None] | None = None,
) -> BatchResult:
    """Run each (label, job) in order. A failing job doesn't stop the rest."""
    batch = BatchResult()
    for label, job in jobs:
        try:
            job()
            result = JobResult(label=label, success=True)
        except AudioToolError as exc:
            result = JobResult(label=label, success=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - surface any unexpected failure per-file
            result = JobResult(label=label, success=False, error=f"Unexpected error: {exc}")
        batch.results.append(result)
        if on_progress:
            on_progress(result)
    return batch


def batch_convert(
    in_paths: list[str | Path],
    out_dir: str | Path,
    fmt: str,
    bitrate: str | None = None,
    on_progress: Callable[[JobResult], None] | None = None,
) -> BatchResult:
    from . import core

    out_dir = Path(out_dir)
    jobs = []
    for raw_path in in_paths:
        in_path = Path(raw_path)
        out_path = out_dir / f"{in_path.stem}.{fmt}"
        jobs.append((str(in_path), lambda ip=in_path, op=out_path: core.convert(ip, op, bitrate=bitrate)))
    return run_batch(jobs, on_progress=on_progress)


_EFFECT_BUILDERS: dict[str, Callable] = {}


def _effect_builders():
    if not _EFFECT_BUILDERS:
        from . import effects

        _EFFECT_BUILDERS.update(
            {
                "fade-in": lambda ip, op, p: effects.fade_in(ip, op, p.get("duration", 1000)),
                "fade-out": lambda ip, op, p: effects.fade_out(ip, op, p.get("duration", 1000)),
                "normalize": lambda ip, op, p: effects.normalize(ip, op),
                "reverse": lambda ip, op, p: effects.reverse(ip, op),
                "echo": lambda ip, op, p: effects.echo(
                    ip, op, delay_ms=p.get("delay", 500), decay=p.get("decay", 0.5)
                ),
                "chorus": lambda ip, op, p: effects.chorus(ip, op),
                "flanger": lambda ip, op, p: effects.flanger(ip, op),
                "reverb": lambda ip, op, p: effects.reverb(ip, op, amount=p.get("decay", 0.5)),
            }
        )
    return _EFFECT_BUILDERS


def batch_effect(
    effect_name: str,
    in_paths: list[str | Path],
    out_dir: str | Path,
    on_progress: Callable[[JobResult], None] | None = None,
    **params,
) -> BatchResult:
    builders = _effect_builders()
    if effect_name not in builders:
        raise AudioToolError(f"Unknown effect '{effect_name}'. Choices: {', '.join(builders)}")
    builder = builders[effect_name]

    out_dir = Path(out_dir)
    jobs = []
    for raw_path in in_paths:
        in_path = Path(raw_path)
        out_path = out_dir / in_path.name
        jobs.append((str(in_path), lambda ip=in_path, op=out_path: builder(ip, op, params)))
    return run_batch(jobs, on_progress=on_progress)
