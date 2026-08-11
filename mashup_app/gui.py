"""Tkinter GUI: pick two MP3s, choose a mashup mode, tweak its controls, render."""
import itertools
import json
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import mixer

MODE_SIMPLE = "Simple Overlay/Crossfade"
MODE_BEAT_SYNCED = "Beat-Synced Blend"
MODE_VOCALS = "Vocals-over-Instrumental"
MODE_STEMS = "Stem Mixer (vocals/drums/bass/other)"
MODES = [MODE_SIMPLE, MODE_BEAT_SYNCED, MODE_VOCALS, MODE_STEMS]
MODE_SHORT_NAMES = {
    MODE_SIMPLE: "Simple Overlay",
    MODE_BEAT_SYNCED: "Beat-Synced",
    MODE_VOCALS: "Vocals-over-Inst.",
    MODE_STEMS: "Stem Mixer",
}
STEM_NAMES = ("vocals", "drums", "bass", "other")
STEM_DEFAULT_SOURCE = {"vocals": "Secondary", "drums": "Secondary", "bass": "Primary", "other": "Primary"}

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
MUSIC_DIR = str(Path.home() / "Music") if (Path.home() / "Music").exists() else str(Path.home())
PREFS_PATH = PROJECT_ROOT / ".gui_prefs.json"

# ---- palette (dark DAW theme) -------------------------------------------
# One dark palette drives every widget, canvas and menu in the app, so the
# playlist no longer sits as a dark island inside a light dashboard.
BG = "#0f1319"           # window background
SURFACE = "#171c23"      # panels / grouped sections
SURFACE_ALT = "#1e242d"  # entries, spinboxes, raised rows
SURFACE_HI = "#28303b"   # hover / pressed
BORDER = "#2a313b"
BORDER_HI = "#39424f"
TEXT = "#e6eaf1"
MUTED = "#8f99a8"
ACCENT = "#f0883b"       # FL-style amber
ACCENT_ACTIVE = "#ff9d55"
ACCENT_TEXT = "#14181e"
SUCCESS = "#4ade80"
ERROR = "#f87171"
TIMELINE_BG = "#12161c"
TIMELINE_PRIMARY = "#c87c2e"
TIMELINE_SECONDARY = "#2d7fc4"

# ---- playlist canvas colors ---------------------------------------------
PLAYLIST_BG = "#0b0e12"
TRACK_BG = "#161b22"
TRACK_BG_ALT = "#1a2029"
TRACK_HEADER_BG = "#12161c"
TRACK_HEADER_ACTIVE = "#202834"
GRID_MINOR = "#232932"
GRID_MAJOR = "#39414e"
CLIP_COLORS = {"primary": "#c87c2e", "secondary": "#2d7fc4", "import": "#7a5bd0"}
BED_COLORS = {"primary": "#3d2c17", "secondary": "#152e4a", "import": "#291f45"}
LANE_LABELS = {"primary": "P", "secondary": "S", "import": "A"}
LANE_TINTS = {"primary": "#f0a45c", "secondary": "#5fa8ec", "import": "#a68bec"}
CLIP_SELECTED = "#ffd75e"
PLAYHEAD_COLOR = "#ff6b4a"
REGION_FILL = "#9ecbff"
REGION_EDGE = "#5aa9f0"
MUTED_CLIP = "#464e5b"
MODE_COLORS = {"layer": "#5fd68a", "replace": "#ff7b72"}

STEM_LANES = ("primary", "secondary")
AUDIO_LANES = ("import",)


def _track_lanes(track):
    """Sublanes a track exposes: stem tracks stack Primary over Secondary,
    imported audio tracks carry a single lane fed by their own file."""
    return AUDIO_LANES if track.get("kind") == "audio" else STEM_LANES


_track_serial = itertools.count(1)


def _new_track_id():
    return f"trk{next(_track_serial)}"


def make_stem_track(stem, name=None, bed=None):
    """A timeline track fed by one separated stem of both source songs."""
    return {
        "id": _new_track_id(),
        "name": name or stem.capitalize(),
        "kind": "stem",
        "stem": stem,
        "path": None,
        "bed": bed if bed is not None else STEM_DEFAULT_SOURCE[stem].lower(),
        "gain_db": 0.0,
        "pan": 0.0,
        "mute": False,
        "solo": False,
    }


def make_audio_track(path, name=None):
    """A timeline track fed by an imported audio file. It starts clip-only
    (bed "muted") so importing never silently drops a whole extra song on top
    of the mashup - switch its bed to Import to play it end to end."""
    return {
        "id": _new_track_id(),
        "name": name or Path(path).stem[:24],
        "kind": "audio",
        "stem": None,
        "path": path,
        "bed": "muted",
        "gain_db": 0.0,
        "pan": 0.0,
        "mute": False,
        "solo": False,
    }


def _load_prefs() -> dict:
    try:
        return json.loads(PREFS_PATH.read_text())
    except Exception:
        return {}


def _save_prefs(prefs: dict):
    try:
        PREFS_PATH.write_text(json.dumps(prefs))
    except Exception:
        pass


