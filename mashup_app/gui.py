"""Tkinter GUI: pick two MP3s, choose a mashup mode, tweak its controls, render."""
import os
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import mixer

MODE_SIMPLE = "Simple Overlay/Crossfade"
MODE_BEAT_SYNCED = "Beat-Synced Blend"
MODE_VOCALS = "Vocals-over-Instrumental"
MODE_STEMS = "Stem Mixer (vocals/drums/bass/other)"
MODES = [MODE_SIMPLE, MODE_BEAT_SYNCED, MODE_VOCALS, MODE_STEMS]
STEM_NAMES = ("vocals", "drums", "bass", "other")
STEM_DEFAULT_SOURCE = {"vocals": "Secondary", "drums": "Secondary", "bass": "Primary", "other": "Primary"}

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output"
MUSIC_DIR = str(Path.home() / "Music") if (Path.home() / "Music").exists() else str(Path.home())


class LabeledScale(ttk.Frame):
    """A ttk.Scale with a text label and a live value readout."""

    def __init__(self, parent, label, from_, to, default, unit="", fmt="{:.0f}"):
        super().__init__(parent)
        self.var = tk.DoubleVar(value=default)
        self.fmt = fmt
        self.unit = unit
        self._change_callback = None

        ttk.Label(self, text=label, width=20, anchor="w").grid(row=0, column=0, sticky="w")
        self.value_label = ttk.Label(self, text=self._format(default), width=10, anchor="e")
        self.scale = ttk.Scale(self, from_=from_, to=to, variable=self.var, command=self._on_change)
        self.scale.grid(row=0, column=1, sticky="ew", padx=8)
        self.value_label.grid(row=0, column=2, sticky="e")
        self.columnconfigure(1, weight=1)

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
    secondary's start instead of typing milliseconds."""

    BG = "#f5f5f5"
    PRIMARY_COLOR = "#6b7280"
    SECONDARY_COLOR = "#2563eb"

    def __init__(self, parent, height=64, on_offset_change=None):
        super().__init__(parent, height=height, bg=self.BG, highlightthickness=1, highlightbackground="#bbb")
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

        self.create_rectangle(0, 4, self._ms_to_x(self.primary_ms), 4 + lane_h, fill=self.PRIMARY_COLOR, outline="")
        self.create_text(4, 4 + lane_h / 2, text="Primary", anchor="w", fill="white", font=("Segoe UI", 8))

        sec_y0 = h - lane_h - 4
        x0, x1 = self._ms_to_x(self.offset_ms), self._ms_to_x(self.offset_ms + self.secondary_ms)
        self.create_rectangle(x0, sec_y0, x1, sec_y0 + lane_h, fill=self.SECONDARY_COLOR, outline="")
        self.create_text(x0 + 4, sec_y0 + lane_h / 2, text="Secondary", anchor="w", fill="white", font=("Segoe UI", 8))

        self.create_text(2, h - 1, text="0:00", anchor="sw", fill="#333", font=("Segoe UI", 7))
        self.create_text(w - 2, h - 1, text=self._fmt(self._total_ms()), anchor="se", fill="#333", font=("Segoe UI", 7))

    def _on_drag(self, event):
        self.offset_ms = min(self._x_to_ms(event.x), max(0.0, self._total_ms() - 1))
        self._redraw()
        if self.on_offset_change:
            self.on_offset_change(self.offset_ms)


