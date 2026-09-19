import { useRef, useState } from "react";
import { AudioLines, Download, FilePlus2, FolderOpen, Save, Upload } from "lucide-react";
import type { AudioEngine } from "../hooks/useAudioEngine";
import { ExportDialog } from "./ExportDialog";
import { ConfirmButton } from "./ConfirmButton";

export function Toolbar({ engine }: { engine: AudioEngine }) {
  const openInputRef = useRef<HTMLInputElement>(null);
  const importInputRef = useRef<HTMLInputElement>(null);
  const [exportOpen, setExportOpen] = useState(false);

  return (
    <div className="flex h-14 shrink-0 items-center gap-2 border-b border-border bg-[hsl(var(--panel))] px-3">
      <div className="mr-2 flex items-center gap-2 pr-3">
        <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-[hsl(var(--accent)/0.15)] text-[hsl(var(--accent))]">
          <AudioLines size={18} strokeWidth={2.25} />
        </div>
        <div className="leading-tight">
          <div className="text-[13.5px] font-semibold text-[hsl(var(--text))]">Layer Mixer</div>
          <div className="text-[10.5px] text-[hsl(var(--text-dim))]">multi-track audio layering</div>
        </div>
      </div>

      <div className="h-7 w-px bg-border" />

      <ConfirmButton
        icon={<FilePlus2 size={15} />}
        label="New Project"
        confirmLabel="Clear all tracks?"
        onConfirm={engine.newProject}
      />

      <ToolbarButton icon={<FolderOpen size={15} />} label="Open Project" onClick={() => openInputRef.current?.click()} />
      <input
        ref={openInputRef}
        type="file"
        accept="application/json,.json"
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) engine.loadProjectFile(file);
          e.target.value = "";
        }}
      />

      <ToolbarButton icon={<Save size={15} />} label="Save Project" onClick={engine.saveProject} />

      <div className="h-7 w-px bg-border" />

      <ToolbarButton
        icon={<Upload size={15} />}
        label="Import Audio"
        onClick={() => importInputRef.current?.click()}
      />
      <input
        ref={importInputRef}
        type="file"
        multiple
        accept="audio/*,.mp3,.wav,.m4a,.ogg,.flac,.aac,.wma,.mp2,.amr"
        className="hidden"
        onChange={(e) => {
          if (e.target.files?.length) engine.addFiles(e.target.files);
          e.target.value = "";
        }}
      />

      <div className="flex-1" />

      <button
        type="button"
        onClick={() => setExportOpen(true)}
        className="flex items-center gap-1.5 rounded-md bg-[hsl(var(--accent))] px-3.5 py-2 text-[12.5px] font-semibold text-[#0d0f14] transition-colors hover:bg-[hsl(var(--accent-strong))]"
      >
        <Download size={15} strokeWidth={2.5} />
        Export Audio
      </button>

      {exportOpen && <ExportDialog engine={engine} onClose={() => setExportOpen(false)} />}
    </div>
  );
}

function ToolbarButton({
  icon,
  label,
  onClick,
}: {
  icon: React.ReactNode;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex items-center gap-1.5 rounded-md px-2.5 py-2 text-[12.5px] font-medium text-[hsl(var(--text-muted))] transition-colors hover:bg-[hsl(var(--field))] hover:text-[hsl(var(--text))]"
    >
      {icon}
      {label}
    </button>
  );
}
