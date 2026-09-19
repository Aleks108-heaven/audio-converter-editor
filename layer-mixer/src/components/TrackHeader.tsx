import { useRef, useState } from "react";
import { Trash2, Upload, Volume2, VolumeX } from "lucide-react";
import type { Track } from "../types";
import type { AudioEngine } from "../hooks/useAudioEngine";
import { formatDb, formatPan, formatSemitones } from "../lib/format";
import { MiniSlider } from "./MiniSlider";

export const TRACK_ROW_H = 114;
export const HEADER_W = 252;

export function TrackHeader({ track, engine }: { track: Track; engine: AudioEngine }) {
  const [name, setName] = useState(track.name);
  const fileInputRef = useRef<HTMLInputElement>(null);

  function commitName() {
    const trimmed = name.trim() || track.name;
    setName(trimmed);
    if (trimmed !== track.name) engine.updateTrack(track.id, { name: trimmed });
  }

  return (
    <div
      className="flex shrink-0 flex-col gap-1.5 border-b border-r border-border px-2.5 py-2"
      style={{ height: TRACK_ROW_H, width: HEADER_W }}
    >
      <div className="flex items-center gap-1.5">
        <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ background: track.color }} />
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          onBlur={commitName}
          onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
          className="min-w-0 flex-1 truncate bg-transparent text-[12.5px] font-medium text-[hsl(var(--text))] outline-none"
        />
        <button
          onClick={() => engine.removeTrack(track.id)}
          title="Delete track"
          className="shrink-0 rounded p-1 text-[hsl(var(--text-dim))] hover:bg-[hsl(var(--record)/0.15)] hover:text-[hsl(var(--record))]"
        >
          <Trash2 size={12.5} />
        </button>
      </div>

      <div className="flex items-center gap-1">
        <ToggleChip active={track.muted} onClick={() => engine.updateTrack(track.id, { muted: !track.muted })} tone="record">
          Mute
        </ToggleChip>
        <ToggleChip active={track.solo} onClick={() => engine.updateTrack(track.id, { solo: !track.solo })} tone="solo">
          Solo
        </ToggleChip>
        {track.buffer ? (
          <span className="ml-auto truncate text-[9.5px] text-[hsl(var(--text-dim))]" title={track.fileName}>
            {track.fileName}
          </span>
        ) : (
          <>
            <button
              onClick={() => fileInputRef.current?.click()}
              className="ml-auto flex items-center gap-1 rounded bg-[hsl(var(--solo)/0.15)] px-1.5 py-0.5 text-[9.5px] font-medium text-[hsl(var(--solo))] hover:bg-[hsl(var(--solo)/0.25)]"
            >
              <Upload size={10} />
              Attach "{track.fileName}"
            </button>
            <input
              ref={fileInputRef}
              type="file"
              accept="audio/*,.mp3,.wav,.m4a,.ogg,.flac,.aac,.wma"
              className="hidden"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) engine.attachAudioToTrack(track.id, file);
                e.target.value = "";
              }}
            />
          </>
        )}
      </div>

      <MiniSlider
        label="Vol"
        value={track.gainDb}
        display={formatDb(track.gainDb)}
        min={-60}
        max={12}
        step={0.5}
        onChange={(v) => engine.updateTrack(track.id, { gainDb: v })}
      />
      <div className="flex items-center gap-2">
        <MiniSlider
          label="Pan"
          value={track.pan}
          display={formatPan(track.pan)}
          min={-1}
          max={1}
          step={0.05}
          onChange={(v) => engine.updateTrack(track.id, { pan: v })}
          className="min-w-0 flex-1"
        />
        <MiniSlider
          label="Pitch"
          value={track.semitones}
          display={formatSemitones(track.semitones)}
          min={-12}
          max={12}
          step={1}
          onChange={(v) => engine.updateTrack(track.id, { semitones: v })}
          className="min-w-0 flex-1"
        />
      </div>
    </div>
  );
}

function ToggleChip({
  active,
  onClick,
  tone,
  children,
}: {
  active: boolean;
  onClick: () => void;
  tone: "record" | "solo";
  children: React.ReactNode;
}) {
  const activeClass =
    tone === "record" ? "bg-[hsl(var(--record))] text-[#1a0d10]" : "bg-[hsl(var(--solo))] text-[#1a1206]";
  return (
    <button
      onClick={onClick}
      className={`flex items-center gap-1 rounded px-1.5 py-0.5 text-[9.5px] font-semibold transition-colors ${
        active ? activeClass : "bg-[hsl(var(--field))] text-[hsl(var(--text-dim))] hover:text-[hsl(var(--text-muted))]"
      }`}
    >
      {tone === "record" && (active ? <VolumeX size={9} /> : <Volume2 size={9} />)}
      {children}
    </button>
  );
}
