"""Audio load/export and numpy conversion helpers.

pydub (backed by ffmpeg) handles mp3 <-> AudioSegment. librosa/demucs work in
float32 numpy, so we convert between the two representations here.
"""
from pathlib import Path

import numpy as np
from pydub import AudioSegment


def load_mp3(path: str) -> AudioSegment:
    return AudioSegment.from_file(path, format="mp3")


def export_mp3(segment: AudioSegment, path: str, bitrate: str = "320k") -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    segment.export(path, format="mp3", bitrate=bitrate)


def load_mono_float(path: str, sr: int | None = None) -> tuple[np.ndarray, int]:
    """Load an audio file (any format ffmpeg understands) as mono float32 in [-1, 1]."""
    import librosa

    y, sr = librosa.load(path, sr=sr, mono=True)
    return y, sr


def segment_to_float_mono(segment: AudioSegment) -> np.ndarray:
    """Convert a single-channel AudioSegment to float32 samples in [-1, 1]."""
    samples = np.array(segment.get_array_of_samples()).astype(np.float32)
    max_val = float(1 << (8 * segment.sample_width - 1))
    return samples / max_val


def float_array_to_segment(samples: np.ndarray, sample_rate: int, sample_width: int = 2) -> AudioSegment:
    """Convert float32 samples in [-1, 1] back to a pydub AudioSegment.

    samples: shape (n,) for mono, or (channels, n) for multichannel.
    """
    if samples.ndim == 1:
        channels = 1
        interleaved = samples
    else:
        channels = samples.shape[0]
        interleaved = samples.T.reshape(-1)

    interleaved = np.clip(interleaved, -1.0, 1.0)
    max_val = float((1 << (8 * sample_width - 1)) - 1)
    int_samples = (interleaved * max_val).astype(f"<i{sample_width}")

    return AudioSegment(
        int_samples.tobytes(),
        frame_rate=sample_rate,
        sample_width=sample_width,
        channels=channels,
    )
