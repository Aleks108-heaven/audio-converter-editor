import { Pause, Play, Repeat, Square, ZoomIn, ZoomOut } from "lucide-react";
import type { AudioEngine } from "../hooks/useAudioEngine";
import { formatTime, clamp } from "../lib/format";

const MIN_ZOOM = 20;
const MAX_ZOOM = 400;

export function TransportBar({ engine }: { engine: AudioEngine }) {
  const sel = engine.selection;
  const selLen = sel ? sel.endSec - sel.startSec : 0;

  return (
    <div className="flex h-14 shrink-0 items-center gap-4 border-t border-border bg-[hsl(var(--panel))] px-3">
      <div className="flex items-center gap-1">
        <TransportButton
          onClick={() => (engine.isPlaying ? engine.pause() : engine.play())}
          active
          title={engine.isPlaying ? "Pause" : "Play"}
        >
          {engine.isPlaying ? <Pause size={16} fill="currentColor" /> : <Play size={16} fill="currentColor" />}
        </TransportButton>
        <TransportButton onClick={engine.stop} title="Stop">
          <Square size={14} fill="currentColor" />
        </TransportButton>
        <TransportButton
          onClick={() => engine.setLoop(!engine.loop)}
          title="Loop"
          className={engine.loop ? "!bg-[hsl(var(--accent)/0.18)] !text-[hsl(var(--accent))]" : ""}
        >
          <Repeat size={15} />
        </TransportButton>
      </div>

      <div className="font-mono-tech text-[19px] font-semibold tabular-nums text-[hsl(var(--text))]">
        {formatTime(engine.currentTime)}
      </div>

      <div className="h-7 w-px bg-border" />

      <div className="flex items-center gap-4 font-mono-tech text-[11px] text-[hsl(var(--text-muted))]">
        <Field label="Sel. start" value={sel ? formatTime(sel.startSec, false) : "—"} />
        <Field label="Sel. end" value={sel ? formatTime(sel.endSec, false) : "—"} />
        <Field label="Sel. length" value={sel ? formatTime(selLen, false) : "—"} />
      </div>

      <div className="flex-1" />

      <div className="flex items-center gap-1">
        <TransportButton onClick={() => engine.setZoom(clamp(engine.zoom / 1.35, MIN_ZOOM, MAX_ZOOM))} title="Zoom out">
          <ZoomOut size={15} />
        </TransportButton>
        <TransportButton onClick={() => engine.setZoom(clamp(engine.zoom * 1.35, MIN_ZOOM, MAX_ZOOM))} title="Zoom in">
          <ZoomIn size={15} />
        </TransportButton>
      </div>
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="leading-tight">
      <div className="text-[9px] uppercase tracking-wide text-[hsl(var(--text-dim))]">{label}</div>
      <div>{value}</div>
    </div>
  );
}

function TransportButton({
  children,
  onClick,
  title,
  active,
  className = "",
}: {
  children: React.ReactNode;
  onClick: () => void;
  title: string;
  active?: boolean;
  className?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      aria-label={title}
      className={`flex h-9 w-9 items-center justify-center rounded-md text-[hsl(var(--text-muted))] transition-colors hover:bg-[hsl(var(--field))] hover:text-[hsl(var(--text))] ${
        active ? "bg-[hsl(var(--accent))] !text-[#0d0f14] hover:bg-[hsl(var(--accent-strong))]" : ""
      } ${className}`}
    >
      {children}
    </button>
  );
}
