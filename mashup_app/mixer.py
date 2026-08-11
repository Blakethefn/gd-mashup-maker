"""The mashup algorithms. Each returns a rendered pydub AudioSegment."""
from typing import Callable, Dict, Optional

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
    reverb_amount: float = 0.0,
    reverb_size: float = 0.5,
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

    if reverb_amount > 0:
        if progress_callback:
            progress_callback("Adding reverb to secondary...")
        secondary = mastering.add_reverb(secondary, room_size=reverb_size, wet_level=reverb_amount)

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
    reverb_amount: float = 0.0,
    reverb_size: float = 0.5,
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

    if reverb_amount > 0:
        if progress_callback:
            progress_callback("Adding reverb to secondary...")
        secondary = mastering.add_reverb(secondary, room_size=reverb_size, wet_level=reverb_amount)

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
    reverb_amount: float = 0.0,
    reverb_size: float = 0.5,
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

    if reverb_amount > 0:
        if progress_callback:
            progress_callback("Adding reverb to vocals...")
        vocals_segment = mastering.add_reverb(vocals_segment, room_size=reverb_size, wet_level=reverb_amount)

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


def _normalize_format(segment: AudioSegment, frame_rate: int = 44100) -> AudioSegment:
    """Force a consistent frame rate/channels/sample width so stem segments
    from different sources (a stereo Demucs wav vs. a mono numpy round-trip
    after time-stretching) can be safely concatenated, not just overlaid."""
    return segment.set_frame_rate(frame_rate).set_channels(2).set_sample_width(2)


def _pad_to(segment: AudioSegment, length_ms: int) -> AudioSegment:
    if len(segment) < length_ms:
        return segment + AudioSegment.silent(duration=length_ms - len(segment), frame_rate=segment.frame_rate)
    return segment


def _resolve_regions(default_source: str, overrides: list, total_len_ms: int) -> list:
    """Partition [0, total_len_ms) into (start, end, source) regions: the
    default source everywhere, except inside override windows, where the
    override's source wins (later overrides in the list win over earlier,
    overlapping ones)."""
    breakpoints = {0, total_len_ms}
    clipped = []
    for ov in overrides:
        start = max(0, min(int(ov["start_ms"]), total_len_ms))
        end = max(0, min(int(ov["end_ms"]), total_len_ms))
        if end > start:
            clipped.append({"start_ms": start, "end_ms": end, "source": ov["source"]})
            breakpoints.add(start)
            breakpoints.add(end)

    points = sorted(breakpoints)
    regions = []
    for start, end in zip(points, points[1:]):
        if start >= end:
            continue
        mid = (start + end) / 2
        source = default_source
        for ov in clipped:
            if ov["start_ms"] <= mid < ov["end_ms"]:
                source = ov["source"]
        regions.append((start, end, source))
    return regions


def _build_stem_track(
    default_source: str, overrides: list, primary_segment: AudioSegment, secondary_segment: AudioSegment,
    total_len_ms: int,
) -> AudioSegment:
    regions = _resolve_regions(default_source, overrides, total_len_ms)
    result = AudioSegment.silent(duration=0, frame_rate=primary_segment.frame_rate)
    for start, end, source in regions:
        src = primary_segment if source == "primary" else secondary_segment
        result += src[start:end]
    return result


