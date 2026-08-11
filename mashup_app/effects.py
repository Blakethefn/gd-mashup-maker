"""Per-clip effects, in the spirit of Audacity's Effect menu.

A clip carries its processing in three fields, all optional:

    "speed":   playback rate, 1.0 = unchanged (pitch preserved)
    "pitch":   semitones to shift, 0 = unchanged
    "effects": an ordered list of {"type": ..., <params>} dicts

``apply_clip_processing`` renders all three onto one clip's audio and
returns a segment of exactly the requested length, so a processed clip
still occupies the timeline slot the editor drew for it.

Everything here works on pydub AudioSegments and the numpy helpers in
mastering.py; nothing is taken from any other project's source.
"""
import math
from typing import Optional

from pydub import AudioSegment

from . import mastering

# Effect catalogue. Each entry lists the parameters the UI should ask for as
# (key, label, minimum, maximum, step, default), so the dialog and the
# renderer never drift apart.
EFFECT_SPECS = {
    "amplify": {
        "label": "Amplify...",
        "help": "Raise or lower the clip's level by a fixed amount.",
        "params": [("gain_db", "Amplification (dB)", -36.0, 24.0, 0.5, 3.0)],
    },
    "normalize": {
        "label": "Normalize...",
        "help": "Bring the clip to a consistent loudness.",
        "params": [("target_dbfs", "Normalize to (dBFS)", -40.0, -1.0, 0.5, -18.0)],
    },
    "fade_in": {
        "label": "Fade In...",
        "help": "Fade the clip up from silence.",
        "params": [("duration_ms", "Fade length (ms)", 10.0, 30000.0, 50.0, 1000.0)],
    },
    "fade_out": {
        "label": "Fade Out...",
        "help": "Fade the clip down to silence.",
        "params": [("duration_ms", "Fade length (ms)", 10.0, 30000.0, 50.0, 1000.0)],
    },
    "reverse": {
        "label": "Reverse",
        "help": "Play the clip backwards.",
        "params": [],
    },
    "echo": {
        "label": "Echo...",
        "help": "Repeat the clip at a fixed delay, each repeat quieter.",
        "params": [
            ("delay_ms", "Delay (ms)", 20.0, 4000.0, 10.0, 350.0),
            ("decay", "Decay per repeat", 0.05, 0.95, 0.05, 0.5),
            ("repeats", "Repeats", 1.0, 12.0, 1.0, 3.0),
        ],
    },
    "reverb": {
        "label": "Reverb...",
        "help": "Put the clip in a room.",
        "params": [
            ("room_size", "Room size (0-1)", 0.0, 1.0, 0.05, 0.5),
            ("wet_level", "Wet level (0-1)", 0.0, 1.0, 0.05, 0.3),
            ("damping", "Damping (0-1)", 0.0, 1.0, 0.05, 0.5),
        ],
    },
    "bass_treble": {
        "label": "Bass and Treble...",
        "help": "Shelve the low and high ends up or down.",
        "params": [
            ("bass_db", "Bass (dB)", -24.0, 24.0, 0.5, 0.0),
            ("treble_db", "Treble (dB)", -24.0, 24.0, 0.5, 0.0),
        ],
    },
    "filter_curve": {
        "label": "High-pass / Low-pass...",
        "help": "Roll off everything below and above the given corners.",
        "params": [
            ("high_pass_hz", "High-pass below (Hz, 0 = off)", 0.0, 20000.0, 10.0, 0.0),
            ("low_pass_hz", "Low-pass above (Hz, 0 = off)", 0.0, 20000.0, 100.0, 0.0),
        ],
    },
    "compressor": {
        "label": "Compressor...",
        "help": "Even out the clip's dynamics.",
        "params": [
            ("threshold_db", "Threshold (dBFS)", -60.0, 0.0, 1.0, -20.0),
            ("ratio", "Ratio (n:1)", 1.0, 20.0, 0.5, 4.0),
            ("attack_ms", "Attack (ms)", 0.1, 200.0, 1.0, 5.0),
            ("release_ms", "Release (ms)", 10.0, 2000.0, 10.0, 100.0),
        ],
    },
    "eq_band": {
        "label": "Single-band EQ...",
        "help": "Boost or cut one frequency band.",
        "params": [
            ("freq", "Frequency (Hz)", 30.0, 18000.0, 10.0, 1000.0),
            ("gain_db", "Gain (dB)", -24.0, 24.0, 0.5, -4.0),
            ("q", "Q (bandwidth)", 0.2, 10.0, 0.1, 1.0),
        ],
    },
    "change_pitch": {
        "label": "Change Pitch...",
        "help": "Shift the clip up or down without changing how long it plays.",
        # The dialog offers notes, frequencies and percent as well, but they
        # are all just ways of naming this one number.
        "params": [
            ("semitones", "Semitones (half-steps)", -24.0, 24.0, 1.0, 0.0),
            ("high_quality", "High quality (slow): 1 = on", 0.0, 1.0, 1.0, 0.0),
            ("preserve_formants", "Optimize for voice: 1 = on", 0.0, 1.0, 1.0, 0.0),
        ],
        "summary": "{semitones:+g} st",
    },
}

