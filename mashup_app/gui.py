"""Tkinter GUI: pick two MP3s, choose a mashup mode, tweak its controls, render."""
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

# ---- palette ------------------------------------------------------------
BG = "#f4f5f8"
SURFACE = "#ffffff"
BORDER = "#d9dce3"
TEXT = "#1f2430"
MUTED = "#6b7280"
ACCENT = "#2563eb"
ACCENT_ACTIVE = "#1d4ed8"
ACCENT_TEXT = "#ffffff"
SUCCESS = "#15803d"
ERROR = "#b91c1c"
TIMELINE_BG = "#eef1f6"
TIMELINE_PRIMARY = "#64748b"
TIMELINE_SECONDARY = ACCENT


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
            self.tip, text=self.text, background="#1f2430", foreground="#ffffff",
            font=("Segoe UI", 8), padx=8, pady=5, wraplength=280, justify="left",
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


class StemSourceClip(tk.Label):
    """A source chip that can be dragged onto a StemOverrideEditor lane."""

    def __init__(self, parent, text, source, editor_getter, on_select=None, **kwargs):
        color = TIMELINE_SECONDARY if source == "secondary" else TIMELINE_PRIMARY
        super().__init__(
            parent, text=text, bg=color, fg="white", padx=10, pady=5,
            relief="raised", bd=1, cursor="hand2", font=("Segoe UI", 8, "bold"), **kwargs,
        )
        self.source = source
        self.editor_getter = editor_getter
        self.on_select = on_select
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_motion)
        self.bind("<ButtonRelease-1>", self._on_release)

    def _on_press(self, event):
        if self.on_select:
            self.on_select(self.source)
        fraction = event.x / max(1, self.winfo_width())
        self.editor_getter().begin_source_drag(self.source, fraction)

    def _on_motion(self, event):
        self.editor_getter().drag_source(event.x_root, event.y_root, getattr(event, "state", 0))

    def _on_release(self, event):
        self.editor_getter().end_source_drag(event.x_root, event.y_root, getattr(event, "state", 0))


