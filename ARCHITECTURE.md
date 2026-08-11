# Real-Time Audio Architecture

The application uses one immutable, block-based audio graph for both live
playback and export.

```text
source files
    |
    v
explicit pre-processing
  - canonical 44.1 kHz stereo WAV
  - tempo/loudness analysis
  - Demucs stems when the selected mode needs them
    |
    v
prepared-asset manifest
    |
    v
audio graph snapshot
  FileSource -> routing/clips -> gain/pan/duck -> track cache -> master mix
       |                                                        |
       +---------------- live callback (small blocks) ----------+
       +---------------- smart atomic export -------------------+
```

## Preparation boundary

`mashup_app.preprocessing` is the only application layer allowed to invoke
stem separation. Prepared files are content-versioned by source path, size,
and modification time. `mashup_app.separation.get_prepared_*` and final export
are cache-only; missing assets produce a preparation error instead of silently
starting Demucs.

## Real-time graph

`mashup_app.audio_graph` contains random-access `AudioNode` implementations.
Nodes render float32 stereo frames on demand, so seeking and live playback do
not require a full-song mixdown. Timeline clips and track settings remain edit
descriptors and never overwrite source media. Slow clip/track effects are
materialized as fingerprinted derived assets, while routing, gain, pan, fades,
sidechain ducking, and summing remain live graph operations.

`RealtimeMixer` drives the graph from a `sounddevice` output callback. A new
graph snapshot can replace the current snapshot while preserving transport
position.

## Smart rendering

`SmartRenderer` renders the same graph used for preview. Exports are written to
a temporary file and atomically moved into place. A manifest records the graph
fingerprint; exporting an unchanged snapshot reuses the existing output.
Per-track `CachedNode` instances also reuse blocks already computed during
preview or an earlier graph pass.
