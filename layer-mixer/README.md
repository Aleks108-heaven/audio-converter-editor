# Layer Mixer

A browser-based multi-track audio mixer inspired by AVS Audio Editor's Mix
module: drag audio clips around a shared timeline to layer/overlap them,
trim their edges, and nudge each track's pitch — all client-side, no
backend, via the Web Audio API.

It's a separate tool from the `audio_toolkit` Python desktop app in this
repo, not a panel inside it. It's a React + TypeScript + Tailwind + Web
Audio single-page app, published as a Claude Artifact.

Live version: https://claude.ai/artifact/94LkYxK9rTcct1ReMG11YN

## Why it depends on claude.ai

**Export Audio** and **Save Project** use the Claude Artifact platform's
`downloads` runtime capability (`window.claude.use("downloads")`) to hand
the viewer a file to save, because a published Claude Artifact runs in a
sandbox that blocks ordinary browser downloads (`<a download>`, etc.).

That means those two buttons only work when this app is running as the
published claude.ai artifact above. Opening `bundle.html` directly in a
browser, or embedding it elsewhere, loses Export Audio and Save Project
(the code detects the missing capability and disables them rather than
failing silently) — everything else (import, layering, trim, pitch, pan,
mute/solo, playback) works anywhere.

If you ever want this to run standalone (no claude.ai dependency), the fix
is localized: swap the `getDownloads()` calls in `src/lib/claude.ts` and
`src/hooks/useAudioEngine.ts` for a plain browser download (`URL
.createObjectURL` + a temporary `<a download>` click), and WAV export
becomes possible too, since the artifact sandbox's file-type allowlist
(no `.wav`/`.mp3`) no longer applies.

## Development

```bash
pnpm install
pnpm dev          # local dev server with HMR
```

## Rebuilding & republishing the artifact

This project was scaffolded and is bundled using Anthropic's
`web-artifacts-builder` skill/scripts (React + Vite + Tailwind + shadcn/ui,
bundled to one self-contained HTML file via Parcel + html-inline):

```bash
pnpm install
pnpm exec tsc -b && pnpm exec vite build   # sanity check
bash <path-to-web-artifacts-builder-skill>/scripts/bundle-artifact.sh
```

That produces `bundle.html` (gitignored is `dist/`, not `bundle.html` — the
checked-in copy in this folder may go stale after source edits; rebuild it
before republishing). Windows note: build under a short path
(e.g. `C:\Users\<you>\somewhere-short\`) — deep `node_modules/.pnpm/...`
paths combined with this repo's long default path can exceed Windows'
path-length limit and break the Parcel build.

Note: `package.json`/`pnpm-lock.yaml` still carry the full shadcn/ui +
Radix dependency set from the scaffold, even though the current UI is
hand-built with plain Tailwind (no shadcn components are imported). They're
left in so a real shadcn component can be added later without reinstalling;
trim them if you'd rather keep the lockfile minimal.

## Structure

- `src/hooks/useAudioEngine.ts` — the audio engine: track state, Web Audio
  playback graph (per-track gain/pan/rate), real-time export via
  `MediaRecorder`, project save/load.
- `src/components/ClipLane.tsx` — per-track waveform canvas + drag-to-move
  / drag-to-trim clip interaction.
- `src/components/Ruler.tsx`, `TrackHeader.tsx`, `TransportBar.tsx`,
  `StatusBar.tsx`, `Toolbar.tsx` — the rest of the UI shell.
- `src/lib/audio.ts` — peak extraction for waveform drawing, procedural
  demo-track synthesis (the two example clips the app opens with).
- `src/lib/claude.ts` — the `downloads` capability wrapper described above.