def stem_mix(
    primary_path: str,
    secondary_path: str,
    stem_sources: Dict[str, str],
    stem_overrides: Optional[list] = None,
    offset_ms: int = 0,
    primary_gain_db: float = 0.0,
    secondary_gain_db: float = 0.0,
    tempo_match: bool = True,
    match_loudness: bool = True,
    duck_amount: float = 0.3,
    reverb_amount: float = 0.0,
    reverb_size: float = 0.5,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> AudioSegment:
    """Separate both tracks into vocals/drums/bass/other, pick each stem's
    default source per stem_sources (e.g. {"vocals": "secondary", "drums":
    "secondary", "bass": "primary", "other": "primary"}), then mix.

    stem_overrides optionally swaps a stem's source for a specific time
    window, e.g. [{"stem": "bass", "start_ms": 30000, "end_ms": 60000,
    "source": "secondary"}] plays secondary's bass just for that 30s section
    and the default elsewhere - so a single stem can switch source mid-song.
    """
    import librosa

    stem_overrides = stem_overrides or []

    primary_stem_paths = separation.separate_stems(primary_path, progress_callback)
    secondary_stem_paths = separation.separate_stems(secondary_path, progress_callback)

    if progress_callback:
        progress_callback("Detecting tempo...")
    primary_bpm = analysis.detect_bpm(primary_path)
    secondary_bpm = analysis.detect_bpm(secondary_path)
    rate = primary_bpm / secondary_bpm if (tempo_match and secondary_bpm > 0) else 1.0

    if progress_callback:
        progress_callback("Loading stems...")

    primary_stems = {
        name: _normalize_format(AudioSegment.from_file(primary_stem_paths[name]))
        for name in separation.STEM_NAMES
    }

    def load_secondary_stem(name: str) -> AudioSegment:
        y, sr = audio_io.load_mono_float(secondary_stem_paths[name])
        if abs(rate - 1.0) > 0.01:
            y = librosa.effects.time_stretch(y, rate=rate)
        seg = _normalize_format(audio_io.float_array_to_segment(y, sr))
        # Silence-pad so slice positions/override times line up with the
        # primary's absolute timeline, not secondary's own internal time 0.
        return AudioSegment.silent(duration=offset_ms, frame_rate=seg.frame_rate) + seg

    secondary_stems = {name: load_secondary_stem(name) for name in separation.STEM_NAMES}

    if match_loudness:
        # Adjust each source's stems by ONE gain (derived from the full
        # track's loudness), not per-stem, so the natural balance between
        # e.g. vocals and drums within a source is preserved - only the
        # overall primary-vs-secondary level mismatch gets corrected.
        primary_full_dbfs = audio_io.load_mp3(primary_path).dBFS
        secondary_full_dbfs = audio_io.load_mp3(secondary_path).dBFS
        primary_adjust = -18.0 - primary_full_dbfs if primary_full_dbfs != float("-inf") else 0.0
        secondary_adjust = -18.0 - secondary_full_dbfs if secondary_full_dbfs != float("-inf") else 0.0
    else:
        primary_adjust = 0.0
        secondary_adjust = 0.0

    primary_stems = {n: s.apply_gain(primary_adjust + primary_gain_db) for n, s in primary_stems.items()}
    secondary_stems = {n: s.apply_gain(secondary_adjust + secondary_gain_db) for n, s in secondary_stems.items()}

    if reverb_amount > 0:
        if progress_callback:
            progress_callback("Adding reverb to secondary's stems...")
        secondary_stems = {
            n: mastering.add_reverb(s, room_size=reverb_size, wet_level=reverb_amount)
            for n, s in secondary_stems.items()
        }

    total_len = max(
        max((len(s) for s in primary_stems.values()), default=0),
        max((len(s) for s in secondary_stems.values()), default=0),
    )
    primary_stems = {n: _pad_to(s, total_len) for n, s in primary_stems.items()}
    secondary_stems = {n: _pad_to(s, total_len) for n, s in secondary_stems.items()}

    if duck_amount > 0:
        if progress_callback:
            progress_callback("Ducking...")
        # Duck against the *default* secondary-sourced stems (overrides are a
        # creative per-section choice and don't reshape the duck trigger).
        secondary_default_group = None
        for name in separation.STEM_NAMES:
            if stem_sources.get(name, "primary") == "secondary":
                secondary_default_group = (
                    secondary_stems[name] if secondary_default_group is None
                    else secondary_default_group.overlay(secondary_stems[name])
                )
        if secondary_default_group is not None:
            primary_stems = {
                n: mastering.duck_segment(s, secondary_default_group, amount=duck_amount)
                for n, s in primary_stems.items()
            }

    if progress_callback:
        progress_callback("Applying overrides and mixing...")

    final = None
    for name in separation.STEM_NAMES:
        default_source = stem_sources.get(name, "primary")
        overrides_for_stem = [ov for ov in stem_overrides if ov.get("stem") == name]
        track = _build_stem_track(default_source, overrides_for_stem, primary_stems[name], secondary_stems[name], total_len)
        final = track if final is None else final.overlay(track)

    return final if final is not None else AudioSegment.silent(duration=0)