# ---- pitch arithmetic ----------------------------------------------------
# One pitch change can be named four ways - semitones, percent, a frequency
# ratio, or a pair of notes. These convert between them so a dialog can offer
# all four and keep them in step.

NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
MIN_OCTAVE, MAX_OCTAVE = 0, 9


def semitones_to_ratio(semitones: float) -> float:
    return 2.0 ** (float(semitones) / 12.0)


def semitones_to_percent(semitones: float) -> float:
    return (semitones_to_ratio(semitones) - 1.0) * 100.0


def percent_to_semitones(percent: float) -> float:
    ratio = 1.0 + float(percent) / 100.0
    if ratio <= 0:
        return 0.0
    return 12.0 * math.log2(ratio)


def ratio_to_semitones(from_hz: float, to_hz: float) -> float:
    if from_hz <= 0 or to_hz <= 0:
        return 0.0
    return 12.0 * math.log2(to_hz / from_hz)


def note_to_midi(note_index: int, octave: int) -> int:
    """MIDI number for a note, with middle C (C4) at 60."""
    return (int(octave) + 1) * 12 + int(note_index)


def midi_to_frequency(midi: float) -> float:
    return 440.0 * (2.0 ** ((float(midi) - 69.0) / 12.0))


def frequency_to_midi(hz: float) -> float:
    if hz <= 0:
        return 69.0
    return 69.0 + 12.0 * math.log2(hz / 440.0)


def midi_to_note(midi: float) -> tuple:
    """(note_index, octave) for the nearest named note."""
    nearest = int(round(midi))
    return nearest % 12, nearest // 12 - 1

# Effects offered as a menu, in Audacity's rough ordering.
EFFECT_ORDER = (
    "amplify", "normalize", "compressor",
    None,
    "fade_in", "fade_out", "reverse",
    None,
    "change_pitch",
    None,
    "bass_treble", "filter_curve", "eq_band",
    None,
    "echo", "reverb",
)


def default_params(effect_type: str) -> dict:
    spec = EFFECT_SPECS.get(effect_type, {})
    return {key: default for key, _label, _lo, _hi, _step, default in spec.get("params", [])}


def describe(effect: dict) -> str:
    """One-line summary for the clip list and tooltips."""
    spec = EFFECT_SPECS.get(effect.get("type"), {})
    label = spec.get("label", effect.get("type", "?")).rstrip(".")
    params = spec.get("params", [])
    if not params:
        return label
    summary = spec.get("summary")
    if summary:
        values = {**default_params(effect.get("type")),
                  **{k: v for k, v in effect.items() if k != "type"}}
        try:
            return f"{label} ({summary.format(**values)})"
        except (KeyError, ValueError):
            pass
    shown = ", ".join(
        f"{key.replace('_', ' ')} {float(effect.get(key, default)):g}"
        for key, _label, _lo, _hi, _step, default in params
    )
    return f"{label} ({shown})"


