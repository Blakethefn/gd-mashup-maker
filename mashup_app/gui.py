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
MODES = [MODE_SIMPLE, MODE_BEAT_SYNCED, MODE_VOCALS]

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

    def get(self):
        return self.var.get()


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
        ttk.Entry(frame, textvariable=self.primary_path, width=55).grid(row=0, column=1, padx=4, pady=4)
        ttk.Button(frame, text="Browse...", command=self._browse_primary).grid(row=0, column=2, padx=4, pady=4)

        ttk.Label(frame, text="Secondary:").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        ttk.Entry(frame, textvariable=self.secondary_path, width=55).grid(row=1, column=1, padx=4, pady=4)
        ttk.Button(frame, text="Browse...", command=self._browse_secondary).grid(row=1, column=2, padx=4, pady=4)

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
        }

    def _build_simple_frame(self):
        frame = ttk.Frame(self.mode_container)
        self.simple_offset = LabeledScale(frame, "Secondary start offset", 0, 60000, 0, unit=" ms")
        self.simple_offset.pack(fill="x", pady=4, padx=8)
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
        return frame

    def _build_beat_synced_frame(self):
        frame = ttk.Frame(self.mode_container)

        analyze_row = ttk.Frame(frame)
        analyze_row.pack(fill="x", pady=(4, 8), padx=8)
        ttk.Button(analyze_row, text="Analyze BPM", command=self._analyze).pack(side="left")
        self.bpm_label = ttk.Label(analyze_row, text="Detected BPM: (not analyzed yet)")
        self.bpm_label.pack(side="left", padx=10)

        self.beat_start = LabeledScale(frame, "Blend start", 0, 120000, 0, unit=" ms")
        self.beat_start.pack(fill="x", pady=4, padx=8)
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

        self.vocals_offset = LabeledScale(frame, "Vocal start offset", 0, 60000, 0, unit=" ms")
        self.vocals_offset.pack(fill="x", pady=4, padx=8)
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

        note = ttk.Label(
            frame,
            text="First run downloads the Demucs model and separation takes ~1-3 min per track on CPU.\n"
                 "Results are cached, so re-rendering the same file is instant after the first time.",
            foreground="#666666", wraplength=560, justify="left",
        )
        note.pack(fill="x", pady=(8, 4), padx=8)
        return frame

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

    def _browse_secondary(self):
        path = filedialog.askopenfilename(
            title="Choose secondary MP3", initialdir=MUSIC_DIR, filetypes=[("MP3 files", "*.mp3")]
        )
        if path:
            self.secondary_path.set(path)

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
                    progress_callback=self._queue_status,
                )
            else:  # MODE_VOCALS
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
