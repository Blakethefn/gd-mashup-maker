"""Vocal/instrumental separation via Demucs, with on-disk caching.

torch/demucs are only imported inside the functions below (lazy import) so
that modes 1 and 2 never pay the cost of loading them.
"""
import hashlib
from pathlib import Path
from typing import Callable, Optional

STEMS_DIR = Path(__file__).resolve().parent.parent / "output" / ".stems"
MODEL_NAME = "htdemucs"


def _cache_key(path: str) -> str:
    p = Path(path).resolve()
    stat = p.stat()
    digest = hashlib.sha1(f"{p}:{stat.st_size}:{stat.st_mtime}".encode()).hexdigest()[:16]
    return f"{p.stem}_{digest}"


def separate_vocals(path: str, progress_callback: Optional[Callable[[str], None]] = None) -> tuple[str, str]:
    """Split an audio file into (vocals_path, instrumental_path) wav files.

    Results are cached under output/.stems/ keyed by the source file's path,
    size and mtime, so re-rendering with the same source doesn't re-run
    Demucs.
    """
    key = _cache_key(path)
    out_dir = STEMS_DIR / key
    stem_name = Path(path).stem
    vocals_path = out_dir / MODEL_NAME / stem_name / "vocals.wav"
    other_path = out_dir / MODEL_NAME / stem_name / "no_vocals.wav"

    if vocals_path.exists() and other_path.exists():
        return str(vocals_path), str(other_path)

    if progress_callback:
        progress_callback(f"Separating vocals/instrumental for {Path(path).name} "
                           f"(first run downloads the Demucs model; this can take a few minutes)...")

    out_dir.mkdir(parents=True, exist_ok=True)

    from demucs.separate import main as demucs_main

    try:
        demucs_main([
            "--two-stems", "vocals",
            "-n", MODEL_NAME,
            "-o", str(out_dir),
            str(path),
        ])
    except SystemExit as exc:
        raise RuntimeError(f"Demucs failed to process {path}: {exc}") from exc

    if not vocals_path.exists() or not other_path.exists():
        raise RuntimeError(f"Demucs did not produce the expected output under {out_dir}")

    return str(vocals_path), str(other_path)
