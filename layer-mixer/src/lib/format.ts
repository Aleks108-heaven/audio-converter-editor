export function formatTime(totalSeconds: number, withMs = true): string {
  const s = Math.max(0, totalSeconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  const ms = Math.floor((s - Math.floor(s)) * 1000);
  const mm = h > 0 ? String(m).padStart(2, "0") : String(m);
  const ss = String(sec).padStart(2, "0");
  const base = h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
  return withMs ? `${base}.${String(ms).padStart(3, "0")}` : base;
}

export function formatDb(db: number): string {
  if (db <= -59.5) return "-∞ dB";
  return `${db > 0 ? "+" : ""}${db.toFixed(1)} dB`;
}

export function formatPan(pan: number): string {
  if (Math.abs(pan) < 0.01) return "C";
  const pct = Math.round(Math.abs(pan) * 100);
  return pan < 0 ? `L${pct}` : `R${pct}`;
}

export function formatSemitones(st: number): string {
  if (st === 0) return "0 st";
  return `${st > 0 ? "+" : ""}${st} st`;
}

export function formatHz(hz: number): string {
  return hz >= 1000 ? `${(hz / 1000).toFixed(1)} kHz` : `${Math.round(hz)} Hz`;
}

export function formatBytesApprox(bytes: number): string {
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function dbToLinear(db: number): number {
  if (db <= -60) return 0;
  return Math.pow(10, db / 20);
}

export function semitonesToRate(semitones: number): number {
  return Math.pow(2, semitones / 12);
}

export function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}
