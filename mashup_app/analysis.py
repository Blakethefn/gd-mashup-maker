"""BPM detection and key estimation, used by the beat-synced and vocals modes."""
import numpy as np

from . import audio_io

_PITCH_CLASSES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Krumhansl-Schmuckler key profiles, used to correlate against a track's chroma
# to guess its musical key.
_MAJOR_PROFILE = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR_PROFILE = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


def detect_bpm(path: str) -> float:
    import librosa

    y, sr = audio_io.load_mono_float(path)
    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
    return float(np.atleast_1d(tempo)[0])


def estimate_key(y: np.ndarray, sr: int) -> tuple[str, str]:
    """Return (pitch_class, mode), e.g. ('C', 'major')."""
    import librosa

    chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    chroma_mean = chroma.mean(axis=1)

    best_score = -np.inf
    best = ("C", "major")
    for i in range(12):
        major_corr = np.corrcoef(np.roll(_MAJOR_PROFILE, i), chroma_mean)[0, 1]
        minor_corr = np.corrcoef(np.roll(_MINOR_PROFILE, i), chroma_mean)[0, 1]
        if major_corr > best_score:
            best_score = major_corr
            best = (_PITCH_CLASSES[i], "major")
        if minor_corr > best_score:
            best_score = minor_corr
            best = (_PITCH_CLASSES[i], "minor")
    return best


def semitone_shift_to_match(source_pc: str, target_pc: str) -> int:
    """Semitones to shift source's pitch class onto target's, via the shortest direction."""
    si = _PITCH_CLASSES.index(source_pc)
    ti = _PITCH_CLASSES.index(target_pc)
    diff = (ti - si) % 12
    if diff > 6:
        diff -= 12
    return diff
