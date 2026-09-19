import { useMemo, useRef, useState } from "react";
import { TriangleAlert, UploadCloud, X } from "lucide-react";
import { useAudioEngine } from "./hooks/useAudioEngine";
import { Toolbar } from "./components/Toolbar";
import { EffectsSidebar } from "./components/EffectsSidebar";
import { TransportBar } from "./components/TransportBar";
import { StatusBar } from "./components/StatusBar";
import { Ruler, RULER_H } from "./components/Ruler";
import { TrackHeader, TRACK_ROW_H, HEADER_W } from "./components/TrackHeader";
import { ClipLane } from "./components/ClipLane";
import { formatTime } from "./lib/format";

export default function App() {
  const engine = useAudioEngine();
  const [dragActive, setDragActive] = useState(false);
  const dragDepth = useRef(0);

  const contentWidth = useMemo(() => {
    const min = 900;
    return Math.max(min, (engine.totalDuration + 12) * engine.zoom);
  }, [engine.totalDuration, engine.zoom]);

  const contentHeight = RULER_H + Math.max(engine.tracks.length, 1) * TRACK_ROW_H;

  function onDragEnter(e: React.DragEvent) {
    e.preventDefault();
    dragDepth.current += 1;
    if (Array.from(e.dataTransfer.types).includes("Files")) setDragActive(true);
  }
  function onDragOver(e: React.DragEvent) {
    e.preventDefault();
  }
  function onDragLeave(e: React.DragEvent) {
    e.preventDefault();
    dragDepth.current = Math.max(0, dragDepth.current - 1);
    if (dragDepth.current === 0) setDragActive(false);
  }
  function onDrop(e: React.DragEvent) {
    e.preventDefault();
    dragDepth.current = 0;
    setDragActive(false);
    if (e.dataTransfer.files?.length) engine.addFiles(e.dataTransfer.files);
  }

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <Toolbar engine={engine} />

      <div className="flex min-h-0 flex-1">
        <EffectsSidebar />

        <div
          className="relative flex min-h-0 flex-1 flex-col"
          onDragEnter={onDragEnter}
          onDragOver={onDragOver}
          onDragLeave={onDragLeave}
          onDrop={onDrop}
        >
          <div className="min-h-0 flex-1 overflow-y-auto">
            <div className="flex" style={{ minHeight: contentHeight }}>
              {/* fixed headers column */}
              <div className="flex shrink-0 flex-col bg-[hsl(var(--panel))]">
                <div
                  className="sticky top-0 z-30 flex shrink-0 items-center border-b border-r border-border bg-[hsl(var(--panel-alt))] px-2.5 text-[10px] font-semibold uppercase tracking-wide text-[hsl(var(--text-dim))]"
                  style={{ width: HEADER_W, height: RULER_H }}
                >
                  Tracks
                </div>
                {engine.tracks.map((t) => (
                  <TrackHeader key={t.id} track={t} engine={engine} />
                ))}
                {engine.tracks.length === 0 && (
                  <div className="flex flex-col items-start gap-1 border-r border-border px-3 py-4 text-[11.5px] text-[hsl(var(--text-dim))]" style={{ width: HEADER_W }}>
                    No tracks yet.
                  </div>
                )}
              </div>

              {/* horizontally-scrolling timeline */}
              <div className="min-w-0 flex-1" style={{ overflowX: "auto", overflowY: "visible" }}>
                <div style={{ width: contentWidth, height: contentHeight, position: "relative" }}>
                  <Ruler engine={engine} widthPx={contentWidth} />
                  {engine.tracks.map((t, i) => (
                    <ClipLane key={t.id} track={t} engine={engine} widthPx={contentWidth} active={i % 2 === 0} />
                  ))}
                  <PlayheadLine seconds={engine.currentTime} zoom={engine.zoom} heightPx={contentHeight} />
                </div>
              </div>
            </div>
          </div>

          {engine.tracks.length === 0 && (
            <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
              <div className="flex flex-col items-center gap-2 rounded-xl border border-dashed border-border-strong bg-[hsl(var(--panel)/0.7)] px-8 py-7 text-center">
                <UploadCloud size={24} className="text-[hsl(var(--text-dim))]" />
                <p className="text-[13px] font-medium text-[hsl(var(--text-muted))]">Drop audio files here</p>
                <p className="text-[11.5px] text-[hsl(var(--text-dim))]">or use Import Audio above - mp3, wav, m4a, ogg, flac, aac</p>
              </div>
            </div>
          )}

          {dragActive && (
            <div className="pointer-events-none absolute inset-0 z-40 flex items-center justify-center border-2 border-dashed border-[hsl(var(--accent))] bg-[hsl(var(--accent)/0.08)]">
              <div className="flex items-center gap-2 rounded-lg bg-[hsl(var(--accent))] px-4 py-2 text-[13px] font-semibold text-[#0d0f14]">
                <UploadCloud size={16} />
                Drop to import
              </div>
            </div>
          )}

          {engine.importError && (
            <div className="absolute bottom-3 left-1/2 z-40 flex -translate-x-1/2 items-center gap-2 rounded-lg border border-[hsl(var(--record)/0.4)] bg-[hsl(var(--panel-alt))] px-3.5 py-2 text-[12px] text-[hsl(var(--record))] shadow-lg">
              <TriangleAlert size={14} className="shrink-0" />
              {engine.importError}
              <button onClick={() => engine.setImportError(null)} className="ml-1 text-[hsl(var(--text-dim))] hover:text-[hsl(var(--text))]">
                <X size={13} />
              </button>
            </div>
          )}
        </div>
      </div>

      <TransportBar engine={engine} />
      <StatusBar engine={engine} />
    </div>
  );
}

function PlayheadLine({ seconds, zoom, heightPx }: { seconds: number; zoom: number; heightPx: number }) {
  return (
    <div
      className="pointer-events-none absolute top-0 z-10 w-px bg-[hsl(var(--record))]"
      style={{ left: seconds * zoom, height: heightPx }}
      title={formatTime(seconds)}
    >
      <div className="absolute -left-[3px] -top-0.5 h-1.5 w-1.5 rounded-full bg-[hsl(var(--record))]" />
    </div>
  );
}