def has_processing(clip: dict) -> bool:
    return bool(
        clip.get("effects")
        or abs(float(clip.get("speed", 1.0) or 1.0) - 1.0) > 1e-6
        or abs(float(clip.get("pitch", 0.0) or 0.0)) > 1e-6
    )


def clip_speed(clip: Optional[dict]) -> float:
    """How much source audio one millisecond of timeline consumes."""
    if not clip:
        return 1.0
    try:
        speed = float(clip.get("speed", 1.0) or 1.0)
    except (TypeError, ValueError):
        return 1.0
    return max(0.1, min(10.0, speed))


def _fit(segment: AudioSegment, length_ms: int) -> AudioSegment:
    if length_ms <= 0:
        return segment[:0]
    if len(segment) < length_ms:
        segment = segment + AudioSegment.silent(
            duration=length_ms - len(segment), frame_rate=segment.frame_rate
        ).set_channels(segment.channels).set_sample_width(segment.sample_width)
    return segment[:length_ms]


def _stretch(segment: AudioSegment, rate: float) -> AudioSegment:
    """Time-stretch by rate without moving the pitch (rate > 1 = shorter)."""
    if abs(rate - 1.0) <= 1e-6 or len(segment) == 0:
        return segment
    import librosa

    def stretch_channel(y, _sr):
        return librosa.effects.time_stretch(y, rate=rate)

    return mastering.apply_stereo(segment, stretch_channel)


def _cepstral_envelope(magnitude, order: int = 40):
    """Smooth spectral envelope per frame - the formants, with the harmonics
    liftered away. Low quefrencies describe the resonating body; high ones
    describe the pitch that is exciting it."""
    import numpy as np

    log_magnitude = np.log(magnitude + 1e-10)
    cepstrum = np.fft.irfft(log_magnitude, axis=0)
    cepstrum[order:-order] = 0.0
    return np.exp(np.real(np.fft.rfft(cepstrum, axis=0)))


def _restore_formants(original, shifted, sr: int):
    """Put the original's formants back on a pitch-shifted signal.

    Shifting pitch naively drags the vocal tract's resonances along with it,
    which is what makes a transposed voice sound like a chipmunk or an ogre.
    Dividing out the shifted envelope and re-imposing the original one moves
    the pitch while leaving the voice sounding like the same person.
    """
    import librosa
    import numpy as np

    n_fft, hop = 2048, 512
    if len(shifted) < n_fft:
        return shifted
    spectrum_shifted = librosa.stft(shifted, n_fft=n_fft, hop_length=hop)
    spectrum_original = librosa.stft(original[: len(shifted)], n_fft=n_fft, hop_length=hop)
    frames = min(spectrum_shifted.shape[1], spectrum_original.shape[1])
    spectrum_shifted = spectrum_shifted[:, :frames]
    envelope_original = _cepstral_envelope(np.abs(spectrum_original[:, :frames]))
    envelope_shifted = _cepstral_envelope(np.abs(spectrum_shifted))
    gain = np.clip(envelope_original / (envelope_shifted + 1e-10), 0.05, 20.0)
    corrected = librosa.istft(spectrum_shifted * gain, hop_length=hop, length=len(shifted))
    return corrected.astype(np.float32)


def _shift_pitch(
    segment: AudioSegment, semitones: float, high_quality: bool = False,
    preserve_formants: bool = False,
) -> AudioSegment:
    if abs(semitones) <= 1e-6 or len(segment) == 0:
        return segment
    import librosa

    def shift_channel(y, sr):
        # res_type is only honoured by newer librosa; fall back to its default.
        try:
            shifted = librosa.effects.pitch_shift(
                y, sr=sr, n_steps=semitones,
                res_type="soxr_hq" if high_quality else "soxr_qq",
            )
        except TypeError:
            shifted = librosa.effects.pitch_shift(y, sr=sr, n_steps=semitones)
        if preserve_formants:
            try:
                shifted = _restore_formants(y, shifted, sr)
            except Exception:  # noqa: BLE001 - fall back to the plain shift
                pass
        return shifted

    return mastering.apply_stereo(segment, shift_channel)


