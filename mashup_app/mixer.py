"""The mashup algorithms. Each returns a rendered pydub AudioSegment."""
from typing import Callable, Dict, Optional

from pydub import AudioSegment

from . import analysis, audio_io, effects, mastering, separation


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
    primary_reverb_amount: float = 0.0,
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

    if primary_reverb_amount > 0:
        if progress_callback:
            progress_callback("Adding reverb to primary...")
        primary = mastering.add_reverb(primary, room_size=reverb_size, wet_level=primary_reverb_amount)

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
    primary_reverb_amount: float = 0.0,
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

    if primary_reverb_amount > 0:
        if progress_callback:
            progress_callback("Adding reverb to primary...")
        primary = mastering.add_reverb(primary, room_size=reverb_size, wet_level=primary_reverb_amount)

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
    instrumental_reverb_amount: float = 0.0,
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

    if instrumental_reverb_amount > 0:
        if progress_callback:
            progress_callback("Adding reverb to instrumental...")
        instrumental_segment = mastering.add_reverb(
            instrumental_segment, room_size=reverb_size, wet_level=instrumental_reverb_amount
        )

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
        if ov.get("mode", "replace") == "layer":
            continue
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


def _auto_fallback_clips(stem: str, bed: str, primary_end: int, secondary_end: int) -> list:
    """Replacement clips that swap a stem back to whichever source is still
    playing once its own bed's audio has ended - without them a stem sourced
    from the shorter track goes silent for the rest of the song."""
    if bed == "secondary" and secondary_end < primary_end:
        return [{"stem": stem, "start_ms": secondary_end, "end_ms": primary_end, "source": "primary"}]
    if bed == "primary" and primary_end < secondary_end:
        return [{"stem": stem, "start_ms": primary_end, "end_ms": secondary_end, "source": "secondary"}]
    return []


