export function makeId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `id-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

export const TRACK_COLORS = [
  "#4f8cff", // accent blue
  "#35d399", // green
  "#ffb020", // amber
  "#ff5470", // pink/red
  "#b98aff", // violet
  "#4fd6ff", // cyan
  "#ff9a4f", // orange
];

export function colorForIndex(index: number): string {
  return TRACK_COLORS[index % TRACK_COLORS.length];
}
