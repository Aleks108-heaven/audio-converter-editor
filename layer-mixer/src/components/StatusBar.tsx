import type { AudioEngine } from "../hooks/useAudioEngine";
import { formatTime } from "../lib/format";

export function StatusBar({ engine }: { engine: AudioEngine }) {
  const loaded = engine.tracks.filter((t) => t.buffer).length;
  const missing = engine.tracks.length - loaded;

  return (
    <div className="flex h-7 shrink-0 items-center gap-4 border-t border-border bg-[hsl(var(--bg))] px-3 font-mono-tech text-[10.5px] text-[hsl(var(--text-dim))]">
      <span>{engine.sampleRate.toLocaleString()} Hz</span>
      <Dot />
      <span>32-bit float</span>
      <Dot />
      <span>2 ch</span>
      <Dot />
      <span>{engine.tracks.length} track{engine.tracks.length === 1 ? "" : "s"}</span>
      {missing > 0 && (
        <>
          <Dot />
          <span className="text-[hsl(var(--solo))]">
            {missing} missing audio
          </span>
        </>
      )}
      <Dot />
      <span>total {formatTime(engine.totalDuration, false)}</span>
      <div className="flex-1" />
      <span>rendered on export, client-side only</span>
    </div>
  );
}

function Dot() {
  return <span className="text-[hsl(var(--border-strong))]">{"·"}</span>;
}
