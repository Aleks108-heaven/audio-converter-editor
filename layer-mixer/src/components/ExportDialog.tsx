import { useState } from "react";
import { CheckCircle2, Download, Loader2, TriangleAlert, X } from "lucide-react";
import type { AudioEngine } from "../hooks/useAudioEngine";
import { formatTime } from "../lib/format";

export function ExportDialog({ engine, onClose }: { engine: AudioEngine; onClose: () => void }) {
  const [filename, setFilename] = useState("my-mix");

  const canClose = !engine.isExporting;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={() => canClose && onClose()}
    >
      <div
        className="w-full max-w-sm rounded-xl border border-border bg-[hsl(var(--panel-alt))] p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-1 flex items-center justify-between">
          <h2 className="text-[14px] font-semibold text-[hsl(var(--text))]">Export mix</h2>
          {canClose && (
            <button onClick={onClose} className="text-[hsl(var(--text-dim))] hover:text-[hsl(var(--text))]">
              <X size={16} />
            </button>
          )}
        </div>
        <p className="mb-4 text-[12px] leading-relaxed text-[hsl(var(--text-muted))]">
          Renders every unmuted track in real time ({formatTime(engine.totalDuration, false)}) and saves a{" "}
          <code className="rounded bg-[hsl(var(--field))] px-1 py-0.5 text-[11px]">.webm</code> audio file - the
          format this view can save.
        </p>

        {!engine.isExporting && engine.exportDone === null && (
          <>
            <label className="mb-1 block text-[11px] font-medium text-[hsl(var(--text-muted))]">File name</label>
            <div className="mb-4 flex items-center overflow-hidden rounded-md border border-border-strong bg-[hsl(var(--field))]">
              <input
                autoFocus
                value={filename}
                onChange={(e) => setFilename(e.target.value)}
                className="min-w-0 flex-1 bg-transparent px-2.5 py-2 text-[13px] text-[hsl(var(--text))] outline-none"
                placeholder="my-mix"
              />
              <span className="pr-2.5 text-[12px] text-[hsl(var(--text-dim))]">.webm</span>
            </div>
            <button
              onClick={() => engine.exportMix(filename)}
              className="flex w-full items-center justify-center gap-2 rounded-md bg-[hsl(var(--accent))] py-2.5 text-[13px] font-semibold text-[#0d0f14] hover:bg-[hsl(var(--accent-strong))]"
            >
              <Download size={15} strokeWidth={2.5} />
              Render &amp; save
            </button>
          </>
        )}

        {engine.isExporting && (
          <div className="py-2">
            <div className="mb-2 flex items-center gap-2 text-[12.5px] text-[hsl(var(--text-muted))]">
              <Loader2 size={15} className="animate-spin text-[hsl(var(--accent))]" />
              Rendering... {Math.round(engine.exportProgress * 100)}%
            </div>
            <div className="h-1.5 w-full overflow-hidden rounded-full bg-[hsl(var(--field))]">
              <div
                className="h-full rounded-full bg-[hsl(var(--accent))] transition-[width]"
                style={{ width: `${engine.exportProgress * 100}%` }}
              />
            </div>
          </div>
        )}

        {!engine.isExporting && engine.exportDone && (
          <div className="flex items-start gap-2 rounded-md bg-[hsl(var(--ok)/0.12)] p-3 text-[12.5px] text-[hsl(var(--ok))]">
            <CheckCircle2 size={16} className="mt-0.5 shrink-0" />
            <span>
              {engine.exportDone === "saved" ? "Saved." : "Delivered."} Close this dialog to keep working, or export
              again.
            </span>
          </div>
        )}

        {engine.exportError && (
          <div className="mt-3 flex items-start gap-2 rounded-md bg-[hsl(var(--record)/0.12)] p-3 text-[12.5px] text-[hsl(var(--record))]">
            <TriangleAlert size={16} className="mt-0.5 shrink-0" />
            <span>{engine.exportError}</span>
          </div>
        )}
      </div>
    </div>
  );
}