def _rounded_rect(canvas, x0, y0, x1, y1, radius=6, **kwargs):
    """A rounded rectangle drawn as a smoothed polygon (plain tk.Canvas has no native one)."""
    radius = max(0, min(radius, (x1 - x0) / 2, (y1 - y0) / 2))
    if radius <= 0:
        return canvas.create_rectangle(x0, y0, x1, y1, **kwargs)
    points = [
        x0 + radius, y0, x1 - radius, y0, x1, y0, x1, y0 + radius,
        x1, y1 - radius, x1, y1, x1 - radius, y1, x0 + radius, y1,
        x0, y1, x0, y1 - radius, x0, y0 + radius, x0, y0,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


class ToolTip:
    """Small delayed hover popup used to explain a control without cluttering the layout."""

    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _evt=None):
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(
            self.tip, text=self.text, background=SURFACE_HI, foreground=TEXT,
            font=("Segoe UI", 8), padx=8, pady=5, wraplength=280, justify="left",
            relief="solid", bd=1, highlightthickness=0,
        ).pack()

    def _hide(self, _evt=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class LabeledScale(ttk.Frame):
    """A ttk.Scale with a text label and a live value readout."""

    def __init__(self, parent, label, from_, to, default, unit="", fmt="{:.0f}", tooltip=None):
        super().__init__(parent)
        self.var = tk.DoubleVar(value=default)
        self.fmt = fmt
        self.unit = unit
        self._change_callback = None

        ttk.Label(self, text=label, width=32, anchor="w").grid(row=0, column=0, sticky="w")
        self.value_label = ttk.Label(self, text=self._format(default), width=9, anchor="e", style="Muted.TLabel")
        self.scale = ttk.Scale(self, from_=from_, to=to, variable=self.var, command=self._on_change)
        self.scale.grid(row=0, column=1, sticky="ew", padx=8)
        self.value_label.grid(row=0, column=2, sticky="e")
        self.columnconfigure(1, weight=1)
        if tooltip:
            ToolTip(self, tooltip)

    def _format(self, value):
        return f"{self.fmt.format(float(value))}{self.unit}"

    def _on_change(self, _evt=None):
        self.value_label.config(text=self._format(self.var.get()))
        if self._change_callback:
            self._change_callback(self.var.get())

    def get(self):
        return self.var.get()

    def set(self, value):
        self.var.set(value)
        self._on_change()

    def bind_change(self, callback):
        """Register a callback(value) fired whenever this slider's value
        changes (by dragging the slider OR via set()), so an external widget
        like a timeline can stay in sync."""
        self._change_callback = callback


class OffsetTimeline(tk.Canvas):
    """Visualizes primary's length as one bar and secondary's placement
    within it as a second, draggable bar - click/drag anywhere to place
    secondary's start instead of typing milliseconds. Shared by every mode:
    whichever mode is active rebinds this same widget to its own offset
    slider instead of each mode carrying its own copy."""

    BG = TIMELINE_BG
    PRIMARY_COLOR = TIMELINE_PRIMARY
    SECONDARY_COLOR = TIMELINE_SECONDARY

    def __init__(self, parent, height=60, on_offset_change=None):
        super().__init__(parent, height=height, bg=self.BG, highlightthickness=1, highlightbackground=BORDER)
        self.height_px = height
        self.primary_ms = 0
        self.secondary_ms = 0
        self.offset_ms = 0
        self.on_offset_change = on_offset_change
        self.bind("<Configure>", lambda _e: self._redraw())
        self.bind("<ButtonPress-1>", self._on_drag)
        self.bind("<B1-Motion>", self._on_drag)

    def set_durations(self, primary_ms, secondary_ms):
        self.primary_ms = max(0, primary_ms)
        self.secondary_ms = max(0, secondary_ms)
        self._redraw()

    def set_offset(self, offset_ms):
        self.offset_ms = max(0, offset_ms)
        self._redraw()

    def _total_ms(self):
        return max(1000, self.primary_ms, self.offset_ms + self.secondary_ms)

    def _ms_to_x(self, ms):
        return (ms / self._total_ms()) * max(1, self.winfo_width())

    def _x_to_ms(self, x):
        return max(0.0, (x / max(1, self.winfo_width())) * self._total_ms())

    @staticmethod
    def _fmt(ms):
        s = int(ms // 1000)
        return f"{s // 60}:{s % 60:02d}"

    def _redraw(self):
        self.delete("all")
        w = max(1, self.winfo_width())
        h = self.height_px
        lane_h = h // 2 - 8

        _rounded_rect(self, 0, 4, max(2, self._ms_to_x(self.primary_ms)), 4 + lane_h, radius=5, fill=self.PRIMARY_COLOR, outline="")
        self.create_text(8, 4 + lane_h / 2, text="Primary", anchor="w", fill="white", font=("Segoe UI", 8, "bold"))

        sec_y0 = h - lane_h - 4
        x0, x1 = self._ms_to_x(self.offset_ms), self._ms_to_x(self.offset_ms + self.secondary_ms)
        _rounded_rect(self, x0, sec_y0, max(x0 + 2, x1), sec_y0 + lane_h, radius=5, fill=self.SECONDARY_COLOR, outline="")
        self.create_text(x0 + 8, sec_y0 + lane_h / 2, text="Secondary", anchor="w", fill="white", font=("Segoe UI", 8, "bold"))

        self.create_text(2, h - 1, text="0:00", anchor="sw", fill=MUTED, font=("Segoe UI", 7))
        self.create_text(w - 2, h - 1, text=self._fmt(self._total_ms()), anchor="se", fill=MUTED, font=("Segoe UI", 7))

    def _on_drag(self, event):
        self.offset_ms = min(self._x_to_ms(event.x), max(0.0, self._total_ms() - 1))
        self._redraw()
        if self.on_offset_change:
            self.on_offset_change(self.offset_ms)


class SourceChip(tk.Label):
    """A browser chip that can be dragged onto a matching PlaylistEditor lane.

    ``source`` is the lane kind the chip produces ("primary", "secondary" or
    "import"), which is also what decides which tracks will accept the drop.
    """

    def __init__(self, parent, text, source, editor_getter, **kwargs):
        color = CLIP_COLORS.get(source, CLIP_COLORS["primary"])
        super().__init__(
            parent, text=text, bg=color, fg="#ffffff", padx=10, pady=5,
            relief="flat", bd=0, cursor="hand2", font=("Segoe UI", 8, "bold"), **kwargs,
        )
        self.source = source
        self.editor_getter = editor_getter
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_motion)
        self.bind("<ButtonRelease-1>", self._on_release)

    def _on_press(self, event):
        fraction = event.x / max(1, self.winfo_width())
        self.editor_getter().begin_source_drag(self.source, fraction)

    def _on_motion(self, event):
        self.editor_getter().drag_source(event.x_root, event.y_root, getattr(event, "state", 0))

    def _on_release(self, event):
        self.editor_getter().end_source_drag(event.x_root, event.y_root, getattr(event, "state", 0))


class PlaylistEditor(tk.Canvas):
    """FL-Studio-style playlist canvas with dynamic tracks.

    Tracks are created at runtime: a stem track stacks Primary over Secondary
    sublanes, an imported audio track carries a single lane fed by its own
    file. Clips store a destination window plus a track-local source in-point,
    so move, trim, slip and split behave like real audio clips rather than
    absolute-time source switches.

    Editing is multi-clip throughout: marquee select, Alt-drag duplication,
    copy/paste and split-at-playhead all act on the whole selection. A clip
    plays whatever its lane feeds it, so dragging a clip onto another track
    re-sources it - the same rule that makes duplicated tracks useful.

    Legacy override dicts (no track/mode/source_start_ms) are adopted by the
    first matching stem track and keep their original meaning.
    """

    HEADER_W = 176
    EDGE_PX = 7
    MIN_LEN_MS = 250
    MIN_VIEW_MS = 1000
    RULER_H = 30
    HISTORY_LIMIT = 60
    MAX_VISIBLE_TRACKS = 7
    ALT_MASK = 0x0008
    SHIFT_MASK = 0x0001
    CTRL_MASK = 0x0004
    TOOLS = ("select", "draw", "razor")
    TOOL_CURSORS = {"select": "", "draw": "crosshair", "razor": "X_cursor"}

    def __init__(
        self, parent, get_clip_length_ms=None, get_clip_mode=None, get_snap_enabled=None,
        on_change=None, on_status=None, on_tool_change=None, on_track_command=None,
        on_region=None, lane_h=58,
    ):
        self.lane_h = lane_h
        self.tracks: list = []
        self.clips: list = []
        super().__init__(
            parent, height=self.RULER_H + lane_h * 4, bg=PLAYLIST_BG, takefocus=True,
            highlightthickness=1, highlightbackground=BORDER_HI,
        )
        self.total_ms = 60000
        self.view_start_ms = 0.0
        self.view_span_ms = 60000.0
        self.scroll_y = 0.0
        self.playhead_ms = 0.0
        self.tool = "select"
        self.selection: list = []
        self.clipboard: list = []
        self.active_track_id = None
        # Audacity-style time selection: a region plus the tracks it applies
        # to. The region collapses to a point (the cursor) on a plain click,
        # and every edit verb below reads it.
        self.sel_start_ms = 0.0
        self.sel_end_ms = 0.0
        self.selected_track_ids: list = []
        self._zoom_toggle_span = None

        self.get_clip_length_ms = get_clip_length_ms or (lambda: 8000)
        self.get_clip_mode = get_clip_mode or (lambda: "layer")
        self.get_snap_enabled = get_snap_enabled or (lambda: True)
        self.on_change = on_change
        self.on_status = on_status
        self.on_tool_change = on_tool_change
        self.on_track_command = on_track_command
        self.on_region = on_region

        self.undo_stack: list = []
        self.redo_stack: list = []
        self._transaction_before = None
        self._drag_mode = None  # None|scrub|create|move|resize-left|resize-right|marquee
        self._drag_clips: list = []  # [(live_clip, snapshot_taken_at_press)]
        self._drag_ref = None
        self._drag_ref_before = None
        self._drag_anchor_ms = 0.0
        self._drag_grab_offset_ms = 0.0
        self._drag_slot_anchor = 0
        self._drag_additive = None
        self._marquee = None
        self._external_drag = None
        self._feedback = None
        self._pan_anchor = None
        self._header_hits: list = []
        self._xscrollcommand = None
        self._yscrollcommand = None
        self._menu = None

        self.bind("<Configure>", lambda _e: self._redraw())
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Double-Button-1>", self._on_double_click)
        self.bind("<Button-3>", self._on_right_click)
        self.bind("<Motion>", self._on_hover)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-2>", self._on_pan_press)
        self.bind("<B2-Motion>", self._on_pan_drag)
        self.bind("<ButtonRelease-2>", self._on_pan_release)
        self.bind("<MouseWheel>", self._on_wheel)
        self.bind("<Control-MouseWheel>", self._on_zoom_wheel)
        self.bind("<Shift-MouseWheel>", self._on_scroll_wheel)
        self.bind("<Control-z>", lambda _e: self.undo())
        self.bind("<Control-y>", lambda _e: self.redo())
        self.bind("<Control-Shift-Z>", lambda _e: self.redo())
        self.bind("<Control-a>", lambda _e: self.select_all())
        self.bind("<Control-Shift-A>", lambda _e: self.select_none())
        self.bind("<Control-c>", lambda _e: self.copy_selection())
        self.bind("<Control-x>", lambda _e: self.cut_selection())
        self.bind("<Control-v>", lambda _e: self.paste_clipboard())
        self.bind("<Control-d>", lambda _e: self.duplicate_to_new_track())
        self.bind("<Control-Shift-D>", lambda _e: self.duplicate_selection())
        self.bind("<Delete>", lambda _e: self.delete_selected())
        self.bind("<BackSpace>", lambda _e: self.delete_selected())
        # Audacity's clip-edit verbs, on Audacity's shortcuts.
        self.bind("<Control-i>", lambda _e: self.split_at_selection())
        self.bind("<Control-Alt-i>", lambda _e: self.split_to_new_track())
        self.bind("<Control-k>", lambda _e: self.ripple_delete_selection())
        self.bind("<Control-Alt-k>", lambda _e: self.split_delete_selection())
        self.bind("<Control-Alt-x>", lambda _e: self.split_cut_selection())
        self.bind("<Control-t>", lambda _e: self.trim_to_selection())
        self.bind("<Control-l>", lambda _e: self.silence_selection())
        self.bind("<Control-j>", lambda _e: self.join_selection())
        self.bind("<Control-m>", lambda _e: self.toggle_mute_selection())
        self.bind("<Control-e>", lambda _e: self.zoom_to_selection())
        self.bind("<Control-f>", lambda _e: self.fit_view() or "break")
        self.bind("<Control-Key-2>", lambda _e: self.zoom_normal())
        self.bind("<Shift-Z>", lambda _e: self.zoom_toggle())
        self.bind("<bracketleft>", lambda _e: self.cursor_to_boundary(-1))
        self.bind("<bracketright>", lambda _e: self.cursor_to_boundary(1))
        self.bind("<Shift-braceleft>", lambda _e: self.cursor_to_boundary(-1, extend=True))
        self.bind("<Shift-braceright>", lambda _e: self.cursor_to_boundary(1, extend=True))
        self.bind("<Key-v>", lambda _e: self.set_tool("select"))
        self.bind("<Key-b>", lambda _e: self.set_tool("draw"))
        self.bind("<Key-r>", lambda _e: self.set_tool("razor"))
        self.bind("<Key-s>", lambda _e: self.split_at_playhead())
        self.bind("<Escape>", self._cancel_interaction)
        self.bind("<Left>", lambda event: self._nudge_selected(event, -1))
        self.bind("<Right>", lambda event: self._nudge_selected(event, 1))
        self.bind("<Up>", lambda _e: self._move_selection_slots(-1))
        self.bind("<Down>", lambda _e: self._move_selection_slots(1))
        self.bind("<Home>", lambda _e: self._set_playhead(0))
        self.bind("<End>", lambda _e: self._set_playhead(self.total_ms))

    # ---- track / clip model ---------------------------------------------

    def set_tracks(self, tracks):
        self.tracks = tracks
        self._adopt_clips()
        self._sync_height()
        self._clamp_scroll_y()
        self._redraw()

    def set_clips(self, clips):
        if clips is not self.clips:
            self.clips = clips
            self.undo_stack.clear()
            self.redo_stack.clear()
            self.selection = []
        self._adopt_clips()
        self._prune_selection()
        self._redraw()

    def _adopt_clips(self):
        """Bind every clip to a real track id and a lane that track actually
        has. That covers clips saved before tracks existed (matched by stem)
        and clips left dangling by a removed track, so what the canvas draws is
        always what the mixer will render."""
        for clip in self.clips:
            index = self._track_index_of(clip)
            if index is None:
                continue
            track = self.tracks[index]
            if clip.get("track") != track["id"]:
                clip["track"] = track["id"]
            lane = self._clip_lane(clip, track)
            if clip.get("source") != lane:
                clip["source"] = lane

    def _track_index_of(self, clip):
        track_id = clip.get("track")
        if track_id:
            for i, track in enumerate(self.tracks):
                if track["id"] == track_id:
                    return i
        stem = clip.get("stem")
        for i, track in enumerate(self.tracks):
            if track.get("kind") == "stem" and track.get("stem") == stem:
                return i
        return None

    def _clip_lane(self, clip, track):
        lanes = _track_lanes(track)
        source = clip.get("source", lanes[0])
        return source if source in lanes else lanes[0]

    def _clip_index(self, clip):
        for i, existing in enumerate(self.clips):
            if existing is clip:
                return i
        return -1

    # Clip dicts are mutable and often equal-valued (a duplicate starts out
    # identical to its original), so selection membership is by identity.
    def _in_selection(self, clip):
        return any(existing is clip for existing in self.selection)

    def _prune_selection(self):
        self.selection = [clip for clip in self.selection if self._clip_index(clip) >= 0]

    def _is_silenced(self, track):
        if track.get("mute"):
            return True
        return any(other.get("solo") for other in self.tracks) and not track.get("solo")

    # ---- public controls -------------------------------------------------

    def set_total_ms(self, total_ms):
        old_total = self.total_ms
        was_fit = self.view_span_ms >= old_total - 1
        self.total_ms = max(1000, int(total_ms))
        if was_fit:
            self.fit_view()
            return
        self.view_span_ms = min(self.view_span_ms, float(self.total_ms))
        self._clamp_view()
        self._redraw()

    def set_tool(self, tool):
        if tool not in self.TOOLS:
            return "break"
        self.tool = tool
        if self.on_tool_change:
            self.on_tool_change(tool)
        self.configure(cursor=self.TOOL_CURSORS[tool])
        self.focus_set()
        self._redraw()
        return "break"

    def set_xscrollcommand(self, command):
        self._xscrollcommand = command
        self._update_scrollbars()

    def set_yscrollcommand(self, command):
        self._yscrollcommand = command
        self._update_scrollbars()

    def xview(self, *args):
        if not args:
            return self._view_fractions()
        if args[0] == "moveto":
            self.view_start_ms = float(args[1]) * self.total_ms
        elif args[0] == "scroll":
            amount = int(args[1])
            step = self.view_span_ms * (0.85 if args[2] == "pages" else 0.1)
            self.view_start_ms += amount * step
        self._clamp_view()
        self._redraw()

    def yview(self, *args):
        if not args:
            return self._yview_fractions()
        if args[0] == "moveto":
            self.scroll_y = float(args[1]) * max(1, self._content_h())
        elif args[0] == "scroll":
            step = self.lane_h if args[2] == "units" else self._viewport_h()
            self.scroll_y += int(args[1]) * step
        self._clamp_scroll_y()
        self._redraw()

    def zoom_in(self, center_ms=None):
        self._zoom(0.5, center_ms)

    def zoom_out(self, center_ms=None):
        self._zoom(2.0, center_ms)

    def fit_view(self):
        self.view_start_ms = 0.0
        self.view_span_ms = float(self.total_ms)
        self._redraw()

    @property
    def can_undo(self):
        return bool(self.undo_stack)

    @property
    def can_redo(self):
        return bool(self.redo_stack)

    def undo(self):
        if not self.undo_stack:
            return "break"
        self.redo_stack.append(self._copy_clips())
        self._restore_snapshot(self.undo_stack.pop())
        self._notify_change("Undid edit")
        return "break"

    def redo(self):
        if not self.redo_stack:
            return "break"
        self.undo_stack.append(self._copy_clips())
        self._restore_snapshot(self.redo_stack.pop())
        self._notify_change("Redid edit")
        return "break"

    def delete_selected(self):
        if not self.selection:
            return "break"
        count = len(self.selection)
        self._begin_transaction()
        keep = [clip for clip in self.clips if not self._in_selection(clip)]
        self.clips[:] = keep
        self.selection = []
        self._commit_transaction(f"Deleted {count} clip{'s' if count != 1 else ''}")
        return "break"

    def delete_indices(self, indices):
        valid = sorted({i for i in indices if 0 <= i < len(self.clips)}, reverse=True)
        if not valid:
            return
        self._begin_transaction()
        for index in valid:
            del self.clips[index]
        self._prune_selection()
        self._commit_transaction(f"Deleted {len(valid)} clip{'s' if len(valid) != 1 else ''}")

    def copy_selection(self):
        if not self.selection:
            self._set_status("Nothing selected to copy")
            return "break"
        origin = min(int(clip["start_ms"]) for clip in self.selection)
        self.clipboard = [dict(clip, _offset=int(clip["start_ms"]) - origin) for clip in self.selection]
        self._set_status(f"Copied {len(self.clipboard)} clip{'s' if len(self.clipboard) != 1 else ''}")
        return "break"

    def cut_selection(self):
        if not self.selection:
            return "break"
        self.copy_selection()
        self.delete_selected()
        return "break"

    def paste_clipboard(self):
        if not self.clipboard:
            self._set_status("Clipboard is empty")
            return "break"
        self._begin_transaction()
        pasted = []
        for entry in self.clipboard:
            clip = {key: value for key, value in entry.items() if key != "_offset"}
            length = int(clip["end_ms"]) - int(clip["start_ms"])
            start = int(max(0, min(self.total_ms - length, self.playhead_ms + entry["_offset"])))
            clip["start_ms"] = start
            clip["end_ms"] = start + length
            if self._track_index_of(clip) is None and self.tracks:
                target = self._active_track() or self.tracks[0]
                self._retarget(clip, self.tracks.index(target), _track_lanes(target)[0])
            pasted.append(clip)
        self.clips.extend(pasted)
        self.selection = pasted
        self._commit_transaction(f"Pasted {len(pasted)} clip{'s' if len(pasted) != 1 else ''}")
        return "break"

    def duplicate_selection(self):
        if not self.selection:
            self._set_status("Select a clip first, then Ctrl+D to duplicate it")
            return "break"
        span = max(int(clip["end_ms"]) for clip in self.selection) - min(
            int(clip["start_ms"]) for clip in self.selection
        )
        self._begin_transaction()
        copies = []
        for clip in self.selection:
            copy = dict(clip)
            length = int(copy["end_ms"]) - int(copy["start_ms"])
            start = int(max(0, min(self.total_ms - length, int(copy["start_ms"]) + span)))
            copy["start_ms"] = start
            copy["end_ms"] = start + length
            copies.append(copy)
        self.clips.extend(copies)
        self.selection = copies
        self._commit_transaction(f"Duplicated {len(copies)} clip{'s' if len(copies) != 1 else ''}")
        return "break"

    def split_at_playhead(self):
        playhead = int(round(self.playhead_ms))
        targets = self.selection or self.clips
        crossing = [
            clip for clip in targets
            if int(clip["start_ms"]) + self.MIN_LEN_MS <= playhead <= int(clip["end_ms"]) - self.MIN_LEN_MS
        ]
        if not crossing:
            self._set_status("No clip crosses the playhead (needs 250 ms either side)")
            return "break"
        self._begin_transaction()
        pieces = []
        for clip in crossing:
            pieces.extend(self._split_clip(clip, playhead))
        self.selection = pieces
        self._commit_transaction(f"Split {len(crossing)} clip{'s' if len(crossing) != 1 else ''}")
        return "break"

    def _split_clip(self, clip, split_ms):
        """Replace clip with its two halves; the right half advances its source
        in-point by the cut offset so the audio stays continuous."""
        index = self._clip_index(clip)
        if index < 0:
            return []
        source_start = self._clip_source_start(clip)
        left = {**clip, "end_ms": split_ms, "source_start_ms": source_start, "mode": self._clip_mode(clip)}
        right = {
            **clip, "start_ms": split_ms,
            "source_start_ms": source_start + split_ms - int(clip["start_ms"]),
            "mode": self._clip_mode(clip),
        }
        self.clips[index:index + 1] = [left, right]
        return [left, right]

    # ---- time selection (Audacity's selected region) ---------------------

    def has_region(self):
        return self.sel_end_ms - self.sel_start_ms > 0.5

    def region(self):
        return min(self.sel_start_ms, self.sel_end_ms), max(self.sel_start_ms, self.sel_end_ms)

    def set_region(self, start_ms, end_ms=None, track_ids=None):
        start = max(0.0, min(float(self.total_ms), float(start_ms)))
        end = start if end_ms is None else max(0.0, min(float(self.total_ms), float(end_ms)))
        self.sel_start_ms, self.sel_end_ms = min(start, end), max(start, end)
        self.playhead_ms = self.sel_start_ms
        if track_ids is not None:
            self.selected_track_ids = list(track_ids)
        elif not self.selected_track_ids and self.active_track_id:
            self.selected_track_ids = [self.active_track_id]
        self._redraw()

    def selected_tracks(self):
        """Tracks the edit verbs act on: whatever the last marquee covered,
        falling back to the active track, then to every track."""
        picked = [t for t in self.tracks if t["id"] in self.selected_track_ids]
        if picked:
            return picked
        active = self._active_track()
        return [active] if active else list(self.tracks)

    def select_all(self):
        self.selection = list(self.clips)
        self.selected_track_ids = [track["id"] for track in self.tracks]
        self.sel_start_ms, self.sel_end_ms = 0.0, float(self.total_ms)
        self._set_status(f"Selected all {len(self.selection)} clip(s) over the whole timeline")
        self._redraw()
        return "break"

    def select_none(self):
        self.selection = []
        self.selected_track_ids = []
        self.sel_end_ms = self.sel_start_ms
        self._set_status("Selection cleared")
        self._redraw()
        return "break"

    def _clips_in_region(self, tracks=None, start=None, end=None):
        """Clips of the given tracks that overlap the region, paired with the
        track they live on."""
        if start is None or end is None:
            start, end = self.region()
        ids = {track["id"] for track in (tracks if tracks is not None else self.selected_tracks())}
        found = []
        for clip in self.clips:
            index = self._track_index_of(clip)
            if index is None or self.tracks[index]["id"] not in ids:
                continue
            if int(clip["end_ms"]) > start and int(clip["start_ms"]) < end:
                found.append((clip, self.tracks[index]))
        return found

    def _split_at(self, clip, split_ms):
        """Split in place if the cut is strictly inside the clip. Returns the
        pieces, or the original clip when the cut lands on an edge."""
        split_ms = int(round(split_ms))
        if not clip["start_ms"] < split_ms < clip["end_ms"]:
            return [clip]
        return self._split_clip(clip, split_ms)

    def _split_edges(self, clips, start, end):
        """Split every clip at both region edges and return the pieces that
        land inside the region."""
        inside = []
        for clip in list(clips):
            for piece in self._split_at(clip, start):
                for part in self._split_at(piece, end):
                    if int(part["start_ms"]) >= start and int(part["end_ms"]) <= end:
                        inside.append(part)
        return inside

    # ---- Audacity edit verbs ---------------------------------------------

    def split_at_selection(self):
        """Split (Ctrl+I): cut selected clips at both edges of the region,
        without removing anything."""
        if not self.has_region():
            return self._require_region()
        start, end = self.region()
        self._begin_transaction()
        inside = self._split_edges([clip for clip, _t in self._clips_in_region()], start, end)
        self.selection = inside
        self._commit_transaction(f"Split at {self._fmt_precise(start)} and {self._fmt_precise(end)}")
        return "break"

    def split_delete_selection(self):
        """Delete and leave gap (Ctrl+Alt+K)."""
        if not self.has_region():
            return self._require_region()
        start, end = self.region()
        self._begin_transaction()
        inside = self._split_edges([clip for clip, _t in self._clips_in_region()], start, end)
        for clip in inside:
            index = self._clip_index(clip)
            if index >= 0:
                del self.clips[index]
        self.selection = []
        self._commit_transaction(f"Deleted {len(inside)} clip section(s), gap left")
        return "break"

    def split_cut_selection(self):
        """Cut and leave gap (Ctrl+Alt+X)."""
        if not self.has_region():
            return self._require_region()
        start, end = self.region()
        self._begin_transaction()
        inside = self._split_edges([clip for clip, _t in self._clips_in_region()], start, end)
        self.selection = inside
        self.copy_selection()
        for clip in inside:
            index = self._clip_index(clip)
            if index >= 0:
                del self.clips[index]
        self.selection = []
        self._commit_transaction(f"Cut {len(inside)} clip section(s), gap left")
        return "break"

    def ripple_delete_selection(self):
        """Delete (Ctrl+K): remove the region and close the gap, pulling every
        later clip on the selected tracks back by the region's length."""
        if not self.has_region():
            return self._require_region()
        start, end = self.region()
        span = end - start
        tracks = self.selected_tracks()
        ids = {track["id"] for track in tracks}
        self._begin_transaction()
        for clip in self._split_edges([clip for clip, _t in self._clips_in_region(tracks)], start, end):
            index = self._clip_index(clip)
            if index >= 0:
                del self.clips[index]
        for clip in self.clips:
            track_index = self._track_index_of(clip)
            if track_index is None or self.tracks[track_index]["id"] not in ids:
                continue
            if int(clip["start_ms"]) >= end:
                clip["start_ms"] = int(clip["start_ms"]) - span
                clip["end_ms"] = int(clip["end_ms"]) - span
        self.selection = []
        self.set_region(start)
        self._commit_transaction(f"Deleted {self._fmt_duration(span)} and closed the gap")
        return "break"

    def trim_to_selection(self):
        """Trim Audio (Ctrl+T): keep only what is inside the region on the
        selected tracks, dropping the rest of those clips."""
        if not self.has_region():
            return self._require_region()
        start, end = self.region()
        tracks = self.selected_tracks()
        ids = {track["id"] for track in tracks}
        self._begin_transaction()
        kept = self._split_edges([clip for clip, _t in self._clips_in_region(tracks)], start, end)
        keep_ids = {id(clip) for clip in kept}
        survivors = []
        for clip in self.clips:
            track_index = self._track_index_of(clip)
            on_track = track_index is not None and self.tracks[track_index]["id"] in ids
            if not on_track or id(clip) in keep_ids:
                survivors.append(clip)
        self.clips[:] = survivors
        self.selection = kept
        self._commit_transaction(f"Trimmed to {self._fmt_precise(start)} - {self._fmt_precise(end)}")
        return "break"

    def silence_selection(self):
        """Silence Audio (Ctrl+L): mute the region on the selected tracks.

        Clip sections inside the region become muted Replace clips, which also
        silences whatever full-song bed plays underneath them.
        """
        if not self.has_region():
            return self._require_region()
        start, end = self.region()
        tracks = self.selected_tracks()
        self._begin_transaction()
        touched = self._split_edges([clip for clip, _t in self._clips_in_region(tracks)], start, end)
        for clip in touched:
            clip["mute"] = True
            clip["mode"] = "replace"
        # A track whose bed still plays needs a muted clip of its own, since
        # there is no clip section there to mute.
        for track in tracks:
            if track.get("bed", "muted") == "muted":
                continue
            lane = _track_lanes(track)[0]
            covered = any(
                self._track_index_of(clip) is not None
                and self.tracks[self._track_index_of(clip)]["id"] == track["id"]
                and int(clip["start_ms"]) <= start and int(clip["end_ms"]) >= end
                and clip.get("mute")
                for clip in self.clips
            )
            if covered:
                continue
            blank = {
                "track": track["id"], "stem": track.get("stem"), "source": lane,
                "start_ms": int(start), "end_ms": int(end), "source_start_ms": 0,
                "mode": "replace", "mute": True,
            }
            self.clips.append(blank)
            touched.append(blank)
        self.selection = touched
        self._commit_transaction(f"Silenced {self._fmt_duration(end - start)}")
        return "break"

    def join_selection(self):
        """Join (Ctrl+J): merge the selected clips of each lane into one clip
        spanning them. Pieces that were split apart rejoin seamlessly; clips
        with unrelated source in-points take the earliest one's timing."""
        groups = {}
        for clip in self.selection:
            index = self._track_index_of(clip)
            if index is None:
                continue
            groups.setdefault((index, self._clip_lane(clip, self.tracks[index])), []).append(clip)
        mergeable = {key: group for key, group in groups.items() if len(group) > 1}
        if not mergeable:
            self._set_status("Join needs two or more selected clips in the same lane")
            return "break"
        self._begin_transaction()
        joined = []
        for group in mergeable.values():
            group.sort(key=lambda clip: int(clip["start_ms"]))
            first = group[0]
            merged = dict(first)
            merged["start_ms"] = int(first["start_ms"])
            merged["end_ms"] = max(int(clip["end_ms"]) for clip in group)
            merged["source_start_ms"] = self._clip_source_start(first)
            for clip in group:
                index = self._clip_index(clip)
                if index >= 0:
                    del self.clips[index]
            self.clips.append(merged)
            joined.append(merged)
        self.selection = joined
        self._commit_transaction(f"Joined into {len(joined)} clip(s)")
        return "break"

    def duplicate_to_new_track(self):
        """Duplicate (Ctrl+D): copy the selected clips onto fresh tracks that
        sit directly below the ones they came from."""
        if not self.selection:
            self._set_status("Select clips first, then Ctrl+D to duplicate them to a new track")
            return "break"
        if not self.on_track_command:
            return self.duplicate_selection()
        self.on_track_command("duplicate-selection-to-track", None)
        return "break"

    def split_to_new_track(self):
        """Split New (Ctrl+Alt+I): move the region's audio onto a new track."""
        if not self.has_region():
            return self._require_region()
        if not self.on_track_command:
            return "break"
        start, end = self.region()
        self._begin_transaction()
        self.selection = self._split_edges([clip for clip, _t in self._clips_in_region()], start, end)
        self._commit_transaction("Split for a new track")
        self.on_track_command("move-selection-to-track", None)
        return "break"

    def toggle_mute_selection(self):
        if not self.selection:
            self._set_status("Select a clip to mute or unmute it")
            return "break"
        target = not all(clip.get("mute") for clip in self.selection)
        self._begin_transaction()
        for clip in self.selection:
            clip["mute"] = target
        self._commit_transaction(("Muted " if target else "Unmuted ") + f"{len(self.selection)} clip(s)")
        return "break"

    def _require_region(self):
        self._set_status("Drag across the timeline to select a region first")
        return "break"

    # ---- navigation -------------------------------------------------------

    def _clip_boundaries(self):
        edges = {0.0, float(self.total_ms)}
        ids = {track["id"] for track in self.selected_tracks()}
        for clip in self.clips:
            index = self._track_index_of(clip)
            if index is not None and self.tracks[index]["id"] in ids:
                edges.update((float(clip["start_ms"]), float(clip["end_ms"])))
        return sorted(edges)

    def cursor_to_boundary(self, direction, extend=False):
        """[ and ]: jump the cursor to the previous/next clip boundary."""
        edges = self._clip_boundaries()
        here = self.sel_end_ms if extend and direction > 0 else self.playhead_ms
        if direction > 0:
            target = next((edge for edge in edges if edge > here + 0.5), edges[-1])
        else:
            target = next((edge for edge in reversed(edges) if edge < here - 0.5), edges[0])
        if extend:
            self.set_region(self.sel_start_ms, target)
        else:
            self.set_region(target)
        self._set_status(f"Cursor {self._fmt_precise(target)}")
        return "break"

    def zoom_to_selection(self):
        if not self.has_region():
            return self._require_region()
        start, end = self.region()
        self.view_span_ms = max(self.MIN_VIEW_MS, end - start)
        self.view_start_ms = start - (self.view_span_ms - (end - start)) / 2
        self._clamp_view()
        self._redraw()
        self._set_status(f"Zoomed to {self._fmt_precise(start)} - {self._fmt_precise(end)}")
        return "break"

    def zoom_normal(self):
        """Ctrl+2: back to a fixed, readable one-minute window."""
        centre = self.playhead_ms
        self.view_span_ms = min(float(self.total_ms), 60000.0)
        self.view_start_ms = centre - self.view_span_ms / 2
        self._clamp_view()
        self._redraw()
        self._set_status("Zoom reset")
        return "break"

    def zoom_toggle(self):
        """Shift+Z: flip between the current zoom and the last one."""
        previous = self._zoom_toggle_span
        self._zoom_toggle_span = (self.view_start_ms, self.view_span_ms)
        if previous is None:
            return self.fit_view() or "break"
        self.view_start_ms, self.view_span_ms = previous
        self._clamp_view()
        self._redraw()
        return "break"

    # ---- track order ------------------------------------------------------

    def move_track(self, track, where):
        """Tracks > Move: up / down / top / bottom, keeping clips attached."""
        if track not in self.tracks:
            return "break"
        index = self.tracks.index(track)
        target = {
            "up": index - 1, "down": index + 1, "top": 0, "bottom": len(self.tracks) - 1,
        }.get(where, index)
        target = max(0, min(len(self.tracks) - 1, target))
        if target == index:
            self._set_status(f"'{track['name']}' is already there")
            return "break"
        self.tracks.insert(target, self.tracks.pop(index))
        self.active_track_id = track["id"]
        self._redraw()
        if self.on_track_command:
            self.on_track_command("reordered", track)
        self._set_status(f"Moved '{track['name']}' {where}")
        return "break"

    # ---- source-palette drag and drop -----------------------------------

    def begin_source_drag(self, source, grab_fraction=0.0):
        try:
            duration = int(float(self.get_clip_length_ms()))
        except (TypeError, ValueError, tk.TclError):
            duration = 8000
        duration = max(self.MIN_LEN_MS, min(self.total_ms, duration))
        self._external_drag = {
            "source": source, "duration_ms": duration,
            "grab_fraction": max(0.0, min(1.0, grab_fraction)), "preview": None,
        }
        self._set_status(f"Dragging {source} clip ({self._fmt_duration(duration)})")

    def drag_source(self, root_x, root_y, state=0):
        if not self._external_drag:
            return
        x = root_x - self.winfo_rootx()
        y = root_y - self.winfo_rooty()
        info = self._lane_info(y)
        if info is None or x < self.HEADER_W or x > self.winfo_width():
            self._external_drag["preview"] = None
            self._feedback = None
            self._redraw()
            return
        index, track, _lane = info
        duration = self._external_drag["duration_ms"]
        raw_start = self._x_to_ms(x) - duration * self._external_drag["grab_fraction"]
        start = self._snap_point(raw_start, state)
        start = max(0.0, min(self.total_ms - duration, start))
        preview = {
            "track": track["id"], "stem": track.get("stem"),
            "start_ms": int(round(start)), "end_ms": int(round(start + duration)),
            "source": self._external_drag["source"], "source_start_ms": 0,
            "mode": self.get_clip_mode(),
        }
        lanes = _track_lanes(track)
        if preview["source"] not in lanes:
            self._external_drag["preview"] = None
            self._set_feedback(x, y, f"{track['name']} has no {preview['source']} lane", None, None)
            self._redraw()
            return
        self._external_drag["preview"] = preview
        self._set_feedback(x, y, f"Drop {self._clip_summary(preview)}", start, index)
        self._redraw()

    def end_source_drag(self, root_x, root_y, state=0):
        if not self._external_drag:
            return
        self.drag_source(root_x, root_y, state)
        preview = self._external_drag.get("preview")
        self._external_drag = None
        self._feedback = None
        if preview:
            self._begin_transaction()
            self.clips.append(preview)
            self.selection = [preview]
            self._commit_transaction(f"Added {preview['source']} clip")
        else:
            self._set_status("Drop cancelled")
            self._redraw()

    # ---- coordinate mapping and viewport --------------------------------

    def _timeline_w(self):
        return max(1, self.winfo_width() - self.HEADER_W)

    def _view_end_ms(self):
        return self.view_start_ms + self.view_span_ms

    def _ms_to_x(self, ms):
        return self.HEADER_W + ((ms - self.view_start_ms) / self.view_span_ms) * self._timeline_w()

    def _x_to_ms(self, x):
        fraction = (x - self.HEADER_W) / self._timeline_w()
        return max(0.0, min(self.total_ms, self.view_start_ms + fraction * self.view_span_ms))

    def _content_h(self):
        return len(self.tracks) * self.lane_h

    def _viewport_h(self):
        return max(self.lane_h, self.winfo_height() - self.RULER_H)

    def _sync_height(self):
        visible = max(1, min(len(self.tracks), self.MAX_VISIBLE_TRACKS))
        wanted = self.RULER_H + visible * self.lane_h
        if int(self.cget("height")) != wanted:
            self.configure(height=wanted)

    def _track_top(self, index):
        return self.RULER_H + index * self.lane_h - self.scroll_y

    def _track_at(self, y):
        if y < self.RULER_H:
            return None
        index = int((y - self.RULER_H + self.scroll_y) // self.lane_h)
        return index if 0 <= index < len(self.tracks) else None

    def _lane_info(self, y):
        """(track_index, track, lane_name) under a canvas y, or None."""
        index = self._track_at(y)
        if index is None:
            return None
        track = self.tracks[index]
        lanes = _track_lanes(track)
        share = self.lane_h / len(lanes)
        slot = int(max(0, min(len(lanes) - 1, (y - self._track_top(index)) // share)))
        return index, track, lanes[slot]

    def _lane_bounds(self, index, lane):
        lanes = _track_lanes(self.tracks[index])
        slot = lanes.index(lane) if lane in lanes else 0
        share = self.lane_h / len(lanes)
        top = self._track_top(index) + slot * share
        return top + 2, top + share - 2

    def _lane_slots(self):
        return [(i, lane) for i, track in enumerate(self.tracks) for lane in _track_lanes(track)]

    def _slot_of(self, clip):
        index = self._track_index_of(clip)
        if index is None:
            return 0
        lane = self._clip_lane(clip, self.tracks[index])
        slots = self._lane_slots()
        for position, slot in enumerate(slots):
            if slot == (index, lane):
                return position
        return 0

    def _retarget(self, clip, track_index, lane):
        track = self.tracks[track_index]
        clip["track"] = track["id"]
        clip["stem"] = track.get("stem")
        clip["source"] = lane

    def _active_track(self):
        for track in self.tracks:
            if track["id"] == self.active_track_id:
                return track
        return self.tracks[0] if self.tracks else None

    def _clamp_view(self):
        self.view_span_ms = max(self.MIN_VIEW_MS, min(float(self.total_ms), self.view_span_ms))
        self.view_start_ms = max(0.0, min(self.total_ms - self.view_span_ms, self.view_start_ms))

    def _clamp_scroll_y(self):
        self.scroll_y = max(0.0, min(max(0.0, self._content_h() - self._viewport_h()), self.scroll_y))

    def _view_fractions(self):
        return (self.view_start_ms / self.total_ms, min(1.0, self._view_end_ms() / self.total_ms))

    def _yview_fractions(self):
        content = max(1, self._content_h())
        return (self.scroll_y / content, min(1.0, (self.scroll_y + self._viewport_h()) / content))

    def _update_scrollbars(self):
        if self._xscrollcommand:
            self._xscrollcommand(*self._view_fractions())
        if self._yscrollcommand:
            self._yscrollcommand(*self._yview_fractions())

    def _zoom(self, factor, center_ms=None):
        old_span = self.view_span_ms
        new_span = max(self.MIN_VIEW_MS, min(float(self.total_ms), old_span * factor))
        if center_ms is None:
            center_ms = self.view_start_ms + old_span / 2
        ratio = (center_ms - self.view_start_ms) / old_span if old_span else 0.5
        self.view_span_ms = new_span
        self.view_start_ms = center_ms - ratio * new_span
        self._clamp_view()
        self._redraw()
        self._set_status(
            f"View {self._fmt_precise(self.view_start_ms)} - {self._fmt_precise(self._view_end_ms())}"
        )

    def _on_zoom_wheel(self, event):
        self._zoom(0.8 if event.delta > 0 else 1.25, self._x_to_ms(event.x))
        return "break"

    def _on_scroll_wheel(self, event):
        self.xview("scroll", -1 if event.delta > 0 else 1, "units")
        return "break"

    def _on_wheel(self, event):
        # Scroll tracks, and stop the event before the window-wide binding
        # scrolls the whole dashboard out from under the pointer.
        self.yview("scroll", -1 if event.delta > 0 else 1, "units")
        return "break"

    # ---- formatting -------------------------------------------------------

    @staticmethod
    def _fmt(ms):
        seconds = int(max(0, ms) // 1000)
        return f"{seconds // 60}:{seconds % 60:02d}"

    @staticmethod
    def _fmt_precise(ms):
        ms = max(0, int(round(ms)))
        seconds, millis = divmod(ms, 1000)
        return f"{seconds // 60}:{seconds % 60:02d}.{millis:03d}"

    def _fmt_duration(self, ms):
        return self._fmt_precise(ms)

    @staticmethod
    def _clip_mode(clip):
        return clip.get("mode", "replace")

    @staticmethod
    def _clip_source_start(clip):
        return int(clip.get("source_start_ms", clip["start_ms"]))

    def _range_text(self, clip):
        duration = clip["end_ms"] - clip["start_ms"]
        return (
            f"{self._fmt_precise(clip['start_ms'])} - {self._fmt_precise(clip['end_ms'])}"
            f"  ({self._fmt_duration(duration)})"
        )

    def _clip_summary(self, clip):
        duration = int(clip["end_ms"] - clip["start_ms"])
        source_start = self._clip_source_start(clip)
        index = self._track_index_of(clip)
        track_name = self.tracks[index]["name"] if index is not None else "?"
        return (
            f"{track_name} | {clip.get('source', 'primary').capitalize()} | "
            f"{self._clip_mode(clip).capitalize()} | Timeline {self._range_text(clip)} | "
            f"Source {self._fmt_precise(source_start)} - {self._fmt_precise(source_start + duration)}"
        )

    # ---- snapping and hit testing ----------------------------------------

    def _snap_mode(self, event=None):
        """Audacity's three snapping modes. Alt always bypasses snapping so a
        fine adjustment is one modifier away."""
        state = event if isinstance(event, int) else getattr(event, "state", 0)
        if state & self.ALT_MASK:
            return "off"
        mode = self.get_snap_enabled()
        if isinstance(mode, bool):  # older callers passed a plain on/off flag
            return "nearest" if mode else "off"
        return mode if mode in ("off", "nearest", "prior") else "off"

    def _snap_active(self, event=None):
        return self._snap_mode(event) != "off"

    def _snap_point(self, ms, event=None, exclude=None):
        ms = max(0.0, min(self.total_ms, ms))
        mode = self._snap_mode(event)
        if mode == "off":
            return ms
        candidates = [round(ms / 1000) * 1000, 0, self.total_ms, self.playhead_ms]
        excluded = exclude if isinstance(exclude, (list, tuple)) else ([exclude] if exclude else [])
        for clip in self.clips:
            if any(clip is item for item in excluded):
                continue
            candidates.extend((clip["start_ms"], clip["end_ms"]))
        if mode == "prior":
            # Snap back to the closest boundary at or before the pointer.
            earlier = [c for c in candidates if c <= ms] or [0]
            return float(max(earlier))
        nearest = min(candidates, key=lambda candidate: abs(candidate - ms))
        threshold = max(25.0, min(250.0, self.view_span_ms / self._timeline_w() * 8))
        return float(nearest) if abs(nearest - ms) <= threshold else ms

    def _snap_move(self, start, length, event=None, exclude=None):
        if not self._snap_active(event):
            return start
        snapped_start = self._snap_point(start, event, exclude)
        snapped_end = self._snap_point(start + length, event, exclude) - length
        return snapped_start if abs(snapped_start - start) <= abs(snapped_end - start) else snapped_end

    def _clips_in_lane(self, index, lane):
        track = self.tracks[index]
        return [
            clip for clip in self.clips
            if self._track_index_of(clip) == index and self._clip_lane(clip, track) == lane
        ]

    def _hit_test(self, event):
        if event.x < self.HEADER_W:
            return None
        info = self._lane_info(event.y)
        if info is None:
            return None
        index, _track, lane = info
        ms = self._x_to_ms(event.x)
        edge_ms = self.EDGE_PX / self._timeline_w() * self.view_span_ms
        candidates = self._clips_in_lane(index, lane)
        # Handles win over interiors so both sides of an edge are grabbable.
        for clip in reversed(candidates):
            if abs(ms - clip["start_ms"]) <= edge_ms:
                return "resize-left", clip
            if abs(ms - clip["end_ms"]) <= edge_ms:
                return "resize-right", clip
        for clip in reversed(candidates):
            if clip["start_ms"] <= ms <= clip["end_ms"]:
                return "move", clip
        return None

    # ---- history ---------------------------------------------------------

    def _copy_clips(self):
        return [dict(clip) for clip in self.clips]

    def _restore_snapshot(self, snapshot):
        self.clips[:] = [dict(clip) for clip in snapshot]
        self.selection = []
        self._redraw()

    def _begin_transaction(self):
        if self._transaction_before is None:
            self._transaction_before = self._copy_clips()

    def _commit_transaction(self, message):
        before = self._transaction_before
        self._transaction_before = None
        after = self._copy_clips()
        if before is None or before == after:
            self._redraw()
            return False
        self.undo_stack.append(before)
        del self.undo_stack[:-self.HISTORY_LIMIT]
        self.redo_stack.clear()
        self._notify_change(message)
        return True

    def _notify_change(self, message):
        self._redraw()
        self._set_status(message)
        if self.on_change:
            self.on_change()

    # ---- drawing ---------------------------------------------------------

    def _tick_interval(self):
        target = self.view_span_ms / 7
        for step in (100, 250, 500, 1000, 2000, 5000, 10000, 15000, 30000, 60000, 120000):
            if step >= target:
                return step
        return 300000

    def _ticks(self):
        step = self._tick_interval()
        minor_step = max(25, step // 4)
        tick = int(self.view_start_ms // minor_step) * minor_step
        while tick <= self._view_end_ms() + minor_step:
            if tick >= self.view_start_ms:
                yield tick, self._ms_to_x(tick), tick % step == 0
            tick += minor_step

    def _redraw(self):
        self.delete("all")
        self._header_hits = []
        w = max(1, self.winfo_width())
        h = max(1, self.winfo_height())
        self.create_rectangle(0, self.RULER_H, w, h, fill=PLAYLIST_BG, outline="")

        visible = [
            (index, track) for index, track in enumerate(self.tracks)
            if self._track_top(index) + self.lane_h >= self.RULER_H and self._track_top(index) <= h
        ]
        for index, track in visible:
            self._draw_track_body(index, track, w)
        for _tick, x, major in self._ticks():
            self.create_line(x, self.RULER_H, x, h, fill=GRID_MAJOR if major else GRID_MINOR)

        for clip in self.clips:
            self._draw_clip(clip, selected=self._in_selection(clip))
        if self._external_drag and self._external_drag.get("preview"):
            self._draw_clip(self._external_drag["preview"], ghost=True)

        for index, track in visible:
            if self._is_silenced(track):
                top = self._track_top(index)
                self.create_rectangle(
                    self.HEADER_W, top, w, top + self.lane_h,
                    fill=PLAYLIST_BG, outline="", stipple="gray50",
                )
        self._draw_region(visible, h)
        if self._marquee:
            x0, y0, x1, y1 = self._marquee
            self.create_rectangle(
                min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1),
                outline=CLIP_SELECTED, width=1, dash=(3, 3),
            )
        for index, track in visible:
            self._draw_track_header(index, track)

        self._draw_ruler(w)
        self.create_line(self.HEADER_W, 0, self.HEADER_W, h, fill=BORDER_HI)
        self._draw_playhead(w, h)
        if self._feedback:
            x, y, text, guide_ms, track_index = self._feedback
            if guide_ms is not None and track_index is not None:
                guide_x = self._ms_to_x(guide_ms)
                top = self._track_top(track_index)
                self.create_line(guide_x, top, guide_x, top + self.lane_h, fill=CLIP_SELECTED, width=2)
            self._draw_feedback(x, y, text)
        if self.on_region:
            self.on_region(*self.region())
        self._update_scrollbars()

    def _draw_region(self, visible, h):
        """Tint the selected time range over the tracks it applies to, and
        mirror it in the ruler the way Audacity's timeline does."""
        if not self.has_region():
            return
        start, end = self.region()
        x0, x1 = self._ms_to_x(start), self._ms_to_x(end)
        if x1 < self.HEADER_W or x0 > self.winfo_width():
            return
        x0, x1 = max(self.HEADER_W, x0), min(self.winfo_width(), x1)
        applies = {track["id"] for track in self.selected_tracks()}
        for index, track in visible:
            if track["id"] not in applies:
                continue
            top = self._track_top(index)
            self.create_rectangle(
                x0, max(self.RULER_H, top), x1, min(h, top + self.lane_h),
                fill=REGION_FILL, outline="", stipple="gray25",
            )
        for x in (x0, x1):
            self.create_line(x, self.RULER_H, x, h, fill=REGION_EDGE)

    def _draw_track_body(self, index, track, w):
        top = self._track_top(index)
        bottom = top + self.lane_h
        self.create_rectangle(
            self.HEADER_W, top, w, bottom,
            fill=TRACK_BG if index % 2 == 0 else TRACK_BG_ALT, outline="",
        )
        lanes = _track_lanes(track)
        share = self.lane_h / len(lanes)
        bed = track.get("bed", "muted")
        for slot, lane in enumerate(lanes):
            if slot:
                self.create_line(self.HEADER_W, top + slot * share, w, top + slot * share, fill="#212832")
            if bed == lane or (bed == "both" and lane in STEM_LANES):
                self._draw_bed(index, lane)
        self.create_line(0, bottom, w, bottom, fill="#05070a")

    def _draw_bed(self, index, lane):
        top, bottom = self._lane_bounds(index, lane)
        x0, x1 = self.HEADER_W + 1, self.winfo_width() - 1
        self.create_rectangle(x0, top, x1, bottom, fill=BED_COLORS[lane], outline="#495261", dash=(2, 4))
        self.create_text(
            x0 + 7, (top + bottom) / 2, text=f"BED  {lane.upper()}", anchor="w",
            fill="#98a2b1", font=("Segoe UI", 7, "bold"),
        )

    def _draw_track_header(self, index, track):
        top = self._track_top(index)
        bottom = top + self.lane_h
        active = track["id"] == self.active_track_id
        self.create_rectangle(
            0, top, self.HEADER_W, bottom,
            fill=TRACK_HEADER_ACTIVE if active else TRACK_HEADER_BG, outline="",
        )
        self.create_line(0, bottom, self.HEADER_W, bottom, fill="#05070a")
        accent = LANE_TINTS["import"] if track.get("kind") == "audio" else ACCENT
        self.create_rectangle(0, top + 1, 3, bottom - 1, fill=accent, outline="")

        name = track.get("name") or "Track"
        if len(name) > 17:
            name = name[:16] + ".."
        self.create_text(
            10, top + 12, text=name, anchor="w",
            fill=MUTED if self._is_silenced(track) else TEXT, font=("Segoe UI", 8, "bold"),
        )
        self.create_text(
            10, top + 26, text=f"BED {track.get('bed', 'muted').upper()}", anchor="w",
            fill=MUTED, font=("Segoe UI", 7),
        )
        self._draw_header_button("M", 10, top + 36, track, "mute", ERROR)
        self._draw_header_button("S", 34, top + 36, track, "solo", ACCENT)
        gain = float(track.get("gain_db", 0.0) or 0.0)
        if abs(gain) > 0.05:
            self.create_text(60, top + 43, text=f"{gain:+.1f} dB", anchor="w", fill=MUTED, font=("Consolas", 7))

        self.create_line(self.HEADER_W - 22, top, self.HEADER_W - 22, bottom, fill="#20262f")
        lanes = _track_lanes(track)
        share = self.lane_h / len(lanes)
        for slot, lane in enumerate(lanes):
            self.create_text(
                self.HEADER_W - 11, top + share * (slot + 0.5), text=LANE_LABELS[lane],
                fill=LANE_TINTS[lane], font=("Segoe UI", 8, "bold"),
            )

    def _draw_header_button(self, label, x, y, track, key, color, w=20, h=14):
        on = bool(track.get(key))
        self.create_rectangle(
            x, y, x + w, y + h, fill=color if on else SURFACE_ALT,
            outline=color if on else BORDER_HI,
        )
        self.create_text(
            x + w / 2, y + h / 2, text=label,
            fill=ACCENT_TEXT if on else MUTED, font=("Segoe UI", 7, "bold"),
        )
        self._header_hits.append((x, y, x + w, y + h, key, track))

    def _draw_ruler(self, w):
        self.create_rectangle(0, 0, w, self.RULER_H, fill=TRACK_HEADER_BG, outline="")
        self.create_line(0, self.RULER_H, w, self.RULER_H, fill=BORDER_HI)
        self.create_text(
            10, self.RULER_H / 2, text="PLAYLIST", anchor="w", fill=TEXT, font=("Segoe UI", 8, "bold")
        )
        self.create_text(
            self.HEADER_W - 8, self.RULER_H / 2, text=self.tool.upper(), anchor="e",
            fill=ACCENT, font=("Segoe UI", 7, "bold"),
        )
        if self.has_region():
            start, end = self.region()
            rx0, rx1 = max(self.HEADER_W, self._ms_to_x(start)), min(w, self._ms_to_x(end))
            if rx1 > rx0:
                self.create_rectangle(rx0, self.RULER_H - 7, rx1, self.RULER_H,
                                      fill=REGION_EDGE, outline="")
        for tick, x, major in self._ticks():
            self.create_line(x, self.RULER_H - (9 if major else 5), x, self.RULER_H,
                             fill=GRID_MAJOR if major else GRID_MINOR)
            if major:
                self.create_text(x + 3, 4, text=self._fmt_precise(tick), anchor="nw",
                                 fill="#aeb7c4", font=("Consolas", 7))

    def _draw_playhead(self, w, h):
        x = self._ms_to_x(self.playhead_ms)
        if not self.HEADER_W <= x <= w:
            return
        self.create_line(x, self.RULER_H - 2, x, h, fill=PLAYHEAD_COLOR, width=2)
        self.create_polygon(
            x - 5, self.RULER_H - 10, x + 5, self.RULER_H - 10, x, self.RULER_H - 2,
            fill=PLAYHEAD_COLOR, outline="",
        )

    def _draw_clip(self, clip, selected=False, ghost=False):
        index = self._track_index_of(clip)
        if index is None:
            return
        track = self.tracks[index]
        lane = self._clip_lane(clip, track)
        top, bottom = self._lane_bounds(index, lane)
        if bottom < self.RULER_H or top > self.winfo_height():
            return
        x0, x1 = self._ms_to_x(clip["start_ms"]), self._ms_to_x(clip["end_ms"])
        if x1 < self.HEADER_W or x0 > self.winfo_width():
            return
        x0 = max(self.HEADER_W, x0)
        x1 = min(self.winfo_width(), x1)
        muted = bool(clip.get("mute"))
        color = MUTED_CLIP if muted else CLIP_COLORS.get(lane, CLIP_COLORS["primary"])
        outline = CLIP_SELECTED if selected or ghost else MODE_COLORS[
            "layer" if self._clip_mode(clip) == "layer" else "replace"
        ]
        item = _rounded_rect(
            self, x0, top, max(x0 + 3, x1), bottom, radius=3,
            fill=color, outline=outline, width=2 if selected or ghost else 1,
        )
        if ghost or muted:
            self.itemconfigure(item, stipple="gray50")
        if muted and x1 - x0 > 40:
            self.create_text((x0 + x1) / 2, (top + bottom) / 2, text="SILENCED",
                             fill=TEXT, font=("Segoe UI", 7, "bold"))
            return
        if selected and x1 - x0 > 16:
            self.create_line(x0 + 4, top + 4, x0 + 4, bottom - 4, fill="white", width=2)
            self.create_line(x1 - 4, top + 4, x1 - 4, bottom - 4, fill="white", width=2)
        wave_mid = (top + bottom) / 2 + 3
        wave_x = int(x0) + 5
        wave_index = 0
        while wave_x < x1 - 4:
            amplitude = 2 + ((wave_index * 7 + index * 3) % 8) / 2
            self.create_line(wave_x, wave_mid - amplitude, wave_x, wave_mid + amplitude,
                             fill="#ffffff", stipple="gray50")
            wave_x += 5
            wave_index += 1
        if x1 - x0 > 68:
            label = f"{self._clip_mode(clip).upper()}  IN {self._fmt_precise(self._clip_source_start(clip))}"
            self.create_text(x0 + 7, top + 3, text=label, anchor="nw", fill="white",
                             font=("Segoe UI", 7, "bold"))

    def _draw_feedback(self, x, y, text):
        use_right_anchor = x > self.winfo_width() * 0.62
        x = x - 12 if use_right_anchor else x + 12
        x = max(self.HEADER_W + 4, min(self.winfo_width() - 8, x))
        y = max(self.RULER_H + 4, min(self.winfo_height() - 24, y - 24))
        text_id = self.create_text(
            x, y, text=text, anchor="ne" if use_right_anchor else "nw",
            fill=TEXT, font=("Segoe UI", 8, "bold"),
        )
        box = self.bbox(text_id)
        if box:
            pad = 4
            bg = self.create_rectangle(
                box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad,
                fill=SURFACE_HI, outline=BORDER_HI,
            )
            self.tag_lower(bg, text_id)

    # ---- mouse interaction ----------------------------------------------

    def _on_press(self, event):
        self.focus_set()
        if event.x < self.HEADER_W:
            self._press_header(event)
            return
        if event.y < self.RULER_H:
            # Dragging the ruler selects a time range, as in Audacity's
            # timeline; a plain click collapses it to the cursor.
            self._drag_mode = "scrub"
            self._drag_anchor_ms = self._snap_point(self._x_to_ms(event.x), event)
            self.set_region(self._drag_anchor_ms)
            self._set_status(f"Cursor {self._fmt_precise(self._drag_anchor_ms)}")
            return
        info = self._lane_info(event.y)
        if info is None:
            self._drag_mode = None
            return
        index, track, lane = info
        self.active_track_id = track["id"]
        hit = self._hit_test(event)
        if self.tool == "razor":
            self._razor_at(event)
            return
        if hit is None:
            if self.tool == "draw":
                self._start_create(event, index, track, lane)
            else:
                self.selection = []
                self._drag_mode = "marquee"
                self._marquee = (event.x, event.y, event.x, event.y)
                self._redraw()
            return
        self._start_clip_drag(event, hit)

    def _start_create(self, event, index, track, lane):
        self._begin_transaction()
        anchor = self._snap_point(self._x_to_ms(event.x), event)
        clip = {
            "track": track["id"], "stem": track.get("stem"),
            "start_ms": anchor, "end_ms": anchor, "source": lane,
            "source_start_ms": 0, "mode": self.get_clip_mode(),
        }
        self.clips.append(clip)
        self.selection = [clip]
        self._drag_mode = "create"
        self._drag_anchor_ms = anchor
        self._drag_ref = clip
        self._drag_clips = [(clip, dict(clip))]
        self._redraw()

    def _start_clip_drag(self, event, hit):
        kind, clip = hit
        state = getattr(event, "state", 0)
        if state & self.CTRL_MASK:
            if self._in_selection(clip):
                self.selection = [other for other in self.selection if other is not clip]
                self._drag_mode = None
                self._redraw()
                return
            self.selection.append(clip)
        elif state & self.SHIFT_MASK:
            if not self._in_selection(clip):
                self.selection.append(clip)
        elif not self._in_selection(clip):
            self.selection = [clip]

        self._begin_transaction()
        duplicating = bool(state & self.ALT_MASK) and kind == "move"
        if duplicating:
            copies = [dict(existing) for existing in self.selection]
            grabbed_at = next((i for i, existing in enumerate(self.selection) if existing is clip), 0)
            self.clips.extend(copies)
            self.selection = copies
            clip = copies[grabbed_at]
            self._set_status("Alt-drag: dragging a copy")

        group = self.selection if kind == "move" else [clip]
        self._drag_mode = kind
        self._drag_ref = clip
        self._drag_ref_before = dict(clip)
        self._drag_clips = [(existing, dict(existing)) for existing in group]
        self._drag_grab_offset_ms = self._x_to_ms(event.x) - clip["start_ms"]
        self._drag_slot_anchor = self._slot_of(clip)
        self._redraw()

    def _on_drag(self, event):
        if self._drag_mode is None:
            return
        if self._drag_mode == "scrub":
            edge = self._snap_point(self._x_to_ms(event.x), event)
            self.set_region(self._drag_anchor_ms, edge)
            self._set_status(
                f"Region {self._fmt_precise(min(self._drag_anchor_ms, edge))} - "
                f"{self._fmt_precise(max(self._drag_anchor_ms, edge))}"
            )
            return
        if self._drag_mode == "marquee":
            self._marquee = (self._marquee[0], self._marquee[1], event.x, event.y)
            self._redraw()
            return
        ms = self._x_to_ms(event.x)
        if self._drag_mode == "create":
            clip = self._drag_ref
            edge = self._snap_point(ms, event, clip)
            clip["start_ms"] = min(self._drag_anchor_ms, edge)
            clip["end_ms"] = max(self._drag_anchor_ms, edge)
        elif self._drag_mode == "move":
            self._drag_move(event, ms)
        elif self._drag_mode == "resize-left":
            clip, before = self._drag_clips[0]
            edge = self._snap_point(ms, event, clip)
            original_start = int(before["start_ms"])
            original_source = self._clip_source_start(before)
            earliest = max(0, original_start - original_source)
            new_start = max(earliest, min(edge, clip["end_ms"] - self.MIN_LEN_MS))
            clip["start_ms"] = new_start
            clip["source_start_ms"] = original_source + int(round(new_start - original_start))
            clip["mode"] = self._clip_mode(before)
        elif self._drag_mode == "resize-right":
            clip, before = self._drag_clips[0]
            edge = self._snap_point(ms, event, clip)
            clip["end_ms"] = min(self.total_ms, max(edge, clip["start_ms"] + self.MIN_LEN_MS))
            clip["source_start_ms"] = self._clip_source_start(before)
            clip["mode"] = self._clip_mode(before)
        reference = self._drag_ref if self._drag_ref is not None else self._drag_clips[0][0]
        self._set_feedback(
            event.x, event.y, self._clip_summary(reference), ms, self._track_index_of(reference)
        )
        self._redraw()

    def _drag_move(self, event, ms):
        """Move every selected clip by one shared timeline delta and one shared
        lane delta, so a multi-clip drag keeps the group's internal layout."""
        before_ref = self._drag_ref_before
        length = int(before_ref["end_ms"]) - int(before_ref["start_ms"])
        raw_start = ms - self._drag_grab_offset_ms
        exclude = [clip for clip, _snapshot in self._drag_clips]
        new_start = self._snap_move(raw_start, length, event, exclude)
        delta = new_start - int(before_ref["start_ms"])
        lowest = min(int(snapshot["start_ms"]) for _clip, snapshot in self._drag_clips)
        highest = max(int(snapshot["end_ms"]) for _clip, snapshot in self._drag_clips)
        delta = max(-lowest, min(self.total_ms - highest, delta))

        slots = self._lane_slots()
        info = self._lane_info(event.y)
        slot_delta = 0
        if info is not None and slots:
            index, _track, lane = info
            target = next(
                (position for position, slot in enumerate(slots) if slot == (index, lane)),
                self._drag_slot_anchor,
            )
            slot_delta = target - self._drag_slot_anchor
            starts = [self._slot_of(clip) for clip, _snapshot in self._drag_clips]
            slot_delta = max(-min(starts), min(len(slots) - 1 - max(starts), slot_delta))

        for clip, snapshot in self._drag_clips:
            start = int(snapshot["start_ms"]) + delta
            clip["start_ms"] = start
            clip["end_ms"] = start + int(snapshot["end_ms"]) - int(snapshot["start_ms"])
            clip["source_start_ms"] = self._clip_source_start(snapshot)
            clip["mode"] = self._clip_mode(snapshot)
            if slot_delta and slots:
                position = max(0, min(len(slots) - 1, self._slot_of(clip) + slot_delta))
                self._retarget(clip, *slots[position])

    def _on_release(self, _event):
        if self._drag_mode is None:
            return
        mode = self._drag_mode
        self._drag_mode = None
        self._feedback = None
        if mode == "scrub":
            self._redraw()
            return
        if mode == "marquee":
            self._finish_marquee()
            return
        if mode == "create":
            clip = self._drag_ref
            if clip["end_ms"] - clip["start_ms"] < self.MIN_LEN_MS:
                index = self._clip_index(clip)
                if index >= 0:
                    del self.clips[index]
                self.selection = []
        for clip, _snapshot in self._drag_clips:
            if self._clip_index(clip) >= 0:
                clip["start_ms"] = int(round(clip["start_ms"]))
                clip["end_ms"] = int(round(clip["end_ms"]))
        self._drag_clips = []
        self._drag_ref = None
        self._drag_ref_before = None
        self._commit_transaction({
            "create": "Created clip", "move": "Moved clip",
            "resize-left": "Trimmed clip start", "resize-right": "Trimmed clip end",
        }.get(mode, "Edited clip"))

    def _finish_marquee(self):
        """A marquee does double duty, as in Audacity: it sets the time region
        and the tracks the edit verbs apply to, and it selects the clips it
        touched so the clip-level commands have something to work on."""
        x0, y0, x1, y1 = self._marquee
        self._marquee = None
        left, right = sorted((self._x_to_ms(x0), self._x_to_ms(x1)))
        top, bottom = sorted((y0, y1))
        picked = []
        track_ids = []
        for index, track in enumerate(self.tracks):
            track_top = self._track_top(index)
            if track_top + self.lane_h >= top and track_top <= bottom:
                track_ids.append(track["id"])
        for clip in self.clips:
            index = self._track_index_of(clip)
            if index is None:
                continue
            lane_top, lane_bottom = self._lane_bounds(index, self._clip_lane(clip, self.tracks[index]))
            if lane_bottom < top or lane_top > bottom:
                continue
            if clip["end_ms"] >= left and clip["start_ms"] <= right:
                picked.append(clip)
        self.selection = picked
        self._transaction_before = None
        self.set_region(left, right, track_ids)
        self._set_status(
            f"Selected {self._fmt_precise(left)} - {self._fmt_precise(right)} "
            f"on {len(track_ids)} track(s), {len(picked)} clip(s)"
        )

    def _razor_at(self, event):
        hit = self._hit_test(event)
        if hit is None:
            self._set_status("Razor: click inside a clip to split it")
            return
        clip = hit[1]
        split_ms = int(round(self._snap_point(self._x_to_ms(event.x), event, clip)))
        if split_ms - clip["start_ms"] < self.MIN_LEN_MS or clip["end_ms"] - split_ms < self.MIN_LEN_MS:
            self._set_status("Cut must leave at least 250 ms on both sides")
            return
        self._begin_transaction()
        pieces = self._split_clip(clip, split_ms)
        self.selection = pieces[-1:]
        self._feedback = None
        self._commit_transaction(f"Cut clip at {self._fmt_precise(split_ms)}")

    def _press_header(self, event):
        for x0, y0, x1, y1, key, track in self._header_hits:
            if x0 <= event.x <= x1 and y0 <= event.y <= y1:
                track[key] = not track.get(key)
                self.active_track_id = track["id"]
                self._redraw()
                if self.on_track_command:
                    self.on_track_command(key, track)
                return
        index = self._track_at(event.y)
        if index is not None:
            self.active_track_id = self.tracks[index]["id"]
        self._drag_mode = None
        self._redraw()

    def _on_double_click(self, event):
        if event.x < self.HEADER_W:
            index = self._track_at(event.y)
            if index is not None and self.on_track_command:
                self.on_track_command("rename", self.tracks[index])
            return "break"
        hit = self._hit_test(event)
        if hit is not None:
            clip = hit[1]
            self._begin_transaction()
            clip["mode"] = "replace" if self._clip_mode(clip) == "layer" else "layer"
            self.selection = [clip]
            self._commit_transaction(f"Clip set to {clip['mode'].capitalize()}")
        return "break"

    def _on_right_click(self, event):
        self.focus_set()
        if event.x < self.HEADER_W:
            index = self._track_at(event.y)
            if index is not None:
                self.active_track_id = self.tracks[index]["id"]
                self._redraw()
                self._popup(self._build_track_menu(self.tracks[index]), event)
            return "break"
        info = self._lane_info(event.y)
        if info is None:
            return "break"
        self.active_track_id = info[1]["id"]
        hit = self._hit_test(event)
        if hit is not None and not self._in_selection(hit[1]):
            self.selection = [hit[1]]
            self._redraw()
        self._popup(self._build_clip_menu(hit[1] if hit else None), event)
        return "break"

    def _new_menu(self):
        return tk.Menu(
            self, tearoff=0, bg=SURFACE_ALT, fg=TEXT, activebackground=ACCENT,
            activeforeground=ACCENT_TEXT, bd=0, relief="flat",
            disabledforeground=MUTED,
        )

    def _popup(self, menu, event):
        if self._menu is not None:
            self._menu.destroy()
        self._menu = menu
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _build_clip_menu(self, clip):
        menu = self._new_menu()
        has_selection = bool(self.selection)
        menu.add_command(label="Duplicate\tCtrl+D", command=self.duplicate_selection,
                         state="normal" if has_selection else "disabled")
        menu.add_command(label="Copy\tCtrl+C", command=self.copy_selection,
                         state="normal" if has_selection else "disabled")
        menu.add_command(label="Cut\tCtrl+X", command=self.cut_selection,
                         state="normal" if has_selection else "disabled")
        menu.add_command(label="Paste at playhead\tCtrl+V", command=self.paste_clipboard,
                         state="normal" if self.clipboard else "disabled")
        menu.add_separator()
        region = "normal" if self.has_region() else "disabled"
        menu.add_command(label="Split at playhead\tS", command=self.split_at_playhead)
        menu.add_command(label="Split at region edges\tCtrl+I", command=self.split_at_selection,
                         state=region)
        menu.add_command(label="Trim to region\tCtrl+T", command=self.trim_to_selection, state=region)
        menu.add_command(label="Silence region\tCtrl+L", command=self.silence_selection, state=region)
        menu.add_command(label="Delete region, close gap\tCtrl+K",
                         command=self.ripple_delete_selection, state=region)
        menu.add_command(label="Delete region, leave gap\tCtrl+Alt+K",
                         command=self.split_delete_selection, state=region)
        menu.add_command(label="Join selected clips\tCtrl+J", command=self.join_selection,
                         state="normal" if len(self.selection) > 1 else "disabled")
        menu.add_separator()
        if clip is not None:
            other = "Replace" if self._clip_mode(clip) == "layer" else "Layer"
            menu.add_command(label=f"Set to {other}", command=lambda: self._set_mode(clip, other.lower()))
        menu.add_command(label="Mute / unmute clips\tCtrl+M", command=self.toggle_mute_selection,
                         state="normal" if has_selection else "disabled")
        menu.add_separator()
        menu.add_command(label="Select all\tCtrl+A", command=self.select_all)
        menu.add_command(label="Select none\tCtrl+Shift+A", command=self.select_none)
        menu.add_command(label="Delete\tDel", command=self.delete_selected,
                         state="normal" if has_selection else "disabled")
        return menu

    def _build_track_menu(self, track):
        menu = self._new_menu()
        menu.add_command(label=f"{track['name']}", state="disabled")
        menu.add_separator()
        beds = ("import", "muted") if track.get("kind") == "audio" else ("primary", "secondary", "both", "muted")
        bed_menu = self._new_menu()
        for bed in beds:
            bed_menu.add_command(
                label=bed.capitalize(),
                command=lambda value=bed: self._track_command(f"bed:{value}", track),
            )
        menu.add_cascade(label="Full-song bed", menu=bed_menu)
        menu.add_command(label="Rename...", command=lambda: self._track_command("rename", track))
        menu.add_separator()
        move_menu = self._new_menu()
        for label, where in (("Up", "up"), ("Down", "down"), ("To top", "top"), ("To bottom", "bottom")):
            move_menu.add_command(label=label, command=lambda w=where: self.move_track(track, w))
        menu.add_cascade(label="Move track", menu=move_menu)
        menu.add_command(label="Duplicate track", command=lambda: self._track_command("duplicate", track))
        menu.add_command(label="Remove track", command=lambda: self._track_command("remove", track))
        return menu

    def _track_command(self, action, track):
        if self.on_track_command:
            self.on_track_command(action, track)

    def _set_mode(self, clip, mode):
        self._begin_transaction()
        clip["mode"] = mode
        self._commit_transaction(f"Clip set to {mode.capitalize()}")

    def _on_hover(self, event):
        if self._drag_mode is not None or self._external_drag:
            return
        if event.x < self.HEADER_W:
            self._feedback = None
            self.configure(cursor="hand2" if event.y >= self.RULER_H else "")
            self._redraw()
            return
        if event.y < self.RULER_H:
            ms = self._snap_point(self._x_to_ms(event.x), event)
            self.configure(cursor="hand2")
            self._set_feedback(event.x, self.RULER_H + 24, f"Playhead {self._fmt_precise(ms)}")
            self._redraw()
            return
        info = self._lane_info(event.y)
        if info is None:
            self._feedback = None
            self.configure(cursor=self.TOOL_CURSORS[self.tool])
            self._redraw()
            return
        index, _track, _lane = info
        hit = self._hit_test(event)
        ms = self._snap_point(self._x_to_ms(event.x), event, hit[1] if hit else None)
        if self.tool == "razor":
            self.configure(cursor="X_cursor")
            text = f"Cut at {self._fmt_precise(ms)}" if hit else f"{self._fmt_precise(ms)}  (no clip)"
            self._set_feedback(event.x, event.y, text, ms, index)
        elif hit is None:
            self.configure(cursor=self.TOOL_CURSORS[self.tool])
            text = (
                f"Draw from {self._fmt_precise(ms)}" if self.tool == "draw"
                else f"{self._fmt_precise(ms)}  (drag to marquee-select)"
            )
            self._set_feedback(event.x, event.y, text, ms, index)
        else:
            kind, clip = hit
            self.configure(cursor="fleur" if kind == "move" else "sb_h_double_arrow")
            self._set_feedback(event.x, event.y, self._clip_summary(clip), ms, index)
        self._redraw()

    def _on_leave(self, _event):
        if not self._external_drag:
            self._feedback = None
            self.configure(cursor=self.TOOL_CURSORS[self.tool])
            self._redraw()

    def _cancel_interaction(self, _event=None):
        if self._transaction_before is not None:
            self._restore_snapshot(self._transaction_before)
        self._transaction_before = None
        self._drag_mode = None
        self._drag_clips = []
        self._drag_ref = None
        self._drag_ref_before = None
        self._external_drag = None
        self._marquee = None
        self._feedback = None
        self._set_status("Edit cancelled")
        self._redraw()
        return "break"

    def _set_feedback(self, x, y, text, guide_ms=None, track_index=None):
        self._feedback = (x, y, text, guide_ms, track_index)
        self._set_status(text)

    def _set_status(self, text):
        if self.on_status:
            self.on_status(text)

    def _set_playhead(self, ms):
        self.playhead_ms = max(0.0, min(float(self.total_ms), float(ms)))
        self._set_status(f"Playhead {self._fmt_precise(self.playhead_ms)}")
        self._redraw()
        return "break"

    def _on_pan_press(self, event):
        self._pan_anchor = (event.x, event.y, self.view_start_ms, self.scroll_y)
        self.configure(cursor="fleur")

    def _on_pan_drag(self, event):
        if not self._pan_anchor:
            return
        anchor_x, anchor_y, anchor_start, anchor_scroll = self._pan_anchor
        self.view_start_ms = anchor_start + (anchor_x - event.x) / self._timeline_w() * self.view_span_ms
        self.scroll_y = anchor_scroll + (anchor_y - event.y)
        self._clamp_view()
        self._clamp_scroll_y()
        self._redraw()

    def _on_pan_release(self, _event):
        self._pan_anchor = None
        self.configure(cursor=self.TOOL_CURSORS[self.tool])

    # ---- keyboard --------------------------------------------------------

    def _nudge_selected(self, event, direction):
        if not self.selection:
            return "break"
        state = getattr(event, "state", 0)
        amount = 1000 if state & self.SHIFT_MASK else 100
        self._begin_transaction()
        if state & self.ALT_MASK:
            for clip in self.selection:
                clip["source_start_ms"] = max(0, self._clip_source_start(clip) + direction * amount)
                clip["mode"] = self._clip_mode(clip)
            message = "Slipped clip source"
        else:
            lowest = min(int(clip["start_ms"]) for clip in self.selection)
            highest = max(int(clip["end_ms"]) for clip in self.selection)
            delta = direction * amount
            delta = max(-lowest, min(self.total_ms - highest, delta))
            for clip in self.selection:
                clip["source_start_ms"] = self._clip_source_start(clip)
                clip["start_ms"] = int(clip["start_ms"]) + delta
                clip["end_ms"] = int(clip["end_ms"]) + delta
                clip["mode"] = self._clip_mode(clip)
            message = "Nudged clip"
        self._commit_transaction(message)
        return "break"

    def _move_selection_slots(self, direction):
        slots = self._lane_slots()
        if not self.selection or not slots:
            return "break"
        positions = [self._slot_of(clip) for clip in self.selection]
        delta = max(-min(positions), min(len(slots) - 1 - max(positions), direction))
        if delta == 0:
            return "break"
        self._begin_transaction()
        for clip in self.selection:
            clip["source_start_ms"] = self._clip_source_start(clip)
            clip["mode"] = self._clip_mode(clip)
            self._retarget(clip, *slots[self._slot_of(clip) + delta])
        self._commit_transaction("Moved clip to another lane")
        return "break"


class MashupApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MP3 Mashup Tool")
        self.minsize(720, 560)
        self.resizable(True, True)

        self.prefs = _load_prefs()
        self.last_source_dir = self.prefs.get("source_dir", MUSIC_DIR)
        self.last_output_dir = self.prefs.get("output_dir", str(OUTPUT_DIR))
        self._tooltips = []

        self.primary_path = tk.StringVar()
        self.secondary_path = tk.StringVar()
        self.mode_var = tk.StringVar(value=MODE_SIMPLE)
        self.output_path = tk.StringVar(value=str(OUTPUT_DIR / "mashup.mp3"))
        self.status_var = tk.StringVar(value="Pick two MP3 files to begin.")
        self.vocals_from_var = tk.StringVar(value="Secondary")
        self.tempo_match_var = tk.BooleanVar(value=True)
        self.key_match_var = tk.BooleanVar(value=True)

        # "Make it actually blend" controls, shared in spirit across modes:
        # match loudness, carve out competing frequencies, duck the
        # background track under the foreground one.
        self.simple_match_loudness_var = tk.BooleanVar(value=True)
        self.simple_low_cut_var = tk.BooleanVar(value=True)
        self.beat_match_loudness_var = tk.BooleanVar(value=True)
        self.beat_low_cut_var = tk.BooleanVar(value=True)
        self.vocals_match_loudness_var = tk.BooleanVar(value=True)
        self.vocals_carve_var = tk.BooleanVar(value=True)
        self.stems_match_loudness_var = tk.BooleanVar(value=True)
        self.stems_tempo_match_var = tk.BooleanVar(value=True)
        self.stems_auto_fallback_var = tk.BooleanVar(value=True)
        # The playlist's tracks and clips. Both lists are handed to the editor
        # by identity so canvas edits are immediately visible to the renderer.
        self.timeline_tracks: list = [make_stem_track(name) for name in STEM_NAMES]
        self.stem_overrides: list = []
        self._track_vars: dict = {}
        self._imported_durations: dict = {}
        self._syncing_clip_list = False
        self.override_tool_var = tk.StringVar(value="select")
        self.override_clip_length_var = tk.DoubleVar(value=8.0)
        self.override_clip_mode_var = tk.StringVar(value="Layer")
        self.override_snap_var = tk.StringVar(value="Nearest")
        self.region_var = tk.StringVar(value="0:00.000 - 0:00.000  (cursor)")
        self.override_status_var = tk.StringVar(
            value="Draw (B) clips in a lane, drag them with Select (V), cut with Razor (R)."
        )

        self.primary_duration_ms = 0
        self.secondary_duration_ms = 0
        self._output_is_auto = True
        self._setting_output_programmatically = False
        self.output_path.trace_add("write", self._on_output_path_edited)

        self.render_queue: queue.Queue = queue.Queue()
        self.last_output_path = None
        self.render_thread = None

        self._apply_theme()
        self._build_scroll_container()
        self._build_header()
        self._build_file_pickers()
        self._build_mode_selector()
        self._build_mode_frames()
        self._build_menubar()
        self._build_output_row()
        self._build_status_row()
        self._build_action_buttons()

        self._show_mode_frame()
        self._center_window(800, 780)
        self.after(100, self._poll_queue)

    # ---- theme & layout scaffolding ----------------------------------

    def _apply_theme(self):
        """One dark DAW-style theme for every widget: ttk styles for the
        dashboard, option_add defaults for the plain-tk widgets (menus,
        listboxes, dialogs) that ttk styles never reach."""
        self.configure(bg=BG)
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        default_font = ("Segoe UI", 9)
        header_font = ("Segoe UI", 9, "bold")
        self.option_add("*Font", default_font)
        self.option_add("*background", BG)
        self.option_add("*foreground", TEXT)
        self.option_add("*Menu.background", SURFACE_ALT)
        self.option_add("*Menu.foreground", TEXT)
        self.option_add("*Menu.activeBackground", ACCENT)
        self.option_add("*Menu.activeForeground", ACCENT_TEXT)
        self.option_add("*Menu.relief", "flat")
        self.option_add("*TCombobox*Listbox.background", SURFACE_ALT)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", ACCENT_TEXT)

        style.configure(".", background=BG, foreground=TEXT, font=default_font,
                        bordercolor=BORDER, darkcolor=SURFACE, lightcolor=SURFACE,
                        troughcolor=SURFACE, focuscolor=ACCENT)
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=SURFACE)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 16, "bold"))
        style.configure("Subtitle.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Section.TLabel", background=BG, foreground=ACCENT, font=("Segoe UI", 8, "bold"))
        style.configure("Success.TLabel", background=BG, foreground=SUCCESS, font=("Segoe UI", 9, "bold"))
        style.configure("Error.TLabel", background=BG, foreground=ERROR, font=("Segoe UI", 9, "bold"))
        style.configure("Working.TLabel", background=BG, foreground=ACCENT, font=("Segoe UI", 9, "bold"))

        style.configure("TLabelframe", background=BG, bordercolor=BORDER, relief="solid", borderwidth=1)
        style.configure("TLabelframe.Label", background=BG, foreground=ACCENT, font=header_font)
        style.configure("TCheckbutton", background=BG, foreground=TEXT)
        style.map("TCheckbutton",
                  background=[("active", BG)],
                  indicatorcolor=[("selected", ACCENT), ("!selected", SURFACE_ALT)])
        style.configure("TRadiobutton", background=BG, foreground=TEXT)
        style.map("TRadiobutton",
                  background=[("active", BG)],
                  indicatorcolor=[("selected", ACCENT), ("!selected", SURFACE_ALT)])

        style.configure("TButton", padding=(10, 6), background=SURFACE_ALT, foreground=TEXT,
                        bordercolor=BORDER_HI, relief="flat")
        style.map("TButton",
                  background=[("active", SURFACE_HI), ("disabled", SURFACE)],
                  foreground=[("disabled", MUTED)])
        style.configure("Mini.TButton", padding=(4, 2), font=("Segoe UI", 8))
        style.map("Mini.TButton", background=[("active", SURFACE_HI)])
        style.configure("Accent.TButton", background=ACCENT, foreground=ACCENT_TEXT,
                        padding=(16, 9), font=("Segoe UI", 10, "bold"))
        style.map(
            "Accent.TButton",
            background=[("active", ACCENT_ACTIVE), ("disabled", "#4a423a")],
            foreground=[("disabled", MUTED)],
        )

        style.configure("Toolbutton", padding=(10, 8), background=SURFACE_ALT,
                        foreground=TEXT, font=("Segoe UI", 9, "bold"), relief="flat")
        style.map(
            "Toolbutton",
            background=[("selected", ACCENT), ("active", SURFACE_HI)],
            foreground=[("selected", ACCENT_TEXT)],
        )

        style.configure("TEntry", fieldbackground=SURFACE_ALT, foreground=TEXT,
                        bordercolor=BORDER_HI, insertcolor=TEXT, padding=3)
        style.configure("TSpinbox", fieldbackground=SURFACE_ALT, foreground=TEXT,
                        bordercolor=BORDER_HI, arrowcolor=TEXT, insertcolor=TEXT)
        style.configure("TCombobox", fieldbackground=SURFACE_ALT, foreground=TEXT,
                        background=SURFACE_ALT, bordercolor=BORDER_HI, arrowcolor=TEXT)
        style.map("TCombobox",
                  fieldbackground=[("readonly", SURFACE_ALT)],
                  foreground=[("readonly", TEXT)],
                  selectbackground=[("readonly", SURFACE_ALT)],
                  selectforeground=[("readonly", TEXT)])
        style.configure("Horizontal.TScale", background=BG, troughcolor=SURFACE_ALT)
        style.map("Horizontal.TScale", background=[("active", BG)])
        style.configure("TScrollbar", background=SURFACE_ALT, troughcolor=BG,
                        bordercolor=BG, arrowcolor=MUTED, relief="flat")
        style.map("TScrollbar", background=[("active", SURFACE_HI)])
        style.configure("Accent.Horizontal.TProgressbar", background=ACCENT, troughcolor=SURFACE_ALT,
                        bordercolor=BORDER, lightcolor=ACCENT, darkcolor=ACCENT)
        style.configure("TSeparator", background=BORDER)
        style.configure("Menubar.TFrame", background=SURFACE)
        style.configure("Menubar.TMenubutton", background=SURFACE, foreground=TEXT,
                        padding=(10, 4), relief="flat", arrowsize=0, font=default_font)
        style.map("Menubar.TMenubutton",
                  background=[("active", SURFACE_HI), ("pressed", ACCENT)],
                  foreground=[("pressed", ACCENT_TEXT)])
        style.configure("Treeview", background=SURFACE_ALT, fieldbackground=SURFACE_ALT,
                        foreground=TEXT, bordercolor=BORDER, rowheight=20)
        style.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", ACCENT_TEXT)])
        style.configure("Treeview.Heading", background=SURFACE, foreground=MUTED,
                        font=("Segoe UI", 8, "bold"), relief="flat")
        style.map("Treeview.Heading", background=[("active", SURFACE_HI)])

    def _build_scroll_container(self):
        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True)
        self._scroll_outer = outer

        self.canvas = tk.Canvas(outer, bg=BG, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.content = ttk.Frame(self.canvas)
        self._content_window = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.content.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig(self._content_window, width=e.width))
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _center_window(self, width, height):
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        x, y = max(0, (sw - width) // 2), max(0, (sh - height) // 3)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _tip(self, widget, text):
        self._tooltips.append(ToolTip(widget, text))

    # ---- layout ----------------------------------------------------

    def _build_header(self):
        frame = ttk.Frame(self.content)
        frame.pack(fill="x", padx=18, pady=(16, 6))
        ttk.Label(frame, text="MP3 Mashup Tool", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            frame, text="Blend two tracks into one - pick a mode, tweak the mix, render.",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(2, 0))

    def _build_file_pickers(self):
        frame = ttk.LabelFrame(self.content, text=" Source tracks ")
        frame.pack(fill="x", padx=18, pady=(8, 8))
        inner = ttk.Frame(frame)
        inner.pack(fill="x", padx=10, pady=10)
        inner.columnconfigure(1, weight=1)

        ttk.Label(inner, text="Primary", width=9, anchor="w").grid(row=0, column=0, sticky="w", padx=(0, 4), pady=4)
        ttk.Entry(inner, textvariable=self.primary_path).grid(row=0, column=1, sticky="ew", padx=4, pady=4)
        self.primary_length_label = ttk.Label(inner, text="", width=6, style="Muted.TLabel", anchor="center")
        self.primary_length_label.grid(row=0, column=2, padx=2, pady=4)
        ttk.Button(inner, text="Browse...", command=self._browse_primary).grid(row=0, column=3, padx=2, pady=4)
        ttk.Button(inner, text="✕", width=3, command=self._clear_primary).grid(row=0, column=4, padx=(2, 0), pady=4)

        swap_row = ttk.Frame(inner)
        swap_row.grid(row=1, column=0, columnspan=5, pady=2)
        ttk.Button(swap_row, text="⇅ Swap primary / secondary", command=self._swap_tracks).pack()

        ttk.Label(inner, text="Secondary", width=9, anchor="w").grid(row=2, column=0, sticky="w", padx=(0, 4), pady=4)
        ttk.Entry(inner, textvariable=self.secondary_path).grid(row=2, column=1, sticky="ew", padx=4, pady=4)
        self.secondary_length_label = ttk.Label(inner, text="", width=6, style="Muted.TLabel", anchor="center")
        self.secondary_length_label.grid(row=2, column=2, padx=2, pady=4)
        ttk.Button(inner, text="Browse...", command=self._browse_secondary).grid(row=2, column=3, padx=2, pady=4)
        ttk.Button(inner, text="✕", width=3, command=self._clear_secondary).grid(row=2, column=4, padx=(2, 0), pady=4)

    def _build_mode_selector(self):
        frame = ttk.Frame(self.content)
        frame.pack(fill="x", padx=18, pady=(2, 8))
        ttk.Label(frame, text="Mashup mode", style="Subtitle.TLabel").pack(anchor="w")
        bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=(4, 0))
        for i, mode in enumerate(MODES):
            bar.columnconfigure(i, weight=1)
            ttk.Radiobutton(
                bar, text=MODE_SHORT_NAMES[mode], value=mode, variable=self.mode_var,
                style="Toolbutton", command=self._show_mode_frame,
            ).grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 4, 0))

    def _build_mode_frames(self):
        self.mode_container = ttk.LabelFrame(self.content, text=" Mode settings ")
        self.mode_container.pack(fill="x", padx=18, pady=6)

        # One timeline shared by every mode - whichever mode is active rebinds
        # it to that mode's own offset slider, instead of each mode carrying
        # its own duplicate bar.
        self.shared_timeline = OffsetTimeline(self.mode_container)
        self.shared_timeline.pack(fill="x", padx=8, pady=(8, 4))

        self.mode_frames = {
            MODE_SIMPLE: self._build_simple_frame(),
            MODE_BEAT_SYNCED: self._build_beat_synced_frame(),
            MODE_VOCALS: self._build_vocals_frame(),
            MODE_STEMS: self._build_stems_frame(),
        }
        self._offset_scales = {
            MODE_SIMPLE: self.simple_offset,
            MODE_BEAT_SYNCED: self.beat_start,
            MODE_VOCALS: self.vocals_offset,
            MODE_STEMS: self.stems_offset,
        }

    def _build_blend_section(self, parent, title="Blend & effects"):
        """A consistently-styled sub-frame for the loudness/duck/reverb controls
        shared across modes, so each mode's panel reads the same way."""
        section = ttk.LabelFrame(parent, text=f" {title} ")
        section.pack(fill="x", pady=(10, 4), padx=8)
        return section

    def _build_simple_frame(self):
        frame = ttk.Frame(self.mode_container)
        self.simple_offset = LabeledScale(frame, "Secondary start offset", 0, 60000, 0, unit=" ms")
        self.simple_offset.pack(fill="x", pady=(8, 4), padx=8)
        self.simple_offset.bind_change(lambda v: self._on_offset_slider_change(MODE_SIMPLE, v))

        self.simple_primary_gain = LabeledScale(frame, "Primary gain", -20, 6, 0, unit=" dB", fmt="{:.1f}")
        self.simple_primary_gain.pack(fill="x", pady=4, padx=8)
        self.simple_secondary_gain = LabeledScale(frame, "Secondary gain", -20, 6, -3, unit=" dB", fmt="{:.1f}")
        self.simple_secondary_gain.pack(fill="x", pady=4, padx=8)
        self.simple_crossfade = LabeledScale(frame, "Crossfade", 0, 8000, 1000, unit=" ms")
        self.simple_crossfade.pack(fill="x", pady=4, padx=8)

        blend = self._build_blend_section(frame)
        check_row = ttk.Frame(blend)
        check_row.pack(fill="x", pady=(6, 2), padx=6)
        c1 = ttk.Checkbutton(check_row, text="Match loudness", variable=self.simple_match_loudness_var)
        c1.pack(side="left")
        self._tip(c1, "Automatically levels the two tracks so neither overpowers the other.")
        c2 = ttk.Checkbutton(check_row, text="Low-cut secondary bass", variable=self.simple_low_cut_var)
        c2.pack(side="left", padx=12)
        self._tip(c2, "Rolls off secondary's low end so both tracks' basslines don't clash.")
        self.simple_duck_amount = LabeledScale(
            blend, "Duck primary under secondary", 0, 100, 30, unit="%",
            tooltip="Temporarily lowers primary's volume under secondary, sidechain-style.",
        )
        self.simple_duck_amount.pack(fill="x", pady=4, padx=6)
        self.simple_primary_reverb_amount = LabeledScale(
            blend, "Reverb on primary", 0, 100, 0, unit="%",
            tooltip="Adds room ambience so primary sits in a shared space with the mix.",
        )
        self.simple_primary_reverb_amount.pack(fill="x", pady=4, padx=6)
        self.simple_reverb_amount = LabeledScale(
            blend, "Reverb on secondary", 0, 100, 0, unit="%",
            tooltip="Adds room ambience so secondary sits in a shared space with the mix.",
        )
        self.simple_reverb_amount.pack(fill="x", pady=4, padx=6)
        self.simple_reverb_size = LabeledScale(
            blend, "Reverb room size", 0, 100, 50, unit="%",
            tooltip="Larger size = longer, more spacious reverb tail (shared by both reverb sends).",
        )
        self.simple_reverb_size.pack(fill="x", pady=(4, 6), padx=6)
        return frame

    def _build_beat_synced_frame(self):
        frame = ttk.Frame(self.mode_container)

        analyze_row = ttk.Frame(frame)
        analyze_row.pack(fill="x", pady=(8, 8), padx=8)
        ttk.Button(analyze_row, text="Analyze BPM", command=self._analyze).pack(side="left")
        self.bpm_label = ttk.Label(analyze_row, text="Detected BPM: (not analyzed yet)", style="Muted.TLabel")
        self.bpm_label.pack(side="left", padx=10)

        self.beat_start = LabeledScale(frame, "Blend start", 0, 120000, 0, unit=" ms")
        self.beat_start.pack(fill="x", pady=4, padx=8)
        self.beat_start.bind_change(lambda v: self._on_offset_slider_change(MODE_BEAT_SYNCED, v))

        self.beat_duration = LabeledScale(frame, "Blend duration", 500, 20000, 8000, unit=" ms")
        self.beat_duration.pack(fill="x", pady=4, padx=8)
        self.beat_primary_gain = LabeledScale(frame, "Primary gain", -20, 6, 0, unit=" dB", fmt="{:.1f}")
        self.beat_primary_gain.pack(fill="x", pady=4, padx=8)
        self.beat_secondary_gain = LabeledScale(frame, "Secondary gain", -20, 6, 0, unit=" dB", fmt="{:.1f}")
        self.beat_secondary_gain.pack(fill="x", pady=4, padx=8)

        blend = self._build_blend_section(frame)
        check_row = ttk.Frame(blend)
        check_row.pack(fill="x", pady=(6, 2), padx=6)
        c1 = ttk.Checkbutton(check_row, text="Match loudness", variable=self.beat_match_loudness_var)
        c1.pack(side="left")
        self._tip(c1, "Automatically levels the two tracks so neither overpowers the other.")
        c2 = ttk.Checkbutton(check_row, text="Low-cut secondary bass", variable=self.beat_low_cut_var)
        c2.pack(side="left", padx=12)
        self._tip(c2, "Rolls off secondary's low end so both tracks' basslines don't clash.")
        self.beat_duck_amount = LabeledScale(
            blend, "Duck primary under secondary", 0, 100, 30, unit="%",
            tooltip="Temporarily lowers primary's volume under secondary, sidechain-style.",
        )
        self.beat_duck_amount.pack(fill="x", pady=4, padx=6)
        self.beat_primary_reverb_amount = LabeledScale(
            blend, "Reverb on primary", 0, 100, 0, unit="%",
            tooltip="Adds room ambience so primary sits in a shared space with the mix.",
        )
        self.beat_primary_reverb_amount.pack(fill="x", pady=4, padx=6)
        self.beat_reverb_amount = LabeledScale(
            blend, "Reverb on secondary", 0, 100, 0, unit="%",
            tooltip="Adds room ambience so secondary sits in a shared space with the mix.",
        )
        self.beat_reverb_amount.pack(fill="x", pady=4, padx=6)
        self.beat_reverb_size = LabeledScale(
            blend, "Reverb room size", 0, 100, 50, unit="%",
            tooltip="Larger size = longer, more spacious reverb tail (shared by both reverb sends).",
        )
        self.beat_reverb_size.pack(fill="x", pady=(4, 6), padx=6)
        return frame

    def _build_vocals_frame(self):
        frame = ttk.Frame(self.mode_container)

        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(8, 4), padx=8)
        ttk.Label(row, text="Take vocals from:").pack(side="left")
        ttk.Combobox(
            row, textvariable=self.vocals_from_var, values=["Primary", "Secondary"],
            state="readonly", width=12,
        ).pack(side="left", padx=8)

        check_row = ttk.Frame(frame)
        check_row.pack(fill="x", pady=4, padx=8)
        t1 = ttk.Checkbutton(check_row, text="Tempo match", variable=self.tempo_match_var)
        t1.pack(side="left")
        self._tip(t1, "Time-stretches the vocal track so it shares the instrumental's BPM.")
        t2 = ttk.Checkbutton(check_row, text="Key match", variable=self.key_match_var)
        t2.pack(side="left", padx=12)
        self._tip(t2, "Pitch-shifts the vocal track so it shares the instrumental's musical key.")

        self.vocals_offset = LabeledScale(frame, "Vocal start offset", 0, 60000, 0, unit=" ms")
        self.vocals_offset.pack(fill="x", pady=4, padx=8)
        self.vocals_offset.bind_change(lambda v: self._on_offset_slider_change(MODE_VOCALS, v))

        self.vocals_gain = LabeledScale(frame, "Vocal gain", -20, 6, 0, unit=" dB", fmt="{:.1f}")
        self.vocals_gain.pack(fill="x", pady=4, padx=8)
        self.instrumental_gain = LabeledScale(frame, "Instrumental gain", -20, 6, 0, unit=" dB", fmt="{:.1f}")
        self.instrumental_gain.pack(fill="x", pady=4, padx=8)

        blend = self._build_blend_section(frame)
        check_row2 = ttk.Frame(blend)
        check_row2.pack(fill="x", pady=(6, 2), padx=6)
        c1 = ttk.Checkbutton(check_row2, text="Match loudness", variable=self.vocals_match_loudness_var)
        c1.pack(side="left")
        self._tip(c1, "Automatically levels the vocal and instrumental so neither overpowers the other.")
        c2 = ttk.Checkbutton(check_row2, text="Carve instrumental for vocal presence (~2.5kHz)", variable=self.vocals_carve_var)
        c2.pack(side="left", padx=12)
        self._tip(c2, "Cuts a notch around ~2.5kHz in the instrumental so the vocal cuts through cleanly.")
        self.vocals_duck_amount = LabeledScale(
            blend, "Duck instrumental under vocals", 0, 100, 50, unit="%",
            tooltip="Temporarily lowers the instrumental's volume under the vocal, sidechain-style.",
        )
        self.vocals_duck_amount.pack(fill="x", pady=4, padx=6)
        self.vocals_reverb_amount = LabeledScale(
            blend, "Reverb on vocals", 0, 100, 0, unit="%",
            tooltip="Adds room ambience so the vocal sits in a shared space with the instrumental.",
        )
        self.vocals_reverb_amount.pack(fill="x", pady=4, padx=6)
        self.vocals_instrumental_reverb_amount = LabeledScale(
            blend, "Reverb on instrumental", 0, 100, 0, unit="%",
            tooltip="Adds room ambience to the instrumental side of the mix.",
        )
        self.vocals_instrumental_reverb_amount.pack(fill="x", pady=4, padx=6)
        self.vocals_reverb_size = LabeledScale(
            blend, "Reverb room size", 0, 100, 50, unit="%",
            tooltip="Larger size = longer, more spacious reverb tail (shared by both reverb sends).",
        )
        self.vocals_reverb_size.pack(fill="x", pady=(4, 6), padx=6)

        note = ttk.Label(
            frame,
            text="First run downloads the Demucs model and separation takes ~1-3 min per track on CPU.\n"
                 "Results are cached, so re-rendering the same file is instant after the first time.",
            style="Muted.TLabel", wraplength=680, justify="left",
        )
        note.pack(fill="x", pady=(8, 8), padx=8)
        return frame

    def _build_stems_frame(self):
        frame = ttk.Frame(self.mode_container)

        self._build_track_panel(frame)
        self._build_playlist_panel(frame)

        self.stems_offset = LabeledScale(frame, "Secondary start offset", 0, 60000, 0, unit=" ms")
        self.stems_offset.pack(fill="x", pady=4, padx=8)
        self.stems_offset.bind_change(lambda v: self._on_offset_slider_change(MODE_STEMS, v))

        self.stems_primary_gain = LabeledScale(frame, "Primary group gain", -20, 6, 0, unit=" dB", fmt="{:.1f}")
        self.stems_primary_gain.pack(fill="x", pady=4, padx=8)
        self.stems_secondary_gain = LabeledScale(frame, "Secondary group gain", -20, 6, 0, unit=" dB", fmt="{:.1f}")
        self.stems_secondary_gain.pack(fill="x", pady=4, padx=8)
        self.stems_primary_reverb_amount = LabeledScale(
            frame, "Reverb on primary's stems", 0, 100, 0, unit="%",
            tooltip="Adds room ambience to whichever stems are sourced from primary.",
        )
        self.stems_primary_reverb_amount.pack(fill="x", pady=4, padx=8)
        self.stems_reverb_amount = LabeledScale(
            frame, "Reverb on secondary's stems", 0, 100, 0, unit="%",
            tooltip="Adds room ambience to whichever stems are sourced from secondary.",
        )
        self.stems_reverb_amount.pack(fill="x", pady=4, padx=8)
        self.stems_reverb_size = LabeledScale(
            frame, "Reverb room size", 0, 100, 50, unit="%",
            tooltip="Larger size = longer, more spacious reverb tail (shared by both reverb sends).",
        )
        self.stems_reverb_size.pack(fill="x", pady=4, padx=8)

        blend = self._build_blend_section(frame, title="Tempo & mix")
        check_row = ttk.Frame(blend)
        check_row.pack(fill="x", pady=(6, 2), padx=6)
        t1 = ttk.Checkbutton(check_row, text="Tempo match", variable=self.stems_tempo_match_var)
        t1.pack(side="left")
        self._tip(t1, "Time-stretches secondary's stems so they share primary's BPM.")
        c1 = ttk.Checkbutton(check_row, text="Match loudness", variable=self.stems_match_loudness_var)
        c1.pack(side="left", padx=12)
        self._tip(c1, "Automatically levels the two stem groups so neither overpowers the other.")
        c2 = ttk.Checkbutton(check_row, text="Auto-restore when a track ends", variable=self.stems_auto_fallback_var)
        c2.pack(side="left", padx=12)
        self._tip(
            c2,
            "If secondary runs out before primary (or vice versa), automatically switches that stem back to "
            "whichever track is still playing instead of leaving it silent for the rest of the song.",
        )
        self.stems_duck_amount = LabeledScale(
            blend, "Duck primary under secondary", 0, 100, 30, unit="%",
            tooltip="Temporarily lowers the primary-sourced stems under the secondary-sourced ones.",
        )
        self.stems_duck_amount.pack(fill="x", pady=4, padx=6)
        self.stems_override_crossfade = LabeledScale(
            blend, "Clip edge fade / replacement crossfade", 0, 3000, 900, unit=" ms",
            tooltip="Fades Layer clip edges and blends across Replace clip or auto-restore source changes.",
        )
        self.stems_override_crossfade.pack(fill="x", pady=(4, 6), padx=6)

        note = ttk.Label(
            frame,
            text="Both sources can contribute the same stem at the same time. Full four-stem separation is "
                 "slower than the vocals mode; results are cached per file. Imported tracks are used as-is "
                 "(no separation, no tempo matching).",
            style="Muted.TLabel", wraplength=680, justify="left",
        )
        note.pack(fill="x", pady=(8, 8), padx=8)

        self._refresh_track_panel()
        return frame

    # ---- track management ------------------------------------------------

    def _build_track_panel(self, parent):
        panel = ttk.LabelFrame(parent, text=" Tracks ")
        panel.pack(fill="x", pady=(8, 6), padx=8)

        bar = ttk.Frame(panel)
        bar.pack(fill="x", padx=6, pady=(6, 2))
        add_button = ttk.Button(bar, text="+ Stem track", command=self._popup_add_stem_menu)
        add_button.pack(side="left")
        self._tip(add_button, "Adds another lane fed by one separated stem - handy for stacking clips.")
        import_button = ttk.Button(bar, text="Import audio...", command=self._import_audio_track)
        import_button.pack(side="left", padx=6)
        self._tip(import_button, "Brings any audio file in as its own playlist track.")
        ttk.Label(
            bar, text="Right-click a track header on the timeline for the same commands.",
            style="Muted.TLabel",
        ).pack(side="left", padx=8)

        self.track_rows = ttk.Frame(panel)
        self.track_rows.pack(fill="x", padx=6, pady=(0, 6))
        return panel

    def _popup_add_stem_menu(self):
        menu = tk.Menu(self, tearoff=0, bg=SURFACE_ALT, fg=TEXT,
                       activebackground=ACCENT, activeforeground=ACCENT_TEXT, bd=0)
        for name in STEM_NAMES:
            menu.add_command(label=name.capitalize(), command=lambda stem=name: self._add_stem_track(stem))
        try:
            menu.tk_popup(self.winfo_pointerx(), self.winfo_pointery())
        finally:
            menu.grab_release()

    def _add_stem_track(self, stem):
        # Extra stem lanes default to a muted bed: a second full-song bed for
        # the same stem would just double that stem's level.
        track = make_stem_track(stem, name=self._unique_track_name(stem.capitalize()), bed="muted")
        self.timeline_tracks.append(track)
        self.playlist_editor.active_track_id = track["id"]
        self._refresh_track_panel()
        self.override_status_var.set(f"Added track '{track['name']}'")

    def _import_audio_track(self):
        path = filedialog.askopenfilename(
            title="Import audio into the playlist", initialdir=self.last_source_dir,
            filetypes=[("Audio files", "*.mp3 *.wav *.flac *.ogg *.m4a *.aac"), ("All files", "*.*")],
        )
        if not path:
            return
        from . import audio_io

        try:
            duration_ms = audio_io.get_duration_ms(path)
        except Exception as exc:  # noqa: BLE001 - a bad file should not kill the app
            messagebox.showerror("Can't import audio", f"{Path(path).name}\n\n{exc}")
            return
        track = make_audio_track(path, name=self._unique_track_name(Path(path).stem[:20]))
        self._imported_durations[track["id"]] = duration_ms
        self.timeline_tracks.append(track)
        self.playlist_editor.active_track_id = track["id"]
        self._remember_source_dir(path)
        self._refresh_track_panel()
        self._update_timelines()
        self.override_status_var.set(
            f"Imported '{track['name']}' ({PlaylistEditor._fmt(duration_ms)}) - drag the AUDIO chip onto its lane"
        )

    def _duplicate_track(self, track):
        """Copy a track and its clips. The copy is clip-only (muted bed) so it
        layers on top of the original instead of doubling its full-song bed."""
        copy = dict(track)
        copy["id"] = _new_track_id()
        copy["name"] = self._unique_track_name(track["name"])
        copy["bed"] = "muted"
        copy["solo"] = False
        index = self.timeline_tracks.index(track) + 1
        self.timeline_tracks.insert(index, copy)
        if track["id"] in self._imported_durations:
            self._imported_durations[copy["id"]] = self._imported_durations[track["id"]]
        cloned = [dict(clip, track=copy["id"]) for clip in self.stem_overrides if clip.get("track") == track["id"]]
        if cloned:
            self.playlist_editor._begin_transaction()
            self.stem_overrides.extend(cloned)
            self.playlist_editor._commit_transaction(f"Duplicated track '{track['name']}'")
        self.playlist_editor.active_track_id = copy["id"]
        self._refresh_track_panel()
        self.override_status_var.set(
            f"Duplicated '{track['name']}' as '{copy['name']}' with {len(cloned)} clip(s)"
        )

    def _remove_track(self, track):
        if len(self.timeline_tracks) <= 1:
            messagebox.showinfo("Can't remove track", "The playlist needs at least one track.")
            return
        clips = [clip for clip in self.stem_overrides if clip.get("track") == track["id"]]
        if clips and not messagebox.askyesno(
            "Remove track", f"Remove '{track['name']}' and its {len(clips)} clip(s)?"
        ):
            return
        if clips:
            self.playlist_editor._begin_transaction()
            self.stem_overrides[:] = [
                clip for clip in self.stem_overrides if clip.get("track") != track["id"]
            ]
            self.playlist_editor._commit_transaction(f"Removed track '{track['name']}'")
        self.timeline_tracks.remove(track)
        self._imported_durations.pop(track["id"], None)
        self.playlist_editor.selection = []
        self._refresh_track_panel()
        self._update_timelines()
        self.override_status_var.set(f"Removed track '{track['name']}'")

    def _rename_track(self, track):
        from tkinter import simpledialog

        name = simpledialog.askstring("Rename track", "Track name:", initialvalue=track["name"], parent=self)
        if name:
            track["name"] = name.strip()[:24] or track["name"]
            self._refresh_track_panel()

    def _unique_track_name(self, base):
        existing = {track["name"] for track in self.timeline_tracks}
        if base not in existing:
            return base
        for suffix in range(2, 100):
            candidate = f"{base} {suffix}"
            if candidate not in existing:
                return candidate
        return base

    def _on_track_command(self, action, track):
        """Commands raised from the timeline's own track headers."""
        if action.startswith("bed:"):
            track["bed"] = action.split(":", 1)[1]
            self._refresh_track_panel()
        elif action.startswith("move:"):
            self.playlist_editor.move_track(track, action.split(":", 1)[1])
        elif action == "duplicate":
            self._duplicate_track(track)
        elif action == "remove":
            self._remove_track(track)
        elif action == "rename":
            self._rename_track(track)
        elif action == "duplicate-selection-to-track":
            self._selection_to_new_tracks(move=False)
        elif action == "move-selection-to-track":
            self._selection_to_new_tracks(move=True)
        else:  # mute / solo / reordered, already applied to the track list
            self._refresh_track_panel()

    def _refresh_track_panel(self):
        """Rebuild the per-track control rows and re-sync the timeline. Cheap
        enough to redo wholesale, and it keeps the rows honest after add,
        duplicate, remove, rename and reorder."""
        for child in self.track_rows.winfo_children():
            child.destroy()
        self._track_vars = {}

        header = ttk.Frame(self.track_rows)
        header.pack(fill="x", pady=(2, 0))
        for text, width in (("Track", 18), ("Full-song bed", 15), ("Gain", 7), ("Pan", 6)):
            ttk.Label(header, text=text, width=width, anchor="w", style="Section.TLabel").pack(side="left", padx=2)

        for track in self.timeline_tracks:
            self._build_track_row(track)

        self.playlist_editor.set_tracks(self.timeline_tracks)
        self._refresh_clip_list()

    def _build_track_row(self, track):
        row = ttk.Frame(self.track_rows)
        row.pack(fill="x", pady=1)

        name_var = tk.StringVar(value=track["name"])
        bed_var = tk.StringVar(value=track["bed"].capitalize())
        gain_var = tk.DoubleVar(value=float(track.get("gain_db", 0.0)))
        pan_var = tk.DoubleVar(value=float(track.get("pan", 0.0)))
        mute_var = tk.BooleanVar(value=bool(track.get("mute")))
        solo_var = tk.BooleanVar(value=bool(track.get("solo")))
        self._track_vars[track["id"]] = (name_var, bed_var, gain_var, pan_var, mute_var, solo_var)

        name_entry = ttk.Entry(row, textvariable=name_var, width=18)
        name_entry.pack(side="left", padx=2)
        name_var.trace_add("write", lambda *_a: self._write_track_name(track, name_var))

        beds = ["Import", "Muted"] if track["kind"] == "audio" else ["Primary", "Secondary", "Both", "Muted"]
        bed_combo = ttk.Combobox(row, textvariable=bed_var, values=beds, state="readonly", width=13)
        bed_combo.pack(side="left", padx=2)
        bed_combo.bind("<<ComboboxSelected>>", lambda _e: self._write_track_bed(track, bed_var))

        gain_spin = ttk.Spinbox(row, from_=-20.0, to=6.0, increment=0.5, textvariable=gain_var, width=6)
        gain_spin.pack(side="left", padx=2)
        gain_var.trace_add("write", lambda *_a: self._write_track_number(track, "gain_db", gain_var))
        self._tip(gain_spin, "Level for this track alone, on top of the group gains below.")

        pan_spin = ttk.Spinbox(row, from_=-1.0, to=1.0, increment=0.1, textvariable=pan_var, width=5)
        pan_spin.pack(side="left", padx=2)
        pan_var.trace_add("write", lambda *_a: self._write_track_number(track, "pan", pan_var))
        self._tip(pan_spin, "Stereo position: -1 is hard left, 0 centre, +1 hard right.")

        ttk.Checkbutton(
            row, text="M", variable=mute_var, style="Toolbutton", width=2,
            command=lambda: self._write_track_flag(track, "mute", mute_var),
        ).pack(side="left", padx=(6, 1))
        ttk.Checkbutton(
            row, text="S", variable=solo_var, style="Toolbutton", width=2,
            command=lambda: self._write_track_flag(track, "solo", solo_var),
        ).pack(side="left", padx=1)

        up = ttk.Button(row, text="▲", style="Mini.TButton", width=2,
                        command=lambda: self.playlist_editor.move_track(track, "up"))
        up.pack(side="left", padx=(8, 1))
        self._tip(up, "Move this track up the playlist.")
        down = ttk.Button(row, text="▼", style="Mini.TButton", width=2,
                          command=lambda: self.playlist_editor.move_track(track, "down"))
        down.pack(side="left", padx=1)
        self._tip(down, "Move this track down the playlist.")

        ttk.Button(
            row, text="Duplicate", style="Mini.TButton", command=lambda: self._duplicate_track(track),
        ).pack(side="left", padx=(6, 2))
        ttk.Button(
            row, text="Remove", style="Mini.TButton", command=lambda: self._remove_track(track),
        ).pack(side="left", padx=2)
        kind = "audio file" if track["kind"] == "audio" else f"{track['stem']} stem"
        ttk.Label(row, text=kind, style="Muted.TLabel").pack(side="left", padx=8)

    def _write_track_name(self, track, var):
        track["name"] = var.get()[:24]
        self.playlist_editor._redraw()

    def _write_track_bed(self, track, var):
        track["bed"] = var.get().lower()
        self.playlist_editor._redraw()
        self._update_timelines()

    def _write_track_number(self, track, key, var):
        try:
            track[key] = float(var.get())
        except (tk.TclError, ValueError):
            return
        self.playlist_editor._redraw()

    def _write_track_flag(self, track, key, var):
        track[key] = bool(var.get())
        self.playlist_editor._redraw()

    # ---- playlist panel --------------------------------------------------

    def _build_playlist_panel(self, parent):
        panel = ttk.LabelFrame(parent, text=" Playlist ")
        self.stems_playlist_frame = panel
        panel.pack(fill="x", pady=(0, 6), padx=8)

        tool_row = ttk.Frame(panel)
        tool_row.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(tool_row, text="Tool:").pack(side="left")
        for label, value in (("Select (V)", "select"), ("Draw (B)", "draw"), ("Razor (R)", "razor")):
            ttk.Radiobutton(
                tool_row, text=label, value=value, variable=self.override_tool_var,
                style="Toolbutton", command=lambda v=value: self.playlist_editor.set_tool(v),
            ).pack(side="left", padx=2)
        self.override_undo_button = ttk.Button(
            tool_row, text="Undo", command=lambda: self.playlist_editor.undo(), state="disabled",
        )
        self.override_undo_button.pack(side="right", padx=(2, 0))
        self.override_redo_button = ttk.Button(
            tool_row, text="Redo", command=lambda: self.playlist_editor.redo(), state="disabled",
        )
        self.override_redo_button.pack(side="right", padx=2)

        clip_row = ttk.Frame(panel)
        clip_row.pack(fill="x", padx=6, pady=(2, 4))
        ttk.Label(clip_row, text="Drag in:").pack(side="left")
        for text, source in (("P  PRIMARY", "primary"), ("S  SECONDARY", "secondary"), ("A  AUDIO", "import")):
            SourceChip(clip_row, text, source, lambda: self.playlist_editor).pack(side="left", padx=3)
        ttk.Label(clip_row, text="Length:").pack(side="left", padx=(10, 3))
        ttk.Spinbox(
            clip_row, from_=0.25, to=3600.0, increment=0.25,
            textvariable=self.override_clip_length_var, width=6,
        ).pack(side="left")
        ttk.Label(clip_row, text="sec").pack(side="left", padx=(2, 0))
        ttk.Button(
            clip_row, text="Use full length", style="Mini.TButton",
            command=lambda: self.override_clip_length_var.set(self.playlist_editor.total_ms / 1000),
        ).pack(side="left", padx=6)

        behavior_row = ttk.Frame(panel)
        behavior_row.pack(fill="x", padx=6, pady=(0, 3))
        ttk.Label(behavior_row, text="New clips:").pack(side="left")
        ttk.Radiobutton(
            behavior_row, text="Layer (play together)", value="Layer", variable=self.override_clip_mode_var,
        ).pack(side="left", padx=(6, 2))
        ttk.Radiobutton(
            behavior_row, text="Replace bed", value="Replace", variable=self.override_clip_mode_var,
        ).pack(side="left", padx=2)
        ttk.Label(behavior_row, text="Snap:").pack(side="left", padx=(12, 3))
        snap_combo = ttk.Combobox(
            behavior_row, textvariable=self.override_snap_var,
            values=["Off", "Nearest", "Prior"], state="readonly", width=8,
        )
        snap_combo.pack(side="left")
        self._tip(snap_combo, "Nearest snaps to whichever boundary is closest; Prior snaps back to the "
                              "last boundary before the pointer. Alt always bypasses snapping.")
        ttk.Label(behavior_row, text="Alt = fine / slip / drag-a-copy", style="Muted.TLabel").pack(
            side="left", padx=(8, 0)
        )

        canvas_row = ttk.Frame(panel)
        canvas_row.pack(fill="x", padx=6, pady=(2, 0))
        self.playlist_editor = PlaylistEditor(
            canvas_row,
            get_clip_length_ms=lambda: self.override_clip_length_var.get() * 1000,
            get_clip_mode=lambda: self.override_clip_mode_var.get().lower(),
            get_snap_enabled=lambda: self.override_snap_var.get().lower(),
            on_change=self._refresh_clip_list,
            on_status=self.override_status_var.set,
            on_tool_change=self.override_tool_var.set,
            on_track_command=self._on_track_command,
            on_region=self._show_region,
        )
        self.playlist_vscroll = ttk.Scrollbar(canvas_row, orient="vertical", command=self.playlist_editor.yview)
        self.playlist_vscroll.pack(side="right", fill="y")
        self.playlist_editor.pack(side="left", fill="x", expand=True)
        self.playlist_editor.set_yscrollcommand(self.playlist_vscroll.set)

        self.override_scrollbar = ttk.Scrollbar(panel, orient="horizontal", command=self.playlist_editor.xview)
        self.override_scrollbar.pack(fill="x", padx=(PlaylistEditor.HEADER_W + 6, 22), pady=(0, 2))
        self.playlist_editor.set_xscrollcommand(self.override_scrollbar.set)

        # The region toolbar: what the Audacity-style edit verbs act on.
        region_row = ttk.Frame(panel)
        region_row.pack(fill="x", padx=6, pady=(2, 0))
        ttk.Label(region_row, text="Region:", style="Section.TLabel").pack(side="left")
        ttk.Label(region_row, textvariable=self.region_var, style="Muted.TLabel").pack(side="left", padx=(4, 10))
        for label, command, tip in (
            ("Split", self.playlist_editor.split_at_selection, "Ctrl+I - cut clips at both region edges."),
            ("Trim", self.playlist_editor.trim_to_selection, "Ctrl+T - keep only what is inside the region."),
            ("Silence", self.playlist_editor.silence_selection, "Ctrl+L - mute the region, bed included."),
            ("Delete+close", self.playlist_editor.ripple_delete_selection,
             "Ctrl+K - remove the region and pull later clips back."),
            ("Delete+gap", self.playlist_editor.split_delete_selection,
             "Ctrl+Alt+K - remove the region and leave the gap."),
            ("Join", self.playlist_editor.join_selection, "Ctrl+J - merge the selected clips of a lane."),
        ):
            button = ttk.Button(region_row, text=label, style="Mini.TButton", command=command)
            button.pack(side="left", padx=2)
            self._tip(button, tip)

        view_row = ttk.Frame(panel)
        view_row.pack(fill="x", padx=6, pady=(2, 2))
        ttk.Button(view_row, text="Zoom in", style="Mini.TButton",
                   command=self.playlist_editor.zoom_in).pack(side="left")
        ttk.Button(view_row, text="Zoom out", style="Mini.TButton",
                   command=self.playlist_editor.zoom_out).pack(side="left", padx=3)
        ttk.Button(view_row, text="Fit song", style="Mini.TButton",
                   command=self.playlist_editor.fit_view).pack(side="left")
        ttk.Button(view_row, text="Zoom to region", style="Mini.TButton",
                   command=self.playlist_editor.zoom_to_selection).pack(side="left", padx=3)
        ttk.Button(view_row, text="Duplicate clip", style="Mini.TButton",
                   command=lambda: self.playlist_editor.duplicate_selection()).pack(side="left", padx=(10, 3))
        ttk.Button(view_row, text="Split at playhead", style="Mini.TButton",
                   command=lambda: self.playlist_editor.split_at_playhead()).pack(side="left")
        ttk.Label(view_row, textvariable=self.override_status_var, style="Muted.TLabel").pack(
            side="left", padx=10, fill="x", expand=True,
        )

        ttk.Label(
            panel,
            text="Every track plays whatever its lanes feed it: stem tracks have Primary (P) and Secondary (S) "
                 "sublanes, imported tracks a single Audio (A) lane, so dragging a clip onto another track "
                 "re-sources it. Overlapping Layer clips really play together; Replace clips take over from the "
                 "full-song bed. Select (V) drags clips and marquee-drags a region, Draw (B) paints new clips, "
                 "Razor (R) splits. Drag the ruler to select a time region, then use the region buttons above "
                 "or Ctrl+I split / Ctrl+T trim / Ctrl+L silence / Ctrl+K delete-and-close / Ctrl+Alt+K "
                 "delete-and-gap / Ctrl+J join. Ctrl+D duplicates to a new track, Ctrl+Shift+D in place, "
                 "Alt-drag drags a copy, Ctrl+C/X/V copy-paste at the cursor, S splits at the playhead, "
                 "[ and ] jump clip boundaries. Arrows nudge, Alt+arrows slip source audio, up/down move lanes, "
                 "middle-drag pans, Ctrl+wheel zooms, wheel scrolls tracks. Right-click for the full menu; the "
                 "menu bar lists every command.",
            style="Muted.TLabel", wraplength=680, justify="left",
        ).pack(fill="x", padx=6, pady=(2, 6))

        self.override_tree = ttk.Treeview(
            panel, columns=("track", "source", "mode", "start", "end", "source_start"),
            show="headings", height=3,
        )
        for col, label, width in [
            ("track", "Track", 90), ("source", "Lane", 70), ("mode", "Mode", 70),
            ("start", "Timeline in", 85), ("end", "Timeline out", 85), ("source_start", "Source in", 85),
        ]:
            self.override_tree.heading(col, text=label)
            self.override_tree.column(col, width=width, anchor="center")
        self.override_tree.pack(fill="x", padx=6, pady=(0, 4))
        self.override_tree.bind("<<TreeviewSelect>>", self._select_clips_from_list)
        ttk.Button(panel, text="Remove Selected", command=self._remove_override).pack(
            anchor="e", padx=6, pady=(0, 6)
        )
        return panel

    def _remove_override(self):
        selected = self.override_tree.selection()
        self.playlist_editor.delete_indices([int(iid) for iid in selected])

    def _select_clips_from_list(self, _event=None):
        # Rebuilding the list clears its own selection, which would otherwise
        # wipe the canvas selection after every edit.
        if self._syncing_clip_list:
            return
        indices = [int(iid) for iid in self.override_tree.selection()]
        self.playlist_editor.selection = [
            self.stem_overrides[i] for i in indices if 0 <= i < len(self.stem_overrides)
        ]
        self.playlist_editor._redraw()

    def _show_region(self, start_ms, end_ms):
        fmt = PlaylistEditor._fmt_precise
        if end_ms - start_ms <= 0.5:
            self.region_var.set(f"{fmt(start_ms)}  (cursor)")
        else:
            tracks = len(self.playlist_editor.selected_tracks())
            self.region_var.set(
                f"{fmt(start_ms)} - {fmt(end_ms)}   length {fmt(end_ms - start_ms)}   "
                f"on {tracks} track{'s' if tracks != 1 else ''}"
            )

    def _track_name_for(self, clip):
        for track in self.timeline_tracks:
            if track["id"] == clip.get("track"):
                return track["name"]
        return clip.get("stem") or "?"

    def _refresh_clip_list(self):
        self._syncing_clip_list = True
        try:
            self.override_tree.delete(*self.override_tree.get_children())
            for i, clip in enumerate(self.stem_overrides):
                self.override_tree.insert(
                    "", "end", iid=str(i),
                    values=(
                        self._track_name_for(clip), clip.get("source", "primary").capitalize(),
                        clip.get("mode", "replace").capitalize(),
                        clip["start_ms"], clip["end_ms"], clip.get("source_start_ms", clip["start_ms"]),
                    ),
                )
        finally:
            self._syncing_clip_list = False
        self.playlist_editor.set_clips(self.stem_overrides)
        self.override_undo_button.configure(state="normal" if self.playlist_editor.can_undo else "disabled")
        self.override_redo_button.configure(state="normal" if self.playlist_editor.can_redo else "disabled")

    # ---- menu bar ---------------------------------------------------------

    def _menu(self):
        return tk.Menu(
            self, tearoff=0, bg=SURFACE_ALT, fg=TEXT, activebackground=ACCENT,
            activeforeground=ACCENT_TEXT, disabledforeground=MUTED, bd=0, relief="flat",
        )

    def _build_menubar(self):
        """A DAW-style menu bar for the playlist commands. The accelerators
        shown here are the timeline's own key bindings, so they fire while the
        playlist has focus; the menu items themselves always work."""
        editor = self.playlist_editor
        bar = self._menu()

        file_menu = self._menu()
        file_menu.add_command(label="Open primary track...", command=self._browse_primary)
        file_menu.add_command(label="Open secondary track...", command=self._browse_secondary)
        file_menu.add_command(label="Import audio into playlist...", command=self._import_audio_track)
        file_menu.add_separator()
        file_menu.add_command(label="Swap primary / secondary", command=self._swap_tracks)
        file_menu.add_command(label="Choose output file...", command=self._browse_output)
        file_menu.add_separator()
        file_menu.add_command(label="Render mashup", command=self._render)
        file_menu.add_command(label="Open output folder", command=self._open_output_folder)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.destroy)
        bar.add_cascade(label="File", menu=file_menu)

        edit_menu = self._menu()
        edit_menu.add_command(label="Undo", accelerator="Ctrl+Z", command=editor.undo)
        edit_menu.add_command(label="Redo", accelerator="Ctrl+Y", command=editor.redo)
        edit_menu.add_separator()
        edit_menu.add_command(label="Cut", accelerator="Ctrl+X", command=editor.cut_selection)
        edit_menu.add_command(label="Copy", accelerator="Ctrl+C", command=editor.copy_selection)
        edit_menu.add_command(label="Paste at cursor", accelerator="Ctrl+V", command=editor.paste_clipboard)
        edit_menu.add_command(label="Delete clips", accelerator="Del", command=editor.delete_selected)
        edit_menu.add_command(label="Duplicate to new track", accelerator="Ctrl+D",
                              command=editor.duplicate_to_new_track)
        edit_menu.add_command(label="Duplicate in place", accelerator="Ctrl+Shift+D",
                              command=editor.duplicate_selection)
        edit_menu.add_separator()

        remove_menu = self._menu()
        remove_menu.add_command(label="Delete and close gap", accelerator="Ctrl+K",
                                command=editor.ripple_delete_selection)
        remove_menu.add_command(label="Cut and leave gap", accelerator="Ctrl+Alt+X",
                                command=editor.split_cut_selection)
        remove_menu.add_command(label="Delete and leave gap", accelerator="Ctrl+Alt+K",
                                command=editor.split_delete_selection)
        remove_menu.add_command(label="Silence audio", accelerator="Ctrl+L",
                                command=editor.silence_selection)
        remove_menu.add_command(label="Trim audio", accelerator="Ctrl+T",
                                command=editor.trim_to_selection)
        edit_menu.add_cascade(label="Remove special", menu=remove_menu)

        boundary_menu = self._menu()
        boundary_menu.add_command(label="Split", accelerator="Ctrl+I", command=editor.split_at_selection)
        boundary_menu.add_command(label="Split new", accelerator="Ctrl+Alt+I",
                                  command=editor.split_to_new_track)
        boundary_menu.add_command(label="Split at playhead", accelerator="S",
                                  command=editor.split_at_playhead)
        boundary_menu.add_command(label="Join", accelerator="Ctrl+J", command=editor.join_selection)
        edit_menu.add_cascade(label="Clip boundaries", menu=boundary_menu)
        edit_menu.add_separator()
        edit_menu.add_command(label="Mute / unmute clips", accelerator="Ctrl+M",
                              command=editor.toggle_mute_selection)
        bar.add_cascade(label="Edit", menu=edit_menu)

        select_menu = self._menu()
        select_menu.add_command(label="All", accelerator="Ctrl+A", command=editor.select_all)
        select_menu.add_command(label="None", accelerator="Ctrl+Shift+A", command=editor.select_none)
        select_menu.add_separator()
        select_menu.add_command(label="Cursor to previous clip boundary", accelerator="[",
                                command=lambda: editor.cursor_to_boundary(-1))
        select_menu.add_command(label="Cursor to next clip boundary", accelerator="]",
                                command=lambda: editor.cursor_to_boundary(1))
        select_menu.add_command(label="Extend to previous boundary", accelerator="Shift+{",
                                command=lambda: editor.cursor_to_boundary(-1, extend=True))
        select_menu.add_command(label="Extend to next boundary", accelerator="Shift+}",
                                command=lambda: editor.cursor_to_boundary(1, extend=True))
        select_menu.add_separator()
        select_menu.add_command(label="Cursor to start", accelerator="Home",
                                command=lambda: editor.set_region(0))
        select_menu.add_command(label="Cursor to end", accelerator="End",
                                command=lambda: editor.set_region(editor.total_ms))
        bar.add_cascade(label="Select", menu=select_menu)

        tracks_menu = self._menu()
        add_menu = self._menu()
        for name in STEM_NAMES:
            add_menu.add_command(label=name.capitalize(), command=lambda s=name: self._add_stem_track(s))
        tracks_menu.add_cascade(label="Add stem track", menu=add_menu)
        tracks_menu.add_command(label="Import audio...", command=self._import_audio_track)
        tracks_menu.add_separator()
        tracks_menu.add_command(label="Duplicate selected track",
                                command=lambda: self._with_active_track(self._duplicate_track))
        tracks_menu.add_command(label="Rename selected track...",
                                command=lambda: self._with_active_track(self._rename_track))
        tracks_menu.add_command(label="Remove selected track",
                                command=lambda: self._with_active_track(self._remove_track))
        tracks_menu.add_separator()
        move_menu = self._menu()
        for label, where in (("Up", "up"), ("Down", "down"), ("To top", "top"), ("To bottom", "bottom")):
            move_menu.add_command(
                label=label,
                command=lambda w=where: self._with_active_track(lambda t: editor.move_track(t, w)),
            )
        tracks_menu.add_cascade(label="Move selected track", menu=move_menu)
        tracks_menu.add_separator()
        tracks_menu.add_command(label="Mute all tracks", command=lambda: self._set_all_tracks("mute", True))
        tracks_menu.add_command(label="Unmute all tracks", command=lambda: self._set_all_tracks("mute", False))
        tracks_menu.add_command(label="Clear all solos", command=lambda: self._set_all_tracks("solo", False))
        tracks_menu.add_separator()
        tracks_menu.add_command(label="Sort tracks by name", command=lambda: self._sort_tracks("name"))
        tracks_menu.add_command(label="Sort tracks by first clip", command=lambda: self._sort_tracks("time"))
        bar.add_cascade(label="Tracks", menu=tracks_menu)

        view_menu = self._menu()
        view_menu.add_command(label="Zoom in", accelerator="Ctrl+Wheel", command=editor.zoom_in)
        view_menu.add_command(label="Zoom out", command=editor.zoom_out)
        view_menu.add_command(label="Zoom normal", accelerator="Ctrl+2", command=editor.zoom_normal)
        view_menu.add_command(label="Zoom to selection", accelerator="Ctrl+E",
                              command=editor.zoom_to_selection)
        view_menu.add_command(label="Fit in window", accelerator="Ctrl+F", command=editor.fit_view)
        view_menu.add_command(label="Zoom toggle", accelerator="Shift+Z", command=editor.zoom_toggle)
        bar.add_cascade(label="View", menu=view_menu)

        # Windows draws a toplevel's menu with the native, light-themed
        # menubar that Tk cannot restyle, so the strip lives inside the window
        # as themed menubuttons instead. The dropdowns are the same menus.
        self.menus = {
            "File": file_menu, "Edit": edit_menu, "Select": select_menu,
            "Tracks": tracks_menu, "View": view_menu,
        }
        strip = ttk.Frame(self, style="Menubar.TFrame")
        strip.pack(side="top", fill="x", before=self._scroll_outer)
        for label, menu in self.menus.items():
            button = ttk.Menubutton(strip, text=label, menu=menu, direction="below",
                                    style="Menubar.TMenubutton")
            button.pack(side="left", padx=1, pady=1)
        ttk.Separator(self, orient="horizontal").pack(
            side="top", fill="x", before=self._scroll_outer
        )
        self.menubar = strip

    def _with_active_track(self, action):
        track = self.playlist_editor._active_track()
        if track is None:
            messagebox.showinfo("No track selected", "Click a track header on the timeline first.")
            return
        action(track)

    def _set_all_tracks(self, key, value):
        for track in self.timeline_tracks:
            track[key] = value
        self._refresh_track_panel()
        self.override_status_var.set(f"{'Set' if value else 'Cleared'} {key} on every track")

    def _sort_tracks(self, by):
        def first_clip(track):
            starts = [int(c["start_ms"]) for c in self.stem_overrides if c.get("track") == track["id"]]
            return min(starts) if starts else float("inf")

        key = (lambda t: t["name"].lower()) if by == "name" else first_clip
        self.timeline_tracks.sort(key=key)
        self._refresh_track_panel()
        self.override_status_var.set(f"Sorted tracks by {by}")

    # ---- selection-to-new-track commands ---------------------------------

    def _selection_to_new_tracks(self, move):
        """Audacity's Duplicate / Split New: copy (or move) the selected clips
        onto fresh tracks that mirror the ones they came from."""
        editor = self.playlist_editor
        selection = list(editor.selection)
        if not selection:
            self.override_status_var.set("Select clips first")
            return
        by_track = {}
        for clip in selection:
            index = editor._track_index_of(clip)
            if index is not None:
                by_track.setdefault(self.timeline_tracks[index]["id"], []).append(clip)

        editor._begin_transaction()
        made = []
        for track_id, clips in by_track.items():
            source = next(t for t in self.timeline_tracks if t["id"] == track_id)
            fresh = dict(source)
            fresh["id"] = _new_track_id()
            fresh["name"] = self._unique_track_name(source["name"])
            fresh["bed"] = "muted"
            fresh["solo"] = False
            self.timeline_tracks.insert(self.timeline_tracks.index(source) + 1, fresh)
            if source["id"] in self._imported_durations:
                self._imported_durations[fresh["id"]] = self._imported_durations[source["id"]]
            for clip in clips:
                if move:
                    clip["track"] = fresh["id"]
                    made.append(clip)
                else:
                    copy = dict(clip, track=fresh["id"])
                    self.stem_overrides.append(copy)
                    made.append(copy)
        editor.selection = made
        editor._commit_transaction(
            f"{'Moved' if move else 'Duplicated'} {len(made)} clip(s) to {len(by_track)} new track(s)"
        )
        self._refresh_track_panel()

    def _build_output_row(self):
        frame = ttk.LabelFrame(self.content, text=" Output ")
        frame.pack(fill="x", padx=18, pady=6)
        inner = ttk.Frame(frame)
        inner.pack(fill="x", padx=10, pady=10)
        inner.columnconfigure(0, weight=1)
        ttk.Entry(inner, textvariable=self.output_path).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(inner, text="Browse...", command=self._browse_output).grid(row=0, column=1)
        ttk.Label(
            inner, text="Auto-named from your two tracks until you edit it or pick your own path.",
            style="Muted.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

    def _build_status_row(self):
        frame = ttk.Frame(self.content)
        frame.pack(fill="x", padx=18, pady=(6, 6))
        self.status_label = ttk.Label(frame, textvariable=self.status_var, style="Muted.TLabel", wraplength=740, justify="left")
        self.status_label.pack(fill="x")
        self.progress = ttk.Progressbar(frame, mode="indeterminate", style="Accent.Horizontal.TProgressbar")
        self.progress.pack(fill="x", pady=(6, 0))

    def _build_action_buttons(self):
        frame = ttk.Frame(self.content)
        frame.pack(fill="x", padx=18, pady=(4, 18))
        self.render_button = ttk.Button(frame, text="▶  Render Mashup", style="Accent.TButton", command=self._render)
        self.render_button.pack(side="left")
        self.play_button = ttk.Button(frame, text="\U0001F50A  Play Result", command=self._play_result, state="disabled")
        self.play_button.pack(side="left", padx=8)
        self.open_folder_button = ttk.Button(frame, text="\U0001F4C2  Open Output Folder", command=self._open_output_folder)
        self.open_folder_button.pack(side="left")

    def _show_mode_frame(self):
        for frame in self.mode_frames.values():
            frame.pack_forget()
        self.shared_timeline.pack_forget()
        mode = self.mode_var.get()
        if mode != MODE_STEMS:
            self.shared_timeline.pack(fill="x", padx=8, pady=(8, 4))
        self.mode_frames[mode].pack(fill="x")
        if mode != MODE_STEMS:
            self._sync_shared_timeline(mode)

    def _sync_shared_timeline(self, mode):
        scale = self._offset_scales[mode]
        self.shared_timeline.set_offset(scale.get())
        self.shared_timeline.on_offset_change = lambda ms: scale.set(ms)

    def _on_offset_slider_change(self, mode, value):
        if self.mode_var.get() == mode:
            self.shared_timeline.set_offset(value)
        self._update_timelines()

    # ---- file dialogs ------------------------------------------------

    def _browse_primary(self):
        path = filedialog.askopenfilename(
            title="Choose primary MP3", initialdir=self.last_source_dir, filetypes=[("MP3 files", "*.mp3")]
        )
        if path:
            self.primary_path.set(path)
            self._remember_source_dir(path)
            self._refresh_duration("primary")

    def _browse_secondary(self):
        path = filedialog.askopenfilename(
            title="Choose secondary MP3", initialdir=self.last_source_dir, filetypes=[("MP3 files", "*.mp3")]
        )
        if path:
            self.secondary_path.set(path)
            self._remember_source_dir(path)
            self._refresh_duration("secondary")

    def _clear_primary(self):
        self.primary_path.set("")
        self._refresh_duration("primary")

    def _clear_secondary(self):
        self.secondary_path.set("")
        self._refresh_duration("secondary")

    def _swap_tracks(self):
        primary, secondary = self.primary_path.get(), self.secondary_path.get()
        self.primary_path.set(secondary)
        self.secondary_path.set(primary)
        self._refresh_duration("primary")
        self._refresh_duration("secondary")

    def _remember_source_dir(self, path):
        self.last_source_dir = str(Path(path).parent)
        self.prefs["source_dir"] = self.last_source_dir
        _save_prefs(self.prefs)

    def _refresh_duration(self, which):
        from . import audio_io

        path = self.primary_path.get() if which == "primary" else self.secondary_path.get()
        try:
            duration_ms = audio_io.get_duration_ms(path) if path else 0
        except Exception:
            duration_ms = 0

        label_text = OffsetTimeline._fmt(duration_ms) if duration_ms else ""
        if which == "primary":
            self.primary_duration_ms = duration_ms
            self.primary_length_label.config(text=label_text)
        else:
            self.secondary_duration_ms = duration_ms
            self.secondary_length_label.config(text=label_text)

        self._update_timelines()
        self._maybe_autofill_output()

    def _update_timelines(self):
        self.shared_timeline.set_durations(self.primary_duration_ms, self.secondary_duration_ms)
        self.playlist_editor.set_total_ms(self._timeline_total_ms())

    def _timeline_total_ms(self):
        """How long the playlist runs: the two songs, plus any imported track
        that plays as a full-song bed, plus whatever clips reach past them."""
        candidates = [
            1000,
            self.primary_duration_ms,
            self.stems_offset.get() + self.secondary_duration_ms,
        ]
        for track in self.timeline_tracks:
            if track["kind"] == "audio" and track["bed"] != "muted":
                candidates.append(self._imported_durations.get(track["id"], 0))
        candidates.extend(int(clip["end_ms"]) for clip in self.stem_overrides)
        return int(max(candidates))

    def _on_output_path_edited(self, *_args):
        if self._setting_output_programmatically:
            return
        self._output_is_auto = False

    def _maybe_autofill_output(self):
        if not self._output_is_auto:
            return
        primary, secondary = self.primary_path.get(), self.secondary_path.get()
        if not primary or not secondary:
            return
        name = f"{Path(primary).stem}_x_{Path(secondary).stem}.mp3"
        self._setting_output_programmatically = True
        try:
            self.output_path.set(str(Path(self.last_output_dir) / name))
        finally:
            self._setting_output_programmatically = False

    def _browse_output(self):
        path = filedialog.asksaveasfilename(
            title="Save mashup as", initialdir=self.last_output_dir, defaultextension=".mp3",
            filetypes=[("MP3 files", "*.mp3")],
        )
        if path:
            self.output_path.set(path)
            self.last_output_dir = str(Path(path).parent)
            self.prefs["output_dir"] = self.last_output_dir
            _save_prefs(self.prefs)

    def _open_output_folder(self):
        folder = Path(self.last_output_path).parent if self.last_output_path else Path(self.output_path.get()).parent
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(str(folder))  # noqa: S606 - user-initiated, local folder only
        except OSError as exc:
            messagebox.showerror("Can't open folder", str(exc))

    # ---- validation ----------------------------------------------------

    def _validate_inputs(self) -> bool:
        if not self.primary_path.get() or not Path(self.primary_path.get()).exists():
            messagebox.showerror("Missing file", "Choose a valid primary MP3 file.")
            return False
        if not self.secondary_path.get() or not Path(self.secondary_path.get()).exists():
            messagebox.showerror("Missing file", "Choose a valid secondary MP3 file.")
            return False
        if not self.output_path.get():
            messagebox.showerror("Missing output", "Choose an output path.")
            return False
        return True

    # ---- analyze (mode 2 helper) ----------------------------------------

    def _analyze(self):
        if not self._validate_inputs():
            return
        self.bpm_label.config(text="Detected BPM: analyzing...")
        threading.Thread(target=self._analyze_worker, daemon=True).start()

    def _analyze_worker(self):
        from . import analysis

        try:
            primary_bpm = analysis.detect_bpm(self.primary_path.get())
            secondary_bpm = analysis.detect_bpm(self.secondary_path.get())
            self.render_queue.put(("bpm", (primary_bpm, secondary_bpm)))
        except Exception as exc:  # noqa: BLE001 - surface any analysis failure to the user
            self.render_queue.put(("error", str(exc)))

    # ---- render ----------------------------------------------------

    def _render(self):
        if not self._validate_inputs():
            return
        if self.render_thread and self.render_thread.is_alive():
            return

        self.render_button.config(state="disabled")
        self.play_button.config(state="disabled")
        self.progress.start(10)
        self.status_label.configure(style="Working.TLabel")
        self.status_var.set("Starting render...")

        self.render_thread = threading.Thread(target=self._render_worker, daemon=True)
        self.render_thread.start()

    def _queue_status(self, message: str):
        self.render_queue.put(("status", message))

    def _stem_source_map(self):
        """The per-stem bed the sidechain duck triggers off: the first stem
        track that actually plays a full-song bed for that stem."""
        mapping = {}
        for track in self.timeline_tracks:
            if track["kind"] != "stem" or track.get("mute") or track["bed"] == "muted":
                continue
            mapping.setdefault(track["stem"], track["bed"])
        return {name: mapping.get(name, "muted") for name in STEM_NAMES}

    def _render_worker(self):
        from . import audio_io

        mode = self.mode_var.get()
        try:
            if mode == MODE_SIMPLE:
                segment = mixer.simple_overlay(
                    self.primary_path.get(),
                    self.secondary_path.get(),
                    offset_ms=int(self.simple_offset.get()),
                    primary_gain_db=self.simple_primary_gain.get(),
                    secondary_gain_db=self.simple_secondary_gain.get(),
                    crossfade_ms=int(self.simple_crossfade.get()),
                    match_loudness=self.simple_match_loudness_var.get(),
                    low_cut_secondary=self.simple_low_cut_var.get(),
                    duck_amount=self.simple_duck_amount.get() / 100.0,
                    reverb_amount=self.simple_reverb_amount.get() / 100.0,
                    reverb_size=self.simple_reverb_size.get() / 100.0,
                    primary_reverb_amount=self.simple_primary_reverb_amount.get() / 100.0,
                    progress_callback=self._queue_status,
                )
            elif mode == MODE_BEAT_SYNCED:
                segment = mixer.beat_synced_blend(
                    self.primary_path.get(),
                    self.secondary_path.get(),
                    blend_start_ms=int(self.beat_start.get()),
                    blend_duration_ms=int(self.beat_duration.get()),
                    primary_gain_db=self.beat_primary_gain.get(),
                    secondary_gain_db=self.beat_secondary_gain.get(),
                    match_loudness=self.beat_match_loudness_var.get(),
                    low_cut_secondary=self.beat_low_cut_var.get(),
                    duck_amount=self.beat_duck_amount.get() / 100.0,
                    reverb_amount=self.beat_reverb_amount.get() / 100.0,
                    reverb_size=self.beat_reverb_size.get() / 100.0,
                    primary_reverb_amount=self.beat_primary_reverb_amount.get() / 100.0,
                    progress_callback=self._queue_status,
                )
            elif mode == MODE_VOCALS:
                if self.vocals_from_var.get() == "Primary":
                    vocal_source, instrumental_source = self.primary_path.get(), self.secondary_path.get()
                else:
                    vocal_source, instrumental_source = self.secondary_path.get(), self.primary_path.get()
                segment = mixer.vocals_over_instrumental(
                    vocal_source,
                    instrumental_source,
                    tempo_match=self.tempo_match_var.get(),
                    key_match=self.key_match_var.get(),
                    offset_ms=int(self.vocals_offset.get()),
                    vocal_gain_db=self.vocals_gain.get(),
                    instrumental_gain_db=self.instrumental_gain.get(),
                    match_loudness=self.vocals_match_loudness_var.get(),
                    carve_for_vocal=self.vocals_carve_var.get(),
                    duck_amount=self.vocals_duck_amount.get() / 100.0,
                    reverb_amount=self.vocals_reverb_amount.get() / 100.0,
                    reverb_size=self.vocals_reverb_size.get() / 100.0,
                    instrumental_reverb_amount=self.vocals_instrumental_reverb_amount.get() / 100.0,
                    progress_callback=self._queue_status,
                )
            else:  # MODE_STEMS
                segment = mixer.stem_mix(
                    self.primary_path.get(),
                    self.secondary_path.get(),
                    stem_sources=self._stem_source_map(),
                    stem_overrides=[dict(clip) for clip in self.stem_overrides],
                    timeline_tracks=[dict(track) for track in self.timeline_tracks],
                    offset_ms=int(self.stems_offset.get()),
                    primary_gain_db=self.stems_primary_gain.get(),
                    secondary_gain_db=self.stems_secondary_gain.get(),
                    tempo_match=self.stems_tempo_match_var.get(),
                    match_loudness=self.stems_match_loudness_var.get(),
                    duck_amount=self.stems_duck_amount.get() / 100.0,
                    reverb_amount=self.stems_reverb_amount.get() / 100.0,
                    reverb_size=self.stems_reverb_size.get() / 100.0,
                    primary_reverb_amount=self.stems_primary_reverb_amount.get() / 100.0,
                    auto_fallback=self.stems_auto_fallback_var.get(),
                    override_crossfade_ms=int(self.stems_override_crossfade.get()),
                    progress_callback=self._queue_status,
                )

            self._queue_status("Exporting mp3...")
            output_path = self.output_path.get()
            audio_io.export_mp3(segment, output_path)
            self.render_queue.put(("done", output_path))
        except Exception as exc:  # noqa: BLE001 - surface any render failure to the user
            self.render_queue.put(("error", str(exc)))

    def _play_result(self):
        if self.last_output_path and Path(self.last_output_path).exists():
            os.startfile(self.last_output_path)  # noqa: S606 - user-initiated, local file only

    # ---- queue polling ----------------------------------------------------

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.render_queue.get_nowait()
                if kind == "status":
                    self.status_label.configure(style="Working.TLabel")
                    self.status_var.set(payload)
                elif kind == "bpm":
                    primary_bpm, secondary_bpm = payload
                    self.bpm_label.config(text=f"Detected BPM: primary {primary_bpm:.1f}, secondary {secondary_bpm:.1f}")
                elif kind == "done":
                    self.progress.stop()
                    self.render_button.config(state="normal")
                    self.play_button.config(state="normal")
                    self.last_output_path = payload
                    self.status_label.configure(style="Success.TLabel")
                    self.status_var.set(f"Done. Saved to {payload}")
                elif kind == "error":
                    self.progress.stop()
                    self.render_button.config(state="normal")
                    self.status_label.configure(style="Error.TLabel")
                    self.status_var.set("Failed - see error dialog.")
                    messagebox.showerror("Render failed", payload)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)


def main():
    app = MashupApp()
    app.mainloop()
