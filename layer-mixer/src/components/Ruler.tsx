import { useRef } from "react";
import type { AudioEngine } from "../hooks/useAudioEngine";
import { clamp, formatTime } from "../lib/format";

export const RULER_H = 30;

const TICK_STEPS = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1200];

function buildTicks(visibleSeconds: number, zoom: number): number[] {
  const targetPx = 78;
  const rawSec = targetPx / zoom;
  const step = TICK_STEPS.find((s) => s >= rawSec) ?? TICK_STEPS[TICK_STEPS.length - 1];
  const ticks: number[] = [];
  for (let t = 0; t <= visibleSeconds + step; t += step) ticks.push(Math.round(t * 1000) / 1000);
  return ticks;
}

export function Ruler({ engine, widthPx }: { engine: AudioEngine; widthPx: number }) {
  const ref = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{ startSec: number } | null>(null);

  function xToSec(clientX: number): number {
    const rect = ref.current!.getBoundingClientRect();
    return clamp((clientX - rect.left) / engine.zoom, 0, 1e9);
  }

  function onPointerDown(e: React.PointerEvent) {
    e.currentTarget.setPointerCapture(e.pointerId);
    const sec = xToSec(e.clientX);
    dragRef.current = { startSec: sec };
    engine.setSelection(null);
    engine.seek(sec);
  }
  function onPointerMove(e: React.PointerEvent) {
    if (!dragRef.current) return;
    const sec = xToSec(e.clientX);
    const lo = Math.min(dragRef.current.startSec, sec);
    const hi = Math.max(dragRef.current.startSec, sec);
    if (hi - lo > 0.05) engine.setSelection({ startSec: lo, endSec: hi });
  }
  function onPointerUp() {
    dragRef.current = null;
  }

  const ticks = buildTicks(widthPx / engine.zoom, engine.zoom);
  const sel = engine.selection;

  return (
    <div
      ref={ref}
      className="sticky top-0 z-20 cursor-text select-none border-b border-border bg-[hsl(var(--panel-alt))]"
      style={{ width: widthPx, height: RULER_H }}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
    >
      {sel && (
        <div
          className="absolute top-0 h-full bg-[hsl(var(--accent)/0.18)]"
          style={{ left: sel.startSec * engine.zoom, width: (sel.endSec - sel.startSec) * engine.zoom }}
        />
      )}
      {ticks.map((t) => (
        <div key={t} className="absolute top-0 h-full" style={{ left: t * engine.zoom }}>
          <div className="h-2 w-px bg-[hsl(var(--border-strong))]" />
          <span className="absolute left-1 top-2 whitespace-nowrap font-mono-tech text-[9.5px] text-[hsl(var(--text-dim))]">
            {formatTime(t, false)}
          </span>
        </div>
      ))}
    </div>
  );
}