class RangeSelector(tk.Canvas):
    """Click-drag to pick a start/end range on the shared output timeline,
    used to fill in a stem override's Start/End instead of typing ms.
    Existing overrides are drawn as labeled blocks for reference."""

    BG = "#f5f5f5"
    SELECT_COLOR = "#93c5fd"
    OVERRIDE_COLOR = "#f97316"

    def __init__(self, parent, height=56, on_range_change=None):
        super().__init__(parent, height=height, bg=self.BG, highlightthickness=1, highlightbackground="#bbb")
        self.height_px = height
        self.total_ms = 60000
        self.overrides = []
        self.on_range_change = on_range_change
        self._dragging = False
        self._sel_start_ms = None
        self._sel_end_ms = None
        self.bind("<Configure>", lambda _e: self._redraw())
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)

    def set_total_ms(self, total_ms):
        self.total_ms = max(1000, total_ms)
        self._redraw()

    def set_overrides(self, overrides):
        self.overrides = overrides
        self._redraw()

    def _ms_to_x(self, ms):
        return (ms / self.total_ms) * max(1, self.winfo_width())

    def _x_to_ms(self, x):
        return max(0.0, min(self.total_ms, (x / max(1, self.winfo_width())) * self.total_ms))

    @staticmethod
    def _fmt(ms):
        s = int(ms // 1000)
        return f"{s // 60}:{s % 60:02d}"

    def _redraw(self):
        self.delete("all")
        w = max(1, self.winfo_width())
        h = self.height_px

        self.create_rectangle(0, h * 0.5, w, h * 0.78, fill="#d0d0d0", outline="")
        for ov in self.overrides:
            x0, x1 = self._ms_to_x(ov["start_ms"]), self._ms_to_x(ov["end_ms"])
            self.create_rectangle(x0, h * 0.5, x1, h * 0.78, fill=self.OVERRIDE_COLOR, outline="")
            self.create_text((x0 + x1) / 2, h * 0.64, text=ov["stem"][:3], fill="white", font=("Segoe UI", 7))

        if self._sel_start_ms is not None and self._sel_end_ms is not None:
            x0 = self._ms_to_x(min(self._sel_start_ms, self._sel_end_ms))
            x1 = self._ms_to_x(max(self._sel_start_ms, self._sel_end_ms))
            self.create_rectangle(x0, h * 0.08, x1, h * 0.46, fill=self.SELECT_COLOR, outline="")

        self.create_text(2, h - 1, text="0:00", anchor="sw", fill="#333", font=("Segoe UI", 7))
        self.create_text(w - 2, h - 1, text=self._fmt(self.total_ms), anchor="se", fill="#333", font=("Segoe UI", 7))

    def _on_press(self, event):
        self._dragging = True
        self._sel_start_ms = self._x_to_ms(event.x)
        self._sel_end_ms = self._sel_start_ms
        self._redraw()

    def _on_drag(self, event):
        if not self._dragging:
            return
        self._sel_end_ms = self._x_to_ms(event.x)
        self._redraw()
        if self.on_range_change:
            self.on_range_change(min(self._sel_start_ms, self._sel_end_ms), max(self._sel_start_ms, self._sel_end_ms))

    def _on_release(self, _event):
        self._dragging = False


class MashupApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MP3 Mashup Tool")
        self.geometry("640x640")
        self.resizable(True, True)

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
        self.stem_source_vars = {name: tk.StringVar(value=STEM_DEFAULT_SOURCE[name]) for name in STEM_NAMES}
        self.stem_overrides: list = []
        self.override_stem_var = tk.StringVar(value=STEM_NAMES[0])
        self.override_start_var = tk.StringVar(value="0")
        self.override_end_var = tk.StringVar(value="30000")
        self.override_source_var = tk.StringVar(value="Secondary")

        self.primary_duration_ms = 0
        self.secondary_duration_ms = 0

        self.render_queue: queue.Queue = queue.Queue()
        self.last_output_path = None
        self.render_thread = None

        self._build_file_pickers()
        self._build_mode_selector()
        self._build_mode_frames()
        self._build_output_row()
        self._build_status_row()
        self._build_action_buttons()

        self._show_mode_frame()
        self.after(100, self._poll_queue)

    # ---- layout ----------------------------------------------------

    def _build_file_pickers(self):
        frame = ttk.LabelFrame(self, text="Source tracks")
        frame.pack(fill="x", padx=12, pady=(12, 6))

        ttk.Label(frame, text="Primary:").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        ttk.Entry(frame, textvariable=self.primary_path, width=50).grid(row=0, column=1, padx=4, pady=4)
        ttk.Button(frame, text="Browse...", command=self._browse_primary).grid(row=0, column=2, padx=4, pady=4)
        self.primary_length_label = ttk.Label(frame, text="", width=6, foreground="#666666")
        self.primary_length_label.grid(row=0, column=3, padx=4, pady=4)

        ttk.Label(frame, text="Secondary:").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        ttk.Entry(frame, textvariable=self.secondary_path, width=50).grid(row=1, column=1, padx=4, pady=4)
        ttk.Button(frame, text="Browse...", command=self._browse_secondary).grid(row=1, column=2, padx=4, pady=4)
        self.secondary_length_label = ttk.Label(frame, text="", width=6, foreground="#666666")
        self.secondary_length_label.grid(row=1, column=3, padx=4, pady=4)

    def _build_mode_selector(self):
        frame = ttk.Frame(self)
        frame.pack(fill="x", padx=12, pady=6)
        ttk.Label(frame, text="Mashup mode:").pack(side="left")
        combo = ttk.Combobox(frame, textvariable=self.mode_var, values=MODES, state="readonly", width=30)
        combo.pack(side="left", padx=8)
        combo.bind("<<ComboboxSelected>>", lambda _evt: self._show_mode_frame())

    def _build_mode_frames(self):
        self.mode_container = ttk.LabelFrame(self, text="Mode settings")
        self.mode_container.pack(fill="x", padx=12, pady=6)

        self.mode_frames = {
            MODE_SIMPLE: self._build_simple_frame(),
            MODE_BEAT_SYNCED: self._build_beat_synced_frame(),
            MODE_VOCALS: self._build_vocals_frame(),
            MODE_STEMS: self._build_stems_frame(),
        }

    def _build_simple_frame(self):
        frame = ttk.Frame(self.mode_container)
        self.simple_timeline = OffsetTimeline(frame)
        self.simple_timeline.pack(fill="x", pady=(4, 8), padx=8)
        self.simple_offset = LabeledScale(frame, "Secondary start offset", 0, 60000, 0, unit=" ms")
        self.simple_offset.pack(fill="x", pady=4, padx=8)
        self.simple_timeline.on_offset_change = lambda ms: self.simple_offset.set(ms)
        self.simple_offset.bind_change(lambda v: self.simple_timeline.set_offset(v))

        self.simple_primary_gain = LabeledScale(frame, "Primary gain", -20, 6, 0, unit=" dB", fmt="{:.1f}")
        self.simple_primary_gain.pack(fill="x", pady=4, padx=8)
        self.simple_secondary_gain = LabeledScale(frame, "Secondary gain", -20, 6, -3, unit=" dB", fmt="{:.1f}")
        self.simple_secondary_gain.pack(fill="x", pady=4, padx=8)
        self.simple_crossfade = LabeledScale(frame, "Crossfade", 0, 8000, 1000, unit=" ms")
        self.simple_crossfade.pack(fill="x", pady=4, padx=8)

        blend_row = ttk.Frame(frame)
        blend_row.pack(fill="x", pady=(8, 4), padx=8)
        ttk.Checkbutton(blend_row, text="Match loudness", variable=self.simple_match_loudness_var).pack(side="left")
        ttk.Checkbutton(
            blend_row, text="Low-cut secondary bass (avoid muddy overlap)", variable=self.simple_low_cut_var
        ).pack(side="left", padx=12)
        self.simple_duck_amount = LabeledScale(frame, "Duck primary under secondary", 0, 100, 30, unit="%")
        self.simple_duck_amount.pack(fill="x", pady=4, padx=8)
        self.simple_reverb_amount = LabeledScale(frame, "Reverb on secondary", 0, 100, 0, unit="%")
        self.simple_reverb_amount.pack(fill="x", pady=4, padx=8)
        self.simple_reverb_size = LabeledScale(frame, "Reverb room size", 0, 100, 50, unit="%")
        self.simple_reverb_size.pack(fill="x", pady=4, padx=8)
        return frame

    def _build_beat_synced_frame(self):
        frame = ttk.Frame(self.mode_container)

        analyze_row = ttk.Frame(frame)
        analyze_row.pack(fill="x", pady=(4, 8), padx=8)
        ttk.Button(analyze_row, text="Analyze BPM", command=self._analyze).pack(side="left")
        self.bpm_label = ttk.Label(analyze_row, text="Detected BPM: (not analyzed yet)")
        self.bpm_label.pack(side="left", padx=10)

        self.beat_timeline = OffsetTimeline(frame)
        self.beat_timeline.pack(fill="x", pady=(0, 8), padx=8)
        self.beat_start = LabeledScale(frame, "Blend start", 0, 120000, 0, unit=" ms")
        self.beat_start.pack(fill="x", pady=4, padx=8)
        self.beat_timeline.on_offset_change = lambda ms: self.beat_start.set(ms)
        self.beat_start.bind_change(lambda v: self.beat_timeline.set_offset(v))

        self.beat_duration = LabeledScale(frame, "Blend duration", 500, 20000, 8000, unit=" ms")
        self.beat_duration.pack(fill="x", pady=4, padx=8)
        self.beat_primary_gain = LabeledScale(frame, "Primary gain", -20, 6, 0, unit=" dB", fmt="{:.1f}")
        self.beat_primary_gain.pack(fill="x", pady=4, padx=8)
        self.beat_secondary_gain = LabeledScale(frame, "Secondary gain", -20, 6, 0, unit=" dB", fmt="{:.1f}")
        self.beat_secondary_gain.pack(fill="x", pady=4, padx=8)

        blend_row = ttk.Frame(frame)
        blend_row.pack(fill="x", pady=(8, 4), padx=8)
        ttk.Checkbutton(blend_row, text="Match loudness", variable=self.beat_match_loudness_var).pack(side="left")
        ttk.Checkbutton(
            blend_row, text="Low-cut secondary bass (avoid muddy overlap)", variable=self.beat_low_cut_var
        ).pack(side="left", padx=12)
        self.beat_duck_amount = LabeledScale(frame, "Duck primary under secondary", 0, 100, 30, unit="%")
        self.beat_duck_amount.pack(fill="x", pady=4, padx=8)
        self.beat_reverb_amount = LabeledScale(frame, "Reverb on secondary", 0, 100, 0, unit="%")
        self.beat_reverb_amount.pack(fill="x", pady=4, padx=8)
        self.beat_reverb_size = LabeledScale(frame, "Reverb room size", 0, 100, 50, unit="%")
        self.beat_reverb_size.pack(fill="x", pady=4, padx=8)
        return frame

    def _build_vocals_frame(self):
        frame = ttk.Frame(self.mode_container)

        row = ttk.Frame(frame)
        row.pack(fill="x", pady=4, padx=8)
        ttk.Label(row, text="Take vocals from:").pack(side="left")
        ttk.Combobox(
            row, textvariable=self.vocals_from_var, values=["Primary", "Secondary"],
            state="readonly", width=12,
        ).pack(side="left", padx=8)

        check_row = ttk.Frame(frame)
        check_row.pack(fill="x", pady=4, padx=8)
        ttk.Checkbutton(check_row, text="Tempo match", variable=self.tempo_match_var).pack(side="left")
        ttk.Checkbutton(check_row, text="Key match", variable=self.key_match_var).pack(side="left", padx=12)

        self.vocals_timeline = OffsetTimeline(frame)
        self.vocals_timeline.pack(fill="x", pady=(4, 8), padx=8)
        self.vocals_offset = LabeledScale(frame, "Vocal start offset", 0, 60000, 0, unit=" ms")
        self.vocals_offset.pack(fill="x", pady=4, padx=8)
        self.vocals_timeline.on_offset_change = lambda ms: self.vocals_offset.set(ms)
        self.vocals_offset.bind_change(lambda v: self.vocals_timeline.set_offset(v))

        self.vocals_gain = LabeledScale(frame, "Vocal gain", -20, 6, 0, unit=" dB", fmt="{:.1f}")
        self.vocals_gain.pack(fill="x", pady=4, padx=8)
        self.instrumental_gain = LabeledScale(frame, "Instrumental gain", -20, 6, 0, unit=" dB", fmt="{:.1f}")
        self.instrumental_gain.pack(fill="x", pady=4, padx=8)

        blend_row = ttk.Frame(frame)
        blend_row.pack(fill="x", pady=(8, 4), padx=8)
        ttk.Checkbutton(blend_row, text="Match loudness", variable=self.vocals_match_loudness_var).pack(side="left")
        ttk.Checkbutton(
            blend_row, text="Carve instrumental for vocal presence (~2.5kHz)", variable=self.vocals_carve_var
        ).pack(side="left", padx=12)
        self.vocals_duck_amount = LabeledScale(frame, "Duck instrumental under vocals", 0, 100, 50, unit="%")
        self.vocals_duck_amount.pack(fill="x", pady=4, padx=8)
        self.vocals_reverb_amount = LabeledScale(frame, "Reverb on vocals", 0, 100, 0, unit="%")
        self.vocals_reverb_amount.pack(fill="x", pady=4, padx=8)
        self.vocals_reverb_size = LabeledScale(frame, "Reverb room size", 0, 100, 50, unit="%")
        self.vocals_reverb_size.pack(fill="x", pady=4, padx=8)

        note = ttk.Label(
            frame,
            text="First run downloads the Demucs model and separation takes ~1-3 min per track on CPU.\n"
                 "Results are cached, so re-rendering the same file is instant after the first time.",
            foreground="#666666", wraplength=560, justify="left",
        )
        note.pack(fill="x", pady=(8, 4), padx=8)
        return frame

    def _build_stems_frame(self):
        frame = ttk.Frame(self.mode_container)

        picker = ttk.LabelFrame(frame, text="Take each stem from")
        picker.pack(fill="x", pady=(4, 8), padx=8)
        for row, name in enumerate(STEM_NAMES):
            ttk.Label(picker, text=name.capitalize() + ":", width=10, anchor="w").grid(
                row=row, column=0, sticky="w", padx=4, pady=3
            )
            ttk.Combobox(
                picker, textvariable=self.stem_source_vars[name], values=["Primary", "Secondary"],
                state="readonly", width=12,
            ).grid(row=row, column=1, sticky="w", padx=4, pady=3)

        self.stems_timeline = OffsetTimeline(frame)
        self.stems_timeline.pack(fill="x", pady=(0, 8), padx=8)
        self.stems_offset = LabeledScale(frame, "Secondary start offset", 0, 60000, 0, unit=" ms")
        self.stems_offset.pack(fill="x", pady=4, padx=8)
        self.stems_timeline.on_offset_change = self._on_stems_offset_from_timeline
        self.stems_offset.bind_change(self._on_stems_offset_from_slider)

        self.stems_primary_gain = LabeledScale(frame, "Primary group gain", -20, 6, 0, unit=" dB", fmt="{:.1f}")
        self.stems_primary_gain.pack(fill="x", pady=4, padx=8)
        self.stems_secondary_gain = LabeledScale(frame, "Secondary group gain", -20, 6, 0, unit=" dB", fmt="{:.1f}")
        self.stems_secondary_gain.pack(fill="x", pady=4, padx=8)
        self.stems_reverb_amount = LabeledScale(frame, "Reverb on secondary's stems", 0, 100, 0, unit="%")
        self.stems_reverb_amount.pack(fill="x", pady=4, padx=8)
        self.stems_reverb_size = LabeledScale(frame, "Reverb room size", 0, 100, 50, unit="%")
        self.stems_reverb_size.pack(fill="x", pady=4, padx=8)

        override_frame = ttk.LabelFrame(frame, text="Time-based overrides (optional)")
        override_frame.pack(fill="x", pady=(8, 4), padx=8)

        self.stems_range_selector = RangeSelector(override_frame, on_range_change=self._on_range_selected)
        self.stems_range_selector.pack(fill="x", padx=4, pady=(4, 0))
        ttk.Label(
            override_frame, text="Drag on the bar above to set start/end, or type them below.",
            foreground="#666666",
        ).pack(fill="x", padx=4)

        self.override_tree = ttk.Treeview(
            override_frame, columns=("stem", "start", "end", "source"), show="headings", height=4
        )
        for col, label, width in [
            ("stem", "Stem", 80), ("start", "Start (ms)", 90), ("end", "End (ms)", 90), ("source", "Source", 90),
        ]:
            self.override_tree.heading(col, text=label)
            self.override_tree.column(col, width=width, anchor="center")
        self.override_tree.pack(fill="x", padx=4, pady=4)

        add_row = ttk.Frame(override_frame)
        add_row.pack(fill="x", padx=4, pady=(0, 4))
        ttk.Combobox(
            add_row, textvariable=self.override_stem_var, values=list(STEM_NAMES), state="readonly", width=8
        ).pack(side="left", padx=2)
        ttk.Entry(add_row, textvariable=self.override_start_var, width=10).pack(side="left", padx=2)
        ttk.Entry(add_row, textvariable=self.override_end_var, width=10).pack(side="left", padx=2)
        ttk.Combobox(
            add_row, textvariable=self.override_source_var, values=["Primary", "Secondary"],
            state="readonly", width=10,
        ).pack(side="left", padx=2)
        ttk.Button(add_row, text="Add", command=self._add_override).pack(side="left", padx=4)
        ttk.Button(add_row, text="Remove Selected", command=self._remove_override).pack(side="left", padx=4)

        ttk.Label(
            override_frame,
            text="Overrides win over the defaults above for that stem during that time range only "
                 "(times are in the final mashup's timeline). E.g. bass=Secondary 30000-60000 plays "
                 "secondary's bass just for that 30s section, primary's bass everywhere else.",
            foreground="#666666", wraplength=560, justify="left",
        ).pack(fill="x", padx=4, pady=(0, 4))

        check_row = ttk.Frame(frame)
        check_row.pack(fill="x", pady=(8, 4), padx=8)
        ttk.Checkbutton(check_row, text="Tempo match", variable=self.stems_tempo_match_var).pack(side="left")
        ttk.Checkbutton(
            check_row, text="Match loudness", variable=self.stems_match_loudness_var
        ).pack(side="left", padx=12)
        self.stems_duck_amount = LabeledScale(frame, "Duck primary group under secondary", 0, 100, 30, unit="%")
        self.stems_duck_amount.pack(fill="x", pady=4, padx=8)

        note = ttk.Label(
            frame,
            text="E.g. take vocals + drums from a guest track and bass + other from your primary, so the two "
                 "tracks' drums never collide. Separates both tracks fully (slower than the 2-stem vocals mode); "
                 "results are cached per file.",
            foreground="#666666", wraplength=560, justify="left",
        )
        note.pack(fill="x", pady=(8, 4), padx=8)
        return frame

    def _on_stems_offset_from_timeline(self, ms):
        self.stems_offset.set(ms)
        self._update_timelines()

    def _on_stems_offset_from_slider(self, v):
        self.stems_timeline.set_offset(v)
        self._update_timelines()

    def _on_range_selected(self, start_ms, end_ms):
        self.override_start_var.set(str(int(start_ms)))
        self.override_end_var.set(str(int(end_ms)))

    def _add_override(self):
        try:
            start_ms = int(float(self.override_start_var.get()))
            end_ms = int(float(self.override_end_var.get()))
        except ValueError:
            messagebox.showerror("Invalid override", "Start and end must be numbers (milliseconds).")
            return
        if start_ms < 0 or end_ms <= start_ms:
            messagebox.showerror("Invalid override", "End must be greater than start, and both must be >= 0.")
            return

        self.stem_overrides.append({
            "stem": self.override_stem_var.get(),
            "start_ms": start_ms,
            "end_ms": end_ms,
            "source": self.override_source_var.get().lower(),
        })
        self._refresh_override_tree()

    def _remove_override(self):
        selected = self.override_tree.selection()
        for i in sorted((int(iid) for iid in selected), reverse=True):
            del self.stem_overrides[i]
        self._refresh_override_tree()

    def _refresh_override_tree(self):
        self.override_tree.delete(*self.override_tree.get_children())
        for i, ov in enumerate(self.stem_overrides):
            self.override_tree.insert(
                "", "end", iid=str(i),
                values=(ov["stem"], ov["start_ms"], ov["end_ms"], ov["source"].capitalize()),
            )
        self.stems_range_selector.set_overrides(self.stem_overrides)

    def _build_output_row(self):
        frame = ttk.Frame(self)
        frame.pack(fill="x", padx=12, pady=6)
        ttk.Label(frame, text="Save to:").pack(side="left")
        ttk.Entry(frame, textvariable=self.output_path, width=48).pack(side="left", padx=8)
        ttk.Button(frame, text="Browse...", command=self._browse_output).pack(side="left")

    def _build_status_row(self):
        frame = ttk.Frame(self)
        frame.pack(fill="x", padx=12, pady=6)
        ttk.Label(frame, textvariable=self.status_var, wraplength=560, justify="left").pack(fill="x")
        self.progress = ttk.Progressbar(frame, mode="indeterminate")
        self.progress.pack(fill="x", pady=(4, 0))

    def _build_action_buttons(self):
        frame = ttk.Frame(self)
        frame.pack(fill="x", padx=12, pady=(6, 12))
        self.render_button = ttk.Button(frame, text="Render Mashup", command=self._render)
        self.render_button.pack(side="left")
        self.play_button = ttk.Button(frame, text="Play Result", command=self._play_result, state="disabled")
        self.play_button.pack(side="left", padx=8)

    def _show_mode_frame(self):
        for frame in self.mode_frames.values():
            frame.pack_forget()
        self.mode_frames[self.mode_var.get()].pack(fill="x")

    # ---- file dialogs ------------------------------------------------

    def _browse_primary(self):
        path = filedialog.askopenfilename(
            title="Choose primary MP3", initialdir=MUSIC_DIR, filetypes=[("MP3 files", "*.mp3")]
        )
        if path:
            self.primary_path.set(path)
            self._refresh_duration("primary")

    def _browse_secondary(self):
        path = filedialog.askopenfilename(
            title="Choose secondary MP3", initialdir=MUSIC_DIR, filetypes=[("MP3 files", "*.mp3")]
        )
        if path:
            self.secondary_path.set(path)
            self._refresh_duration("secondary")

    def _refresh_duration(self, which):
        from . import audio_io

        path = self.primary_path.get() if which == "primary" else self.secondary_path.get()
        try:
            duration_ms = audio_io.get_duration_ms(path)
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

    def _update_timelines(self):
        for tl in (self.simple_timeline, self.beat_timeline, self.vocals_timeline, self.stems_timeline):
            tl.set_durations(self.primary_duration_ms, self.secondary_duration_ms)
        total = max(self.primary_duration_ms, self.stems_offset.get() + self.secondary_duration_ms, 1000)
        self.stems_range_selector.set_total_ms(total)

    def _browse_output(self):
        path = filedialog.asksaveasfilename(
            title="Save mashup as", initialdir=str(OUTPUT_DIR), defaultextension=".mp3",
            filetypes=[("MP3 files", "*.mp3")],
        )
        if path:
            self.output_path.set(path)

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
                    self.status_var.set(payload)
                elif kind == "bpm":
                    primary_bpm, secondary_bpm = payload
                    self.bpm_label.config(text=f"Detected BPM: primary {primary_bpm:.1f}, secondary {secondary_bpm:.1f}")
                elif kind == "done":
                    self.progress.stop()
                    self.render_button.config(state="normal")
                    self.play_button.config(state="normal")
                    self.last_output_path = payload
                    self.status_var.set(f"Done. Saved to {payload}")
                elif kind == "error":
                    self.progress.stop()
                    self.render_button.config(state="normal")
                    self.status_var.set("Failed - see error dialog.")
                    messagebox.showerror("Render failed", payload)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)


def main():
    app = MashupApp()
    app.mainloop()
