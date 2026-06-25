"""
PAE — Post Production Assistant
Tkinter UI with dynamic stage progress, config editing, video management,
and output preview (chapters, timeline, cost).
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext, simpledialog
import threading
import queue
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Optional, List, Any, Dict

MSG_LOG         = "log"
MSG_STAGE_START = "stage_start"
MSG_STAGE_DONE  = "stage_done"
MSG_STAGE_ERROR = "stage_error"
MSG_CHAPTERS    = "chapters"
MSG_COST        = "cost"
MSG_DONE        = "done"
MSG_ERROR       = "error"

PIPELINE_STAGES = [
    {"id": "borders",    "label": "Process Borders"},
    {"id": "transcript", "label": "Transcribe Audio"},
    {"id": "chapters",   "label": "Generate Chapters"},
    {"id": "timeline",   "label": "Build Timeline"},
    {"id": "tests",      "label": "Run Tests"},
]

STATUS_COLORS = {
    "pending": "#555566",
    "running": "#3b9eff",
    "done":    "#4caf50",
    "error":   "#f44336",
    "skipped": "#666677",
}

CHAPTER_COLORS = {
    "working":         "#4caf50",
    "testing":         "#2196f3",
    "explanation":     "#ffc107",
    "problem_solving": "#ff9800",
    "irrelevant":      "#f44336",
    "setup":           "#9c27b0",
    "review":          "#00bcd4",
    "other":           "#9e9e9e",
    "default":         "#607d8b",
}

# lighter shades for chapter row vs segment row
CHAPTER_COLORS_LIGHT = {k: v + "bb" for k, v in CHAPTER_COLORS.items()}

@dataclass
class StageState:
    id: str
    label: str
    status: str = "pending"
    message: str = ""


@dataclass
class AppState:
    config_path: str = ""
    config_raw: str = ""
    config: Dict = field(default_factory=dict)
    stages: List[StageState] = field(default_factory=list)
    running: bool = False
    chapters: List[Any] = field(default_factory=list)
    total_cost: float = 0.0
    cost_lines: List[str] = field(default_factory=list)
    error: Optional[str] = None

    def reset_run(self):
        self.chapters = []
        self.total_cost = 0.0
        self.cost_lines = []
        self.error = None
        self.running = True
        for s in self.stages:
            s.status = "pending"
            s.message = ""

class _QueueWriter:
    def __init__(self, q: queue.Queue):
        self._q = q
        self._buf = ""

    def write(self, text):
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._q.put((MSG_LOG, line))

    def flush(self):
        if self._buf.strip():
            self._q.put((MSG_LOG, self._buf))
            self._buf = ""

    def fileno(self):
        return sys.__stdout__.fileno()

class ProcessingWorker:
    def __init__(self, q: queue.Queue, config: Dict):
        self._q = q
        self._config = config

    def _emit(self, kind, data=None):
        self._q.put((kind, data))

    def run(self):
        from main import (process_borders, process_transcript, process_timeline,
                          _copy_inputs_to_output, _move_inputs)

        config = self._config
        border_images = None
        chapters = None

        _copy_inputs_to_output(config)

        try:
            self._emit(MSG_STAGE_START, "borders")
            border_images = process_borders(config)
            self._emit(MSG_STAGE_DONE, "borders")
        except Exception as e:
            self._emit(MSG_STAGE_ERROR, ("borders", str(e)))
            self._emit(MSG_ERROR, f"Border processing failed: {e}")
            return

        try:
            self._emit(MSG_STAGE_START, "transcript")
            chapters = process_transcript(config)
            self._emit(MSG_STAGE_DONE, "transcript")
        except Exception as e:
            self._emit(MSG_STAGE_ERROR, ("transcript", str(e)))
            self._emit(MSG_ERROR, f"Transcript failed: {e}")
            return

        if chapters:
            self._emit(MSG_STAGE_START, "chapters")
            self._emit(MSG_CHAPTERS, chapters)
            self._emit(MSG_STAGE_DONE, "chapters")
        else:
            self._emit(MSG_STAGE_ERROR, ("chapters", "none generated"))

        try:
            self._emit(MSG_STAGE_START, "timeline")
            process_timeline(config, border_images, chapters)
            self._emit(MSG_STAGE_DONE, "timeline")
        except Exception as e:
            self._emit(MSG_STAGE_ERROR, ("timeline", str(e)))
            self._emit(MSG_ERROR, f"Timeline failed: {e}")
            return

        _move_inputs(config)

        try:
            self._emit(MSG_STAGE_START, "tests")
            import unittest
            suite = unittest.TestLoader().discover("tests")
            result = unittest.TextTestRunner(verbosity=0, stream=open(os.devnull, "w")).run(suite)
            if result.wasSuccessful():
                self._emit(MSG_STAGE_DONE, "tests")
            else:
                fails = len(result.failures) + len(result.errors)
                self._emit(MSG_STAGE_ERROR, ("tests", f"{fails} failure(s)"))
        except Exception as e:
            self._emit(MSG_STAGE_ERROR, ("tests", str(e)))

        self._emit(MSG_DONE, None)

def _fmt_time(seconds):
    if not seconds:
        return "0:00"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def _chapter_attr(ch, *keys):
    d = ch.__dict__ if hasattr(ch, "__dict__") else ch
    for k in keys:
        if k in d:
            return d[k]
    return None


_COST_RE = re.compile(r"\$\s*([\d.]+)")

class PAEApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PAE — Post Production Assistant")
        self.geometry("1380x860")
        self.minsize(950, 640)
        self.configure(bg="#1e1e1e")

        self._queue: queue.Queue = queue.Queue()
        self._worker_thread: Optional[threading.Thread] = None
        self._orig_stdout = sys.stdout

        # Timeline drag state
        self._drag_boundary: Optional[int] = None
        self._timeline_total: float = 0.0
        self._timeline_ch_segs: List = []   # [(x1, x2, ch_idx)]
        self._timeline_seg_segs: List = []  # [(x1, x2, ch_idx, seg_idx, seg_dict)]

        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        self.state = AppState(
            config_path=config_path,
            stages=[StageState(**s) for s in PIPELINE_STAGES],
        )

        self._load_config_file()
        self._configure_styles()
        self._build_ui()
        self._poll_queue()

    def _load_config_file(self):
        try:
            with open(self.state.config_path, "r", encoding="utf-8") as f:
                raw = f.read()
            self.state.config_raw = raw
            self.state.config = json.loads(raw)
        except Exception as e:
            self.state.config_raw = "{}"
            self.state.config = {}
            messagebox.showerror("Config Error", str(e))

    def _save_config(self):
        try:
            raw = self._config_text.get("1.0", "end-1c")
            parsed = json.loads(raw)
            self.state.config_raw = raw
            self.state.config = parsed
            with open(self.state.config_path, "w", encoding="utf-8") as f:
                f.write(raw)
            self._refresh_videos_list()
            self._set_status("Config saved.")
        except json.JSONDecodeError as e:
            messagebox.showerror("JSON Error", str(e))

    def _reload_config(self):
        self._load_config_file()
        self._config_text.delete("1.0", "end")
        self._config_text.insert("1.0", self.state.config_raw)
        self._refresh_videos_list()
        self._set_status("Config reloaded.")

    def _browse_config(self):
        path = filedialog.askopenfilename(
            title="Open config",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if path:
            self.state.config_path = path
            self._reload_config()

    def _configure_styles(self):
        s = ttk.Style(self)
        s.theme_use("clam")

        bg, fg, tab_bg, sel = "#1e1e1e", "#d4d4d4", "#2d2d30", "#264f78"

        s.configure("TNotebook", background=bg, borderwidth=0)
        s.configure("TNotebook.Tab", background=tab_bg, foreground=fg,
                    padding=[14, 6], font=("Arial", 9))
        s.map("TNotebook.Tab",
              background=[("selected", "#3c3c3c")],
              foreground=[("selected", "white")])
        s.configure("TFrame", background=bg)
        s.configure("TLabel", background=bg, foreground=fg)
        s.configure("Treeview", background="#252526", foreground=fg,
                    fieldbackground="#252526", rowheight=24)
        s.configure("Treeview.Heading", background=tab_bg, foreground="#aaaaaa",
                    font=("Arial", 9))
        s.map("Treeview", background=[("selected", sel)])
        s.configure("TSeparator", background="#3c3c3c")

    def _build_ui(self):
        self._build_toolbar()

        pane = tk.PanedWindow(self, orient="horizontal", bg="#1e1e1e",
                              sashwidth=5, sashrelief="flat", sashpad=0)
        pane.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        pane.add(self._build_left_panel(pane), minsize=380)
        pane.add(self._build_right_panel(pane), minsize=540)

    def _build_toolbar(self):
        bar = tk.Frame(self, bg="#252526", pady=7, padx=12)
        bar.pack(fill="x")

        tk.Label(bar, text="PAE", bg="#252526", fg="#e8e8e8",
                 font=("Arial", 13, "bold")).pack(side="left")
        tk.Label(bar, text="Post Production Assistant", bg="#252526", fg="#777788",
                 font=("Arial", 9)).pack(side="left", padx=(8, 0))

        self._stop_btn = tk.Button(
            bar, text="■  Stop", command=self._stop_run,
            bg="#6b2b2b", fg="#ffaaaa", font=("Arial", 9, "bold"),
            padx=12, pady=3, relief="flat", cursor="hand2", state="disabled")
        self._stop_btn.pack(side="right", padx=3)

        self._run_btn = tk.Button(
            bar, text="▶  Run", command=self._start_run,
            bg="#0e639c", fg="white", font=("Arial", 9, "bold"),
            padx=14, pady=3, relief="flat", cursor="hand2")
        self._run_btn.pack(side="right", padx=3)

        self._status_var = tk.StringVar(value="Ready")
        tk.Label(bar, textvariable=self._status_var, bg="#252526",
                 fg="#888899", font=("Arial", 9)).pack(side="right", padx=16)

    def _build_left_panel(self, parent):
        frame = tk.Frame(parent, bg="#1e1e1e")
        nb = ttk.Notebook(frame)
        nb.pack(fill="both", expand=True)
        nb.add(self._build_config_tab(nb), text="  Config  ")
        nb.add(self._build_videos_tab(nb), text="  Videos  ")
        return frame

    def _build_config_tab(self, parent):
        tab = tk.Frame(parent, bg="#1e1e1e")

        btn_bar = tk.Frame(tab, bg="#252526", pady=5, padx=8)
        btn_bar.pack(fill="x")
        for label, cmd in [("Save", self._save_config),
                            ("Reload", self._reload_config),
                            ("Browse…", self._browse_config)]:
            tk.Button(btn_bar, text=label, command=cmd, bg="#3c3c3c", fg="#d4d4d4",
                      font=("Arial", 9), padx=10, pady=2,
                      relief="flat", cursor="hand2").pack(side="left", padx=2)

        path_lbl = tk.Label(tab, text="", bg="#1e1e1e", fg="#555566",
                            font=("Consolas", 8), anchor="w")
        path_lbl.pack(fill="x", padx=8, pady=(2, 0))
        self._config_path_lbl = path_lbl
        self._update_config_path_label()

        self._config_text = scrolledtext.ScrolledText(
            tab, bg="#0d0d0d", fg="#9cdcfe", insertbackground="white",
            font=("Consolas", 10), relief="flat", borderwidth=0,
            wrap="none", selectbackground="#264f78")
        self._config_text.pack(fill="both", expand=True, padx=0, pady=2)
        self._config_text.insert("1.0", self.state.config_raw)
        return tab

    def _build_videos_tab(self, parent):
        tab = tk.Frame(parent, bg="#1e1e1e")

        btn_bar = tk.Frame(tab, bg="#252526", pady=5, padx=8)
        btn_bar.pack(fill="x")
        for label, cmd in [("+ Add", self._add_video),
                            ("− Remove", self._remove_video),
                            ("Browse…", self._browse_video_path),
                            ("Refresh", self._refresh_videos_list)]:
            tk.Button(btn_bar, text=label, command=cmd, bg="#3c3c3c", fg="#d4d4d4",
                      font=("Arial", 9), padx=8, pady=2,
                      relief="flat", cursor="hand2").pack(side="left", padx=2)

        cols = ("tags", "path", "regex", "found")
        self._videos_tree = ttk.Treeview(tab, columns=cols, show="headings",
                                         selectmode="browse")
        for col, width, heading in [
            ("tags",  90,  "Tags"),
            ("path",  130, "Directory"),
            ("regex", 150, "Filename Pattern"),
            ("found", 50,  "Found"),
        ]:
            self._videos_tree.heading(col, text=heading)
            self._videos_tree.column(col, width=width, minwidth=30)
        self._videos_tree.pack(fill="both", expand=True)

        detail = tk.Frame(tab, bg="#252526", pady=6, padx=10)
        detail.pack(fill="x")
        tk.Label(detail, text="Selected video path:", bg="#252526",
                 fg="#888", font=("Arial", 8)).pack(anchor="w")
        self._video_detail_var = tk.StringVar(value="—")
        tk.Label(detail, textvariable=self._video_detail_var, bg="#252526",
                 fg="#9cdcfe", font=("Consolas", 8), wraplength=350,
                 justify="left", anchor="w").pack(fill="x")

        self._videos_tree.bind("<<TreeviewSelect>>", self._on_video_select)
        self._videos_tree.bind("<Double-1>", self._on_video_double_click)
        self._videos_tree.bind("<Button-3>", self._videos_context_menu)
        self._refresh_videos_list()
        return tab

    def _refresh_videos_list(self):
        for row in self._videos_tree.get_children():
            self._videos_tree.delete(row)

        videos = (self.state.config.get("project") or {}).get("videos") or []
        for v in videos:
            pc = v.get("path") or {}
            path_dir = pc.get("path", pc.get("file", ""))
            regex = pc.get("regex", "")
            tags = ", ".join(v.get("tags") or [])
            found = "✓" if pc.get("exists", False) else "·"
            iid = self._videos_tree.insert("", "end", values=(tags, path_dir, regex, found))
            if not pc.get("exists", False):
                self._videos_tree.tag_configure("missing", foreground="#f44336")
                self._videos_tree.item(iid, tags=("missing",))

    def _on_video_select(self, _event=None):
        sel = self._videos_tree.selection()
        if not sel:
            return
        idx = self._videos_tree.index(sel[0])
        videos = (self.state.config.get("project") or {}).get("videos") or []
        if 0 <= idx < len(videos):
            pc = videos[idx].get("path") or {}
            self._video_detail_var.set(pc.get("file") or pc.get("path") or "—")

    def _on_video_double_click(self, event):
        sel = self._videos_tree.selection()
        if not sel:
            return
        idx = self._videos_tree.index(sel[0])
        videos = (self.state.config.get("project") or {}).get("videos") or []
        if 0 <= idx < len(videos):
            self._open_video_edit_dialog(idx, videos[idx])

    def _videos_context_menu(self, event):
        item = self._videos_tree.identify_row(event.y)
        if item:
            self._videos_tree.selection_set(item)
        idx = self._videos_tree.index(item) if item else None
        menu = tk.Menu(self, tearoff=0, bg="#2d2d30", fg="#d4d4d4",
                       activebackground="#264f78", activeforeground="white",
                       font=("Arial", 9))
        menu.add_command(label="Edit…", command=lambda: self._on_video_double_click(None))
        menu.add_command(label="Change File…", command=self._browse_video_path)
        menu.add_separator()
        menu.add_command(label="Set as Audio Source", command=lambda: self._set_audio_source(idx))
        menu.add_command(label="Toggle 'main' Tag", command=lambda: self._toggle_tag(idx, "main"))
        menu.add_command(label="Toggle 'overlay' Tag", command=lambda: self._toggle_tag(idx, "overlay"))
        menu.add_separator()
        menu.add_command(label="Remove", command=self._remove_video)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _set_audio_source(self, idx):
        if idx is None:
            return
        videos = (self.state.config.get("project") or {}).get("videos") or []
        if not (0 <= idx < len(videos)):
            return
        for i, v in enumerate(videos):
            tags = v.get("tags") or []
            if "audio_source" in tags and i != idx:
                tags.remove("audio_source")
            v["tags"] = tags
        tags = videos[idx].get("tags") or []
        if "audio_source" not in tags:
            tags.append("audio_source")
        videos[idx]["tags"] = tags
        self._sync_config_from_state()

    def _toggle_tag(self, idx, tag):
        if idx is None:
            return
        videos = (self.state.config.get("project") or {}).get("videos") or []
        if not (0 <= idx < len(videos)):
            return
        tags = list(videos[idx].get("tags") or [])
        if tag in tags:
            tags.remove(tag)
        else:
            tags.append(tag)
        videos[idx]["tags"] = tags
        self._sync_config_from_state()

    def _open_video_edit_dialog(self, idx, video):
        dlg = tk.Toplevel(self)
        dlg.title(f"Edit Video {idx + 1}")
        dlg.configure(bg="#1e1e1e")
        dlg.geometry("480x340")
        dlg.transient(self)
        dlg.grab_set()

        fields = {}
        pc = video.get("path") or {}
        ov = video.get("overlay") or {}
        border = ov.get("border") or {}

        def row(label, val=""):
            f = tk.Frame(dlg, bg="#1e1e1e")
            f.pack(fill="x", padx=14, pady=3)
            tk.Label(f, text=label, bg="#1e1e1e", fg="#888", font=("Arial", 9),
                     width=16, anchor="w").pack(side="left")
            e = tk.Entry(f, bg="#252526", fg="#d4d4d4", insertbackground="white",
                         font=("Consolas", 9), relief="flat", bd=4)
            e.insert(0, str(val))
            e.pack(side="left", fill="x", expand=True)
            return e

        tk.Label(dlg, text=f"Video {idx + 1}", bg="#1e1e1e", fg="#e8e8e8",
                 font=("Arial", 11, "bold")).pack(pady=(12, 6))

        fields["path"] = row("Directory", pc.get("path", pc.get("file", "")))
        fields["regex"] = row("File Pattern (regex)", pc.get("regex", ""))
        fields["tags"] = row("Tags (comma-sep)", ", ".join(video.get("tags") or []))
        fields["x"] = row("Overlay X", ov.get("x", 0))
        fields["y"] = row("Overlay Y", ov.get("y", 0))
        fields["w"] = row("Overlay Width", ov.get("width", 1))
        fields["h_ov"] = row("Overlay Height", ov.get("height", 1))

        border_var = tk.BooleanVar(value=border.get("enabled", False))
        bf = tk.Frame(dlg, bg="#1e1e1e")
        bf.pack(fill="x", padx=14, pady=3)
        tk.Label(bf, text="Border Enabled", bg="#1e1e1e", fg="#888", font=("Arial", 9),
                 width=16, anchor="w").pack(side="left")
        tk.Checkbutton(bf, variable=border_var, bg="#1e1e1e", fg="#d4d4d4",
                       activebackground="#1e1e1e", selectcolor="#252526").pack(side="left")

        def save():
            videos = (self.state.config.get("project") or {}).get("videos") or []
            if not (0 <= idx < len(videos)):
                dlg.destroy()
                return
            v = videos[idx]
            v["path"] = {"path": fields["path"].get(), "regex": fields["regex"].get()}
            v["tags"] = [t.strip() for t in fields["tags"].get().split(",") if t.strip()]
            try:
                v["overlay"] = {
                    "x": float(fields["x"].get()),
                    "y": float(fields["y"].get()),
                    "width": float(fields["w"].get()),
                    "height": float(fields["h_ov"].get()),
                    "border": {**border, "enabled": border_var.get()},
                }
            except ValueError:
                pass
            self._sync_config_from_state()
            dlg.destroy()

        btn_row = tk.Frame(dlg, bg="#1e1e1e")
        btn_row.pack(pady=10)
        tk.Button(btn_row, text="Save", command=save, bg="#0e639c", fg="white",
                  font=("Arial", 9), padx=14, relief="flat").pack(side="left", padx=5)
        tk.Button(btn_row, text="Cancel", command=dlg.destroy, bg="#3c3c3c", fg="#d4d4d4",
                  font=("Arial", 9), padx=10, relief="flat").pack(side="left")

    def _add_video(self):
        path = filedialog.askopenfilename(
            title="Select video",
            filetypes=[("Video files", "*.mp4 *.mov *.mkv *.avi *.mxf"), ("All", "*.*")],
        )
        if not path:
            return
        proj = self.state.config.setdefault("project", {})
        videos = proj.setdefault("videos", [])
        videos.append({
            "path": {
                "path": os.path.dirname(path),
                "regex": re.escape(os.path.basename(path)),
            },
            "tags": ["main"],
            "overlay": {"x": 0, "y": 0, "width": 1, "height": 1,
                        "border": {"enabled": False}},
        })
        self._sync_config_from_state()

    def _remove_video(self):
        sel = self._videos_tree.selection()
        if not sel:
            return
        idx = self._videos_tree.index(sel[0])
        videos = (self.state.config.get("project") or {}).get("videos") or []
        if 0 <= idx < len(videos):
            videos.pop(idx)
            self._sync_config_from_state()

    def _browse_video_path(self):
        sel = self._videos_tree.selection()
        if not sel:
            messagebox.showinfo("No selection", "Select a video row first.")
            return
        path = filedialog.askopenfilename(
            title="Select replacement video",
            filetypes=[("Video files", "*.mp4 *.mov *.mkv *.avi *.mxf"), ("All", "*.*")],
        )
        if not path:
            return
        idx = self._videos_tree.index(sel[0])
        videos = (self.state.config.get("project") or {}).get("videos") or []
        if 0 <= idx < len(videos):
            videos[idx]["path"] = {
                "path": os.path.dirname(path),
                "regex": re.escape(os.path.basename(path)),
            }
            self._sync_config_from_state()

    def _sync_config_from_state(self):
        raw = json.dumps(self.state.config, indent=2)
        self.state.config_raw = raw
        self._config_text.delete("1.0", "end")
        self._config_text.insert("1.0", raw)
        self._refresh_videos_list()

    def _update_config_path_label(self):
        if hasattr(self, "_config_path_lbl"):
            self._config_path_lbl.config(
                text=os.path.basename(self.state.config_path))

    def _build_right_panel(self, parent):
        frame = tk.Frame(parent, bg="#1e1e1e")
        self._right_nb = ttk.Notebook(frame)
        self._right_nb.pack(fill="both", expand=True)
        self._right_nb.add(self._build_progress_tab(self._right_nb), text="  Progress  ")
        self._right_nb.add(self._build_output_tab(self._right_nb),   text="  Output    ")
        return frame

    def _build_progress_tab(self, parent):
        tab = tk.Frame(parent, bg="#1e1e1e")

        stages_frame = tk.Frame(tab, bg="#252526", pady=10, padx=14)
        stages_frame.pack(fill="x")

        self._stage_dots: Dict[str, tk.Label] = {}
        self._stage_msg:  Dict[str, tk.Label] = {}

        for s in self.state.stages:
            row = tk.Frame(stages_frame, bg="#252526")
            row.pack(fill="x", pady=3)

            dot = tk.Label(row, text="●", bg="#252526",
                           fg=STATUS_COLORS["pending"], font=("Arial", 13))
            dot.pack(side="left", padx=(0, 8))

            tk.Label(row, text=s.label, bg="#252526", fg="#d4d4d4",
                     font=("Arial", 10), anchor="w", width=22).pack(side="left")

            msg = tk.Label(row, text="", bg="#252526", fg="#666677",
                           font=("Arial", 9), anchor="e")
            msg.pack(side="right")

            self._stage_dots[s.id] = dot
            self._stage_msg[s.id]  = msg

        ttk.Separator(tab, orient="horizontal").pack(fill="x", pady=4)

        tk.Label(tab, text="Log", bg="#1e1e1e", fg="#666677",
                 font=("Arial", 8), anchor="w").pack(fill="x", padx=10, pady=(2, 0))

        self._log_text = scrolledtext.ScrolledText(
            tab, bg="#0a0a0a", fg="#cccccc", insertbackground="white",
            font=("Consolas", 9), relief="flat", borderwidth=0,
            state="disabled", wrap="word", selectbackground="#264f78")
        self._log_text.pack(fill="both", expand=True, padx=4, pady=(0, 4))

        self._log_text.tag_configure("error",   foreground="#f48771")
        self._log_text.tag_configure("cost",    foreground="#dcdcaa")
        self._log_text.tag_configure("success", foreground="#4ec994")

        return tab

    def _build_output_tab(self, parent):
        tab = tk.Frame(parent, bg="#1e1e1e")
        nb = ttk.Notebook(tab)
        nb.pack(fill="both", expand=True)
        nb.add(self._build_chapters_tab(nb), text="  Chapters  ")
        nb.add(self._build_timeline_tab(nb), text="  Timeline  ")
        nb.add(self._build_cost_tab(nb),     text="  Cost      ")
        self._output_nb = nb
        return tab

    def _build_chapters_tab(self, parent):
        tab = tk.Frame(parent, bg="#1e1e1e")

        btn_bar = tk.Frame(tab, bg="#252526", pady=4, padx=8)
        btn_bar.pack(fill="x")
        for label, cmd in [("Rename", self._rename_selected_chapter),
                            ("Delete", self._delete_selected_chapter),
                            ("Merge ↓", self._merge_selected_chapter)]:
            tk.Button(btn_bar, text=label, command=cmd, bg="#3c3c3c", fg="#d4d4d4",
                      font=("Arial", 9), padx=8, pady=2,
                      relief="flat", cursor="hand2").pack(side="left", padx=2)

        cols = ("type", "start", "end", "duration")
        self._chapters_tree = ttk.Treeview(tab, columns=cols,
                                            show="tree headings", selectmode="browse")
        self._chapters_tree.heading("#0",       text="Chapter / Segment")
        self._chapters_tree.heading("type",     text="Type")
        self._chapters_tree.heading("start",    text="Start")
        self._chapters_tree.heading("end",      text="End")
        self._chapters_tree.heading("duration", text="Duration")

        self._chapters_tree.column("#0",       width=210, minwidth=120)
        self._chapters_tree.column("type",     width=110, minwidth=60, anchor="center")
        self._chapters_tree.column("start",    width=65,  minwidth=50, anchor="center")
        self._chapters_tree.column("end",      width=65,  minwidth=50, anchor="center")
        self._chapters_tree.column("duration", width=70,  minwidth=50, anchor="center")

        self._chapters_tree.tag_configure("chapter", font=("Arial", 9, "bold"),
                                           foreground="#e8e8e8")
        self._chapters_tree.tag_configure("segment", foreground="#aaaaaa")

        vsb = ttk.Scrollbar(tab, orient="vertical", command=self._chapters_tree.yview)
        self._chapters_tree.configure(yscrollcommand=vsb.set)

        self._chapters_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self._chapters_count_var = tk.StringVar(value="No chapters yet")
        tk.Label(tab, textvariable=self._chapters_count_var, bg="#1e1e1e",
                 fg="#666677", font=("Arial", 9)).pack(anchor="e", padx=8, pady=4)

        self._chapters_tree.bind("<Double-1>", self._on_chapter_double_click)
        self._chapters_tree.bind("<Button-3>", self._chapters_context_menu)
        return tab

    def _on_chapter_double_click(self, event):
        sel = self._chapters_tree.selection()
        if not sel:
            return
        item = sel[0]
        parent = self._chapters_tree.parent(item)
        if parent == "":
            self._rename_selected_chapter()

    def _chapters_context_menu(self, event):
        item = self._chapters_tree.identify_row(event.y)
        if item:
            self._chapters_tree.selection_set(item)
        menu = tk.Menu(self, tearoff=0, bg="#2d2d30", fg="#d4d4d4",
                       activebackground="#264f78", activeforeground="white",
                       font=("Arial", 9))
        parent = self._chapters_tree.parent(item) if item else ""
        if parent == "":
            menu.add_command(label="Rename…", command=self._rename_selected_chapter)
            menu.add_command(label="Merge with Next", command=self._merge_selected_chapter)
            menu.add_separator()
            menu.add_command(label="Delete Chapter", command=self._delete_selected_chapter)
        else:
            menu.add_command(label="(segment — read only)", state="disabled")
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _selected_chapter_index(self) -> Optional[int]:
        """Return state.chapters index for the selected treeview item, or None."""
        sel = self._chapters_tree.selection()
        if not sel:
            return None
        item = sel[0]
        parent = self._chapters_tree.parent(item)
        root = item if parent == "" else parent
        children = self._chapters_tree.get_children("")
        try:
            return list(children).index(root)
        except ValueError:
            return None

    def _rename_selected_chapter(self):
        idx = self._selected_chapter_index()
        if idx is None or not self.state.chapters:
            return
        ch = self.state.chapters[idx]
        current = getattr(ch, "title", f"Chapter {idx + 1}")
        new_title = simpledialog.askstring(
            "Rename Chapter", "New title:", initialvalue=current, parent=self)
        if new_title and new_title.strip():
            ch.title = new_title.strip()
            self._refresh_chapters_tree()
            self._redraw_timeline()

    def _delete_selected_chapter(self):
        idx = self._selected_chapter_index()
        if idx is None or not self.state.chapters:
            return
        if not messagebox.askyesno("Delete Chapter",
                                    f"Delete chapter {idx + 1}?", parent=self):
            return
        self.state.chapters.pop(idx)
        self._refresh_chapters_tree()
        self._redraw_timeline()

    def _merge_selected_chapter(self):
        idx = self._selected_chapter_index()
        if idx is None or not self.state.chapters:
            return
        chapters = self.state.chapters
        if idx >= len(chapters) - 1:
            messagebox.showinfo("Merge", "No next chapter to merge with.", parent=self)
            return
        a, b = chapters[idx], chapters[idx + 1]
        a.end_time = b.end_time
        a.duration = a.end_time - a.start_time
        a.segments = a.segments + b.segments
        a.segment_types = a.segment_types + b.segment_types
        chapters.pop(idx + 1)
        self._refresh_chapters_tree()
        self._redraw_timeline()

    def _build_timeline_tab(self, parent):
        tab = tk.Frame(parent, bg="#1e1e1e")

        legend_frame = tk.Frame(tab, bg="#1e1e1e")
        legend_frame.pack(fill="x", padx=8, pady=(6, 2))
        items = list(CHAPTER_COLORS.items())
        half = (len(items) + 1) // 2
        for row_idx, row_items in enumerate([items[:half], items[half:]]):
            rf = tk.Frame(legend_frame, bg="#1e1e1e")
            rf.pack(anchor="w")
            for ctype, color in row_items:
                tk.Label(rf, text="■", fg=color, bg="#1e1e1e",
                         font=("Arial", 9)).pack(side="left", padx=(0, 1))
                tk.Label(rf, text=ctype, bg="#1e1e1e", fg="#777",
                         font=("Arial", 8)).pack(side="left", padx=(0, 10))

        self._timeline_canvas = tk.Canvas(tab, bg="#0a0a0a", height=210,
                                          highlightthickness=0, cursor="crosshair")
        self._timeline_canvas.pack(fill="x", padx=8, pady=(4, 2))

        self._timeline_canvas.bind("<Configure>",      lambda _: self._redraw_timeline())
        self._timeline_canvas.bind("<Motion>",         self._timeline_hover)
        self._timeline_canvas.bind("<Button-1>",       self._timeline_click)
        self._timeline_canvas.bind("<B1-Motion>",      self._timeline_drag)
        self._timeline_canvas.bind("<ButtonRelease-1>",self._timeline_drag_end)
        self._timeline_canvas.bind("<Button-3>",       self._timeline_right_click)

        self._timeline_tooltip = tk.Label(tab, text="", bg="#2d2d30", fg="#d4d4d4",
                                          font=("Consolas", 8), padx=6, pady=2,
                                          relief="flat", anchor="w")
        self._timeline_tooltip.pack(fill="x", padx=8, pady=(0, 4))

        return tab

    # Layout constants (canvas height = 210)
    _CH_Y1, _CH_Y2   = 8,   72   # chapter blocks row
    _SEG_Y1, _SEG_Y2 = 80, 145   # segment-type blocks row
    _RULER_Y          = 155       # ruler tick base

    def _redraw_timeline(self):
        c = self._timeline_canvas
        c.delete("all")
        chapters = self.state.chapters
        w = c.winfo_width()
        h = c.winfo_height()

        if not chapters or w < 2:
            c.create_text(w // 2, h // 2, text="No chapters",
                          fill="#444455", font=("Arial", 9))
            return

        ends = [_chapter_attr(ch, "end_time", "end") or 0 for ch in chapters]
        total = max(ends) if ends else 1
        self._timeline_total = total

        self._timeline_ch_segs  = []
        self._timeline_seg_segs = []

        for ci, ch in enumerate(chapters):
            st = _chapter_attr(ch, "start_time", "start") or 0
            en = _chapter_attr(ch, "end_time",   "end")   or 0
            x1 = (st / total) * w
            x2 = (en / total) * w

            seg_types = getattr(ch, "segment_types", []) or []
            dom_type = seg_types[0].get("type", "default") if seg_types else "default"
            color = CHAPTER_COLORS.get(dom_type, CHAPTER_COLORS["default"])

            # Chapter block
            c.create_rectangle(x1, self._CH_Y1, x2, self._CH_Y2,
                                fill=color, outline="#1e1e1e", width=1,
                                tags=(f"ch_{ci}",))
            label = getattr(ch, "title", f"Chapter {ci+1}")
            if x2 - x1 > 30:
                c.create_text(
                    min(x1 + 6, (x1 + x2) / 2), (self._CH_Y1 + self._CH_Y2) / 2,
                    text=label, fill="white", font=("Arial", 8, "bold"),
                    anchor="w" if x2 - x1 > 80 else "center",
                    width=max(1, int(x2 - x1 - 8)),
                )

            self._timeline_ch_segs.append((x1, x2, ci))

            # Segment-type blocks within this chapter
            for si, seg in enumerate(seg_types):
                seg_st = seg.get("start_time", st)
                seg_en = seg.get("end_time", en)
                sx1 = (seg_st / total) * w
                sx2 = (seg_en / total) * w
                stype = seg.get("type", "other")
                scolor = CHAPTER_COLORS.get(stype, CHAPTER_COLORS["other"])
                c.create_rectangle(sx1, self._SEG_Y1, sx2, self._SEG_Y2,
                                   fill=scolor, outline="#1e1e1e", width=1,
                                   tags=(f"ch_{ci}_seg_{si}",))
                if sx2 - sx1 > 20:
                    c.create_text(
                        (sx1 + sx2) / 2, (self._SEG_Y1 + self._SEG_Y2) / 2,
                        text=stype, fill="white", font=("Arial", 7),
                        width=max(1, int(sx2 - sx1 - 4)),
                    )
                self._timeline_seg_segs.append((sx1, sx2, ci, si, seg))

            if ci < len(chapters) - 1:
                c.create_line(x2, self._CH_Y1 - 4, x2, self._SEG_Y2 + 4,
                              fill="#555566", width=2, dash=(3, 3),
                              tags=(f"boundary_{ci}",))

        c.create_text(4, (self._CH_Y1 + self._CH_Y2) // 2,
                      text="Ch", fill="#555566", font=("Arial", 7), anchor="w")
        c.create_text(4, (self._SEG_Y1 + self._SEG_Y2) // 2,
                      text="Seg", fill="#555566", font=("Arial", 7), anchor="w")

        tick_interval = self._nice_interval(total)
        t = 0
        while t <= total:
            x = (t / total) * w
            c.create_line(x, self._RULER_Y, x, self._RULER_Y + 6, fill="#555566")
            c.create_text(x, self._RULER_Y + 8, text=_fmt_time(t),
                          fill="#555566", font=("Arial", 7), anchor="n")
            t += tick_interval

    def _nice_interval(self, total):
        for iv in [30, 60, 120, 300, 600, 900, 1800, 3600]:
            if total / iv <= 12:
                return iv
        return 3600

    def _find_near_boundary(self, x) -> Optional[int]:
        """Return chapter index i where boundary between ch[i] and ch[i+1] is near x."""
        chapters = self.state.chapters
        w = self._timeline_canvas.winfo_width()
        if len(chapters) < 2 or w <= 0 or self._timeline_total <= 0:
            return None
        for i in range(len(chapters) - 1):
            bx = (_chapter_attr(chapters[i+1], "start_time", "start") or 0) / self._timeline_total * w
            if abs(x - bx) <= 8:
                return i
        return None

    def _timeline_hover(self, event):
        x, y = event.x, event.y
        if self._find_near_boundary(x) is not None:
            self._timeline_canvas.config(cursor="sb_h_double_arrow")
        else:
            self._timeline_canvas.config(cursor="crosshair")

        if self._CH_Y1 <= y <= self._CH_Y2:
            for x1, x2, ci in self._timeline_ch_segs:
                if x1 <= x <= x2:
                    ch = self.state.chapters[ci]
                    title = getattr(ch, "title", f"Chapter {ci+1}")
                    st = _chapter_attr(ch, "start_time", "start") or 0
                    en = _chapter_attr(ch, "end_time",   "end")   or 0
                    self._timeline_tooltip.config(
                        text=f"  {title}  —  {_fmt_time(st)} → {_fmt_time(en)}")
                    return
        elif self._SEG_Y1 <= y <= self._SEG_Y2:
            for x1, x2, ci, si, seg in self._timeline_seg_segs:
                if x1 <= x <= x2:
                    stype = seg.get("type", "other")
                    summary = seg.get("summary", "")
                    st = seg.get("start_time", 0)
                    en = seg.get("end_time", 0)
                    tip = f"  [{stype}]  {_fmt_time(st)} → {_fmt_time(en)}"
                    if summary:
                        tip += f"  —  {summary[:60]}"
                    self._timeline_tooltip.config(text=tip)
                    return
        self._timeline_tooltip.config(text="")

    def _timeline_click(self, event):
        boundary = self._find_near_boundary(event.x)
        if boundary is not None:
            self._drag_boundary = boundary
        else:
            self._drag_boundary = None
            for x1, x2, ci in self._timeline_ch_segs:
                if x1 <= event.x <= x2:
                    children = self._chapters_tree.get_children("")
                    if ci < len(children):
                        self._chapters_tree.selection_set(children[ci])
                        self._chapters_tree.see(children[ci])
                    break

    def _timeline_drag(self, event):
        if self._drag_boundary is None or self._timeline_total <= 0:
            return
        w = self._timeline_canvas.winfo_width()
        if w <= 0:
            return
        new_time = max(0.0, min(self._timeline_total, (event.x / w) * self._timeline_total))
        chapters = self.state.chapters
        i = self._drag_boundary
        if i < 0 or i + 1 >= len(chapters):
            return
        # Clamp: min 5s from each chapter's own start/end
        min_t = (_chapter_attr(chapters[i], "start_time", "start") or 0) + 5
        max_t = (_chapter_attr(chapters[i+1], "end_time", "end") or self._timeline_total) - 5
        new_time = max(min_t, min(max_t, new_time))

        if hasattr(chapters[i], "end_time"):
            chapters[i].end_time = new_time
            chapters[i].duration = new_time - (chapters[i].start_time or 0)
        if hasattr(chapters[i+1], "start_time"):
            chapters[i+1].start_time = new_time
            chapters[i+1].duration = (chapters[i+1].end_time or 0) - new_time
        self._redraw_timeline()

    def _timeline_drag_end(self, event):
        self._drag_boundary = None
        self._timeline_canvas.config(cursor="crosshair")

    def _timeline_right_click(self, event):
        x, y = event.x, event.y
        menu = tk.Menu(self, tearoff=0, bg="#2d2d30", fg="#d4d4d4",
                       activebackground="#264f78", activeforeground="white",
                       font=("Arial", 9))

        clicked_ci = None
        for x1, x2, ci in self._timeline_ch_segs:
            if x1 <= x <= x2:
                clicked_ci = ci
                break

        if clicked_ci is not None:
            ch = self.state.chapters[clicked_ci]
            ch_title = getattr(ch, 'title', f'Chapter {clicked_ci + 1}')
            menu.add_command(
                label=f"Rename \"{ch_title}\"…",
                command=lambda: self._rename_chapter_by_index(clicked_ci))
            menu.add_command(
                label="Delete Chapter",
                command=lambda: self._delete_chapter_by_index(clicked_ci))
            if clicked_ci < len(self.state.chapters) - 1:
                menu.add_command(
                    label="Merge with Next",
                    command=lambda: self._merge_chapter_by_index(clicked_ci))
            menu.add_separator()

        menu.add_command(label="Expand All",  command=self._expand_all_chapters)
        menu.add_command(label="Collapse All", command=self._collapse_all_chapters)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _rename_chapter_by_index(self, idx):
        if not (0 <= idx < len(self.state.chapters)):
            return
        children = self._chapters_tree.get_children("")
        if idx < len(children):
            self._chapters_tree.selection_set(children[idx])
        self._rename_selected_chapter()

    def _delete_chapter_by_index(self, idx):
        if not (0 <= idx < len(self.state.chapters)):
            return
        children = self._chapters_tree.get_children("")
        if idx < len(children):
            self._chapters_tree.selection_set(children[idx])
        self._delete_selected_chapter()

    def _merge_chapter_by_index(self, idx):
        if not (0 <= idx < len(self.state.chapters)):
            return
        children = self._chapters_tree.get_children("")
        if idx < len(children):
            self._chapters_tree.selection_set(children[idx])
        self._merge_selected_chapter()

    def _expand_all_chapters(self):
        for child in self._chapters_tree.get_children(""):
            self._chapters_tree.item(child, open=True)

    def _collapse_all_chapters(self):
        for child in self._chapters_tree.get_children(""):
            self._chapters_tree.item(child, open=False)

    def _build_cost_tab(self, parent):
        tab = tk.Frame(parent, bg="#1e1e1e")
        self._cost_text = scrolledtext.ScrolledText(
            tab, bg="#1e1e1e", fg="#dcdcaa", font=("Consolas", 10),
            relief="flat", borderwidth=0, state="disabled", padx=10, pady=10)
        self._cost_text.pack(fill="both", expand=True)
        return tab

    def _start_run(self):
        if self.state.running:
            return

        try:
            raw = self._config_text.get("1.0", "end-1c")
            self.state.config = json.loads(raw)
            self.state.config_raw = raw
        except json.JSONDecodeError as e:
            messagebox.showerror("JSON Error", f"Invalid config:\n{e}")
            return

        from config import resolve_paths
        from main import _assign_asset_ids
        import copy
        config = resolve_paths(copy.deepcopy(self.state.config))
        _assign_asset_ids(config)

        self.state.reset_run()
        self._refresh_stages_ui()
        self._clear_log()
        self._clear_chapters()
        self._clear_cost()
        self._run_btn.config(state="disabled")
        self._stop_btn.config(state="normal")
        self._set_status("Running…")

        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

        sys.stdout = _QueueWriter(self._queue)

        def _run_in_thread():
            try:
                worker = ProcessingWorker(self._queue, config)
                worker.run()
            finally:
                sys.stdout = self._orig_stdout

        self._worker_thread = threading.Thread(target=_run_in_thread, daemon=True)
        self._worker_thread.start()

    def _stop_run(self):
        sys.stdout = self._orig_stdout
        self.state.running = False
        self._run_btn.config(state="normal")
        self._stop_btn.config(state="disabled")
        self._set_status("Stopped.")
        self._append_log("— stopped by user —", tag="error")

    def _poll_queue(self):
        try:
            while True:
                kind, data = self._queue.get_nowait()
                self._dispatch(kind, data)
        except queue.Empty:
            pass
        self.after(50, self._poll_queue)

    def _dispatch(self, kind, data):
        if kind == MSG_LOG:
            text = str(data)
            tag = None
            if "[ERROR]" in text or "Error" in text:
                tag = "error"
            elif "$" in text:
                tag = "cost"
                self.state.cost_lines.append(text)
                self._refresh_cost_tab()
            elif "✓" in text or "complete" in text.lower() or "saved" in text.lower():
                tag = "success"
            self._append_log(text, tag=tag)

        elif kind == MSG_STAGE_START:
            self._set_stage(data, "running")

        elif kind == MSG_STAGE_DONE:
            self._set_stage(data, "done")

        elif kind == MSG_STAGE_ERROR:
            stage_id, msg = data if isinstance(data, tuple) else (data, "")
            self._set_stage(stage_id, "error", msg)

        elif kind == MSG_CHAPTERS:
            self._set_chapters(data)

        elif kind == MSG_DONE:
            self._on_done()

        elif kind == MSG_ERROR:
            self._on_error(str(data))

    def _set_stage(self, stage_id: str, status: str, message: str = ""):
        for s in self.state.stages:
            if s.id == stage_id:
                s.status = status
                s.message = message
                break

        STATUS_TEXT = {
            "pending": "",
            "running": "running…",
            "done":    "✓ done",
            "error":   "✗",
            "skipped": "skipped",
        }

        if stage_id in self._stage_dots:
            self._stage_dots[stage_id].config(fg=STATUS_COLORS.get(status, "#555"))
        if stage_id in self._stage_msg:
            display = message or STATUS_TEXT.get(status, "")
            self._stage_msg[stage_id].config(
                text=display, fg=STATUS_COLORS.get(status, "#666677"))

    def _refresh_stages_ui(self):
        for s in self.state.stages:
            self._set_stage(s.id, s.status, s.message)

    def _append_log(self, text: str, tag: Optional[str] = None):
        self._log_text.config(state="normal")
        if tag:
            self._log_text.insert("end", text + "\n", tag)
        else:
            self._log_text.insert("end", text + "\n")
        self._log_text.see("end")
        self._log_text.config(state="disabled")

    def _clear_log(self):
        self._log_text.config(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.config(state="disabled")

    def _set_chapters(self, chapters):
        self.state.chapters = chapters
        self._refresh_chapters_tree()
        self._redraw_timeline()
        self._right_nb.select(1)  # Switch to Output tab

    def _refresh_chapters_tree(self):
        for row in self._chapters_tree.get_children():
            self._chapters_tree.delete(row)

        chapters = self.state.chapters
        for ci, ch in enumerate(chapters):
            title    = getattr(ch, "title", f"Chapter {ci + 1}")
            start    = _chapter_attr(ch, "start_time", "start") or 0
            end      = _chapter_attr(ch, "end_time",   "end")   or 0
            duration = end - start if end > start else 0
            seg_types = getattr(ch, "segment_types", []) or []
            dom_type = seg_types[0].get("type", "") if seg_types else ""

            ch_iid = self._chapters_tree.insert(
                "", "end",
                text=title,
                values=(dom_type, _fmt_time(start), _fmt_time(end), _fmt_time(duration)),
                open=True, tags=("chapter",))

            for seg in seg_types:
                stype = seg.get("type", "other")
                sst   = seg.get("start_time", start)
                sen   = seg.get("end_time", end)
                sdur  = sen - sst if sen > sst else 0
                summary = seg.get("summary", "")
                label = stype if not summary else f"{stype} — {summary[:40]}"
                self._chapters_tree.insert(
                    ch_iid, "end",
                    text=f"  {label}",
                    values=(stype, _fmt_time(sst), _fmt_time(sen), _fmt_time(sdur)),
                    tags=("segment",))

        self._chapters_count_var.set(
            f"{len(chapters)} chapter(s)" if chapters else "No chapters yet")

    def _clear_chapters(self):
        for row in self._chapters_tree.get_children():
            self._chapters_tree.delete(row)
        self._chapters_count_var.set("No chapters yet")
        self._timeline_ch_segs = []
        self._timeline_seg_segs = []
        self._redraw_timeline()

    def _refresh_cost_tab(self):
        self._cost_text.config(state="normal")
        self._cost_text.delete("1.0", "end")
        self._cost_text.insert("1.0", "\n".join(self.state.cost_lines))
        self._cost_text.config(state="disabled")

    def _clear_cost(self):
        self.state.cost_lines = []
        self._cost_text.config(state="normal")
        self._cost_text.delete("1.0", "end")
        self._cost_text.config(state="disabled")

    def _on_done(self):
        sys.stdout = self._orig_stdout
        self.state.running = False
        self._run_btn.config(state="normal")
        self._stop_btn.config(state="disabled")

        costs = []
        for line in self.state.cost_lines:
            m = _COST_RE.search(line)
            if m and "Total" in line:
                try:
                    costs.append(float(m.group(1)))
                except ValueError:
                    pass
        total = max(costs) if costs else 0.0
        self.state.total_cost = total
        cost_str = f"${total:.4f}" if total else "—"
        self._set_status(f"Done  ·  cost {cost_str}")
        self._append_log("\n✓ Processing complete.", tag="success")

    def _on_error(self, msg: str):
        sys.stdout = self._orig_stdout
        self.state.running = False
        self.state.error = msg
        self._run_btn.config(state="normal")
        self._stop_btn.config(state="disabled")
        self._set_status("Error — see log")
        self._append_log(f"\n[ERROR] {msg}", tag="error")
        for s in self.state.stages:
            if s.status == "running":
                self._set_stage(s.id, "error")

    def _set_status(self, msg: str):
        self._status_var.set(msg)


def main():
    app = PAEApp()
    app.mainloop()


if __name__ == "__main__":
    main()
