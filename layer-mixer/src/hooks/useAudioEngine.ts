import { useCallback, useEffect, useRef, useState } from "react";
import type { SerializedProject, Selection, Track } from "../types";
import { playableLength, trackEndSec } from "../types";
import { clamp, dbToLinear, semitonesToRate } from "../lib/format";
import { computePeaks, decodeAudioFile, synthDemoBeat, synthDemoPad } from "../lib/audio";
import { colorForIndex, makeId } from "../lib/id";
import { getDownloads } from "../lib/claude";

interface ActiveNode {
  source: AudioBufferSourceNode;
  gain: GainNode;
  panner: StereoPannerNode;
}

function computeTotalDuration(tracks: Track[]): number {
  return tracks.reduce((max, t) => Math.max(max, trackEndSec(t)), 0);
}

export function useAudioEngine() {
  const [tracks, setTracks] = useState<Track[]>([]);
  const [isPlaying, setIsPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [loop, setLoop] = useState(false);
  const [selection, setSelectionState] = useState<Selection | null>(null);
  const [zoom, setZoom] = useState(90); // px per second
  const [importError, setImportError] = useState<string | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  const [isExporting, setIsExporting] = useState(false);
  const [exportProgress, setExportProgress] = useState(0);
  const [exportDone, setExportDone] = useState<"saved" | "delivered" | null>(null);

  const ctxRef = useRef<AudioContext | null>(null);
  const masterGainRef = useRef<GainNode | null>(null);
  const nodesRef = useRef<Map<string, ActiveNode>>(new Map());
  const exportNodesRef = useRef<Map<string, ActiveNode>>(new Map());
  const playStartCtxTimeRef = useRef(0);
  const playStartOffsetRef = useRef(0);
  const rafRef = useRef<number | null>(null);
  const initedRef = useRef(false);

  // Stable mirrors so play/pause/seek callbacks (identity kept stable for
  // child memoization) always see the latest state without re-subscribing.
  const tracksRef = useRef<Track[]>(tracks);
  const isPlayingRef = useRef(isPlaying);
  const loopRef = useRef(loop);
  const selectionRef = useRef(selection);
  useEffect(() => {
    tracksRef.current = tracks;
  }, [tracks]);
  useEffect(() => {
    isPlayingRef.current = isPlaying;
  }, [isPlaying]);
  useEffect(() => {
    loopRef.current = loop;
  }, [loop]);
  useEffect(() => {
    selectionRef.current = selection;
  }, [selection]);

  const ensureCtx = useCallback((): AudioContext => {
    if (!ctxRef.current) {
      const Ctx = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      const ctx = new Ctx();
      const master = ctx.createGain();
      master.connect(ctx.destination);
      ctxRef.current = ctx;
      masterGainRef.current = master;
    }
    return ctxRef.current;
  }, []);

  // Seed two generated demo clips on first mount so the mixer opens already
  // demonstrating overlap + pitch, instead of an empty upload prompt.
  useEffect(() => {
    if (initedRef.current) return;
    initedRef.current = true;
    const ctx = ensureCtx();
    const padBuffer = synthDemoPad(ctx);
    const beatBuffer = synthDemoBeat(ctx);
    const pad: Track = {
      id: makeId(),
      name: "Demo Pad",
      fileName: "generated pad",
      color: colorForIndex(0),
      buffer: padBuffer,
      peaks: computePeaks(padBuffer),
      duration: padBuffer.duration,
      startSec: 0,
      trimStart: 0,
      trimEnd: 0,
      gainDb: -2,
      pan: -0.15,
      semitones: 0,
      muted: false,
      solo: false,
      isDemo: true,
    };
    const beat: Track = {
      id: makeId(),
      name: "Demo Beat",
      fileName: "generated beat",
      color: colorForIndex(1),
      buffer: beatBuffer,
      peaks: computePeaks(beatBuffer),
      duration: beatBuffer.duration,
      startSec: 3.2,
      trimStart: 0,
      trimEnd: 0,
      gainDb: -1,
      pan: 0.15,
      semitones: 3,
      muted: false,
      solo: false,
      isDemo: true,
    };
    setTracks([pad, beat]);
  }, [ensureCtx]);

  const stopAllNodes = useCallback((map: Map<string, ActiveNode> = nodesRef.current) => {
    map.forEach(({ source }) => {
      try {
        source.stop();
      } catch {
        /* already stopped */
      }
      try {
        source.disconnect();
      } catch {
        /* already disconnected */
      }
    });
    map.clear();
  }, []);

  const scheduleFrom = useCallback(
    (fromSec: number, bus: AudioNode, list: Track[], targetMap: Map<string, ActiveNode>): number => {
      const ctx = ensureCtx();
      const startCtxTime = ctx.currentTime + 0.06;
      const anySolo = list.some((t) => t.solo);
      list.forEach((t) => {
        if (!t.buffer) return;
        if (t.muted) return;
        if (anySolo && !t.solo) return;
        const len = playableLength(t);
        if (len <= 0) return;
        const end = t.startSec + len;
        if (end <= fromSec) return;

        const rate = semitonesToRate(t.semitones);
        const source = ctx.createBufferSource();
        source.buffer = t.buffer;
        source.playbackRate.value = rate;
        const gain = ctx.createGain();
        gain.gain.value = dbToLinear(t.gainDb);
        const panner = ctx.createStereoPanner();
        panner.pan.value = t.pan;
        source.connect(gain).connect(panner).connect(bus);

        let when: number;
        let offset: number;
        let duration: number;
        if (t.startSec >= fromSec) {
          when = startCtxTime + (t.startSec - fromSec);
          offset = t.trimStart;
          duration = len;
        } else {
          const elapsedTimeline = fromSec - t.startSec;
          const elapsedBuffer = elapsedTimeline * rate;
          if (elapsedBuffer >= len) return;
          when = startCtxTime;
          offset = t.trimStart + elapsedBuffer;
          duration = len - elapsedBuffer;
        }
        try {
          source.start(when, offset, duration);
          targetMap.set(t.id, { source, gain, panner });
        } catch {
          /* zero/negative duration edge case - skip this clip */
        }
      });
      return startCtxTime;
    },
    [ensureCtx]
  );

  const tick = useCallback(() => {
    const ctx = ctxRef.current;
    if (!ctx) return;
    const elapsed = ctx.currentTime - playStartCtxTimeRef.current;
    const t = playStartOffsetRef.current + Math.max(0, elapsed);
    const total = computeTotalDuration(tracksRef.current);
    const sel = selectionRef.current;
    const endBound = sel ? sel.endSec : total;

    if (t >= endBound) {
      if (loopRef.current) {
        const loopStart = sel ? sel.startSec : 0;
        stopAllNodes();
        const startCtxTime = scheduleFrom(loopStart, masterGainRef.current!, tracksRef.current, nodesRef.current);
        playStartCtxTimeRef.current = startCtxTime;
        playStartOffsetRef.current = loopStart;
        setCurrentTime(loopStart);
        rafRef.current = requestAnimationFrame(tick);
        return;
      }
      setCurrentTime(endBound);
      stopAllNodes();
      setIsPlaying(false);
      return;
    }
    setCurrentTime(t);
    rafRef.current = requestAnimationFrame(tick);
  }, [scheduleFrom, stopAllNodes]);

  const play = useCallback(() => {
    const ctx = ensureCtx();
    if (ctx.state === "suspended") ctx.resume();
    const total = computeTotalDuration(tracksRef.current);
    if (total <= 0) return;
    const sel = selectionRef.current;
    const endBound = sel ? sel.endSec : total;
    let from = currentTime;
    if (from >= endBound - 0.01) from = sel ? sel.startSec : 0;

    stopAllNodes();
    const startCtxTime = scheduleFrom(from, masterGainRef.current!, tracksRef.current, nodesRef.current);
    playStartCtxTimeRef.current = startCtxTime;
    playStartOffsetRef.current = from;
    setIsPlaying(true);
    if (rafRef.current) cancelAnimationFrame(rafRef.current);
    rafRef.current = requestAnimationFrame(tick);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentTime, ensureCtx, scheduleFrom, stopAllNodes, tick]);

  const pause = useCallback(() => {
    const ctx = ctxRef.current;
    if (ctx && isPlayingRef.current) {
      const elapsed = ctx.currentTime - playStartCtxTimeRef.current;
      setCurrentTime(clamp(playStartOffsetRef.current + Math.max(0, elapsed), 0, computeTotalDuration(tracksRef.current)));
    }
    stopAllNodes();
    if (rafRef.current) cancelAnimationFrame(rafRef.current);
    setIsPlaying(false);
  }, [stopAllNodes]);

  const stop = useCallback(() => {
    stopAllNodes();
    if (rafRef.current) cancelAnimationFrame(rafRef.current);
    setIsPlaying(false);
    setCurrentTime(selectionRef.current?.startSec ?? 0);
  }, [stopAllNodes]);

  const seek = useCallback(
    (sec: number) => {
      const total = computeTotalDuration(tracksRef.current);
      const clamped = clamp(sec, 0, total);
      setCurrentTime(clamped);
      if (isPlayingRef.current) {
        stopAllNodes();
        const startCtxTime = scheduleFrom(clamped, masterGainRef.current!, tracksRef.current, nodesRef.current);
        playStartCtxTimeRef.current = startCtxTime;
        playStartOffsetRef.current = clamped;
      }
    },
    [scheduleFrom, stopAllNodes]
  );

  const setSelection = useCallback((sel: Selection | null) => {
    setSelectionState(sel);
  }, []);

  const addFiles = useCallback(
    async (files: FileList | File[]) => {
      const ctx = ensureCtx();
      const list = Array.from(files).filter(
        (f) => f.type.startsWith("audio/") || /\.(mp3|wav|m4a|ogg|flac|aac|wma|mp2|amr)$/i.test(f.name)
      );
      if (list.length === 0) {
        setImportError("No supported audio files found (mp3, wav, m4a, ogg, flac, aac).");
        return;
      }
      setImportError(null);
      for (const file of list) {
        try {
          const buffer = await decodeAudioFile(file, ctx);
          const peaks = computePeaks(buffer);
          setTracks((prev) => {
            const newStart = prev.length ? Math.max(0, ...prev.map((t) => trackEndSec(t))) : 0;
            const track: Track = {
              id: makeId(),
              name: file.name.replace(/\.[^./\\]+$/, ""),
              fileName: file.name,
              color: colorForIndex(prev.length),
              buffer,
              peaks,
              duration: buffer.duration,
              startSec: newStart,
              trimStart: 0,
              trimEnd: 0,
              gainDb: 0,
              pan: 0,
              semitones: 0,
              muted: false,
              solo: false,
            };
            return [...prev, track];
          });
        } catch {
          setImportError(`Couldn't decode "${file.name}" - unsupported or corrupt audio file.`);
        }
      }
    },
    [ensureCtx]
  );

  const updateTrack = useCallback((id: string, patch: Partial<Track>) => {
    setTracks((prev) => prev.map((t) => (t.id === id ? { ...t, ...patch } : t)));

    const node = nodesRef.current.get(id);
    if (node) {
      if (patch.gainDb !== undefined) node.gain.gain.value = dbToLinear(patch.gainDb);
      if (patch.pan !== undefined) node.panner.pan.value = patch.pan;
      if (patch.semitones !== undefined) node.source.playbackRate.value = semitonesToRate(patch.semitones);
    }
    if ((patch.muted !== undefined || patch.solo !== undefined) && isPlayingRef.current) {
      requestAnimationFrame(() => {
        const ctx = ctxRef.current;
        if (!ctx) return;
        const elapsed = ctx.currentTime - playStartCtxTimeRef.current;
        const now = playStartOffsetRef.current + Math.max(0, elapsed);
        stopAllNodes();
        const startCtxTime = scheduleFrom(now, masterGainRef.current!, tracksRef.current, nodesRef.current);
        playStartCtxTimeRef.current = startCtxTime;
        playStartOffsetRef.current = now;
      });
    }
  }, [scheduleFrom, stopAllNodes]);

  const removeTrack = useCallback(
    (id: string) => {
      const node = nodesRef.current.get(id);
      if (node) {
        try {
          node.source.stop();
        } catch {
          /* noop */
        }
        nodesRef.current.delete(id);
      }
      setTracks((prev) => prev.filter((t) => t.id !== id));
    },
    []
  );

  const newProject = useCallback(() => {
    pause();
    setTracks([]);
    setCurrentTime(0);
    setSelectionState(null);
    setImportError(null);
    setExportError(null);
    setExportDone(null);
  }, [pause]);

  const saveProject = useCallback(async () => {
    const downloads = await getDownloads();
    if (!downloads) {
      setExportError("Saving is unavailable in this view.");
      return;
    }
    const data: SerializedProject = {
      version: 1,
      savedAt: new Date().toISOString(),
      tracks: tracksRef.current.map((t) => ({
        id: t.id,
        name: t.name,
        fileName: t.fileName,
        color: t.color,
        duration: t.duration,
        startSec: t.startSec,
        trimStart: t.trimStart,
        trimEnd: t.trimEnd,
        gainDb: t.gainDb,
        pan: t.pan,
        semitones: t.semitones,
        muted: t.muted,
        solo: t.solo,
      })),
    };
    try {
      await downloads.save({ filename: "mix-project.json", data: JSON.stringify(data, null, 2) });
    } catch (err) {
      const code = (err as { code?: string })?.code;
      if (code !== "declined") setExportError((err as Error)?.message || "Could not save the project.");
    }
  }, []);

  const loadProjectFile = useCallback(
    async (file: File) => {
      try {
        const text = await file.text();
        const parsed = JSON.parse(text) as SerializedProject;
        if (!parsed || parsed.version !== 1 || !Array.isArray(parsed.tracks)) {
          throw new Error("not a project file");
        }
        pause();
        setTracks(
          parsed.tracks.map((st, i) => ({
            ...st,
            color: st.color || colorForIndex(i),
            buffer: null,
            peaks: null,
          }))
        );
        setCurrentTime(0);
        setSelectionState(null);
        setImportError(null);
      } catch {
        setImportError("That file isn't a valid mix project (.json).");
      }
    },
    [pause]
  );

  const attachAudioToTrack = useCallback(
    async (id: string, file: File) => {
      const ctx = ensureCtx();
      try {
        const buffer = await decodeAudioFile(file, ctx);
        const peaks = computePeaks(buffer);
        setTracks((prev) =>
          prev.map((t) => {
            if (t.id !== id) return t;
            const trimStart = clamp(t.trimStart, 0, buffer.duration);
            const trimEnd = clamp(t.trimEnd, 0, Math.max(0, buffer.duration - trimStart));
            return { ...t, buffer, peaks, duration: buffer.duration, fileName: file.name, trimStart, trimEnd };
          })
        );
      } catch {
        setImportError(`Couldn't decode "${file.name}".`);
      }
    },
    [ensureCtx]
  );

  const exportMix = useCallback(
    async (filename: string) => {
      setExportError(null);
      setExportDone(null);
      const downloads = await getDownloads();
      if (!downloads) {
        setExportError("File saving is unavailable in this view.");
        return;
      }
      const list = tracksRef.current.filter((t) => t.buffer);
      const total = computeTotalDuration(list);
      if (total <= 0) {
        setExportError("Nothing to export yet - add or attach at least one audio clip.");
        return;
      }

      pause();
      const ctx = ensureCtx();
      if (ctx.state === "suspended") await ctx.resume();

      const dest = ctx.createMediaStreamDestination();
      const exportBus = ctx.createGain();
      exportBus.connect(dest);
      exportBus.connect(ctx.destination);

      let recorder: MediaRecorder;
      try {
        recorder = new MediaRecorder(dest.stream, { mimeType: "audio/webm;codecs=opus" });
      } catch {
        setExportError("This browser can't encode audio for export.");
        exportBus.disconnect();
        return;
      }
      const chunks: BlobPart[] = [];
      recorder.ondataavailable = (e) => {
        if (e.data.size > 0) chunks.push(e.data);
      };
      const stopped = new Promise<void>((resolve) => {
        recorder.onstop = () => resolve();
      });

      setIsExporting(true);
      setExportProgress(0);
      recorder.start();
      const startCtxTime = scheduleFrom(0, exportBus, list, exportNodesRef.current);

      const poll = () => {
        const elapsed = ctx.currentTime - startCtxTime;
        setExportProgress(clamp(elapsed / total, 0, 1));
        if (elapsed < total) {
          requestAnimationFrame(poll);
        } else {
          recorder.stop();
          stopAllNodes(exportNodesRef.current);
          exportBus.disconnect();
        }
      };
      requestAnimationFrame(poll);

      await stopped;
      setIsExporting(false);
      setExportProgress(1);

      const blob = new Blob(chunks, { type: "audio/webm" });
      const trimmedName = filename.trim() || "mix";
      const withExt = trimmedName.toLowerCase().endsWith(".webm") ? trimmedName : `${trimmedName}.webm`;
      try {
        const result = await downloads.save({ filename: withExt, data: blob });
        setExportDone(result.status);
      } catch (err) {
        const code = (err as { code?: string })?.code;
        if (code !== "declined") setExportError((err as Error)?.message || "Export failed.");
      }
    },
    [ensureCtx, pause, scheduleFrom, stopAllNodes]
  );

  useEffect(() => {
    return () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
      stopAllNodes();
      stopAllNodes(exportNodesRef.current);
    };
  }, [stopAllNodes]);

  return {
    tracks,
    isPlaying,
    currentTime,
    loop,
    setLoop,
    selection,
    setSelection,
    zoom,
    setZoom,
    totalDuration: computeTotalDuration(tracks),
    importError,
    setImportError,
    exportError,
    setExportError,
    isExporting,
    exportProgress,
    exportDone,
    setExportDone,
    play,
    pause,
    stop,
    seek,
    addFiles,
    updateTrack,
    removeTrack,
    newProject,
    saveProject,
    loadProjectFile,
    attachAudioToTrack,
    exportMix,
    sampleRate: ctxRef.current?.sampleRate ?? 44100,
  };
}

export type AudioEngine = ReturnType<typeof useAudioEngine>;
