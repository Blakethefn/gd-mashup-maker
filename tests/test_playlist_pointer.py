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

    def test_clicking_visible_bed_selects_the_full_track(self):
        editor = _ClickEditor("primary")

        PlaylistEditor._finish_marquee(editor)

        self.assertEqual(editor.region_call, (0, 60_000, ["track-1"]))
        self.assertTrue(editor.whole_tracks_selected)
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


if __name__ == "__main__":
    unittest.main()
