"""Makes two tracks actually blend instead of just stacking: loudness
matching, EQ carving, and sidechain-style ducking.

Two full songs overlaid at their native levels, with no frequency space
carved out for each other and no dynamic interaction, just reads as two
songs playing at once. These helpers are the standard DJ/mashup-production
fixes for that: match perceived loudness first, cut competing frequencies
out of the way, then duck the background track under the foreground one.
"""
from typing import Callable

import numpy as np
from pydub import AudioSegment

from . import audio_io


def normalize_loudness(segment: AudioSegment, target_dbfs: float = -18.0) -> AudioSegment:
    if segment.dBFS == float("-inf"):
        return segment
    return segment.apply_gain(target_dbfs - segment.dBFS)


def peaking_eq(y: np.ndarray, sr: int, freq: float, gain_db: float, q: float = 1.0) -> np.ndarray:
    """RBJ audio-EQ-cookbook peaking filter: boosts/cuts a narrow band around freq."""
    from scipy.signal import lfilter

    a = 10 ** (gain_db / 40)
    w0 = 2 * np.pi * freq / sr
    alpha = np.sin(w0) / (2 * q)
    cos_w0 = np.cos(w0)

    b0, b1, b2 = 1 + alpha * a, -2 * cos_w0, 1 - alpha * a
    a0, a1, a2 = 1 + alpha / a, -2 * cos_w0, 1 - alpha / a

    b = np.array([b0, b1, b2]) / a0
    a_coeffs = np.array([1.0, a1 / a0, a2 / a0])
    return lfilter(b, a_coeffs, y).astype(np.float32)


def apply_stereo(segment: AudioSegment, mono_fn: Callable[[np.ndarray, int], np.ndarray]) -> AudioSegment:
    """Run a (samples, sample_rate) -> samples numpy function on each channel."""
    channels = segment.split_to_mono()
    processed = []
    for ch in channels:
        y = audio_io.segment_to_float_mono(ch)
        y_out = mono_fn(y, segment.frame_rate)
        processed.append(audio_io.float_array_to_segment(y_out, segment.frame_rate, segment.sample_width))
    if len(processed) == 1:
        return processed[0]
    return AudioSegment.from_mono_audiosegments(*processed)


def carve_for_vocal(segment: AudioSegment, freq: float = 2500.0, gain_db: float = -4.0, q: float = 1.0) -> AudioSegment:
    """Cut a narrow band (default: vocal presence range) out of an instrumental
    so a vocal laid on top of it isn't fighting for the same frequencies."""
    return apply_stereo(segment, lambda y, sr: peaking_eq(y, sr, freq, gain_db, q))


def _envelope(y: np.ndarray, sr: int, attack_ms: float, release_ms: float, control_hz: float = 200.0) -> np.ndarray:
    """Rectified, attack/release-smoothed, 0..1-normalized amplitude envelope.

    Computed at a coarse control rate (an envelope follower doesn't need
    full sample-rate resolution) then upsampled, since a full-rate Python
    loop over a multi-minute track would be too slow.
    """
    hop = max(1, int(sr / control_hz))
    usable = len(y) - (len(y) % hop)
    if usable <= 0:
        return np.zeros_like(y)
    frame_peaks = np.abs(y[:usable]).reshape(-1, hop).max(axis=1)

    control_sr = sr / hop
    attack_coef = np.exp(-1.0 / (control_sr * attack_ms / 1000.0))
    release_coef = np.exp(-1.0 / (control_sr * release_ms / 1000.0))

    env = np.zeros_like(frame_peaks)
    prev = 0.0
    for i, x in enumerate(frame_peaks):
        coef = attack_coef if x > prev else release_coef
        prev = coef * prev + (1 - coef) * x
        env[i] = prev

    peak = env.max()
    if peak > 0:
        env /= peak

    control_times = np.arange(len(env)) * hop
    full_times = np.arange(len(y))
    return np.interp(full_times, control_times, env).astype(np.float32)


def duck_segment(
    target: AudioSegment,
    trigger: AudioSegment,
    amount: float = 0.4,
    attack_ms: float = 10.0,
    release_ms: float = 200.0,
) -> AudioSegment:
    """Reduce target's level whenever trigger is loud (sidechain-style
    ducking), so the two tracks interact dynamically instead of just
    sitting on top of each other at a fixed level.

    trigger should already be positioned on the same timeline as target
    (e.g. silence-padded to match target's start offset) so the envelope
    lines up with where trigger actually plays.
    """
    if amount <= 0:
        return target

    sr = target.frame_rate
    trigger_mono = trigger.set_channels(1).set_frame_rate(sr)
    y_trigger = audio_io.segment_to_float_mono(trigger_mono)
    envelope = _envelope(y_trigger, sr, attack_ms, release_ms)

    def duck_channel(y, _sr):
        n = min(len(y), len(envelope))
        gain = np.ones(len(y), dtype=np.float32)
        gain[:n] = 1.0 - amount * envelope[:n]
        return y * gain

    return apply_stereo(target, duck_channel)