class StemOverrideEditor(tk.Canvas):
    """Precise multi-lane editor for time-based stem source clips.

    The renderer-facing data remains the original flat list of override
    dictionaries. Viewport, selection, previews, and history are UI-only.
    """

    LABEL_W = 68
    EDGE_PX = 7
    MIN_LEN_MS = 250
    MIN_VIEW_MS = 1000
    AXIS_H = 22
    HISTORY_LIMIT = 50
    ALT_MASK = 0x0008

    BG = TIMELINE_BG
    PRIMARY_TINT = "#e4e7ec"
    SECONDARY_TINT = "#dbe6fb"
    SELECTED = "#f59e0b"

    def __init__(
        self, parent, stems, lane_h=34, get_default_source=None,
        get_paint_source=None, get_clip_length_ms=None, get_snap_enabled=None,
        on_change=None, on_status=None, on_tool_change=None,
    ):
        self.stems = list(stems)
        self.lane_h = lane_h
        total_h = lane_h * len(self.stems) + self.AXIS_H
        super().__init__(
            parent, height=total_h, bg=self.BG, takefocus=True,
            highlightthickness=1, highlightbackground=BORDER,
        )
        self.total_ms = 60000
        self.view_start_ms = 0.0
        self.view_span_ms = float(self.total_ms)
        self.overrides: list = []
        self.get_default_source = get_default_source or (lambda _stem: "primary")
        self.get_paint_source = get_paint_source or (lambda: "secondary")
        self.get_clip_length_ms = get_clip_length_ms or (lambda: 8000)
        self.get_snap_enabled = get_snap_enabled or (lambda: True)
        self.on_change = on_change
        self.on_status = on_status
        self.on_tool_change = on_tool_change
        self._xscrollcommand = None
        self.tool = "select"

        self.undo_stack: list = []
        self.redo_stack: list = []
        self._transaction_before = None
        self._selected_ov = None
        self._drag_mode = None  # None | create | move | resize-left | resize-right
        self._drag_ov = None
        self._drag_anchor_ms = 0.0
        self._drag_grab_offset_ms = 0.0
        self._drag_len_ms = 0.0
        self._external_drag = None
        self._feedback = None  # (x, y, text, guide_ms, lane)

        self.bind("<Configure>", lambda _e: self._redraw())
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Double-Button-1>", self._on_double_click)
        self.bind("<Button-3>", self._on_right_click)
        self.bind("<Motion>", self._on_hover)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Control-z>", lambda _e: self.undo())
        self.bind("<Control-y>", lambda _e: self.redo())
        self.bind("<Control-Shift-Z>", lambda _e: self.redo())
        self.bind("<Delete>", lambda _e: self.delete_selected())
        self.bind("<Key-r>", lambda _e: self.set_tool("razor"))
        self.bind("<Key-v>", lambda _e: self.set_tool("select"))
        self.bind("<Escape>", self._cancel_interaction)
        self.bind("<Control-MouseWheel>", self._on_zoom_wheel)
        self.bind("<Shift-MouseWheel>", self._on_scroll_wheel)

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

    def set_overrides(self, overrides):
        if overrides is not self.overrides:
            self.overrides = overrides
            self.undo_stack.clear()
            self.redo_stack.clear()
            self._selected_ov = None
        self._redraw()

    def set_tool(self, tool):
        if tool not in ("select", "razor"):
            return
        self.tool = tool
        if self.on_tool_change:
            self.on_tool_change(tool)
        self.configure(cursor="crosshair" if tool == "razor" else "")
        self.focus_set()
        self._redraw()

    def set_xscrollcommand(self, command):
        self._xscrollcommand = command
        self._update_scrollbar()

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
        self.redo_stack.append(self._copy_overrides())
        self._restore_snapshot(self.undo_stack.pop())
        self._notify_change("Undid edit")
        return "break"

    def redo(self):
        if not self.redo_stack:
            return "break"
        self.undo_stack.append(self._copy_overrides())
        self._restore_snapshot(self.redo_stack.pop())
        self._notify_change("Redid edit")
        return "break"

    def delete_selected(self):
        if self._selected_ov not in self.overrides:
            return "break"
        self._begin_transaction()
        self.overrides.remove(self._selected_ov)
        self._selected_ov = None
        self._commit_transaction("Deleted clip")
        return "break"

    def delete_indices(self, indices):
        valid = sorted({i for i in indices if 0 <= i < len(self.overrides)}, reverse=True)
        if not valid:
            return
        self._begin_transaction()
        for index in valid:
            del self.overrides[index]
        self._selected_ov = None
        self._commit_transaction(f"Deleted {len(valid)} clip{'s' if len(valid) != 1 else ''}")

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
        lane = self._lane_at(y)
        if lane is None or x < self.LABEL_W or x > self.winfo_width():
            self._external_drag["preview"] = None
            self._feedback = None
            self._redraw()
            return
        duration = self._external_drag["duration_ms"]
        raw_start = self._x_to_ms(x) - duration * self._external_drag["grab_fraction"]
        start = self._snap_point(raw_start, state)
        start = max(0.0, min(self.total_ms - duration, start))
        preview = {
            "stem": self.stems[lane], "start_ms": int(round(start)),
            "end_ms": int(round(start + duration)), "source": self._external_drag["source"],
        }
        self._external_drag["preview"] = preview
        self._set_feedback(x, y, f"Drop {preview['source'].capitalize()}  {self._range_text(preview)}", start, lane)
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
            self.overrides.append(preview)
            self._selected_ov = preview
            self._commit_transaction(f"Added {preview['source']} clip")
        else:
            self._set_status("Drop cancelled")
            self._redraw()

    # ---- coordinate mapping and viewport --------------------------------

    def _timeline_w(self):
        return max(1, self.winfo_width() - self.LABEL_W)

    def _view_end_ms(self):
        return self.view_start_ms + self.view_span_ms

    def _ms_to_x(self, ms):
        return self.LABEL_W + ((ms - self.view_start_ms) / self.view_span_ms) * self._timeline_w()

    def _x_to_ms(self, x):
        fraction = (x - self.LABEL_W) / self._timeline_w()
        return max(0.0, min(self.total_ms, self.view_start_ms + fraction * self.view_span_ms))

    def _lane_at(self, y):
        idx = int(y // self.lane_h)
        return idx if 0 <= idx < len(self.stems) else None

    def _clamp_view(self):
        self.view_span_ms = max(self.MIN_VIEW_MS, min(float(self.total_ms), self.view_span_ms))
        self.view_start_ms = max(0.0, min(self.total_ms - self.view_span_ms, self.view_start_ms))

    def _view_fractions(self):
        return (
            self.view_start_ms / self.total_ms,
            min(1.0, self._view_end_ms() / self.total_ms),
        )

    def _update_scrollbar(self):
        if self._xscrollcommand:
            self._xscrollcommand(*self._view_fractions())

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

    def _range_text(self, ov):
        duration = ov["end_ms"] - ov["start_ms"]
        return (
            f"{self._fmt_precise(ov['start_ms'])} - {self._fmt_precise(ov['end_ms'])}"
            f"  ({self._fmt_duration(duration)})"
        )

    # ---- snapping and hit testing ----------------------------------------

    def _snap_active(self, event=None):
        state = event if isinstance(event, int) else getattr(event, "state", 0)
        return bool(self.get_snap_enabled()) and not (state & self.ALT_MASK)

    def _snap_point(self, ms, event=None, exclude=None):
        ms = max(0.0, min(self.total_ms, ms))
        if not self._snap_active(event):
            return ms
        candidates = [round(ms / 1000) * 1000, 0, self.total_ms]
        for ov in self.overrides:
            if ov is not exclude:
                candidates.extend((ov["start_ms"], ov["end_ms"]))
        nearest = min(candidates, key=lambda candidate: abs(candidate - ms))
        threshold = max(25.0, min(250.0, self.view_span_ms / self._timeline_w() * 8))
        return float(nearest) if abs(nearest - ms) <= threshold else ms

    def _snap_move(self, start, length, event=None, exclude=None):
        if not self._snap_active(event):
            return start
        snapped_start = self._snap_point(start, event, exclude)
        snapped_end = self._snap_point(start + length, event, exclude) - length
        return snapped_start if abs(snapped_start - start) <= abs(snapped_end - start) else snapped_end

    def _region_at(self, stem, ms):
        # Later entries win on overlap, matching mixer._resolve_regions.
        for ov in reversed(self.overrides):
            if ov["stem"] == stem and ov["start_ms"] <= ms <= ov["end_ms"]:
                return ov
        return None

    def _hit_test(self, event):
        if event.x < self.LABEL_W:
            return None, None
        lane = self._lane_at(event.y)
        if lane is None:
            return None, None
        stem = self.stems[lane]
        ms = self._x_to_ms(event.x)
        edge_ms = self.EDGE_PX / self._timeline_w() * self.view_span_ms
        # Test handles before interiors so both halves of an edge's visual
        # hit area work, including the few pixels immediately outside a clip.
        for ov in reversed(self.overrides):
            if ov["stem"] != stem:
                continue
            if abs(ms - ov["start_ms"]) <= edge_ms:
                return stem, ("resize-left", ov)
            if abs(ms - ov["end_ms"]) <= edge_ms:
                return stem, ("resize-right", ov)
        ov = self._region_at(stem, ms)
        if ov is None:
            return stem, None
        return stem, ("move", ov)

    # ---- history ---------------------------------------------------------

    def _copy_overrides(self):
        return [dict(ov) for ov in self.overrides]

    def _restore_snapshot(self, snapshot):
        self.overrides[:] = [dict(ov) for ov in snapshot]
        self._selected_ov = None
        self._redraw()

    def _begin_transaction(self):
        if self._transaction_before is None:
            self._transaction_before = self._copy_overrides()

    def _commit_transaction(self, message):
        before = self._transaction_before
        self._transaction_before = None
        after = self._copy_overrides()
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

    def _redraw(self):
        self.delete("all")
        w = max(1, self.winfo_width())
        lane_bottom = len(self.stems) * self.lane_h

        for i, stem in enumerate(self.stems):
            top = i * self.lane_h
            bottom = top + self.lane_h - 2
            tint = self.SECONDARY_TINT if self.get_default_source(stem) == "secondary" else self.PRIMARY_TINT
            self.create_rectangle(self.LABEL_W, top, w, bottom, fill=tint, outline="")
            self.create_text(
                6, (top + bottom) / 2, text=stem.capitalize(), anchor="w",
                fill=TEXT, font=("Segoe UI", 8, "bold"),
            )

        step = self._tick_interval()
        first_tick = int(self.view_start_ms // step) * step
        tick = first_tick
        while tick <= self._view_end_ms() + step:
            if tick >= self.view_start_ms:
                x = self._ms_to_x(tick)
                self.create_line(x, 0, x, lane_bottom, fill="#d3d8e2", dash=(2, 3))
                self.create_text(x + 2, lane_bottom + 2, text=self._fmt_precise(tick), anchor="nw", fill=MUTED, font=("Segoe UI", 7))
            tick += step

        for i in range(1, len(self.stems)):
            y = i * self.lane_h
            self.create_line(0, y, w, y, fill=BORDER)

        for ov in self.overrides:
            self._draw_clip(ov, selected=(ov is self._selected_ov))

        if self._external_drag and self._external_drag.get("preview"):
            self._draw_clip(self._external_drag["preview"], ghost=True)

        self.create_line(self.LABEL_W, 0, self.LABEL_W, lane_bottom + self.AXIS_H, fill=BORDER)
        if self._feedback:
            x, y, text, guide_ms, lane = self._feedback
            if guide_ms is not None and lane is not None:
                guide_x = self._ms_to_x(guide_ms)
                top = lane * self.lane_h
                self.create_line(guide_x, top, guide_x, top + self.lane_h - 2, fill=self.SELECTED, width=2)
            self._draw_feedback(x, y, text)
        self._update_scrollbar()

    def _draw_clip(self, ov, selected=False, ghost=False):
        if ov["stem"] not in self.stems:
            return
        x0, x1 = self._ms_to_x(ov["start_ms"]), self._ms_to_x(ov["end_ms"])
        if x1 < self.LABEL_W or x0 > self.winfo_width():
            return
        lane = self.stems.index(ov["stem"])
        top = lane * self.lane_h + 3
        bottom = (lane + 1) * self.lane_h - 4
        x0 = max(self.LABEL_W, x0)
        x1 = min(self.winfo_width(), x1)
        color = TIMELINE_SECONDARY if ov["source"] == "secondary" else TIMELINE_PRIMARY
        outline = self.SELECTED if selected or ghost else "#ffffff"
        item = _rounded_rect(
            self, x0, top, max(x0 + 3, x1), bottom, radius=4,
            fill=color, outline=outline, width=2 if selected or ghost else 1,
        )
        if ghost:
            self.itemconfigure(item, stipple="gray50")
        if selected and x1 - x0 > 16:
            self.create_line(x0 + 4, top + 5, x0 + 4, bottom - 5, fill="white", width=2)
            self.create_line(x1 - 4, top + 5, x1 - 4, bottom - 5, fill="white", width=2)
        if x1 - x0 > 72:
            label = f"{ov['source'].capitalize()}  {self._range_text(ov)}"
            self.create_text((x0 + x1) / 2, (top + bottom) / 2, text=label, fill="white", font=("Segoe UI", 7, "bold"))

    def _draw_feedback(self, x, y, text):
        x = max(self.LABEL_W + 4, min(self.winfo_width() - 8, x + 12))
        y = max(4, min(len(self.stems) * self.lane_h - 24, y - 24))
        text_id = self.create_text(x, y, text=text, anchor="nw", fill="white", font=("Segoe UI", 8, "bold"))
        box = self.bbox(text_id)
        if box:
            pad = 4
            bg = self.create_rectangle(
                box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad,
                fill=TEXT, outline="", tags="feedback-bg",
            )
            self.tag_lower(bg, text_id)

    # ---- mouse interaction ----------------------------------------------

    def _on_press(self, event):
        self.focus_set()
        stem, hit = self._hit_test(event)
        if stem is None:
            self._drag_mode = None
            return
        if self.tool == "razor" or (getattr(event, "state", 0) & 0x0001):
            self._cut_at_event(event)
            return
        self._begin_transaction()
        ms = self._x_to_ms(event.x)
        if hit is None:
            self._drag_mode = "create"
            self._drag_anchor_ms = self._snap_point(ms, event)
            ov = {
                "stem": stem, "start_ms": self._drag_anchor_ms,
                "end_ms": self._drag_anchor_ms, "source": self.get_paint_source(),
            }
            self.overrides.append(ov)
            self._drag_ov = ov
            self._selected_ov = ov
        else:
            mode, ov = hit
            self._drag_mode = mode
            self._drag_ov = ov
            self._selected_ov = ov
            self._drag_grab_offset_ms = ms - ov["start_ms"]
            self._drag_len_ms = ov["end_ms"] - ov["start_ms"]
        self._redraw()

    def _on_drag(self, event):
        if self._drag_mode is None:
            return
        ms = self._x_to_ms(event.x)
        ov = self._drag_ov
        if self._drag_mode == "create":
            edge = self._snap_point(ms, event, ov)
            ov["start_ms"] = min(self._drag_anchor_ms, edge)
            ov["end_ms"] = max(self._drag_anchor_ms, edge)
        elif self._drag_mode == "move":
            raw_start = ms - self._drag_grab_offset_ms
            new_start = self._snap_move(raw_start, self._drag_len_ms, event, ov)
            new_start = max(0.0, min(self.total_ms - self._drag_len_ms, new_start))
            ov["start_ms"] = new_start
            ov["end_ms"] = new_start + self._drag_len_ms
        elif self._drag_mode == "resize-left":
            edge = self._snap_point(ms, event, ov)
            ov["start_ms"] = max(0.0, min(edge, ov["end_ms"] - self.MIN_LEN_MS))
        elif self._drag_mode == "resize-right":
            edge = self._snap_point(ms, event, ov)
            ov["end_ms"] = min(self.total_ms, max(edge, ov["start_ms"] + self.MIN_LEN_MS))
        self._set_feedback(event.x, event.y, self._range_text(ov), ms, self.stems.index(ov["stem"]))
        self._redraw()

    def _on_release(self, _event):
        if self._drag_mode is None:
            return
        ov = self._drag_ov
        mode = self._drag_mode
        if mode == "create" and ov["end_ms"] - ov["start_ms"] < self.MIN_LEN_MS:
            self.overrides.remove(ov)
            self._selected_ov = None
        elif ov is not None:
            ov["start_ms"] = int(round(ov["start_ms"]))
            ov["end_ms"] = int(round(ov["end_ms"]))
        self._drag_mode = None
        self._drag_ov = None
        self._feedback = None
        self._commit_transaction({
            "create": "Created clip", "move": "Moved clip",
            "resize-left": "Trimmed clip start", "resize-right": "Trimmed clip end",
        }.get(mode, "Edited clip"))

    def _cut_at_event(self, event):
        stem, hit = self._hit_test(event)
        if not stem or not hit:
            self._set_status("Razor: click inside a clip to split it")
            return
        ov = hit[1]
        split_ms = int(round(self._snap_point(self._x_to_ms(event.x), event, ov)))
        if split_ms - ov["start_ms"] < self.MIN_LEN_MS or ov["end_ms"] - split_ms < self.MIN_LEN_MS:
            self._set_status("Cut must leave at least 250 ms on both sides")
            return
        self._begin_transaction()
        index = self.overrides.index(ov)
        left = {**ov, "end_ms": split_ms}
        right = {**ov, "start_ms": split_ms}
        self.overrides[index:index + 1] = [left, right]
        self._selected_ov = right
        self._feedback = None
        self._commit_transaction(f"Cut {stem} at {self._fmt_precise(split_ms)}")

    def _on_double_click(self, event):
        # Kept as a compatibility shortcut; the explicit Razor tool is the
        # discoverable path and provides a live cut guide before clicking.
        self._cut_at_event(event)
        return "break"

    def _on_right_click(self, event):
        _, hit = self._hit_test(event)
        if hit:
            self._selected_ov = hit[1]
            self.delete_selected()

    def _on_hover(self, event):
        if self._drag_mode is not None or self._external_drag:
            return
        stem, hit = self._hit_test(event)
        lane = self._lane_at(event.y)
        if stem is None:
            self._feedback = None
            self.configure(cursor="crosshair" if self.tool == "razor" else "")
            self._redraw()
            return
        ms = self._snap_point(self._x_to_ms(event.x), event, hit[1] if hit else None)
        use_razor = self.tool == "razor" or (getattr(event, "state", 0) & 0x0001)
        if use_razor:
            self.configure(cursor="crosshair")
            text = f"Cut at {self._fmt_precise(ms)}" if hit else f"{self._fmt_precise(ms)}  (no clip)"
            self._set_feedback(event.x, event.y, text, ms, lane)
        elif hit is None:
            self.configure(cursor="crosshair")
            self._set_feedback(event.x, event.y, f"Draw from {self._fmt_precise(ms)}", ms, lane)
        else:
            mode, ov = hit
            self.configure(cursor="fleur" if mode == "move" else "sb_h_double_arrow")
            self._set_feedback(event.x, event.y, self._range_text(ov), ms, lane)
        self._redraw()

    def _on_leave(self, _event):
        if not self._external_drag:
            self._feedback = None
            self.configure(cursor="crosshair" if self.tool == "razor" else "")
            self._redraw()

    def _cancel_interaction(self, _event=None):
        if self._transaction_before is not None:
            self._restore_snapshot(self._transaction_before)
        self._transaction_before = None
        self._drag_mode = None
        self._drag_ov = None
        self._external_drag = None
        self._feedback = None
        self._set_status("Edit cancelled")
        self._redraw()
        return "break"

    def _set_feedback(self, x, y, text, guide_ms=None, lane=None):
        self._feedback = (x, y, text, guide_ms, lane)
        self._set_status(text)

    def _set_status(self, text):
        if self.on_status:
            self.on_status(text)


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
        self.stem_source_vars = {name: tk.StringVar(value=STEM_DEFAULT_SOURCE[name]) for name in STEM_NAMES}
        self.stem_overrides: list = []
        self.override_tool_var = tk.StringVar(value="select")
        self.override_source_var = tk.StringVar(value="Secondary")
        self.override_clip_length_var = tk.DoubleVar(value=8.0)
        self.override_snap_var = tk.BooleanVar(value=True)
        self.override_status_var = tk.StringVar(value="Ready - drag a source clip onto a lane, or draw directly in a lane.")

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
        self._build_output_row()
        self._build_status_row()
        self._build_action_buttons()

        self._show_mode_frame()
        self._center_window(800, 780)
        self.after(100, self._poll_queue)

    # ---- theme & layout scaffolding ----------------------------------

    def _apply_theme(self):
        self.configure(bg=BG)
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        default_font = ("Segoe UI", 9)
        header_font = ("Segoe UI", 10, "bold")
        self.option_add("*Font", default_font)

        style.configure(".", background=BG, foreground=TEXT, font=default_font)
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 16, "bold"))
        style.configure("Subtitle.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Success.TLabel", background=BG, foreground=SUCCESS, font=("Segoe UI", 9, "bold"))
        style.configure("Error.TLabel", background=BG, foreground=ERROR, font=("Segoe UI", 9, "bold"))
        style.configure("Working.TLabel", background=BG, foreground=ACCENT, font=("Segoe UI", 9, "bold"))

        style.configure("TLabelframe", background=BG, bordercolor=BORDER, relief="solid", borderwidth=1)
        style.configure("TLabelframe.Label", background=BG, foreground=TEXT, font=header_font)
        style.configure("TCheckbutton", background=BG, foreground=TEXT)
        style.configure("TRadiobutton", background=BG, foreground=TEXT)

        style.configure("TButton", padding=(10, 6))
        style.configure("Accent.TButton", background=ACCENT, foreground=ACCENT_TEXT, padding=(16, 9), font=("Segoe UI", 10, "bold"))
        style.map(
            "Accent.TButton",
            background=[("active", ACCENT_ACTIVE), ("disabled", "#a9b6d6")],
            foreground=[("disabled", "#eef1f8")],
        )

        style.configure("Toolbutton", padding=(10, 8), background=SURFACE, font=("Segoe UI", 9, "bold"))
        style.map(
            "Toolbutton",
            background=[("selected", ACCENT), ("active", "#e2e8f0")],
            foreground=[("selected", "#ffffff")],
        )

        style.configure("TEntry", fieldbackground=SURFACE, bordercolor=BORDER)
        style.configure("TCombobox", fieldbackground=SURFACE)
        style.configure("Horizontal.TScale", background=BG)
        style.configure("Accent.Horizontal.TProgressbar", background=ACCENT, troughcolor="#e2e5eb")
        style.configure("TSeparator", background=BORDER)

    def _build_scroll_container(self):
        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True)

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

        picker = ttk.LabelFrame(frame, text=" Take each stem from ")
        picker.pack(fill="x", pady=(8, 8), padx=8)
        for row, name in enumerate(STEM_NAMES):
            ttk.Label(picker, text=name.capitalize() + ":", width=10, anchor="w").grid(
                row=row, column=0, sticky="w", padx=6, pady=4
            )
            combo = ttk.Combobox(
                picker, textvariable=self.stem_source_vars[name], values=["Primary", "Secondary"],
                state="readonly", width=12,
            )
            combo.grid(row=row, column=1, sticky="w", padx=4, pady=4)
            combo.bind("<<ComboboxSelected>>", lambda _e: self.stems_override_editor._redraw())

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

        override_frame = ttk.LabelFrame(frame, text=" Time-based overrides (optional) ")
        override_frame.pack(fill="x", pady=(8, 4), padx=8)

        tool_row = ttk.Frame(override_frame)
        tool_row.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(tool_row, text="Tool:").pack(side="left")
        ttk.Radiobutton(
            tool_row, text="Select / move (V)", value="select", variable=self.override_tool_var,
            style="Toolbutton", command=lambda: self.stems_override_editor.set_tool("select"),
        ).pack(side="left", padx=(6, 2))
        ttk.Radiobutton(
            tool_row, text="Razor / cut (R)", value="razor", variable=self.override_tool_var,
            style="Toolbutton", command=lambda: self.stems_override_editor.set_tool("razor"),
        ).pack(side="left", padx=2)
        self.override_undo_button = ttk.Button(
            tool_row, text="Undo", command=lambda: self.stems_override_editor.undo(), state="disabled",
        )
        self.override_undo_button.pack(side="right", padx=(2, 0))
        self.override_redo_button = ttk.Button(
            tool_row, text="Redo", command=lambda: self.stems_override_editor.redo(), state="disabled",
        )
        self.override_redo_button.pack(side="right", padx=2)

        clip_row = ttk.Frame(override_frame)
        clip_row.pack(fill="x", padx=6, pady=(2, 4))
        ttk.Label(clip_row, text="Drag a clip:").pack(side="left")
        StemSourceClip(
            clip_row, "Primary clip", "primary", lambda: self.stems_override_editor,
            on_select=lambda source: self.override_source_var.set(source.capitalize()),
        ).pack(side="left", padx=(6, 3))
        StemSourceClip(
            clip_row, "Secondary clip", "secondary", lambda: self.stems_override_editor,
            on_select=lambda source: self.override_source_var.set(source.capitalize()),
        ).pack(side="left", padx=3)
        ttk.Label(clip_row, text="Length:").pack(side="left", padx=(10, 3))
        ttk.Spinbox(
            clip_row, from_=0.25, to=120.0, increment=0.25,
            textvariable=self.override_clip_length_var, width=6,
        ).pack(side="left")
        ttk.Label(clip_row, text="sec").pack(side="left", padx=(2, 10))
        ttk.Checkbutton(clip_row, text="Snap", variable=self.override_snap_var).pack(side="left")
        ttk.Label(clip_row, text="(Alt = fine)", style="Muted.TLabel").pack(side="left", padx=(2, 0))

        draw_row = ttk.Frame(override_frame)
        draw_row.pack(fill="x", padx=6, pady=(0, 2))
        ttk.Label(draw_row, text="Direct-draw source:").pack(side="left")
        ttk.Radiobutton(
            draw_row, text="Primary", value="Primary", variable=self.override_source_var,
        ).pack(side="left", padx=(6, 2))
        ttk.Radiobutton(
            draw_row, text="Secondary", value="Secondary", variable=self.override_source_var,
        ).pack(side="left", padx=2)

        self.stems_override_editor = StemOverrideEditor(
            override_frame, STEM_NAMES,
            get_default_source=lambda stem: self.stem_source_vars[stem].get().lower(),
            get_paint_source=lambda: self.override_source_var.get().lower(),
            get_clip_length_ms=lambda: self.override_clip_length_var.get() * 1000,
            get_snap_enabled=self.override_snap_var.get,
            on_change=self._refresh_override_tree,
            on_status=self.override_status_var.set,
            on_tool_change=self.override_tool_var.set,
        )
        self.stems_override_editor.pack(fill="x", padx=6, pady=(2, 0))
        self.override_scrollbar = ttk.Scrollbar(
            override_frame, orient="horizontal", command=self.stems_override_editor.xview,
        )
        self.override_scrollbar.pack(fill="x", padx=(74, 6), pady=(0, 2))
        self.stems_override_editor.set_xscrollcommand(self.override_scrollbar.set)

        view_row = ttk.Frame(override_frame)
        view_row.pack(fill="x", padx=6, pady=(0, 2))
        ttk.Button(view_row, text="Zoom in", command=self.stems_override_editor.zoom_in).pack(side="left")
        ttk.Button(view_row, text="Zoom out", command=self.stems_override_editor.zoom_out).pack(side="left", padx=3)
        ttk.Button(view_row, text="Fit song", command=self.stems_override_editor.fit_view).pack(side="left")
        ttk.Label(view_row, textvariable=self.override_status_var, style="Muted.TLabel").pack(
            side="left", padx=10, fill="x", expand=True,
        )
        ttk.Label(
            override_frame,
            text="Drag a Primary/Secondary clip onto any lane, or drag empty lane space to draw the selected "
                 "source. Clip centers move 1:1; edge handles trim. Razor shows the exact cut before you click. "
                 "Hold Shift for a temporary razor or Alt to bypass snap. Right-click/Delete removes; "
                 "Ctrl+Z/Ctrl+Y undo/redo; Ctrl+wheel zooms and Shift+wheel scrolls.",
            style="Muted.TLabel", wraplength=680, justify="left",
        ).pack(fill="x", padx=6, pady=(2, 6))

        self.override_tree = ttk.Treeview(
            override_frame, columns=("stem", "start", "end", "source"), show="headings", height=4
        )
        for col, label, width in [
            ("stem", "Stem", 80), ("start", "Start (ms)", 90), ("end", "End (ms)", 90), ("source", "Source", 90),
        ]:
            self.override_tree.heading(col, text=label)
            self.override_tree.column(col, width=width, anchor="center")
        self.override_tree.pack(fill="x", padx=6, pady=(0, 4))
        ttk.Button(override_frame, text="Remove Selected", command=self._remove_override).pack(
            anchor="e", padx=6, pady=(0, 6)
        )

        ttk.Label(
            override_frame,
            text="Overrides win over the defaults above for that stem during that time range only "
                 "(times are in the final mashup's timeline). E.g. bass=Secondary 30000-60000 plays "
                 "secondary's bass just for that 30s section, primary's bass everywhere else.",
            style="Muted.TLabel", wraplength=680, justify="left",
        ).pack(fill="x", padx=6, pady=(0, 6))

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
            blend, "Override crossfade", 0, 3000, 900, unit=" ms",
            tooltip="How long to blend across any point a stem switches source (an override boundary or the "
                    "auto-restore above), instead of cutting instantly between them.",
        )
        self.stems_override_crossfade.pack(fill="x", pady=(4, 6), padx=6)

        note = ttk.Label(
            frame,
            text="E.g. take vocals + drums from a guest track and bass + other from your primary, so the two "
                 "tracks' drums never collide. Separates both tracks fully (slower than the 2-stem vocals mode); "
                 "results are cached per file.",
            style="Muted.TLabel", wraplength=680, justify="left",
        )
        note.pack(fill="x", pady=(8, 8), padx=8)

        # Establishes stems_override_editor.overrides as the *same* list object
        # as self.stem_overrides (not just an equal-valued copy), so drag edits
        # on the canvas are immediately visible to the render worker without
        # needing an explicit sync step.
        self._refresh_override_tree()
        return frame

    def _remove_override(self):
        selected = self.override_tree.selection()
        self.stems_override_editor.delete_indices([int(iid) for iid in selected])

    def _refresh_override_tree(self):
        self.override_tree.delete(*self.override_tree.get_children())
        for i, ov in enumerate(self.stem_overrides):
            self.override_tree.insert(
                "", "end", iid=str(i),
                values=(ov["stem"], ov["start_ms"], ov["end_ms"], ov["source"].capitalize()),
            )
        self.stems_override_editor.set_overrides(self.stem_overrides)
        self.override_undo_button.configure(state="normal" if self.stems_override_editor.can_undo else "disabled")
        self.override_redo_button.configure(state="normal" if self.stems_override_editor.can_redo else "disabled")

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
        mode = self.mode_var.get()
        self.mode_frames[mode].pack(fill="x")
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
        total = max(self.primary_duration_ms, self.stems_offset.get() + self.secondary_duration_ms, 1000)
        self.stems_override_editor.set_total_ms(total)

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
                stem_sources = {name: var.get().lower() for name, var in self.stem_source_vars.items()}
                segment = mixer.stem_mix(
                    self.primary_path.get(),
                    self.secondary_path.get(),
                    stem_sources=stem_sources,
                    stem_overrides=list(self.stem_overrides),
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
