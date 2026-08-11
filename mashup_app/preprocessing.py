"""Explicit source preparation for the real-time audio graph.

Slow, source-dependent work belongs here.  Playback and export consume the
resulting immutable WAV assets and never invoke Demucs themselves.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from pydub import AudioSegment

from . import analysis, separation

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PREPARED_DIR = PROJECT_ROOT / "output" / ".prepared"
MEDIA_DIR = PREPARED_DIR / "media"
MANIFEST_DIR = PREPARED_DIR / "projects"

MODE_SIMPLE = "simple"
MODE_BEAT = "beat"
MODE_VOCALS = "vocals"
MODE_STEMS = "stems"


class PreparationRequiredError(RuntimeError):
    """The requested graph has no valid prepared-asset manifest."""


@dataclass(frozen=True)
class PreparedSource:
    original_path: str
    source_key: str
    audio_path: str
    duration_ms: int
    dbfs: float
    bpm: Optional[float] = None
    stems: dict[str, str] = field(default_factory=dict)
    vocals_path: Optional[str] = None
    instrumental_path: Optional[str] = None


@dataclass(frozen=True)
class PreparedProject:
    version: int
    mode: str
    primary: PreparedSource
    secondary: PreparedSource
    manifest_path: str

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "version": self.version,
                "mode": self.mode,
                "primary": asdict(self.primary),
                "secondary": asdict(self.secondary),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_locks_guard = threading.Lock()
_source_locks: dict[str, threading.Lock] = {}


def _source_lock(key: str) -> threading.Lock:
    with _locks_guard:
        return _source_locks.setdefault(key, threading.Lock())


def _project_key(primary_path: str, secondary_path: str, mode: str) -> str:
    identity = ":".join(
        (separation.source_cache_key(primary_path), separation.source_cache_key(secondary_path), mode)
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _manifest_path(primary_path: str, secondary_path: str, mode: str) -> Path:
    return MANIFEST_DIR / f"{_project_key(primary_path, secondary_path, mode)}.json"


def _canonical_audio(path: str, progress_callback: Optional[Callable[[str], None]]) -> str:
    """Decode any supported input once into graph-friendly 44.1 kHz stereo WAV."""
    key = separation.source_cache_key(path)
    output_path = MEDIA_DIR / f"{key}.wav"
    if output_path.exists():
        return str(output_path)
    with _source_lock(key):
        if output_path.exists():
            return str(output_path)
        if progress_callback:
            progress_callback(f"Preparing audio stream for {Path(path).name}...")
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        temporary = output_path.with_suffix(".tmp.wav")
        segment = AudioSegment.from_file(path).set_frame_rate(44100).set_channels(2).set_sample_width(2)
        segment.export(temporary, format="wav")
        os.replace(temporary, output_path)
    return str(output_path)


def _prepare_source(
    path: str,
    mode: str,
    progress_callback: Optional[Callable[[str], None]],
) -> PreparedSource:
    audio_path = _canonical_audio(path, progress_callback)
    segment = AudioSegment.from_file(audio_path)
    dbfs = float(segment.dBFS)
    bpm = None
    stems: dict[str, str] = {}
    vocals_path = None
    instrumental_path = None

    if mode == MODE_STEMS:
        # This module is intentionally the only architecture layer that may
        # start separation. Graph construction and export use cache-only APIs.
        stems = separation.separate_stems(path, progress_callback)
        bpm = float(analysis.detect_bpm(audio_path))
    elif mode == MODE_VOCALS:
        vocals_path, instrumental_path = separation.separate_vocals(path, progress_callback)
        bpm = float(analysis.detect_bpm(audio_path))
    elif mode == MODE_BEAT:
        bpm = float(analysis.detect_bpm(audio_path))

    return PreparedSource(
        original_path=str(Path(path).resolve()),
        source_key=separation.source_cache_key(path),
        audio_path=audio_path,
        duration_ms=len(segment),
        dbfs=dbfs,
        bpm=bpm,
        stems=stems,
        vocals_path=vocals_path,
        instrumental_path=instrumental_path,
    )


def prepare_project(
    primary_path: str,
    secondary_path: str,
    mode: str,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> PreparedProject:
    """Prepare and persist every source asset required by one project mode."""
    if mode not in (MODE_SIMPLE, MODE_BEAT, MODE_VOCALS, MODE_STEMS):
        raise ValueError(f"Unknown preparation mode: {mode}")
    if progress_callback:
        progress_callback("Pre-processing source audio...")
    primary = _prepare_source(primary_path, mode, progress_callback)
    secondary = _prepare_source(secondary_path, mode, progress_callback)
    manifest_path = _manifest_path(primary_path, secondary_path, mode)
    project = PreparedProject(1, mode, primary, secondary, str(manifest_path))

    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    temporary = manifest_path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {
                "version": project.version,
                "mode": project.mode,
                "primary": asdict(project.primary),
                "secondary": asdict(project.secondary),
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    os.replace(temporary, manifest_path)
    if progress_callback:
        progress_callback("Audio assets are prepared.")
    return project


def load_prepared_project(primary_path: str, secondary_path: str, mode: str) -> PreparedProject:
    """Load a valid manifest without performing analysis or separation."""
    manifest_path = _manifest_path(primary_path, secondary_path, mode)
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        primary = PreparedSource(**data["primary"])
        secondary = PreparedSource(**data["secondary"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PreparationRequiredError("Run Pre-process Audio before playback or export.") from exc

    expected = (
        separation.source_cache_key(primary_path),
        separation.source_cache_key(secondary_path),
    )
    if data.get("version") != 1 or data.get("mode") != mode:
        raise PreparationRequiredError("Prepared assets are from an incompatible project version.")
    if (primary.source_key, secondary.source_key) != expected:
        raise PreparationRequiredError("The source files changed; pre-process them again.")

    required_paths = [primary.audio_path, secondary.audio_path]
    if mode == MODE_STEMS:
        required_paths.extend(primary.stems.values())
        required_paths.extend(secondary.stems.values())
    elif mode == MODE_VOCALS:
        required_paths.extend(
            path for path in (
                primary.vocals_path,
                primary.instrumental_path,
                secondary.vocals_path,
                secondary.instrumental_path,
            ) if path
        )
    if not required_paths or any(not Path(path).exists() for path in required_paths):
        raise PreparationRequiredError("One or more prepared assets are missing; pre-process again.")
    return PreparedProject(data["version"], mode, primary, secondary, str(manifest_path))


def is_project_prepared(primary_path: str, secondary_path: str, mode: str) -> bool:
    try:
        load_prepared_project(primary_path, secondary_path, mode)
        return True
    except (OSError, PreparationRequiredError):
        return False
