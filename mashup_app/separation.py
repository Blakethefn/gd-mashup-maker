"""Vocal/instrumental separation via Demucs, with on-disk caching.

torch/demucs are only imported inside the functions below (lazy import) so
that modes 1 and 2 never pay the cost of loading them.
"""
import hashlib
import shutil
from pathlib import Path
from typing import Callable, Optional

STEMS_DIR = Path(__file__).resolve().parent.parent / "output" / ".stems"
STEMS4_DIR = Path(__file__).resolve().parent.parent / "output" / ".stems4"
MODEL_NAME = "htdemucs"
SHORT_NAME = "src"  # fixed short stand-in name so long source filenames never end up in the path
STEM_NAMES = ("vocals", "drums", "bass", "other")


def _cache_key(path: str) -> str:
    p = Path(path).resolve()
    stat = p.stat()
    return hashlib.sha1(f"{p}:{stat.st_size}:{stat.st_mtime}".encode()).hexdigest()[:16]


def separate_vocals(path: str, progress_callback: Optional[Callable[[str], None]] = None) -> tuple[str, str]:
    """Split an audio file into (vocals_path, instrumental_path) wav files.

    Results are cached under output/.stems/ keyed by the source file's path,
    size and mtime, so re-rendering with the same source doesn't re-run
    Demucs.
    """
    key = _cache_key(path)
    out_dir = STEMS_DIR / key
    vocals_path = out_dir / MODEL_NAME / SHORT_NAME / "vocals.wav"
    other_path = out_dir / MODEL_NAME / SHORT_NAME / "no_vocals.wav"

    if vocals_path.exists() and other_path.exists():
        return str(vocals_path), str(other_path)

    if progress_callback:
        progress_callback(f"Separating vocals/instrumental for {Path(path).name} "
                           f"(first run downloads the Demucs model; this can take a few minutes)...")

    out_dir.mkdir(parents=True, exist_ok=True)

    # Demucs names its own output subfolder after the input file's stem, so a
    # long source filename (e.g. a full song title) combined with our cache
    # path can exceed Windows' 260-char MAX_PATH and fail with WinError 3.
    # Feed it a short, fixed-name copy instead.
    short_src = out_dir / f"{SHORT_NAME}{Path(path).suffix}"
    shutil.copyfile(path, short_src)

    from demucs.separate import main as demucs_main

    try:
        demucs_main([
            "--two-stems", "vocals",
            "-n", MODEL_NAME,
            "-o", str(out_dir),
            str(short_src),
        ])
    except SystemExit as exc:
        raise RuntimeError(f"Demucs failed to process {path}: {exc}") from exc
    finally:
        short_src.unlink(missing_ok=True)

    if not vocals_path.exists() or not other_path.exists():
        raise RuntimeError(f"Demucs did not produce the expected output under {out_dir}")

    return str(vocals_path), str(other_path)


def separate_stems(path: str, progress_callback: Optional[Callable[[str], None]] = None) -> dict:
    """Split an audio file into all four Demucs stems: vocals, drums, bass, other.

    Cached separately from separate_vocals() (different Demucs invocation,
    different output files) under output/.stems4/, keyed the same way.
    """
    key = _cache_key(path)
    out_dir = STEMS4_DIR / key
    stem_paths = {name: out_dir / MODEL_NAME / SHORT_NAME / f"{name}.wav" for name in STEM_NAMES}

    if all(p.exists() for p in stem_paths.values()):
        return {name: str(p) for name, p in stem_paths.items()}

    if progress_callback:
        progress_callback(f"Separating {Path(path).name} into vocals/drums/bass/other "
                           f"(first run downloads the Demucs model; this can take a few minutes)...")

    out_dir.mkdir(parents=True, exist_ok=True)

    short_src = out_dir / f"{SHORT_NAME}{Path(path).suffix}"
    shutil.copyfile(path, short_src)

    from demucs.separate import main as demucs_main

    try:
        demucs_main([
            "-n", MODEL_NAME,
            "-o", str(out_dir),
            str(short_src),
        ])
    except SystemExit as exc:
        raise RuntimeError(f"Demucs failed to process {path}: {exc}") from exc
    finally:
        short_src.unlink(missing_ok=True)

    missing = [name for name, p in stem_paths.items() if not p.exists()]
    if missing:
        raise RuntimeError(f"Demucs did not produce expected stems {missing} under {out_dir}")

    return {name: str(p) for name, p in stem_paths.items()}
