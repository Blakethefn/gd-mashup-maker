"""The three mashup algorithms. Each returns a rendered pydub AudioSegment."""
from typing import Callable, Optional

from pydub import AudioSegment

from . import analysis, audio_io, mastering, separation


def simple_overlay(
    primary_path: str,
    secondary_path: str,
    offset_ms: int = 0,
    primary_gain_db: float = 0.0,
    secondary_gain_db: float = 0.0,
    crossfade_ms: int = 0,
    match_loudness: bool = True,
    low_cut_secondary: bool = True,
    duck_amount: float = 0.3,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> AudioSegment:
    """Layer secondary onto primary at a fixed offset, no tempo/key analysis."""
    primary = audio_io.load_mp3(primary_path)
    secondary = audio_io.load_mp3(secondary_path)

    if match_loudness:
        primary = mastering.normalize_loudness(primary)
        secondary = mastering.normalize_loudness(secondary)

    primary = primary.apply_gain(primary_gain_db)
    secondary = secondary.apply_gain(secondary_gain_db)

    if low_cut_secondary:
        # Keeps secondary's bass from fighting primary's bass, the most
        # common cause of two full tracks just sounding stacked/muddy.
        secondary = secondary.high_pass_filter(120)

    if crossfade_ms > 0:
        fade = min(crossfade_ms, len(secondary) // 2)
        secondary = secondary.fade_in(fade).fade_out(fade)

    total_len = max(len(primary), offset_ms + len(secondary))
    if len(primary) < total_len:
        primary += AudioSegment.silent(duration=total_len - len(primary))

    if duck_amount > 0:
        if progress_callback:
            progress_callback("Ducking primary under secondary...")
        trigger = AudioSegment.silent(duration=offset_ms) + secondary
        primary = mastering.duck_segment(primary, trigger, amount=duck_amount)

    return primary.overlay(secondary, position=offset_ms)


def beat_synced_blend(
    primary_path: str,
    secondary_path: str,
    blend_start_ms: int = 0,
    blend_duration_ms: int = 8000,
    primary_gain_db: float = 0.0,
    secondary_gain_db: float = 0.0,
    match_loudness: bool = True,
    low_cut_secondary: bool = True,
    duck_amount: float = 0.3,
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
    secondary = audio_io.float_array_to_segment(y_stretched, sr_secondary)

    primary = audio_io.load_mp3(primary_path)

    if match_loudness:
        primary = mastering.normalize_loudness(primary)
        secondary = mastering.normalize_loudness(secondary)

    primary = primary.apply_gain(primary_gain_db)
    secondary = secondary.apply_gain(secondary_gain_db)

    if low_cut_secondary:
        secondary = secondary.high_pass_filter(120)

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

    if duck_amount > 0:
        if progress_callback:
            progress_callback("Ducking primary under secondary...")
        trigger = AudioSegment.silent(duration=blend_start_ms) + secondary_faded
        primary_shaped = mastering.duck_segment(primary_shaped, trigger, amount=duck_amount)

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
    match_loudness: bool = True,
    carve_for_vocal: bool = True,
    duck_amount: float = 0.5,
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

    vocals_segment = audio_io.float_array_to_segment(y_vocals, sr_vocals)
    instrumental_segment = AudioSegment.from_file(instrumental_wav)

    if match_loudness:
        vocals_segment = mastering.normalize_loudness(vocals_segment, target_dbfs=-16.0)
        instrumental_segment = mastering.normalize_loudness(instrumental_segment, target_dbfs=-18.0)

    vocals_segment = vocals_segment.apply_gain(vocal_gain_db)
    instrumental_segment = instrumental_segment.apply_gain(instrumental_gain_db)

    if carve_for_vocal:
        if progress_callback:
            progress_callback("Carving vocal presence out of instrumental...")
        instrumental_segment = mastering.carve_for_vocal(instrumental_segment)

    total_len = max(len(instrumental_segment), offset_ms + len(vocals_segment))
    if len(instrumental_segment) < total_len:
        instrumental_segment += AudioSegment.silent(duration=total_len - len(instrumental_segment))

    if duck_amount > 0:
        if progress_callback:
            progress_callback("Ducking instrumental under vocals...")
        trigger = AudioSegment.silent(duration=offset_ms) + vocals_segment
        instrumental_segment = mastering.duck_segment(instrumental_segment, trigger, amount=duck_amount)

    if progress_callback:
        progress_callback("Mixing...")

    return instrumental_segment.overlay(vocals_segment, position=offset_ms)