def _echo(segment: AudioSegment, delay_ms: float, decay: float, repeats: float) -> AudioSegment:
    delay = max(1, int(delay_ms))
    decay = max(0.01, min(0.99, float(decay)))
    result = segment
    for repeat in range(1, int(max(1, repeats)) + 1):
        amplitude = decay ** repeat  # each repeat is quieter than the last
        if amplitude < 0.001:
            break
        tail = segment.apply_gain(20.0 * math.log10(amplitude))
        result = result.overlay(tail, position=delay * repeat)
    return result


def _bass_treble(segment: AudioSegment, bass_db: float, treble_db: float) -> AudioSegment:
    out = segment
    if abs(bass_db) > 0.01:
        out = mastering.apply_stereo(out, lambda y, sr: mastering.peaking_eq(y, sr, 120.0, bass_db, 0.7))
    if abs(treble_db) > 0.01:
        out = mastering.apply_stereo(out, lambda y, sr: mastering.peaking_eq(y, sr, 6000.0, treble_db, 0.7))
    return out


def _filter_curve(segment: AudioSegment, high_pass_hz: float, low_pass_hz: float) -> AudioSegment:
    out = segment
    if high_pass_hz and high_pass_hz > 20:
        out = out.high_pass_filter(int(high_pass_hz))
    if low_pass_hz and low_pass_hz > 20:
        out = out.low_pass_filter(int(low_pass_hz))
    return out


def _apply_one(segment: AudioSegment, effect: dict) -> AudioSegment:
    kind = effect.get("type")
    params = {**default_params(kind), **{k: v for k, v in effect.items() if k != "type"}}
    if kind == "amplify":
        return segment.apply_gain(float(params["gain_db"]))
    if kind == "normalize":
        return mastering.normalize_loudness(segment, float(params["target_dbfs"]))
    if kind == "fade_in":
        return segment.fade_in(min(len(segment), max(1, int(params["duration_ms"]))))
    if kind == "fade_out":
        return segment.fade_out(min(len(segment), max(1, int(params["duration_ms"]))))
    if kind == "reverse":
        return segment.reverse()
    if kind == "echo":
        return _echo(segment, params["delay_ms"], params["decay"], params["repeats"])
    if kind == "reverb":
        return mastering.add_reverb(
            segment, room_size=float(params["room_size"]),
            damping=float(params["damping"]), wet_level=float(params["wet_level"]),
        )
    if kind == "bass_treble":
        return _bass_treble(segment, float(params["bass_db"]), float(params["treble_db"]))
    if kind == "filter_curve":
        return _filter_curve(segment, float(params["high_pass_hz"]), float(params["low_pass_hz"]))
    if kind == "compressor":
        return segment.compress_dynamic_range(
            threshold=float(params["threshold_db"]), ratio=float(params["ratio"]),
            attack=float(params["attack_ms"]), release=float(params["release_ms"]),
        )
    if kind == "eq_band":
        return mastering.apply_stereo(
            segment,
            lambda y, sr: mastering.peaking_eq(
                y, sr, float(params["freq"]), float(params["gain_db"]), float(params["q"])
            ),
        )
    if kind == "change_pitch":
        return _shift_pitch(
            segment, float(params["semitones"]),
            bool(float(params.get("high_quality", 0.0))),
            bool(float(params.get("preserve_formants", 0.0))),
        )
    return segment


def apply_clip_processing(segment: AudioSegment, clip: dict, target_ms: int) -> AudioSegment:
    """Render a clip's speed, pitch and effect chain onto ``segment``.

    ``segment`` holds target_ms * speed of source audio; the result is exactly
    target_ms long, so the clip keeps the slot the editor drew for it.
    """
    out = _stretch(segment, clip_speed(clip))
    out = _shift_pitch(
        out, float(clip.get("pitch", 0.0) or 0.0),
        preserve_formants=bool(clip.get("preserve_formants")),
    )
    out = _fit(out, target_ms)
    for effect in clip.get("effects") or []:
        try:
            out = _apply_one(out, effect)
        except Exception:  # noqa: BLE001 - one bad effect must not kill the render
            continue
        # Echo and reverb add a tail; the clip still owns only its own slot.
        out = _fit(out, target_ms)
    return _fit(out, target_ms)
