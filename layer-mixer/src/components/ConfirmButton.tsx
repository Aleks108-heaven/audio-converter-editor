import { useEffect, useRef, useState } from "react";

/** A destructive action button that requires a second click within a few
 * seconds to confirm - no native `confirm()` dialog (unreliable inside a
 * sandboxed frame). */
export function ConfirmButton({
  icon,
  label,
  confirmLabel,
  onConfirm,
}: {
  icon: React.ReactNode;
  label: string;
  confirmLabel: string;
  onConfirm: () => void;
}) {
  const [armed, setArmed] = useState(false);
  const timeoutRef = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (timeoutRef.current) window.clearTimeout(timeoutRef.current);
    };
  }, []);

  function handleClick() {
    if (!armed) {
      setArmed(true);
      timeoutRef.current = window.setTimeout(() => setArmed(false), 3000);
      return;
    }
    if (timeoutRef.current) window.clearTimeout(timeoutRef.current);
    setArmed(false);
    onConfirm();
  }

  return (
    <button
      type="button"
      onClick={handleClick}
      onBlur={() => setArmed(false)}
      className={`flex items-center gap-1.5 rounded-md px-2.5 py-2 text-[12.5px] font-medium transition-colors ${
        armed
          ? "bg-[hsl(var(--record)/0.18)] text-[hsl(var(--record))]"
          : "text-[hsl(var(--text-muted))] hover:bg-[hsl(var(--field))] hover:text-[hsl(var(--text))]"
      }`}
    >
      {icon}
      {armed ? confirmLabel : label}
    </button>
  );
}
