import { useState } from "react";
import { ChevronRight } from "lucide-react";

interface EffectGroup {
  name: string;
  items: string[];
}

const GROUPS: EffectGroup[] = [
  {
    name: "Amplitude",
    items: [
      "Amplify",
      "Fade In / Fade Out",
      "Normalize",
      "Envelope",
      "Compressor",
      "Multiband Compressor",
      "Mute",
      "Invert",
      "Limiter",
      "Auto Correction",
    ],
  },
  {
    name: "Delay / Modulation",
    items: ["Echo", "Chorus", "Flanger", "Phaser", "Reverb", "Vibrato", "Voice Morpher"],
  },
  {
    name: "Time Stretch / Pitch",
    items: ["Tempo Change", "Pitch Shift", "Rate Change", "Reverse"],
  },
  {
    name: "Filters",
    items: ["Low-pass", "High-pass", "Band-pass", "Notch"],
  },
];

export function EffectsSidebar() {
  const [open, setOpen] = useState<Set<string>>(new Set(GROUPS.map((g) => g.name)));

  function toggle(name: string) {
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  }

  return (
    <div className="flex h-full w-60 shrink-0 flex-col border-r border-border bg-[hsl(var(--panel))]">
      <div className="flex items-center px-3 py-2.5 text-[11px] font-semibold uppercase tracking-wider text-[hsl(var(--text-dim))]">
        Effects &amp; Filters
      </div>
      <div className="flex-1 overflow-y-auto px-1.5 pb-3">
        {GROUPS.map((group) => {
          const isOpen = open.has(group.name);
          return (
            <div key={group.name} className="mb-1">
              <button
                type="button"
                onClick={() => toggle(group.name)}
                className="flex w-full items-center gap-1.5 rounded px-2 py-1.5 text-left text-[12.5px] font-medium text-[hsl(var(--text-muted))] hover:bg-[hsl(var(--field))] hover:text-[hsl(var(--text))]"
              >
                <ChevronRight
                  size={13}
                  strokeWidth={2.5}
                  className={`shrink-0 transition-transform duration-150 ${isOpen ? "rotate-90" : ""}`}
                />
                {group.name}
              </button>
              {isOpen && (
                <ul className="ml-2.5 border-l border-border/70 pl-2.5">
                  {group.items.map((item) => (
                    <li key={item}>
                      <span
                        className="block cursor-not-allowed select-none rounded px-2 py-1 text-[12.5px] text-[hsl(var(--text-dim))]"
                        title="Not wired up yet - static preview of AVS's effects tree"
                      >
                        {item}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          );
        })}
      </div>
      <div className="border-t border-border px-3 py-2 text-[10.5px] leading-snug text-[hsl(var(--text-dim))]">
        Preview only, for now - positioning &amp; pitch are live on each track.
      </div>
    </div>
  );
}
