export const PEAKS_PER_SECOND = 50;

/** Downsample an AudioBuffer (channel-averaged) into [min,max] pairs, at a
 * fixed density of PEAKS_PER_SECOND buckets per second of source audio, so
 * the same peak data can be redrawn at any zoom level without recomputing. */
export function computePeaks(buffer: AudioBuffer): Float32Array {
  const numChannels = buffer.numberOfChannels;
  const length = buffer.length;
  const sampleRate = buffer.sampleRate;
  const bucketSize = Math.max(1, Math.floor(sampleRate / PEAKS_PER_SECOND));
  const numBuckets = Math.max(1, Math.ceil(length / bucketSize));
  const peaks = new Float32Array(numBuckets * 2);

  const channelData: Float32Array[] = [];
  for (let c = 0; c < numChannels; c++) channelData.push(buffer.getChannelData(c));

  for (let b = 0; b < numBuckets; b++) {
    const start = b * bucketSize;
    const end = Math.min(length, start + bucketSize);
    let min = 0;
    let max = 0;
    for (let i = start; i < end; i++) {
      let sample = 0;
      for (let c = 0; c < numChannels; c++) sample += channelData[c][i];
      sample /= numChannels;
      if (sample < min) min = sample;
      if (sample > max) max = sample;
    }
    peaks[b * 2] = min;
    peaks[b * 2 + 1] = max;
  }
  return peaks;
}

export async function decodeAudioFile(file: File, ctx: AudioContext): Promise<AudioBuffer> {
  const arrayBuffer = await file.arrayBuffer();
  return await ctx.decodeAudioData(arrayBuffer);
}

function makeMonoBuffer(
  ctx: AudioContext,
  durationSec: number,
  fill: (t: number) => number
): AudioBuffer {
  const sampleRate = ctx.sampleRate;
  const length = Math.floor(durationSec * sampleRate);
  const buffer = ctx.createBuffer(1, length, sampleRate);
  const data = buffer.getChannelData(0);
  for (let i = 0; i < length; i++) {
    data[i] = fill(i / sampleRate);
  }
  return buffer;
}

/** A soft, slowly-swelling pad - demonstrates a long ambient bed a rhythmic
 * clip can be layered inside of. */
export function synthDemoPad(ctx: AudioContext): AudioBuffer {
  const dur = 7.5;
  return makeMonoBuffer(ctx, dur, (t) => {
    const env = Math.min(1, t / 1.4) * Math.min(1, (dur - t) / 2.2);
    const s =
      0.5 * Math.sin(2 * Math.PI * 220 * t) +
      0.3 * Math.sin(2 * Math.PI * 277.18 * t) +
      0.2 * Math.sin(2 * Math.PI * 329.63 * t);
    return s * env * 0.35;
  });
}

/** A simple deterministic click/kick pattern - demonstrates a rhythmic clip
 * dropped mid-way into the pad, with room to nudge its pitch. */
export function synthDemoBeat(ctx: AudioContext): AudioBuffer {
  const dur = 6;
  const bpm = 100;
  const beatLen = 60 / bpm;
  let seed = 42;
  const rand = () => {
    seed = (seed * 1103515245 + 12345) & 0x7fffffff;
    return seed / 0x7fffffff;
  };
  return makeMonoBuffer(ctx, dur, (t) => {
    const beatPos = t % beatLen;
    const beatIndex = Math.floor(t / beatLen);
    const accent = beatIndex % 4 === 0;
    const decay = Math.exp(-beatPos * (accent ? 18 : 28));
    const click = beatPos < 0.05 ? (rand() * 2 - 1) * decay : 0;
    const tone = accent ? Math.sin(2 * Math.PI * 180 * beatPos) * decay * 0.6 : 0;
    return (click * 0.5 + tone) * 0.5;
  });
}
