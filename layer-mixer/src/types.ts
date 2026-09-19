export interface Track {
  id: string;
  name: string;
  fileName: string;
  color: string;
  /** Decoded audio; null when a loaded project's clip hasn't been re-attached yet. */
  buffer: AudioBuffer | null;
  /** Interleaved [min, max] pairs, PEAKS_PER_SECOND buckets/sec of source audio. */
  peaks: Float32Array | null;
  /** Full source length in seconds (independent of trim). */
  duration: number;
  /** Timeline position, in seconds, of the (trimmed) clip's start. */
  startSec: number;
  /** Seconds trimmed off the front of the source buffer. */
  trimStart: number;
  /** Seconds trimmed off the back of the source buffer. */
  trimEnd: number;
  gainDb: number;
  pan: number;
  /** Pitch shift in semitones; also changes speed (playbackRate-based). */
  semitones: number;
  muted: boolean;
  solo: boolean;
  isDemo?: boolean;
}

export interface Selection {
  startSec: number;
  endSec: number;
}

export interface SerializedTrack {
  id: string;
  name: string;
  fileName: string;
  color: string;
  duration: number;
  startSec: number;
  trimStart: number;
  trimEnd: number;
  gainDb: number;
  pan: number;
  semitones: number;
  muted: boolean;
  solo: boolean;
}

export interface SerializedProject {
  version: 1;
  savedAt: string;
  tracks: SerializedTrack[];
}

export function playableLength(t: Pick<Track, "duration" | "trimStart" | "trimEnd">): number {
  return Math.max(0, t.duration - t.trimStart - t.trimEnd);
}

export function trackEndSec(t: Pick<Track, "duration" | "trimStart" | "trimEnd" | "startSec">): number {
  return t.startSec + playableLength(t);
}
