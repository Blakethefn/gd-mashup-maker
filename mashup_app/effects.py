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
}

# Effects offered as a menu, in Audacity's rough ordering.
EFFECT_ORDER = (
    "amplify", "normalize", "compressor",
    None,
    "fade_in", "fade_out", "reverse",
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


def _shift_pitch(segment: AudioSegment, semitones: float) -> AudioSegment:
    if abs(semitones) <= 1e-6 or len(segment) == 0:
        return segment
    import librosa

    def shift_channel(y, sr):
        return librosa.effects.pitch_shift(y, sr=sr, n_steps=semitones)

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
    return segment


def apply_clip_processing(segment: AudioSegment, clip: dict, target_ms: int) -> AudioSegment:
    """Render a clip's speed, pitch and effect chain onto ``segment``.

    ``segment`` holds target_ms * speed of source audio; the result is exactly
    target_ms long, so the clip keeps the slot the editor drew for it.
    """
    out = _stretch(segment, clip_speed(clip))
    out = _shift_pitch(out, float(clip.get("pitch", 0.0) or 0.0))
    out = _fit(out, target_ms)
    for effect in clip.get("effects") or []:
        try:
            out = _apply_one(out, effect)
        except Exception:  # noqa: BLE001 - one bad effect must not kill the render
            continue
        # Echo and reverb add a tail; the clip still owns only its own slot.
        out = _fit(out, target_ms)
    return _fit(out, target_ms)
