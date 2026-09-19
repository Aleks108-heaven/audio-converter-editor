export function MiniSlider({
  label,
  value,
  display,
  min,
  max,
  step,
  onChange,
  className = "",
}: {
  label: string;
  value: number;
  display: string;
  min: number;
  max: number;
  step: number;
  onChange: (v: number) => void;
  className?: string;
}) {
  return (
    <div className={`flex items-center gap-1.5 ${className}`}>
      <span className="w-6 shrink-0 text-[9.5px] font-medium uppercase text-[hsl(var(--text-dim))]">{label}</span>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(parseFloat(e.target.value))}
        className="h-3 min-w-0 flex-1"
      />
      <span className="w-11 shrink-0 text-right font-mono-tech text-[9.5px] text-[hsl(var(--text-muted))]">
        {display}
      </span>
    </div>
  );
}
