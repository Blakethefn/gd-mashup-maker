import unittest

from mashup_app.gui import PlaylistEditor


class _ClickEditor:
    def __init__(self, bed):
        self.track = {"id": "track-1", "name": "Vocals", "bed": bed}
        self.total_ms = 60_000
        self._marquee = (200, 40, 200, 40)
        self._transaction_before = object()
        self.selection = [object()]
        self.active_track_id = None
        self.whole_tracks_selected = False
        self.playhead_ms = 0
        self.region_call = None
        self.status = None

    def _lane_info(self, _y):
        return 0, self.track, "primary"

    def _lane_has_bed(self, track, lane):
        return PlaylistEditor._lane_has_bed(track, lane)

    def _x_to_ms(self, _x):
        return 12_345

    def _snap_point(self, ms):
        return ms

    def set_region(self, start, end=None, track_ids=None):
        self.region_call = (start, end, track_ids)
        self.playhead_ms = start

    def _set_status(self, text):
        self.status = text

    @staticmethod
    def _fmt_precise(ms):
        return f"{ms:.0f}ms"


class _ClipEditor:
    def __init__(self):
        self.tracks = [{"id": "track-1"}, {"id": "track-2"}]
        self.selection = [
            {"track": "track-1", "start_ms": 1_000, "end_ms": 3_000},
            {"track": "track-2", "start_ms": 4_000, "end_ms": 7_000},
        ]
        self.whole_tracks_selected = True
        self.region_call = None
        self.status = None

    def _prune_selection(self):
        return None

    def _track_index_of(self, clip):
        return next(i for i, track in enumerate(self.tracks) if track["id"] == clip["track"])

    def set_region(self, start, end, track_ids):
        self.region_call = (start, end, track_ids)

    def _set_status(self, text):
        self.status = text

    @staticmethod
    def _fmt_precise(ms):
        return f"{ms:.0f}ms"


class PlaylistPointerTests(unittest.TestCase):
    def test_pointer_uses_a_normal_arrow(self):
        self.assertEqual(PlaylistEditor.TOOL_CURSORS["select"], "arrow")

    def test_clicking_empty_space_never_creates_a_full_track_region(self):
        editor = _ClickEditor("primary")

        PlaylistEditor._finish_marquee(editor)

        self.assertEqual(editor.region_call, (12_345, None, ["track-1"]))
        self.assertFalse(editor.whole_tracks_selected)
        self.assertEqual(editor.selection, [])

    def test_clicking_empty_lane_places_cursor_only(self):
        editor = _ClickEditor("muted")

        PlaylistEditor._finish_marquee(editor)

        self.assertEqual(editor.region_call, (12_345, None, ["track-1"]))
        self.assertFalse(editor.whole_tracks_selected)

    def test_clicking_clips_collapses_stale_region_to_cursor(self):
        editor = _ClipEditor()

        PlaylistEditor._select_clicked_clips(editor)

        self.assertEqual(editor.region_call, (1_000, None, ["track-1", "track-2"]))
        self.assertFalse(editor.whole_tracks_selected)
        self.assertIn("Selected 2 clips", editor.status)

    def test_routed_audio_materializes_as_a_normal_bounded_clip(self):
        editor = type("Editor", (), {})()
        editor.tracks = [{"id": "track-1", "stem": "vocals", "bed": "both"}]
        editor.clips = []
        editor.total_ms = 60_000
        editor.MIN_LEN_MS = PlaylistEditor.MIN_LEN_MS
        editor.get_source_bounds = lambda _track, lane: (2_000, 42_000) if lane == "secondary" else (0, 55_000)
        editor._lane_has_bed = PlaylistEditor._lane_has_bed

        proxy = PlaylistEditor._bed_clip(editor, 0, editor.tracks[0], "primary")
        clip = PlaylistEditor._materialize_bed_clip(editor, proxy)

        self.assertEqual((clip["start_ms"], clip["end_ms"]), (0, 55_000))
        self.assertEqual(clip["source"], "primary")
        self.assertNotIn("_bed_proxy", clip)
        self.assertIs(editor.clips[0], clip)
        self.assertEqual(editor.tracks[0]["bed"], "secondary")

    def test_first_pointer_press_selects_entire_routed_clip_object(self):
        editor = type("Editor", (), {})()
        editor.tracks = [{"id": "track-1", "stem": "vocals", "bed": "primary"}]
        editor.clips = []
        editor.selection = []
        editor.CTRL_MASK = PlaylistEditor.CTRL_MASK
        editor.SHIFT_MASK = PlaylistEditor.SHIFT_MASK
        editor.ALT_MASK = PlaylistEditor.ALT_MASK
        editor._transaction_before = None
        editor._begin_transaction = lambda: setattr(editor, "_transaction_before", object())
        editor._materialize_bed_clip = lambda proxy: PlaylistEditor._materialize_bed_clip(editor, proxy)
        editor._in_selection = lambda clip: any(existing is clip for existing in editor.selection)
        editor._x_to_ms = lambda _x: 15_000
        editor._slot_of = lambda _clip: 0
        editor._set_status = lambda _text: None
        editor._redraw = lambda: None
        event = type("Event", (), {"x": 320, "state": 0})()
        proxy = {
            "track": "track-1", "stem": "vocals", "source": "primary",
            "start_ms": 0, "end_ms": 55_000, "source_start_ms": 0,
            "mode": "layer", "_bed_proxy": True, "_track_index": 0,
        }

        PlaylistEditor._start_clip_drag(editor, event, ("bed", proxy))

        self.assertEqual(editor._drag_mode, "move")
        self.assertEqual(len(editor.selection), 1)
        self.assertIs(editor.selection[0], editor.clips[0])
        self.assertEqual((editor.selection[0]["start_ms"], editor.selection[0]["end_ms"]), (0, 55_000))
        self.assertEqual(editor.tracks[0]["bed"], "muted")

    def test_dragging_full_song_clip_grows_timeline_instead_of_pinning_it(self):
        editor = type("Editor", (), {})()
        clip = {
            "track": "track-1", "source": "primary", "mode": "layer",
            "start_ms": 0, "end_ms": 60_000, "source_start_ms": 0,
        }
        editor._drag_ref_before = dict(clip)
        editor._drag_clips = [(clip, dict(clip))]
        editor._drag_grab_offset_ms = 10_000
        editor._drag_slot_anchor = 0
        editor.total_ms = 60_000
        editor._snap_move = lambda start, _length, _event, _exclude: start
        editor._lane_slots = lambda: []
        editor._lane_info = lambda _y: None
        editor._clip_source_start = PlaylistEditor._clip_source_start
        editor._clip_mode = PlaylistEditor._clip_mode
        event = type("Event", (), {"y": 40})()

        PlaylistEditor._drag_move(editor, event, 30_000)

        self.assertEqual((clip["start_ms"], clip["end_ms"]), (20_000, 80_000))
        self.assertEqual(editor.total_ms, 80_000)


if __name__ == "__main__":
    unittest.main()
