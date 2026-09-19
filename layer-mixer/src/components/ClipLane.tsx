import { useEffect, useRef } from "react";
import type { Track } from "../types";
import { playableLength } from "../types";
import type { AudioEngine } from "../hooks/useAudioEngine";
import { PEAKS_PER_SECOND } from "../lib/audio";
import { clamp } from "../lib/format";
import { TRACK_ROW_H } from "./TrackHeader";

const MIN_CLIP_LEN = 0.15;

type DragMode = "move" | "trimL" | "trimR";
interface DragState {
  mode: DragMode;
  startX: number;
  startSec: number;
  trimStart: number;
  trimEnd: number;
}

export function ClipLane({
  track,
  engine,
  widthPx,
  active,
}: {
  track: Track;
  engine: AudioEngine;
  widthPx: number;
  active: boolean;
}) {
  const zoom = engine.zoom;
  const len = playableLength(track);
  const leftPx = track.startSec * zoom;
  const clipWidthPx = Math.max(4, len * zoom);
  const dragRef = useRef<DragState | null>(null);

  function beginDrag(e: React.PointerEvent, mode: DragMode) {
    e.stopPropagation();
    e.currentTarget.setPointerCapture(e.pointerId);
    engine.pause();
    dragRef.current = { mode, startX: e.clientX, startSec: track.startSec, trimStart: track.trimStart, trimEnd: track.trimEnd };
  }

  function onDragMove(e: React.PointerEvent) {
    const st = dragRef.current;
    if (!st) return;
    const deltaSec = (e.clientX - st.startX) / zoom;
    if (st.mode === "move") {
      engine.updateTrack(track.id, { startSec: clamp(st.startSec + deltaSec, 0, 1e9) });
    } else if (st.mode === "trimL") {
      const maxTrim = Math.max(0, track.duration - st.trimEnd - MIN_CLIP_LEN);
      const newTrimStart = clamp(st.trimStart + deltaSec, 0, maxTrim);
      const applied = newTrimStart - st.trimStart;
      engine.updateTrack(track.id, { trimStart: newTrimStart, startSec: clamp(st.startSec + applied, 0, 1e9) });
    } else {
      const maxTrim = Math.max(0, track.duration - st.trimStart - MIN_CLIP_LEN);
      const newTrimEnd = clamp(st.trimEnd - deltaSec, 0, maxTrim);
      engine.updateTrack(track.id, { trimEnd: newTrimEnd });
    }
  }

  function endDrag() {
    dragRef.current = null;
  }

  const missing = !track.buffer;

  return (
    <div
      className={`relative shrink-0 border-b border-border ${active ? "bg-[hsl(var(--accent)/0.04)]" : ""}`}
      style={{ height: TRACK_ROW_H, width: widthPx }}
    >
      <div
        className="group absolute top-2.5 overflow-hidden rounded-md border"
        style={{
          left: leftPx,
          width: clipWidthPx,
          height: TRACK_ROW_H - 20,
          borderColor: missing ? "hsl(var(--border-strong))" : `${track.color}77`,
          background: missing
            ? "repeating-linear-gradient(135deg, hsl(var(--field)), hsl(var(--field)) 7px, hsl(var(--panel-alt)) 7px, hsl(var(--panel-alt)) 14px)"
            : `${track.color}22`,
        }}
      >
        <div
          className="absolute inset-0 cursor-grab active:cursor-grabbing"
          onPointerDown={(e) => beginDrag(e, "move")}
          onPointerMove={onDragMove}
          onPointerUp={endDrag}
        />
        {!missing && track.peaks && (
          <ClipWaveform
            peaks={track.peaks}
            trimStart={track.trimStart}
            color={track.color}
            widthPx={clipWidthPx}
            heightPx={TRACK_ROW_H - 20}
            zoom={zoom}
          />
        )}
        <div className="pointer-events-none absolute left-1.5 top-1 max-w-[85%] truncate rounded bg-black/40 px-1 text-[9.5px] font-medium text-white/90">
          {missing ? `${track.name} — no audio attached` : track.name}
        </div>
        <div
          className="absolute left-0 top-0 h-full w-2.5 cursor-ew-resize opacity-0 group-hover:opacity-100"
          onPointerDown={(e) => beginDrag(e, "trimL")}
          onPointerMove={onDragMove}
          onPointerUp={endDrag}
        >
          <div className="h-full w-1 bg-white/50" />
        </div>
        <div
          className="absolute right-0 top-0 h-full w-2.5 cursor-ew-resize opacity-0 group-hover:opacity-100"
          onPointerDown={(e) => beginDrag(e, "trimR")}
          onPointerMove={onDragMove}
          onPointerUp={endDrag}
        >
          <div className="ml-auto h-full w-1 bg-white/50" />
        </div>
      </div>
    </div>
  );
}

function ClipWaveform({
  peaks,
  trimStart,
  color,
  widthPx,
  heightPx,
  zoom,
}: {
  peaks: Float32Array;
  trimStart: number;
  color: string;
  widthPx: number;
  heightPx: number;
  zoom: number;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(1, Math.round(widthPx));
    const h = Math.max(1, Math.round(heightPx));
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    canvas.style.width = `${w}px`;
    canvas.style.height = `${h}px`;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const numBuckets = peaks.length / 2;
    const mid = h / 2;

    ctx.strokeStyle = color;
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let x = 0; x < w; x++) {
      const srcTime = trimStart + x / zoom;
      let bucket = Math.floor(srcTime * PEAKS_PER_SECOND);
      if (bucket < 0) bucket = 0;
      if (bucket >= numBuckets) bucket = numBuckets - 1;
      const min = peaks[bucket * 2];
      const max = peaks[bucket * 2 + 1];
      const y0 = mid - max * mid * 0.92;
      const y1 = mid - min * mid * 0.92;
      ctx.moveTo(x + 0.5, y0);
      ctx.lineTo(x + 0.5, Math.max(y1, y0 + 1));
    }
    ctx.stroke();

    ctx.strokeStyle = `${color}55`;
    ctx.beginPath();
    ctx.moveTo(0, mid);
    ctx.lineTo(w, mid);
    ctx.stroke();
  }, [peaks, trimStart, widthPx, heightPx, color, zoom]);

  return <canvas ref={canvasRef} className="pointer-events-none block" />;
}