def _build_stem_track(
    default_source: str, overrides: list, sources: Dict[str, AudioSegment],
    total_len_ms: int, crossfade_ms: int = 900, source_origins: Optional[Dict[str, int]] = None,
) -> AudioSegment:
    """Build one playlist track without changing its total length.

    ``sources`` maps a lane name to the audio it feeds: stem tracks supply
    "primary"/"secondary", imported audio tracks supply "import". Replace clips
    choose the lane for their destination window. Layer clips are mixed over
    that replacement/base track, allowing (for example) both primary and
    secondary drums to play together. Clips with source_start_ms carry their
    own track-local source in-point, so moving a clip moves its audio and
    trimming/cutting can retain the correct source material. Legacy overrides
    without that field keep their original absolute-time behavior.
    """
    origins = {name: 0 for name in sources}
    if source_origins:
        origins.update({name: int(value) for name, value in source_origins.items() if name in sources})

    def source_names(source: str):
        if source == "both":
            return tuple(name for name in ("primary", "secondary") if name in sources)
        if source in sources:
            return (source,)
        if source == "muted":
            return ()
        # An unknown lane on a stem track keeps the original primary fallback.
        return ("primary",) if "primary" in sources else ()

    reference = next(iter(sources.values()))

    def slice_padded(segment: AudioSegment, start_ms: int, end_ms: int) -> AudioSegment:
        duration = max(0, int(end_ms - start_ms))
        def matching_silence(length_ms):
            return (
                AudioSegment.silent(duration=length_ms, frame_rate=segment.frame_rate)
                .set_channels(segment.channels)
                .set_sample_width(segment.sample_width)
            )
        if duration == 0:
            return matching_silence(0)
        left_pad = max(0, -int(start_ms))
        slice_start = max(0, int(start_ms))
        slice_end = max(slice_start, int(end_ms))
        piece = matching_silence(left_pad)
        piece += segment[slice_start:slice_end]
        return _pad_to(piece, duration)[:duration]

    def descriptor_source(desc):
        if desc is None:
            return default_source
        # A muted clip silences its window: as a Replace clip that also
        # silences the bed under it, which is how "silence audio" works.
        return "muted" if desc.get("mute") else desc.get("source", "primary")

    def source_position(desc, source: str, timeline_ms: int) -> int:
        if desc is not None and "source_start_ms" in desc:
            # A sped-up clip eats more source per millisecond of timeline.
            offset = (timeline_ms - int(desc["start_ms"])) * effects.clip_speed(desc)
            return origins[source] + int(desc["source_start_ms"]) + int(round(offset))
        # Old overrides and the default bed are aligned to final timeline
        # time, including secondary's start-offset padding.
        return timeline_ms

    def silence(duration: int) -> AudioSegment:
        return (
            AudioSegment.silent(duration=duration, frame_rate=reference.frame_rate)
            .set_channels(reference.channels)
            .set_sample_width(reference.sample_width)
        )

    def raw_slice(desc, start_ms: int, end_ms: int, speed: float = 1.0) -> AudioSegment:
        duration = max(0, end_ms - start_ms)
        source_len = max(0, int(round(duration * speed)))
        mixed = None
        for source in source_names(descriptor_source(desc)):
            source_start = source_position(desc, source, start_ms)
            piece = slice_padded(sources[source], source_start, source_start + source_len)
            mixed = piece if mixed is None else mixed.overlay(piece)
        if mixed is None:
            mixed = silence(source_len)
        return _pad_to(mixed, source_len)[:source_len]

    # A clip carrying speed/pitch/effects is rendered once across its whole
    # destination window and then indexed into, so an effect that needs the
    # entire clip (a fade, a reverse, a reverb tail) is not re-applied to each
    # crossfade sub-slice.
    processed_cache: Dict[int, AudioSegment] = {}

    def processed_clip(desc) -> AudioSegment:
        cached = processed_cache.get(id(desc))
        if cached is not None:
            return cached
        start, end = int(desc["start_ms"]), int(desc["end_ms"])
        duration = max(0, end - start)
        rendered = effects.apply_clip_processing(
            raw_slice(desc, start, end, effects.clip_speed(desc)), desc, duration
        )
        processed_cache[id(desc)] = rendered
        return rendered

    def slice_descriptor(desc, start_ms: int, end_ms: int) -> AudioSegment:
        duration = max(0, end_ms - start_ms)
        if desc is not None and effects.has_processing(desc):
            offset = max(0, start_ms - int(desc["start_ms"]))
            piece = processed_clip(desc)[offset:offset + duration]
            return _pad_to(piece, duration)[:duration]
        return _pad_to(raw_slice(desc, start_ms, end_ms), duration)[:duration]

    def descriptor_key(desc):
        # Two neighbouring regions share a key only when the same source audio
        # runs continuously across the join, so no crossfade is needed there.
        if desc is not None and effects.has_processing(desc):
            return ("processed", id(desc))
        source = descriptor_source(desc)
        offsets = []
        for name in source_names(source):
            if desc is not None and "source_start_ms" in desc:
                offset = origins[name] + int(desc["source_start_ms"]) - int(desc["start_ms"])
            else:
                offset = 0
            offsets.append((name, offset))
        return tuple(offsets)

    replacements = [ov for ov in overrides if ov.get("mode", "replace") != "layer"]
    breakpoints = {0, total_len_ms}
    for ov in replacements:
        start = max(0, min(int(ov["start_ms"]), total_len_ms))
        end = max(0, min(int(ov["end_ms"]), total_len_ms))
        if end > start:
            breakpoints.update((start, end))

    regions = []
    points = sorted(breakpoints)
    for start, end in zip(points, points[1:]):
        active = None
        mid = (start + end) / 2
        for ov in replacements:
            if int(ov["start_ms"]) <= mid < int(ov["end_ms"]):
                active = ov
        regions.append((start, end, active))

    result = reference[:0]
    prev = None  # (start, end, descriptor)
    for start, end, desc in regions:
        seg_start = start
        if prev is not None and descriptor_key(prev[2]) != descriptor_key(desc):
            prev_start, prev_end, prev_desc = prev
            half = min(crossfade_ms, end - start, prev_end - prev_start) // 2
            if half > 0:
                b = start
                result = result[: len(result) - half]
                outgoing = slice_descriptor(prev_desc, b - half, b + half).fade_out(2 * half)
                incoming = slice_descriptor(desc, b - half, b + half).fade_in(2 * half)
                result += outgoing.overlay(incoming)
                seg_start = start + half
        result += slice_descriptor(desc, seg_start, end)
        prev = (start, end, desc)

    result = _pad_to(result, total_len_ms)[:total_len_ms]
    layers = [ov for ov in overrides if ov.get("mode", "replace") == "layer"]
    for ov in layers:
        start = max(0, min(int(ov["start_ms"]), total_len_ms))
        end = max(0, min(int(ov["end_ms"]), total_len_ms))
        if end <= start:
            continue
        clip = slice_descriptor(ov, start, end)
        fade = min(max(0, int(crossfade_ms)), len(clip) // 2)
        if fade:
            continues_from_previous = any(
                other is not ov
                and int(other["end_ms"]) == int(ov["start_ms"])
                and descriptor_key(other) == descriptor_key(ov)
                for other in layers
            )
            continues_into_next = any(
                other is not ov
                and int(other["start_ms"]) == int(ov["end_ms"])
                and descriptor_key(other) == descriptor_key(ov)
                for other in layers
            )
            if not continues_from_previous:
                clip = clip.fade_in(fade)
            if not continues_into_next:
                clip = clip.fade_out(fade)
        result = result.overlay(clip, position=start)
    return _pad_to(result, total_len_ms)[:total_len_ms]


def stem_mix(
    primary_path: str,
    secondary_path: str,
    stem_sources: Dict[str, str],
    stem_overrides: Optional[list] = None,
    timeline_tracks: Optional[list] = None,
    offset_ms: int = 0,
    primary_gain_db: float = 0.0,
    secondary_gain_db: float = 0.0,
    tempo_match: bool = True,
    match_loudness: bool = True,
    duck_amount: float = 0.3,
    reverb_amount: float = 0.0,
    reverb_size: float = 0.5,
    primary_reverb_amount: float = 0.0,
    auto_fallback: bool = True,
    override_crossfade_ms: int = 900,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> AudioSegment:
    """Separate both tracks into vocals/drums/bass/other, then mix a playlist
    of timeline tracks.

    timeline_tracks describes the playlist: each entry is a dict with an "id",
    a "kind" ("stem" or "audio"), the "stem" it draws from or the "path" it was
    imported from, its full-song "bed" (primary/secondary/both/import/muted),
    "gain_db", and "mute"/"solo" flags. Several tracks may share one stem -
    that is what makes duplicated tracks and stacked clip lanes work. Omit it
    and one track per stem is synthesized from stem_sources, which is the
    original four-stem behavior.

    stem_overrides accepts playlist clips. mode="replace" chooses a source
    for the window; mode="layer" mixes it over the default/replacement track.
    source_start_ms is an optional track-local in-point retained when clips
    move, trim, or split. A clip's "track" field binds it to one timeline
    track; clips without one fall back to the first track carrying their stem.
    Dicts without the new fields remain compatible with the original
    absolute-time replacement behavior.

    auto_fallback (default on) additionally swaps a stem back to whichever
    source is still playing once its own default source's audio has ended -
    without it, a stem sourced from the shorter track goes silent for the
    remainder of the song once that track runs out (e.g. secondary supplying
    vocals+drums ends early, and primary's bass+other alone for the rest of
    the song sounds thin/muffled since nothing ever fills back in for them).
    Every source switch, explicit or automatic, is crossfaded over
    override_crossfade_ms instead of cut hard.
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

    tracks = timeline_tracks or [
        {
            "id": name, "name": name, "kind": "stem", "stem": name,
            "bed": stem_sources.get(name, "primary"), "gain_db": 0.0, "mute": False, "solo": False,
        }
        for name in separation.STEM_NAMES
    ]
    imported: Dict[str, AudioSegment] = {}
    for track in tracks:
        if track.get("kind") == "audio" and track.get("path") and track["id"] not in imported:
            if progress_callback:
                progress_callback(f"Loading imported track {track.get('name', track['id'])}...")
            imported[track["id"]] = _normalize_format(AudioSegment.from_file(track["path"]))

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

    if primary_reverb_amount > 0:
        if progress_callback:
            progress_callback("Adding reverb to primary's stems...")
        primary_stems = {
            n: mastering.add_reverb(s, room_size=reverb_size, wet_level=primary_reverb_amount)
            for n, s in primary_stems.items()
        }

    if reverb_amount > 0:
        if progress_callback:
            progress_callback("Adding reverb to secondary's stems...")
        secondary_stems = {
            n: mastering.add_reverb(s, room_size=reverb_size, wet_level=reverb_amount)
            for n, s in secondary_stems.items()
        }

    # Each source's real content length, captured before padding either one
    # out to match the other - the point where the shorter source's audio
    # actually stops, used below to auto-fallback stems once their source runs out.
    primary_content_end = max((len(s) for s in primary_stems.values()), default=0)
    secondary_content_end = max((len(s) for s in secondary_stems.values()), default=0)
    # The playlist runs as long as its longest content: either source song, an
    # imported track playing as a full-song bed, or a clip placed past both.
    total_len = max(primary_content_end, secondary_content_end)
    for track in tracks:
        if track.get("kind") == "audio" and track.get("bed", "muted") != "muted":
            total_len = max(total_len, len(imported.get(track["id"], AudioSegment.silent(duration=0))))
    total_len = max([total_len] + [int(ov["end_ms"]) for ov in stem_overrides])
    primary_stems = {n: _pad_to(s, total_len) for n, s in primary_stems.items()}
    secondary_stems = {n: _pad_to(s, total_len) for n, s in secondary_stems.items()}

    if duck_amount > 0:
        if progress_callback:
            progress_callback("Ducking...")
        # Duck against the *default* secondary-sourced stems (playlist clips are a
        # creative per-section choice and don't reshape the duck trigger).
        secondary_default_group = None
        for name in separation.STEM_NAMES:
            if stem_sources.get(name, "primary") in ("secondary", "both"):
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
        progress_callback("Applying playlist clips and mixing...")

    soloed = any(track.get("solo") for track in tracks)
    claimed_stems = set()
    final = None
    for track in tracks:
        stem = track.get("stem")
        # An untracked legacy clip belongs to the first track carrying its
        # stem, so duplicating a track never duplicates its audio by accident.
        first_for_stem = stem is not None and stem not in claimed_stems
        claimed_stems.add(stem)
        if track.get("mute") or (soloed and not track.get("solo")):
            continue

        if track.get("kind") == "audio":
            segment = imported.get(track["id"])
            if segment is None:
                continue
            sources = {"import": _pad_to(segment, total_len)}
            origins = {"import": 0}
        elif stem in primary_stems:
            sources = {"primary": primary_stems[stem], "secondary": secondary_stems[stem]}
            origins = {"primary": 0, "secondary": offset_ms}
        else:
            continue

        bed = track.get("bed", "primary")
        clips = [
            ov for ov in stem_overrides
            if ov.get("track") == track["id"]
            or (not ov.get("track") and first_for_stem and ov.get("stem") == stem)
        ]
        # Fallback regions come first so an explicit user clip covering the
        # same window still wins - later entries win ties when they overlap.
        if track.get("kind") == "stem" and auto_fallback:
            clips = _auto_fallback_clips(
                stem, bed, primary_content_end, secondary_content_end
            ) + clips

        built = _build_stem_track(
            bed, clips, sources, total_len,
            crossfade_ms=override_crossfade_ms, source_origins=origins,
        )
        # A track's own effects run over its finished audio, so they reach the
        # full-song bed as well as the clips sitting on it.
        built = effects.apply_track_effects(built, track.get("effects"), total_len)
        gain = float(track.get("gain_db", 0.0) or 0.0)
        if abs(gain) > 0.01:
            built = built.apply_gain(gain)
        pan = float(track.get("pan", 0.0) or 0.0)
        if abs(pan) > 0.01:
            built = built.pan(max(-1.0, min(1.0, pan)))
        final = built if final is None else final.overlay(built)

    return final if final is not None else AudioSegment.silent(duration=total_len)
