"""Block-based, non-destructive audio graph, live transport, and smart export.

The graph never mutates source media.  Every node answers a small range of
floating-point frames, which lets the same project snapshot drive low-latency
playback and final export.  Slow transformations are cached as derived WAV
assets; gain, pan, routing, fades, sidechain ducking, and summing stay live.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import tempfile
import threading
import wave
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

import numpy as np
from pydub import AudioSegment

from . import analysis, audio_io, effects, mastering, preprocessing, separation

SAMPLE_RATE = 44100
CHANNELS = 2
DEFAULT_BLOCK_SIZE = 2048
GRAPH_CACHE_DIR = Path(__file__).resolve().parent.parent / "output" / ".graph-cache"
ASSET_CACHE_DIR = GRAPH_CACHE_DIR / "assets"
RENDER_MANIFEST_DIR = GRAPH_CACHE_DIR / "renders"


def _stable_hash(value) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _file_identity(path: str) -> dict:
    resolved = Path(path).resolve()
    stat = resolved.stat()
    return {"path": str(resolved), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _ms_to_frames(milliseconds: float, sample_rate: int = SAMPLE_RATE) -> int:
    return int(round(float(milliseconds) * sample_rate / 1000.0))


def _silence(frame_count: int) -> np.ndarray:
    return np.zeros((max(0, int(frame_count)), CHANNELS), dtype=np.float32)


class AudioNode:
    """A deterministic random-access source of stereo float32 audio frames."""

    fingerprint: str
    duration_frames: int

    def render(self, start_frame: int, frame_count: int) -> np.ndarray:
        raise NotImplementedError

    def close(self) -> None:
        """Release optional media/device resources held by this node."""


class ArraySourceNode(AudioNode):
    """In-memory source, useful for generated media and focused tests."""

    def __init__(self, samples: np.ndarray, fingerprint: Optional[str] = None):
        values = np.asarray(samples, dtype=np.float32)
        if values.ndim == 1:
            values = np.repeat(values[:, None], CHANNELS, axis=1)
        elif values.shape[1] == 1:
            values = np.repeat(values, CHANNELS, axis=1)
        elif values.shape[1] > CHANNELS:
            values = values[:, :CHANNELS]
        self.samples = np.ascontiguousarray(values)
        self.duration_frames = len(self.samples)
        self.fingerprint = fingerprint or _stable_hash(
            {"kind": "array", "shape": self.samples.shape, "bytes": self.samples.tobytes().hex()}
        )

    def render(self, start_frame: int, frame_count: int) -> np.ndarray:
        output = _silence(frame_count)
        destination_start = max(0, -int(start_frame))
        source_start = max(0, int(start_frame))
        available = min(frame_count - destination_start, self.duration_frames - source_start)
        if available > 0:
            output[destination_start:destination_start + available] = self.samples[
                source_start:source_start + available
            ]
        return output


class FileSourceNode(AudioNode):
    """Thread-safe, random-access reader for a prepared audio asset."""

    def __init__(self, path: str, sample_rate: int = SAMPLE_RATE):
        import soundfile as sf

        self.path = str(Path(path).resolve())
        self.sample_rate = int(sample_rate)
        info = sf.info(self.path)
        self.source_rate = int(info.samplerate)
        self.source_channels = int(info.channels)
        self.source_frames = int(info.frames)
        self.duration_frames = int(math.ceil(self.source_frames * self.sample_rate / self.source_rate))
        self.fingerprint = _stable_hash(
            {"kind": "file", "source": _file_identity(self.path), "sample_rate": self.sample_rate}
        )
        self._reader = None
        self._lock = threading.Lock()

    @staticmethod
    def _stereo(values: np.ndarray) -> np.ndarray:
        if values.shape[1] == 1:
            return np.repeat(values, CHANNELS, axis=1)
        return values[:, :CHANNELS]

    def _read_source(self, start: int, count: int) -> np.ndarray:
        import soundfile as sf

        output = np.zeros((max(0, count), max(1, self.source_channels)), dtype=np.float32)
        destination_start = max(0, -start)
        source_start = max(0, start)
        available = min(count - destination_start, self.source_frames - source_start)
        if available <= 0:
            return self._stereo(output)
        with self._lock:
            if self._reader is None:
                self._reader = sf.SoundFile(self.path, mode="r")
            self._reader.seek(source_start)
            values = self._reader.read(available, dtype="float32", always_2d=True)
        output[destination_start:destination_start + len(values), : values.shape[1]] = values
        return self._stereo(output)

    def render(self, start_frame: int, frame_count: int) -> np.ndarray:
        start_frame, frame_count = int(start_frame), max(0, int(frame_count))
        if self.source_rate == self.sample_rate:
            return self._read_source(start_frame, frame_count)
        if frame_count == 0:
            return _silence(0)
        ratio = self.source_rate / self.sample_rate
        positions = (start_frame + np.arange(frame_count, dtype=np.float64)) * ratio
        first = math.floor(float(positions[0]))
        last = math.ceil(float(positions[-1])) + 2
        source = self._read_source(first, max(0, last - first))
        local = positions - first
        x = np.arange(len(source), dtype=np.float64)
        output = _silence(frame_count)
        for channel in range(CHANNELS):
            output[:, channel] = np.interp(local, x, source[:, channel], left=0.0, right=0.0)
        return output

    def close(self) -> None:
        with self._lock:
            if self._reader is not None:
                self._reader.close()
                self._reader = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class OffsetNode(AudioNode):
    def __init__(self, source: AudioNode, offset_frames: int):
        self.source = source
        self.offset_frames = max(0, int(offset_frames))
        self.duration_frames = self.offset_frames + source.duration_frames
        self.fingerprint = _stable_hash(
            {"kind": "offset", "source": source.fingerprint, "frames": self.offset_frames}
        )

    def render(self, start_frame: int, frame_count: int) -> np.ndarray:
        return self.source.render(int(start_frame) - self.offset_frames, frame_count)

    def close(self) -> None:
        self.source.close()


class GainPanNode(AudioNode):
    def __init__(self, source: AudioNode, gain_db: float = 0.0, pan: float = 0.0):
        self.source = source
        self.gain_db = float(gain_db)
        self.pan = max(-1.0, min(1.0, float(pan)))
        self.duration_frames = source.duration_frames
        self.fingerprint = _stable_hash(
            {"kind": "gain-pan", "source": source.fingerprint, "gain_db": self.gain_db, "pan": self.pan}
        )

    def render(self, start_frame: int, frame_count: int) -> np.ndarray:
        output = self.source.render(start_frame, frame_count)
        gain = 10.0 ** (self.gain_db / 20.0)
        if abs(self.pan) <= 1e-8:
            return output * gain
        channel_gain = np.array(
            [math.sqrt(max(0.0, 1.0 - self.pan)), math.sqrt(max(0.0, 1.0 + self.pan))],
            dtype=np.float32,
        )
        return output * gain * channel_gain

    def close(self) -> None:
        self.source.close()


class AutomationNode(AudioNode):
    """Apply a piecewise-linear gain envelope expressed as (frame, gain)."""

    def __init__(self, source: AudioNode, points: Sequence[tuple[int, float]]):
        self.source = source
        self.points = tuple(sorted((int(frame), float(gain)) for frame, gain in points))
        self.duration_frames = source.duration_frames
        self.fingerprint = _stable_hash(
            {"kind": "automation", "source": source.fingerprint, "points": self.points}
        )

    def render(self, start_frame: int, frame_count: int) -> np.ndarray:
        output = self.source.render(start_frame, frame_count)
        if not self.points or frame_count <= 0:
            return output
        frames = start_frame + np.arange(frame_count)
        point_frames = np.asarray([item[0] for item in self.points], dtype=np.float64)
        point_gains = np.asarray([item[1] for item in self.points], dtype=np.float32)
        envelope = np.interp(frames, point_frames, point_gains, left=point_gains[0], right=point_gains[-1])
        return output * envelope[:, None]

    def close(self) -> None:
        self.source.close()


class MixNode(AudioNode):
    def __init__(self, sources: Sequence[AudioNode], duration_frames: Optional[int] = None):
        self.sources = tuple(sources)
        self.duration_frames = int(
            duration_frames if duration_frames is not None else max((s.duration_frames for s in self.sources), default=0)
        )
        self.fingerprint = _stable_hash(
            {"kind": "mix", "sources": [source.fingerprint for source in self.sources], "duration": self.duration_frames}
        )

    def render(self, start_frame: int, frame_count: int) -> np.ndarray:
        output = _silence(frame_count)
        for source in self.sources:
            output += source.render(start_frame, frame_count)
        return output

    def close(self) -> None:
        for source in self.sources:
            source.close()


class ControlEnvelope:
    """A compact, seekable sidechain envelope prepared at control rate."""

    def __init__(
        self, trigger: AudioNode, attack_ms: float = 10.0, release_ms: float = 200.0,
        sample_rate: int = SAMPLE_RATE, control_hz: int = 200,
    ):
        self.trigger = trigger
        self.attack_ms = float(attack_ms)
        self.release_ms = float(release_ms)
        self.sample_rate = int(sample_rate)
        self.hop = max(1, self.sample_rate // max(1, int(control_hz)))
        self.fingerprint = _stable_hash(
            {
                "kind": "control-envelope-v1", "trigger": trigger.fingerprint,
                "attack": self.attack_ms, "release": self.release_ms, "hop": self.hop,
            }
        )
        self._values: Optional[np.ndarray] = None
        self._lock = threading.Lock()

    def prepare(self) -> None:
        if self._values is not None:
            return
        with self._lock:
            if self._values is not None:
                return
            peaks = []
            chunk_frames = self.hop * 512
            for start in range(0, self.trigger.duration_frames, chunk_frames):
                count = min(chunk_frames, self.trigger.duration_frames - start)
                audio = self.trigger.render(start, count)
                mono = np.max(np.abs(audio), axis=1)
                pad = (-len(mono)) % self.hop
                if pad:
                    mono = np.pad(mono, (0, pad))
                peaks.append(mono.reshape(-1, self.hop).max(axis=1))
            raw = np.concatenate(peaks) if peaks else np.zeros(1, dtype=np.float32)
            control_rate = self.sample_rate / self.hop
            attack = math.exp(-1.0 / max(1.0, control_rate * self.attack_ms / 1000.0))
            release = math.exp(-1.0 / max(1.0, control_rate * self.release_ms / 1000.0))
            values = np.zeros_like(raw, dtype=np.float32)
            previous = 0.0
            for index, peak in enumerate(raw):
                coefficient = attack if peak > previous else release
                previous = coefficient * previous + (1.0 - coefficient) * float(peak)
                values[index] = min(1.0, previous)
            self._values = values

    def render(self, start_frame: int, frame_count: int) -> np.ndarray:
        self.prepare()
        assert self._values is not None
        positions = (start_frame + np.arange(frame_count, dtype=np.float64)) / self.hop
        control_positions = np.arange(len(self._values), dtype=np.float64)
        return np.interp(positions, control_positions, self._values, left=0.0, right=0.0).astype(np.float32)

    def close(self) -> None:
        self.trigger.close()


class SidechainDuckNode(AudioNode):
    """Apply a precomputed control-rate envelope without callback-time scans."""

    def __init__(
        self, target: AudioNode, trigger: AudioNode, amount: float,
        attack_ms: float = 10.0, release_ms: float = 200.0, sample_rate: int = SAMPLE_RATE,
        envelope: Optional[ControlEnvelope] = None,
    ):
        self.target = target
        self.trigger = trigger
        self.amount = max(0.0, min(1.0, float(amount)))
        self.attack_ms = float(attack_ms)
        self.release_ms = float(release_ms)
        self.sample_rate = int(sample_rate)
        self.envelope = envelope or ControlEnvelope(
            trigger, attack_ms=attack_ms, release_ms=release_ms, sample_rate=sample_rate
        )
        self.duration_frames = target.duration_frames
        self.fingerprint = _stable_hash(
            {
                "kind": "duck", "target": target.fingerprint, "trigger": trigger.fingerprint,
                "amount": self.amount, "envelope": self.envelope.fingerprint,
            }
        )

    def render(self, start_frame: int, frame_count: int) -> np.ndarray:
        target = self.target.render(start_frame, frame_count)
        if self.amount <= 0 or frame_count <= 0:
            return target
        envelope = self.envelope.render(start_frame, frame_count)
        return target * (1.0 - self.amount * envelope[:, None])

    def close(self) -> None:
        self.target.close()
        self.envelope.close()


class TimelineTrackNode(AudioNode):
    """Route non-destructive bed/replacement/layer descriptors for one track."""

    def __init__(
        self,
        sources: Mapping[str, AudioNode],
        origins: Mapping[str, int],
        bed: str,
        clips: Sequence[dict],
        duration_frames: int,
        crossfade_frames: int,
        processed_clips: Optional[Mapping[int, AudioNode]] = None,
        fingerprint_extra=None,
    ):
        self.sources = dict(sources)
        self.origins = {name: int(value) for name, value in origins.items()}
        self.bed = str(bed)
        self.clips = tuple(dict(clip) for clip in clips)
        self.duration_frames = int(duration_frames)
        self.crossfade_frames = max(0, int(crossfade_frames))
        self.processed_clips = dict(processed_clips or {})
        self.fingerprint = _stable_hash(
            {
                "kind": "timeline-track",
                "sources": {name: node.fingerprint for name, node in self.sources.items()},
                "origins": self.origins,
                "bed": self.bed,
                "clips": self.clips,
                "duration": self.duration_frames,
                "crossfade": self.crossfade_frames,
                "processed": {str(key): node.fingerprint for key, node in self.processed_clips.items()},
                "extra": fingerprint_extra,
            }
        )

    def _source_names(self, name: str) -> tuple[str, ...]:
        if name == "both":
            return tuple(lane for lane in ("primary", "secondary") if lane in self.sources)
        if name in self.sources:
            return (name,)
        return ()

    @staticmethod
    def _resampled(node: AudioNode, source_start: float, frame_count: int, speed: float) -> np.ndarray:
        if abs(speed - 1.0) <= 1e-7:
            return node.render(int(round(source_start)), frame_count)
        needed = max(2, int(math.ceil(frame_count * speed)) + 2)
        first = math.floor(source_start)
        raw = node.render(first, needed)
        positions = (source_start - first) + np.arange(frame_count, dtype=np.float64) * speed
        x = np.arange(len(raw), dtype=np.float64)
        output = _silence(frame_count)
        for channel in range(CHANNELS):
            output[:, channel] = np.interp(positions, x, raw[:, channel], left=0.0, right=0.0)
        return output

    def _render_lane(self, lane: str, timeline_start: int, frame_count: int, clip: Optional[dict] = None) -> np.ndarray:
        output = _silence(frame_count)
        if clip is not None and clip.get("mute"):
            return output
        for name in self._source_names(lane):
            source = self.sources[name]
            if clip is not None and "source_start_ms" in clip:
                clip_start = _ms_to_frames(clip["start_ms"])
                source_start = _ms_to_frames(clip["source_start_ms"]) + (
                    timeline_start - clip_start
                ) * effects.clip_speed(clip)
            else:
                source_start = timeline_start - self.origins.get(name, 0)
            output += self._resampled(source, source_start, frame_count, effects.clip_speed(clip))
        return output

    def _clip_audio(self, index: int, clip: dict, overlap_start: int, count: int) -> np.ndarray:
        processed = self.processed_clips.get(index)
        if processed is not None:
            return processed.render(overlap_start - _ms_to_frames(clip["start_ms"]), count)
        lane = "muted" if clip.get("mute") else clip.get("source", "primary")
        return self._render_lane(lane, overlap_start, count, clip)

    def _clip_weights(self, clip: dict, start: int, count: int) -> np.ndarray:
        clip_start = _ms_to_frames(clip["start_ms"])
        clip_end = _ms_to_frames(clip["end_ms"])
        frames = start + np.arange(count)
        weights = np.ones(count, dtype=np.float32)
        fade = min(self.crossfade_frames, max(0, (clip_end - clip_start) // 2))
        if fade:
            weights = np.minimum(weights, np.clip((frames - clip_start + 1) / fade, 0.0, 1.0))
            weights = np.minimum(weights, np.clip((clip_end - frames) / fade, 0.0, 1.0))
        return weights

    def render(self, start_frame: int, frame_count: int) -> np.ndarray:
        output = self._render_lane(self.bed, start_frame, frame_count)
        block_end = start_frame + frame_count
        replacements = [
            (index, clip) for index, clip in enumerate(self.clips)
            if clip.get("mode", "replace") != "layer"
        ]
        layers = [
            (index, clip) for index, clip in enumerate(self.clips)
            if clip.get("mode", "replace") == "layer"
        ]
        for index, clip in replacements + layers:
            clip_start = _ms_to_frames(clip["start_ms"])
            clip_end = _ms_to_frames(clip["end_ms"])
            overlap_start = max(start_frame, clip_start)
            overlap_end = min(block_end, clip_end)
            if overlap_end <= overlap_start:
                continue
            destination = slice(overlap_start - start_frame, overlap_end - start_frame)
            audio = self._clip_audio(index, clip, overlap_start, overlap_end - overlap_start)
            weights = self._clip_weights(clip, overlap_start, overlap_end - overlap_start)[:, None]
            if clip.get("mode", "replace") == "layer":
                output[destination] += audio * weights
            else:
                output[destination] = output[destination] * (1.0 - weights) + audio * weights
        return output

    def close(self) -> None:
        for source in self.sources.values():
            source.close()
        for source in self.processed_clips.values():
            source.close()


class CachedNode(AudioNode):
    """Small LRU of rendered blocks, shared by live playback and export."""

    def __init__(self, source: AudioNode, max_blocks: int = 512):
        self.source = source
        self.duration_frames = source.duration_frames
        self.fingerprint = source.fingerprint
        self.max_blocks = max(1, int(max_blocks))
        self._cache: OrderedDict[tuple[int, int], np.ndarray] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def render(self, start_frame: int, frame_count: int) -> np.ndarray:
        key = (int(start_frame), int(frame_count))
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                self.hits += 1
                return cached.copy()
        rendered = self.source.render(*key)
        with self._lock:
            self.misses += 1
            self._cache[key] = rendered.copy()
            self._cache.move_to_end(key)
            while len(self._cache) > self.max_blocks:
                self._cache.popitem(last=False)
        return rendered

    def close(self) -> None:
        self.source.close()


class OutputSafetyNode(AudioNode):
    def __init__(self, source: AudioNode):
        self.source = source
        self.duration_frames = source.duration_frames
        self.fingerprint = _stable_hash({"kind": "output-safety", "source": source.fingerprint})

    def render(self, start_frame: int, frame_count: int) -> np.ndarray:
        return np.clip(self.source.render(start_frame, frame_count), -1.0, 1.0)

    def close(self) -> None:
        self.source.close()


@dataclass(frozen=True)
class AudioGraph:
    root: AudioNode
    sample_rate: int = SAMPLE_RATE
    channels: int = CHANNELS
    block_size: int = DEFAULT_BLOCK_SIZE

    @property
    def fingerprint(self) -> str:
        return self.root.fingerprint

    @property
    def duration_frames(self) -> int:
        return self.root.duration_frames

    @property
    def duration_ms(self) -> int:
        return int(round(self.duration_frames * 1000.0 / self.sample_rate))

    def render_block(self, start_frame: int, frame_count: Optional[int] = None) -> np.ndarray:
        count = self.block_size if frame_count is None else max(0, int(frame_count))
        output = _silence(count)
        if start_frame >= self.duration_frames or count == 0:
            return output
        available = min(count, self.duration_frames - max(0, start_frame))
        if start_frame < 0:
            return self.root.render(start_frame, count)
        output[:available] = self.root.render(start_frame, available)
        return output

    def close(self) -> None:
        self.root.close()


class _AssetCache:
    def __init__(self, directory: Path = ASSET_CACHE_DIR):
        self.directory = directory
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def _lock(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    def transform(self, input_path: str, spec: dict, transform: Callable[[AudioSegment], AudioSegment]) -> str:
        key = _stable_hash({"input": _file_identity(input_path), "spec": spec})
        output_path = self.directory / f"{key}.wav"
        if output_path.exists():
            return str(output_path)
        with self._lock(key):
            if output_path.exists():
                return str(output_path)
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary = output_path.with_suffix(".tmp.wav")
            segment = AudioSegment.from_file(input_path).set_frame_rate(SAMPLE_RATE).set_channels(CHANNELS).set_sample_width(2)
            rendered = transform(segment).set_frame_rate(SAMPLE_RATE).set_channels(CHANNELS).set_sample_width(2)
            rendered.export(temporary, format="wav")
            os.replace(temporary, output_path)
        return str(output_path)

    def materialize_node(
        self, node: AudioNode, spec: dict, transform: Callable[[AudioSegment], AudioSegment]
    ) -> str:
        key = _stable_hash({"node": node.fingerprint, "spec": spec})
        output_path = self.directory / f"{key}.wav"
        if output_path.exists():
            return str(output_path)
        with self._lock(key):
            if output_path.exists():
                return str(output_path)
            blocks = []
            for start in range(0, node.duration_frames, DEFAULT_BLOCK_SIZE * 8):
                blocks.append(node.render(start, min(DEFAULT_BLOCK_SIZE * 8, node.duration_frames - start)))
            samples = np.concatenate(blocks, axis=0).T if blocks else np.zeros((CHANNELS, 0), dtype=np.float32)
            segment = audio_io.float_array_to_segment(samples, SAMPLE_RATE)
            rendered = transform(segment).set_frame_rate(SAMPLE_RATE).set_channels(CHANNELS).set_sample_width(2)
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary = output_path.with_suffix(".tmp.wav")
            rendered.export(temporary, format="wav")
            os.replace(temporary, output_path)
        return str(output_path)


class AudioGraphFactory:
    """Compile prepared assets and edit descriptors into reusable graph nodes."""

    def __init__(self, asset_cache_dir: Path = ASSET_CACHE_DIR):
        self.assets = _AssetCache(asset_cache_dir)
        self._nodes: dict[str, CachedNode] = {}
        self._envelopes: dict[str, ControlEnvelope] = {}
        self._analysis: dict[tuple[str, str], int] = {}

    def _cached(self, node: AudioNode) -> CachedNode:
        cached = self._nodes.get(node.fingerprint)
        if cached is None:
            cached = CachedNode(node)
            self._nodes[node.fingerprint] = cached
        return cached

    def close(self) -> None:
        for node in self._nodes.values():
            node.close()
        for envelope in self._envelopes.values():
            envelope.close()

    def _duck(self, target: AudioNode, trigger: AudioNode, amount: float) -> SidechainDuckNode:
        identity = _stable_hash({"trigger": trigger.fingerprint, "attack": 10.0, "release": 200.0})
        envelope = self._envelopes.get(identity)
        if envelope is None:
            envelope = ControlEnvelope(trigger)
            envelope.prepare()
            self._envelopes[identity] = envelope
        return SidechainDuckNode(target, trigger, amount, envelope=envelope)

    def _transform_source(self, path: str, *, tempo_rate=1.0, high_pass=0, reverb=0.0, room_size=0.5) -> str:
        spec = {
            "kind": "source-dsp-v1", "tempo_rate": round(float(tempo_rate), 8),
            "high_pass": int(high_pass), "reverb": round(float(reverb), 6),
            "room_size": round(float(room_size), 6),
        }
        if abs(tempo_rate - 1.0) <= 0.01 and not high_pass and reverb <= 0:
            return path

        def transform(segment: AudioSegment) -> AudioSegment:
            output = segment
            if abs(tempo_rate - 1.0) > 0.01:
                output = effects._stretch(output, float(tempo_rate))
            if high_pass:
                output = output.high_pass_filter(int(high_pass))
            if reverb > 0:
                output = mastering.add_reverb(output, room_size=float(room_size), wet_level=float(reverb))
            return output

        return self.assets.transform(path, spec, transform)

    def _canonical_source(self, path: str) -> str:
        return self.assets.transform(
            path,
            {"kind": "canonical-graph-media-v1", "sample_rate": SAMPLE_RATE, "channels": CHANNELS},
            lambda segment: segment,
        )

    @staticmethod
    def _normalization_gain(dbfs: float, target: float) -> float:
        return 0.0 if not math.isfinite(float(dbfs)) else float(target) - float(dbfs)

    def _graph(self, root: AudioNode) -> AudioGraph:
        return AudioGraph(OutputSafetyNode(root))

    def build(self, project: preprocessing.PreparedProject, settings: dict) -> AudioGraph:
        if project.mode == preprocessing.MODE_SIMPLE:
            return self.build_simple(project, settings)
        if project.mode == preprocessing.MODE_BEAT:
            return self.build_beat(project, settings)
        if project.mode == preprocessing.MODE_VOCALS:
            return self.build_vocals(project, settings)
        if project.mode == preprocessing.MODE_STEMS:
            return self.build_stems(project, settings)
        raise ValueError(f"Unsupported graph mode: {project.mode}")

    def build_simple(self, project: preprocessing.PreparedProject, settings: dict) -> AudioGraph:
        match = bool(settings.get("match_loudness", True))
        primary_path = self._transform_source(
            project.primary.audio_path,
            reverb=settings.get("primary_reverb_amount", 0.0), room_size=settings.get("reverb_size", 0.5),
        )
        secondary_path = self._transform_source(
            project.secondary.audio_path,
            high_pass=120 if settings.get("low_cut_secondary", True) else 0,
            reverb=settings.get("reverb_amount", 0.0), room_size=settings.get("reverb_size", 0.5),
        )
        primary_gain = float(settings.get("primary_gain_db", 0.0))
        secondary_gain = float(settings.get("secondary_gain_db", 0.0))
        if match:
            primary_gain += self._normalization_gain(project.primary.dbfs, -18.0)
            secondary_gain += self._normalization_gain(project.secondary.dbfs, -18.0)
        primary = GainPanNode(FileSourceNode(primary_path), primary_gain)
        secondary_local: AudioNode = GainPanNode(FileSourceNode(secondary_path), secondary_gain)
        fade = min(
            _ms_to_frames(settings.get("crossfade_ms", 0)),
            max(0, secondary_local.duration_frames // 2),
        )
        if fade:
            secondary_local = AutomationNode(
                secondary_local,
                ((0, 0.0), (fade, 1.0), (max(fade, secondary_local.duration_frames - fade), 1.0),
                 (secondary_local.duration_frames, 0.0)),
            )
        secondary = OffsetNode(secondary_local, _ms_to_frames(settings.get("offset_ms", 0)))
        if settings.get("duck_amount", 0.0) > 0:
            primary = self._duck(primary, secondary, settings["duck_amount"])
        return self._graph(MixNode((self._cached(primary), self._cached(secondary))))

    def build_beat(self, project: preprocessing.PreparedProject, settings: dict) -> AudioGraph:
        primary_bpm = float(project.primary.bpm or 0.0)
        secondary_bpm = float(project.secondary.bpm or 0.0)
        rate = primary_bpm / secondary_bpm if secondary_bpm > 0 else 1.0
        primary_path = self._transform_source(
            project.primary.audio_path,
            reverb=settings.get("primary_reverb_amount", 0.0), room_size=settings.get("reverb_size", 0.5),
        )
        secondary_path = self._transform_source(
            project.secondary.audio_path, tempo_rate=rate,
            high_pass=120 if settings.get("low_cut_secondary", True) else 0,
            reverb=settings.get("reverb_amount", 0.0), room_size=settings.get("reverb_size", 0.5),
        )
        primary_gain = float(settings.get("primary_gain_db", 0.0))
        secondary_gain = float(settings.get("secondary_gain_db", 0.0))
        if settings.get("match_loudness", True):
            primary_gain += self._normalization_gain(project.primary.dbfs, -18.0)
            secondary_gain += self._normalization_gain(project.secondary.dbfs, -18.0)
        primary: AudioNode = GainPanNode(FileSourceNode(primary_path), primary_gain)
        secondary_local: AudioNode = GainPanNode(FileSourceNode(secondary_path), secondary_gain)
        start = _ms_to_frames(settings.get("blend_start_ms", 0))
        duration = max(1, _ms_to_frames(settings.get("blend_duration_ms", 8000)))
        primary = AutomationNode(primary, ((0, 1.0), (start, 1.0), (start + duration, 0.0)))
        secondary_local = AutomationNode(secondary_local, ((0, 0.0), (duration, 1.0)))
        secondary = OffsetNode(secondary_local, start)
        if settings.get("duck_amount", 0.0) > 0:
            primary = self._duck(primary, secondary, settings["duck_amount"])
        return self._graph(MixNode((self._cached(primary), self._cached(secondary))))

    def _key_shift(self, vocal_path: str, instrumental_path: str) -> int:
        key = (str(vocal_path), str(instrumental_path))
        if key in self._analysis:
            return self._analysis[key]
        vocal, vocal_sr = audio_io.load_mono_float(vocal_path)
        instrumental, instrumental_sr = audio_io.load_mono_float(instrumental_path)
        vocal_pc, _ = analysis.estimate_key(vocal, vocal_sr)
        instrumental_pc, _ = analysis.estimate_key(instrumental, instrumental_sr)
        shift = analysis.semitone_shift_to_match(vocal_pc, instrumental_pc)
        self._analysis[key] = shift
        return shift

    def build_vocals(self, project: preprocessing.PreparedProject, settings: dict) -> AudioGraph:
        vocals_from_primary = settings.get("vocals_from", "secondary") == "primary"
        vocal_source = project.primary if vocals_from_primary else project.secondary
        instrumental_source = project.secondary if vocals_from_primary else project.primary
        if not vocal_source.vocals_path or not instrumental_source.instrumental_path:
            raise preprocessing.PreparationRequiredError("Prepared vocal assets are incomplete.")
        vocal_path = vocal_source.vocals_path
        instrumental_path = instrumental_source.instrumental_path
        tempo_rate = 1.0
        if settings.get("tempo_match", True) and vocal_source.bpm and instrumental_source.bpm:
            tempo_rate = float(instrumental_source.bpm) / float(vocal_source.bpm)
        semitones = self._key_shift(vocal_path, instrumental_path) if settings.get("key_match", True) else 0

        def vocal_transform(segment: AudioSegment) -> AudioSegment:
            output = effects._stretch(segment, tempo_rate) if abs(tempo_rate - 1.0) > 0.01 else segment
            if semitones:
                output = effects._shift_pitch(output, semitones)
            if settings.get("reverb_amount", 0.0) > 0:
                output = mastering.add_reverb(
                    output, room_size=float(settings.get("reverb_size", 0.5)),
                    wet_level=float(settings["reverb_amount"]),
                )
            return output

        vocal_path = self.assets.transform(
            vocal_path,
            {"kind": "vocal-v1", "rate": tempo_rate, "semitones": semitones,
             "reverb": settings.get("reverb_amount", 0.0), "room": settings.get("reverb_size", 0.5)},
            vocal_transform,
        )

        def instrumental_transform(segment: AudioSegment) -> AudioSegment:
            output = mastering.carve_for_vocal(segment) if settings.get("carve_for_vocal", True) else segment
            if settings.get("instrumental_reverb_amount", 0.0) > 0:
                output = mastering.add_reverb(
                    output, room_size=float(settings.get("reverb_size", 0.5)),
                    wet_level=float(settings["instrumental_reverb_amount"]),
                )
            return output

        instrumental_path = self.assets.transform(
            instrumental_path,
            {"kind": "instrumental-v1", "carve": settings.get("carve_for_vocal", True),
             "reverb": settings.get("instrumental_reverb_amount", 0.0),
             "room": settings.get("reverb_size", 0.5)},
            instrumental_transform,
        )
        vocal_gain = float(settings.get("vocal_gain_db", 0.0))
        instrumental_gain = float(settings.get("instrumental_gain_db", 0.0))
        if settings.get("match_loudness", True):
            vocal_dbfs = AudioSegment.from_file(vocal_path).dBFS
            instrumental_dbfs = AudioSegment.from_file(instrumental_path).dBFS
            vocal_gain += self._normalization_gain(vocal_dbfs, -16.0)
            instrumental_gain += self._normalization_gain(instrumental_dbfs, -18.0)
        vocal_local = GainPanNode(FileSourceNode(vocal_path), vocal_gain)
        vocal = OffsetNode(vocal_local, _ms_to_frames(settings.get("offset_ms", 0)))
        instrumental: AudioNode = GainPanNode(FileSourceNode(instrumental_path), instrumental_gain)
        if settings.get("duck_amount", 0.0) > 0:
            instrumental = self._duck(instrumental, vocal, settings["duck_amount"])
        return self._graph(MixNode((self._cached(instrumental), self._cached(vocal))))

    def _processed_clip_node(
        self, sources: Mapping[str, AudioNode], origins: Mapping[str, int], clip: dict
    ) -> Optional[AudioNode]:
        if not effects.has_processing(clip):
            return None
        duration_ms = max(0, int(clip["end_ms"]) - int(clip["start_ms"]))
        source_duration_ms = max(0, int(round(duration_ms * effects.clip_speed(clip))))
        local = dict(clip)
        if "source_start_ms" not in local:
            lane = local.get("source", "primary")
            local["source_start_ms"] = int(clip["start_ms"]) - int(round(origins.get(lane, 0) * 1000 / SAMPLE_RATE))
        # Pull the larger/smaller source window first. The offline effect cache
        # then time-stretches that window exactly once into the clip's fixed
        # destination slot.
        local.update({"start_ms": 0, "end_ms": source_duration_ms, "mode": "layer", "speed": 1.0})
        local.pop("effects", None)
        local.pop("pitch", None)
        local.pop("preserve_formants", None)
        raw = TimelineTrackNode(
            sources, origins, "muted", (local,), _ms_to_frames(source_duration_ms), 0
        )
        path = self.assets.materialize_node(
            raw,
            {"kind": "clip-processing-v1", "clip": clip},
            lambda segment: effects.apply_clip_processing(segment, clip, duration_ms),
        )
        return FileSourceNode(path)

    def build_stems(self, project: preprocessing.PreparedProject, settings: dict) -> AudioGraph:
        missing = [name for name in separation.STEM_NAMES if name not in project.primary.stems or name not in project.secondary.stems]
        if missing:
            raise preprocessing.PreparationRequiredError(f"Prepared stems are incomplete: {', '.join(missing)}")
        tempo_rate = 1.0
        if settings.get("tempo_match", True) and project.primary.bpm and project.secondary.bpm:
            tempo_rate = float(project.primary.bpm) / float(project.secondary.bpm)
        primary_gain = float(settings.get("primary_gain_db", 0.0))
        secondary_gain = float(settings.get("secondary_gain_db", 0.0))
        if settings.get("match_loudness", True):
            primary_gain += self._normalization_gain(project.primary.dbfs, -18.0)
            secondary_gain += self._normalization_gain(project.secondary.dbfs, -18.0)

        primary_nodes: dict[str, AudioNode] = {}
        secondary_nodes: dict[str, AudioNode] = {}
        for name in separation.STEM_NAMES:
            primary_path = self._transform_source(
                project.primary.stems[name], reverb=settings.get("primary_reverb_amount", 0.0),
                room_size=settings.get("reverb_size", 0.5),
            )
            secondary_path = self._transform_source(
                project.secondary.stems[name], tempo_rate=tempo_rate,
                reverb=settings.get("reverb_amount", 0.0), room_size=settings.get("reverb_size", 0.5),
            )
            primary_nodes[name] = GainPanNode(FileSourceNode(primary_path), primary_gain)
            secondary_nodes[name] = GainPanNode(FileSourceNode(secondary_path), secondary_gain)

        offset_frames = _ms_to_frames(settings.get("offset_ms", 0))
        stem_sources = settings.get("stem_sources") or {}
        trigger_sources = [
            OffsetNode(secondary_nodes[name], offset_frames)
            for name in separation.STEM_NAMES
            if stem_sources.get(name, "muted") in ("secondary", "both")
        ]
        if trigger_sources and settings.get("duck_amount", 0.0) > 0:
            trigger = MixNode(trigger_sources)
            primary_nodes = {
                name: self._duck(node, trigger, settings["duck_amount"])
                for name, node in primary_nodes.items()
            }

        tracks = [dict(track) for track in settings.get("timeline_tracks") or []]
        if not tracks:
            tracks = [
                {"id": name, "kind": "stem", "stem": name, "bed": stem_sources.get(name, "primary")}
                for name in separation.STEM_NAMES
            ]
        clips = [dict(clip) for clip in settings.get("stem_overrides") or []]
        imported: dict[str, AudioNode] = {}
        for track in tracks:
            if track.get("kind") != "audio" or not track.get("path"):
                continue
            canonical = self._canonical_source(track["path"])
            imported[track["id"]] = FileSourceNode(canonical)

        primary_end = max((node.duration_frames for node in primary_nodes.values()), default=0)
        secondary_local_end = max((node.duration_frames for node in secondary_nodes.values()), default=0)
        secondary_end = offset_frames + secondary_local_end
        total_frames = max(primary_end, secondary_end, _ms_to_frames(1000))
        for track in tracks:
            if track.get("kind") == "audio" and track.get("bed", "muted") != "muted":
                total_frames = max(total_frames, imported.get(track["id"], ArraySourceNode(_silence(0))).duration_frames)
        total_frames = max([total_frames] + [_ms_to_frames(clip["end_ms"]) for clip in clips])

        soloed = any(track.get("solo") for track in tracks)
        claimed_stems: set[str] = set()
        track_nodes: list[AudioNode] = []
        crossfade_frames = _ms_to_frames(settings.get("override_crossfade_ms", 900))
        for track in tracks:
            stem = track.get("stem")
            first_for_stem = stem is not None and stem not in claimed_stems
            if stem is not None:
                claimed_stems.add(stem)
            if track.get("mute") or (soloed and not track.get("solo")):
                continue
            if track.get("kind") == "audio":
                source = imported.get(track.get("id"))
                if source is None:
                    continue
                sources = {"import": source}
                origins = {"import": 0}
            elif stem in primary_nodes:
                sources = {"primary": primary_nodes[stem], "secondary": secondary_nodes[stem]}
                origins = {"primary": 0, "secondary": offset_frames}
            else:
                continue
            track_clips = [
                clip for clip in clips
                if clip.get("track") == track.get("id")
                or (not clip.get("track") and first_for_stem and clip.get("stem") == stem)
            ]
            if track.get("kind") == "stem" and settings.get("auto_fallback", True):
                bed = track.get("bed", "primary")
                primary_ms = int(round(primary_end * 1000 / SAMPLE_RATE))
                secondary_ms = int(round(secondary_end * 1000 / SAMPLE_RATE))
                track_clips = self._fallback_clips(stem, bed, primary_ms, secondary_ms) + track_clips
            processed = {
                index: node for index, clip in enumerate(track_clips)
                if (node := self._processed_clip_node(sources, origins, clip)) is not None
            }
            raw_track: AudioNode = TimelineTrackNode(
                sources, origins, track.get("bed", "primary"), track_clips,
                total_frames, crossfade_frames, processed, fingerprint_extra=track.get("id"),
            )
            if track.get("effects"):
                path = self.assets.materialize_node(
                    raw_track,
                    {"kind": "track-effects-v1", "effects": track["effects"]},
                    lambda segment, chain=track["effects"]: effects.apply_track_effects(
                        segment, chain, int(round(total_frames * 1000 / SAMPLE_RATE))
                    ),
                )
                raw_track = FileSourceNode(path)
            raw_track = GainPanNode(
                raw_track, float(track.get("gain_db", 0.0) or 0.0), float(track.get("pan", 0.0) or 0.0)
            )
            track_nodes.append(self._cached(raw_track))
        return self._graph(MixNode(track_nodes, duration_frames=total_frames))

    @staticmethod
    def _fallback_clips(stem: str, bed: str, primary_end_ms: int, secondary_end_ms: int) -> list[dict]:
        if bed == "secondary" and secondary_end_ms < primary_end_ms:
            return [{"stem": stem, "start_ms": secondary_end_ms, "end_ms": primary_end_ms, "source": "primary"}]
        if bed == "primary" and primary_end_ms < secondary_end_ms:
            return [{"stem": stem, "start_ms": primary_end_ms, "end_ms": secondary_end_ms, "source": "secondary"}]
        return []


@dataclass(frozen=True)
class RenderReport:
    output_path: str
    fingerprint: str
    reused: bool
    rendered_blocks: int


class SmartRenderer:
    """Atomic graph exporter that skips byte-identical project snapshots."""

    def __init__(self, manifest_dir: Path = RENDER_MANIFEST_DIR):
        self.manifest_dir = manifest_dir

    def _manifest_path(self, output_path: str) -> Path:
        return self.manifest_dir / f"{_stable_hash(str(Path(output_path).resolve()))[:24]}.json"

    def render(
        self,
        graph: AudioGraph,
        output_path: str,
        progress_callback: Optional[Callable[[str], None]] = None,
        bitrate: str = "320k",
    ) -> RenderReport:
        target = Path(output_path)
        manifest_path = self._manifest_path(output_path)
        if target.exists() and manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                target_stat = target.stat()
                if (
                    manifest.get("fingerprint") == graph.fingerprint
                    and manifest.get("output") == str(target.resolve())
                    and manifest.get("size") == target_stat.st_size
                    and manifest.get("mtime_ns") == target_stat.st_mtime_ns
                ):
                    if progress_callback:
                        progress_callback("Smart render: project is unchanged; reusing existing export.")
                    return RenderReport(str(target), graph.fingerprint, True, 0)
            except (OSError, ValueError, json.JSONDecodeError):
                pass

        target.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            prefix=f".{target.stem}-", suffix=target.suffix or ".wav", dir=target.parent, delete=False
        )
        temporary = Path(handle.name)
        handle.close()
        blocks = 0
        try:
            if target.suffix.lower() == ".mp3":
                command = [
                    "ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(graph.sample_rate),
                    "-ac", str(graph.channels), "-i", "pipe:0", "-codec:a", "libmp3lame", "-b:a", bitrate,
                    str(temporary),
                ]
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                assert process.stdin is not None
                for start in range(0, graph.duration_frames, graph.block_size):
                    count = min(graph.block_size, graph.duration_frames - start)
                    process.stdin.write(np.asarray(graph.render_block(start, count), dtype="<f4").tobytes())
                    blocks += 1
                    if progress_callback and blocks % 64 == 0:
                        progress_callback(f"Smart rendering... {start * 100 // max(1, graph.duration_frames)}%")
                process.stdin.close()
                stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
                return_code = process.wait()
                if return_code:
                    raise RuntimeError(f"ffmpeg export failed: {stderr.strip()}")
            else:
                with wave.open(str(temporary), "wb") as output:
                    output.setnchannels(graph.channels)
                    output.setsampwidth(2)
                    output.setframerate(graph.sample_rate)
                    for start in range(0, graph.duration_frames, graph.block_size):
                        count = min(graph.block_size, graph.duration_frames - start)
                        samples = np.clip(graph.render_block(start, count), -1.0, 1.0)
                        output.writeframes((samples * 32767.0).astype("<i2").tobytes())
                        blocks += 1
            os.replace(temporary, target)
            target_stat = target.stat()
            self.manifest_dir.mkdir(parents=True, exist_ok=True)
            manifest_temp = manifest_path.with_suffix(".tmp")
            manifest_temp.write_text(
                json.dumps(
                    {
                        "fingerprint": graph.fingerprint,
                        "output": str(target.resolve()),
                        "size": target_stat.st_size,
                        "mtime_ns": target_stat.st_mtime_ns,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            os.replace(manifest_temp, manifest_path)
            return RenderReport(str(target), graph.fingerprint, False, blocks)
        finally:
            if temporary.exists():
                temporary.unlink(missing_ok=True)


class RealtimeMixer:
    """Callback-driven transport; graph snapshots can be swapped while playing."""

    def __init__(
        self,
        on_position: Optional[Callable[[int], None]] = None,
        on_stopped: Optional[Callable[[], None]] = None,
    ):
        self.on_position = on_position
        self.on_stopped = on_stopped
        self._graph: Optional[AudioGraph] = None
        self._position = 0
        self._stream = None
        self._playing = False
        self._lock = threading.RLock()

    @property
    def is_playing(self) -> bool:
        with self._lock:
            return self._playing

    @property
    def position_ms(self) -> int:
        with self._lock:
            graph = self._graph
            return int(round(self._position * 1000.0 / (graph.sample_rate if graph else SAMPLE_RATE)))

    def set_graph(self, graph: AudioGraph, preserve_position: bool = True) -> None:
        with self._lock:
            old_position = self._position if preserve_position else 0
            self._graph = graph
            self._position = min(old_position, graph.duration_frames)

    def seek(self, milliseconds: float) -> None:
        with self._lock:
            if self._graph is None:
                self._position = 0
            else:
                self._position = max(0, min(self._graph.duration_frames, _ms_to_frames(milliseconds, self._graph.sample_rate)))

    def play(self, start_ms: Optional[float] = None) -> None:
        import sounddevice as sd

        old_stream = None
        with self._lock:
            if self._graph is None:
                raise RuntimeError("No audio graph is loaded.")
            if start_ms is not None:
                self.seek(start_ms)
            if self._playing:
                return
            graph = self._graph
            old_stream = self._stream
            self._stream = None
        if old_stream is not None:
            old_stream.close()
        with self._lock:
            self._playing = True

        def callback(outdata, frames, _time_info, status):
            del status
            with self._lock:
                active = self._graph
                start = self._position
                playing = self._playing
            if not playing or active is None:
                outdata.fill(0)
                raise sd.CallbackStop
            block = active.render_block(start, frames)
            outdata[:] = block
            with self._lock:
                self._position += frames
                finished = self._position >= active.duration_frames
                if finished:
                    self._playing = False
                position_ms = int(round(self._position * 1000.0 / active.sample_rate))
            if self.on_position:
                self.on_position(position_ms)
            if finished:
                if self.on_stopped:
                    self.on_stopped()
                raise sd.CallbackStop

        self._stream = sd.OutputStream(
            samplerate=graph.sample_rate, channels=graph.channels, dtype="float32",
            blocksize=graph.block_size, callback=callback, finished_callback=self._stream_finished,
        )
        self._stream.start()

    def _stream_finished(self) -> None:
        with self._lock:
            self._playing = False

    def pause(self) -> None:
        with self._lock:
            self._playing = False
            stream = self._stream
            self._stream = None
        if stream is not None:
            stream.stop()
            stream.close()

    def stop(self) -> None:
        self.pause()
        with self._lock:
            self._position = 0
        if self.on_stopped:
            self.on_stopped()

    def close(self) -> None:
        self.pause()
        with self._lock:
            graph = self._graph
            self._graph = None
        if graph is not None:
            graph.close()
