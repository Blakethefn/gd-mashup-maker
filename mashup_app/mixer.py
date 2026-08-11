"""The three mashup algorithms. Each returns a rendered pydub AudioSegment."""
from typing import Callable, Optional

from pydub import AudioSegment

from . import analysis, audio_io, separation


def simple_overlay(
    primary_path: str,
    secondary_path: str,
    offset_ms: int = 0,
    primary_gain_db: float = 0.0,
    secondary_gain_db: float = 0.0,
    crossfade_ms: int = 0,
) -> AudioSegment:
    """Layer secondary onto primary at a fixed offset, no tempo/key analysis."""
    primary = audio_io.load_mp3(primary_path).apply_gain(primary_gain_db)
    secondary = audio_io.load_mp3(secondary_path).apply_gain(secondary_gain_db)

    if crossfade_ms > 0:
        fade = min(crossfade_ms, len(secondary) // 2)
        secondary = secondary.fade_in(fade).fade_out(fade)

    total_len = max(len(primary), offset_ms + len(secondary))
    if len(primary) < total_len:
        primary += AudioSegment.silent(duration=total_len - len(primary))

    return primary.overlay(secondary, position=offset_ms)


def beat_synced_blend(
    primary_path: str,
    secondary_path: str,
    blend_start_ms: int = 0,
    blend_duration_ms: int = 8000,
    primary_gain_db: float = 0.0,
    secondary_gain_db: float = 0.0,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> AudioSegment:
    """Tempo-match secondary to primary's BPM, then crossfade from primary into
    the blended pair starting at blend_start_ms over blend_duration_ms."""
    import librosa

    if progress_callback:
        progress_callback("Detecting tempo...")
    primary_bpm = analysis.detect_bpm(primary_path)
    secondary_bpm = analysis.detect_bpm(secondary_path)

    if progress_callback:
        progress_callback(f"Primary BPM {primary_bpm:.1f}, secondary BPM {secondary_bpm:.1f}. Time-stretching...")

    y_secondary, sr_secondary = audio_io.load_mono_float(secondary_path)
    rate = primary_bpm / secondary_bpm if secondary_bpm > 0 else 1.0
    y_stretched = librosa.effects.time_stretch(y_secondary, rate=rate)
    secondary = audio_io.float_array_to_segment(y_stretched, sr_secondary).apply_gain(secondary_gain_db)

    primary = audio_io.load_mp3(primary_path).apply_gain(primary_gain_db)

    fade = max(1, min(blend_duration_ms, len(secondary)))
    secondary_faded = secondary.fade_in(fade)

    total_len = max(len(primary), blend_start_ms + len(secondary_faded))
    if len(primary) < total_len:
        primary += AudioSegment.silent(duration=total_len - len(primary))

    before = primary[:blend_start_ms]
    fade_out_end = blend_start_ms + fade
    during = primary[blend_start_ms:fade_out_end].fade_out(fade)
    after = primary[fade_out_end:]
    primary_shaped = before + during + after

    if progress_callback:
        progress_callback("Mixing...")

    return primary_shaped.overlay(secondary_faded, position=blend_start_ms)


def vocals_over_instrumental(
    vocal_source_path: str,
    instrumental_source_path: str,
    tempo_match: bool = True,
    key_match: bool = True,
    offset_ms: int = 0,
    vocal_gain_db: float = 0.0,
    instrumental_gain_db: float = 0.0,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> AudioSegment:
    """Separate both sources, take vocals from one and the instrumental from the
    other, optionally tempo/key match the vocals, then mix."""
    import librosa

    vocals_wav, _ = separation.separate_vocals(vocal_source_path, progress_callback)
    _, instrumental_wav = separation.separate_vocals(instrumental_source_path, progress_callback)

    y_vocals, sr_vocals = audio_io.load_mono_float(vocals_wav)

    if tempo_match:
        if progress_callback:
            progress_callback("Tempo-matching vocals to instrumental...")
        vocal_bpm = analysis.detect_bpm(vocals_wav)
        instrumental_bpm = analysis.detect_bpm(instrumental_wav)
        if vocal_bpm > 0 and instrumental_bpm > 0:
            rate = instrumental_bpm / vocal_bpm
            y_vocals = librosa.effects.time_stretch(y_vocals, rate=rate)

    if key_match:
        if progress_callback:
            progress_callback("Key-matching vocals to instrumental...")
        y_instrumental, sr_instrumental = audio_io.load_mono_float(instrumental_wav)
        vocal_pc, _ = analysis.estimate_key(y_vocals, sr_vocals)
        instrumental_pc, _ = analysis.estimate_key(y_instrumental, sr_instrumental)
        shift = analysis.semitone_shift_to_match(vocal_pc, instrumental_pc)
        if shift != 0:
            y_vocals = librosa.effects.pitch_shift(y_vocals, sr=sr_vocals, n_steps=shift)

    if progress_callback:
        progress_callback("Mixing...")

    vocals_segment = audio_io.float_array_to_segment(y_vocals, sr_vocals).apply_gain(vocal_gain_db)
    instrumental_segment = AudioSegment.from_file(instrumental_wav).apply_gain(instrumental_gain_db)

    total_len = max(len(instrumental_segment), offset_ms + len(vocals_segment))
    if len(instrumental_segment) < total_len:
        instrumental_segment += AudioSegment.silent(duration=total_len - len(instrumental_segment))

    return instrumental_segment.overlay(vocals_segment, position=offset_ms)
