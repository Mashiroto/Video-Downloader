"""
Video Downloader
Version: 1.0.0
Author: Mashiroto (with Claude)

Description:
Small desktop app (pop-up window) to downloads videos or audio from
YouTube, TikTok, Instagram and 1000+ other sites using yt-dlp.
Everything runs locally on your PC.

Setup (once):
    pip install yt-dlp pillow imageio-ffmpeg

Start:
    python video_downloader.py

Tip: sites change often. If a download suddenly stops working, update with:
    pip install -U yt-dlp

Changelog:
- 1.0.0: Initial version (link, preview, MP4/MP3, trim, history)
"""
import io
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ---------- Dependency check (friendly message instead of a crash) ----------
try:
    import yt_dlp
    from yt_dlp.utils import download_range_func
    from PIL import Image, ImageTk
except ImportError:
    _r = tk.Tk()
    _r.withdraw()
    messagebox.showerror(
        "Missing packages",
        "Please install the required packages first.\n\n"
        "Open a terminal and run:\n\n"
        "pip install yt-dlp pillow imageio-ffmpeg",
    )
    sys.exit(1)

APP_TITLE = "Video Downloader"

# ---------- Colors (taken from the Picture Converter) ----------
BG = "#08090b"
PANEL = "#111216"
PANEL2 = "#1a1c21"
PANEL3 = "#212429"
BORDER = "#22242a"
BORDER_STRONG = "#3a3d45"
TEXT = "#f3f1ec"
MUTED = "#8c8e96"
ACCENT = "#b98af2"
ACCENT_HOVER = "#cdaaf7"
ACCENT_SOFT = "#3a2d52"
DANGER = "#ef6461"
OK = "#6fcf97"

if sys.platform.startswith("win"):
    FONT = "Segoe UI"
elif sys.platform == "darwin":
    FONT = "Helvetica Neue"
else:
    FONT = "DejaVu Sans"


# ---------- Helpers ----------
def find_ffmpeg():
    """ffmpeg is needed for MP3, merging video+audio and trimming."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def parse_time(text):
    """'90' / '1:30' / '1:02:03' -> seconds (int) or None."""
    text = text.strip()
    if not text:
        return None
    try:
        parts = [float(p) for p in text.split(":")]
    except ValueError:
        return None
    if len(parts) > 3 or any(p < 0 for p in parts):
        return None
    secs = 0
    for p in parts:
        secs = secs * 60 + p
    return int(secs)


def fmt_time(sec):
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def tag_time(sec):
    """Time for file names (no colons, Windows doesn't allow them)."""
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"


def safe_name(name):
    name = re.sub(r'[\\/:*?"<>|]+', "", name).strip().strip(".")
    return name.replace("%", "%%")


def clean_error(e):
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).strip().replace("ERROR: ", "")
    msg = msg.splitlines()[0] if msg else "Unknown error"
    low = msg.lower()
    if "login" in low or "cookies" in low or "sign in" in low:
        msg += "  (This site wants a logged-in account.)"
    return msg[:220]


def open_path(path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


class QuietLogger:
    """Keeps yt-dlp from printing into the console."""
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


# ---------- Small custom widgets (to get the flat dark look) ----------
class Btn(tk.Label):
    def __init__(self, parent, text, command=None, primary=False, padx=16, pady=8, font=None):
        super().__init__(parent, text=text, font=font or (FONT, 10, "bold"),
                         padx=padx, pady=pady, cursor="hand2")
        self.command = command
        self.primary = primary
        self.active = False
        self.disabled = False
        self._paint(False)
        self.bind("<Enter>", lambda e: self._paint(True))
        self.bind("<Leave>", lambda e: self._paint(False))
        self.bind("<Button-1>", self._click)

    def _paint(self, hover):
        if self.disabled:
            bg, fg = PANEL3, MUTED
        elif self.primary or self.active:
            bg, fg = (ACCENT_HOVER if hover else ACCENT), "#150c24"
        else:
            bg, fg = ("#2b2e35" if hover else PANEL3), TEXT
        self.configure(bg=bg, fg=fg, cursor="arrow" if self.disabled else "hand2")

    def _click(self, e):
        if not self.disabled and self.command:
            self.command()

    def set_active(self, on):
        self.active = on
        self._paint(False)

    def set_disabled(self, on):
        self.disabled = on
        self._paint(False)


class Chips(tk.Frame):
    """A row of buttons where exactly one is selected (toggle / presets)."""
    def __init__(self, parent, command=None, padx=12, pady=6):
        super().__init__(parent, bg=PANEL)
        self.command = command
        self.padx, self.pady = padx, pady
        self.btns = []
        self.value = None

    def set_options(self, options, default):
        for b in self.btns:
            b.destroy()
        self.btns = []
        for label, val in options:
            b = Btn(self, label, lambda v=val: self.select(v), padx=self.padx, pady=self.pady)
            b.val = val
            b.pack(side="left", padx=(0, 6))
            self.btns.append(b)
        self.select(default, notify=False)

    def select(self, val, notify=True):
        changed = val != self.value
        self.value = val
        for b in self.btns:
            b.set_active(b.val == val)
        if notify and changed and self.command:
            self.command(val)


class Switch(tk.Canvas):
    """On/off switch."""
    def __init__(self, parent, command=None):
        super().__init__(parent, width=44, height=24, bg=PANEL,
                         highlightthickness=0, cursor="hand2")
        self.on = False
        self.command = command
        self.bind("<Button-1>", self.toggle)
        self.draw()

    def draw(self):
        self.delete("all")
        col = ACCENT if self.on else PANEL3
        self.create_oval(0, 0, 24, 24, fill=col, outline=col)
        self.create_oval(20, 0, 44, 24, fill=col, outline=col)
        self.create_rectangle(12, 0, 32, 24, fill=col, outline=col)
        x = 24 if self.on else 2
        self.create_oval(x, 2, x + 20, 22, fill="#150c24" if self.on else MUTED, outline="")

    def toggle(self, e=None):
        self.on = not self.on
        self.draw()
        if self.command:
            self.command(self.on)


class RangeSlider(tk.Canvas):
    """One bar with two handles: start and end of the part you want to keep."""
    PAD = 12

    def __init__(self, parent, command):
        super().__init__(parent, height=32, bg=PANEL, highlightthickness=0, cursor="hand2")
        self.command = command
        self.max, self.lo, self.hi = 1, 0, 1
        self.enabled = False
        self.drag = None
        self.bind("<Configure>", lambda e: self.draw())
        self.bind("<Button-1>", self.press)
        self.bind("<B1-Motion>", self.move)

    def _x(self, v):
        span = max(1, self.winfo_width() - 2 * self.PAD)
        return self.PAD + span * (v / self.max)

    def _v(self, x):
        span = max(1, self.winfo_width() - 2 * self.PAD)
        return round(min(max((x - self.PAD) / span, 0), 1) * self.max)

    def set(self, lo, hi, mx=None):
        if mx is not None:
            self.max = max(1, mx)
        self.lo, self.hi = lo, hi
        self.draw()

    def draw(self):
        self.delete("all")
        y = 16
        x1, x2 = self._x(self.lo), self._x(self.hi)
        self.create_line(self.PAD, y, max(self.PAD, self.winfo_width() - self.PAD), y,
                         width=6, fill=PANEL3, capstyle="round")
        col = ACCENT if self.enabled else "#4a4d55"
        self.create_line(x1, y, x2, y, width=6, fill=col, capstyle="round")
        for x in (x1, x2):
            self.create_oval(x - 9, y - 9, x + 9, y + 9, fill=TEXT, outline=col, width=3)

    def press(self, e):
        if not self.enabled:
            return
        x1, x2 = self._x(self.lo), self._x(self.hi)
        d1, d2 = abs(e.x - x1), abs(e.x - x2)
        if d1 < d2:
            self.drag = "lo"
        elif d1 > d2:
            self.drag = "hi"
        else:
            self.drag = "lo" if e.x < x1 else "hi"
        self.move(e)

    def move(self, e):
        if not self.enabled or not self.drag:
            return
        v = self._v(e.x)
        if self.drag == "lo":
            self.lo = min(v, self.hi - 1)
        else:
            self.hi = max(v, self.lo + 1)
        self.draw()
        self.command(self.lo, self.hi)


# ---------- The app ----------
class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.meta = None
        self.duration = None
        self.loading_url = None
        self.loaded_url = None
        self.thumb_img = None
        self.busy = False
        self.paths = {}
        self._debounce = None
        self.ffmpeg = find_ffmpeg()

        root.title(APP_TITLE)
        root.geometry("820x960")
        root.minsize(640, 600)
        root.configure(bg=BG)

        self.setup_style()
        self.build_ui()
        self.set_format("mp4")
        if not self.ffmpeg:
            self.set_status("ffmpeg not found. Run: pip install imageio-ffmpeg", DANGER)
        root.after(100, self.poll)

    # ----- styling for the ttk widgets -----
    def setup_style(self):
        s = ttk.Style()
        s.theme_use("clam")
        s.configure("Purple.Horizontal.TProgressbar", troughcolor=PANEL3, background=ACCENT,
                    bordercolor=PANEL, lightcolor=ACCENT, darkcolor=ACCENT, thickness=8)
        s.configure("Treeview", background=PANEL2, fieldbackground=PANEL2, foreground=TEXT,
                    rowheight=30, borderwidth=0, font=(FONT, 9))
        s.configure("Treeview.Heading", background=PANEL3, foreground=MUTED, borderwidth=0,
                    relief="flat", font=(FONT, 9, "bold"))
        s.map("Treeview", background=[("selected", ACCENT_SOFT)], foreground=[("selected", ACCENT)])
        s.map("Treeview.Heading", background=[("active", PANEL3)])
        s.configure("Vertical.TScrollbar", background=PANEL3, troughcolor=BG, bordercolor=BG,
                    arrowcolor=MUTED, lightcolor=PANEL3, darkcolor=PANEL3)

    # ----- small UI builders -----
    def make_entry(self, parent, var, width=None):
        return tk.Entry(parent, textvariable=var, bg=PANEL2, fg=TEXT, insertbackground=TEXT,
                        relief="flat", highlightthickness=1, highlightbackground=BORDER,
                        highlightcolor=ACCENT, font=(FONT, 11), selectbackground=ACCENT_SOFT,
                        selectforeground=ACCENT, width=width or 20)

    def card(self, num, title):
        outer = tk.Frame(self.page, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        outer.pack(fill="x", pady=(0, 14))
        head = tk.Frame(outer, bg=PANEL)
        head.pack(fill="x", padx=22, pady=(18, 12))
        c = tk.Canvas(head, width=28, height=28, bg=PANEL, highlightthickness=0)
        c.create_oval(1, 1, 27, 27, fill=PANEL3, outline="#2e3138")
        c.create_text(14, 14, text=str(num), fill=ACCENT, font=(FONT, 9, "bold"))
        c.pack(side="left")
        tk.Label(head, text=title, bg=PANEL, fg=TEXT, font=(FONT, 12, "bold")).pack(side="left", padx=12)
        body = tk.Frame(outer, bg=PANEL)
        body.pack(fill="x", padx=22, pady=(0, 20))
        return body

    def row(self, parent, label):
        r = tk.Frame(parent, bg=PANEL)
        r.pack(fill="x", pady=5)
        tk.Label(r, text=label, bg=PANEL, fg=MUTED, font=(FONT, 10), width=9, anchor="w").pack(side="left")
        inner = tk.Frame(r, bg=PANEL)
        inner.pack(side="left", fill="x", expand=True)
        return inner

    # ----- main layout -----
    def build_ui(self):
        # scrollable page
        self.canvas = tk.Canvas(self.root, bg=BG, highlightthickness=0)
        sb = ttk.Scrollbar(self.root, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.page = tk.Frame(self.canvas, bg=BG)
        self.win = self.canvas.create_window((0, 0), window=self.page, anchor="n")
        self.page.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self.on_canvas_resize)
        self.root.bind_all("<MouseWheel>", self.on_wheel)
        self.root.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-1, "units"))
        self.root.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(1, "units"))

        # 1) Header
        head = tk.Frame(self.page, bg=BG)
        head.pack(fill="x", pady=(30, 22))
        tk.Label(head, text=APP_TITLE, font=(FONT, 30, "bold"), bg=BG, fg=ACCENT).pack()
        tk.Label(head, text="Paste a link, pick a format, grab the video.",
                 font=(FONT, 10), bg=BG, fg=MUTED).pack(pady=(4, 10))
        tk.Label(head, text="●  Runs locally on your PC · powered by yt-dlp", font=(FONT, 9),
                 bg=PANEL, fg=OK, padx=14, pady=5, highlightbackground=BORDER_STRONG,
                 highlightthickness=1).pack()

        # 2) Link
        body = self.card(1, "Video link")
        r = tk.Frame(body, bg=PANEL)
        r.pack(fill="x")
        self.url_var = tk.StringVar()
        self.url_entry = self.make_entry(r, self.url_var)
        self.url_entry.pack(side="left", fill="x", expand=True, ipady=8)
        Btn(r, "Paste", self.paste, padx=14).pack(side="left", padx=(8, 0))
        Btn(r, "Load", self.load_info, primary=True, padx=14).pack(side="left", padx=(8, 0))
        self.url_var.trace_add("write", self.on_url_change)
        self.url_entry.bind("<Return>", lambda e: self.load_info())
        self.link_msg = tk.Label(body, text="Works with YouTube, TikTok, Instagram and 1000+ other sites.",
                                 bg=PANEL, fg=MUTED, font=(FONT, 9), anchor="w", justify="left", wraplength=700)
        self.link_msg.pack(fill="x", pady=(10, 0))

        # 3) Preview
        body = self.card(2, "Preview")
        box = tk.Frame(body, bg=PANEL)
        box.pack(fill="x")
        tb = tk.Frame(box, bg="#0a0b0d", width=288, height=162, highlightbackground=BORDER, highlightthickness=1)
        tb.pack(side="left")
        tb.pack_propagate(False)
        self.thumb_lbl = tk.Label(tb, text="No video loaded yet", bg="#0a0b0d", fg=MUTED, font=(FONT, 9))
        self.thumb_lbl.place(relx=0.5, rely=0.5, anchor="center")
        info = tk.Frame(box, bg=PANEL)
        info.pack(side="left", fill="both", expand=True, padx=(18, 0))
        self.title_lbl = tk.Label(info, text="—", bg=PANEL, fg=TEXT, font=(FONT, 12, "bold"),
                                  anchor="w", justify="left", wraplength=360)
        self.title_lbl.pack(fill="x")
        self.meta_lbl = tk.Label(info, text="", bg=PANEL, fg=MUTED, font=(FONT, 10),
                                 anchor="w", justify="left", wraplength=360)
        self.meta_lbl.pack(fill="x", pady=(6, 0))

        # 4) Output
        body = self.card(3, "Output")
        self.fmt_chips = Chips(self.row(body, "Format"), command=self.set_format, padx=18, pady=7)
        self.fmt_chips.pack(anchor="w")
        self.fmt_chips.set_options([("MP4 · Video", "mp4"), ("MP3 · Audio", "mp3")], "mp4")
        self.quality = Chips(self.row(body, "Quality"))
        self.quality.pack(anchor="w")

        fr = self.row(body, "Save to")
        dl = Path.home() / "Downloads"
        self.folder_var = tk.StringVar(value=str(dl if dl.exists() else Path.home()))
        Btn(fr, "Browse", self.browse, padx=14, pady=6).pack(side="right", padx=(8, 0))
        self.make_entry(fr, self.folder_var).pack(side="left", fill="x", expand=True, ipady=5)

        nr = self.row(body, "Filename")
        self.name_var = tk.StringVar()
        self.make_entry(nr, self.name_var).pack(fill="x", ipady=5)
        tk.Label(body, text="Filename is optional. Leave it empty to use the video title.",
                 bg=PANEL, fg=MUTED, font=(FONT, 9), anchor="w").pack(fill="x", pady=(4, 0))

        # 5) Trim
        body = self.card(4, "Trim (optional)")
        top = tk.Frame(body, bg=PANEL)
        top.pack(fill="x")
        self.trim_switch = Switch(top, self.on_trim_toggle)
        self.trim_switch.pack(side="left")
        tk.Label(top, text="Download only a part of the video", bg=PANEL, fg=TEXT,
                 font=(FONT, 10)).pack(side="left", padx=12)

        self.trim_box = tk.Frame(body, bg=PANEL)
        self.trim_note = tk.Label(self.trim_box, text="Load a video first to choose a start and end.",
                                  bg=PANEL, fg=MUTED, font=(FONT, 9), anchor="w")
        self.trim_note.pack(fill="x", pady=(0, 6))
        self.slider = RangeSlider(self.trim_box, self.on_slider)
        self.slider.pack(fill="x")
        self.start_var = tk.StringVar(value="0:00")
        self.end_var = tk.StringVar(value="0:00")
        tr = tk.Frame(self.trim_box, bg=PANEL)
        tr.pack(fill="x", pady=(8, 0))
        tk.Label(tr, text="Start", bg=PANEL, fg=MUTED, font=(FONT, 10)).pack(side="left")
        se = self.make_entry(tr, self.start_var, width=9)
        se.pack(side="left", padx=(8, 0), ipady=5)
        ee = self.make_entry(tr, self.end_var, width=9)
        ee.pack(side="right", ipady=5)
        tk.Label(tr, text="End", bg=PANEL, fg=MUTED, font=(FONT, 10)).pack(side="right", padx=(0, 8))
        self.len_lbl = tk.Label(tr, text="Clip length: 0:00", bg=PANEL, fg=ACCENT, font=(FONT, 10, "bold"))
        self.len_lbl.pack(expand=True)
        for e in (se, ee):
            e.bind("<Return>", lambda ev: self.commit_trim())
            e.bind("<FocusOut>", lambda ev: self.commit_trim())

        # 6) Convert
        body = self.card(5, "Convert")
        self.go_btn = Btn(body, "Convert & Download", self.start_download, primary=True,
                          font=(FONT, 12, "bold"), pady=12)
        self.go_btn.pack(fill="x")
        self.progress = ttk.Progressbar(body, style="Purple.Horizontal.TProgressbar",
                                        mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(14, 0))
        self.status = tk.Label(body, text="Ready.", bg=PANEL, fg=MUTED, font=(FONT, 9),
                               anchor="w", justify="left", wraplength=700)
        self.status.pack(fill="x", pady=(8, 0))

        # 7) History
        body = self.card(6, "History")
        wrap = tk.Frame(body, bg=PANEL)
        wrap.pack(fill="x")
        cols = ("time", "title", "format", "part", "status")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", height=6, selectmode="browse")
        for c, label, w, stretch in (("time", "Time", 60, False), ("title", "Title", 260, True),
                                     ("format", "Format", 110, False), ("part", "Part", 110, False),
                                     ("status", "", 40, False)):
            self.tree.heading(c, text=label, anchor="w")
            self.tree.column(c, width=w, stretch=stretch, anchor="w")
        self.tree.tag_configure("fail", foreground=DANGER)
        tsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tsb.set)
        self.tree.pack(side="left", fill="x", expand=True)
        tsb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self.open_selected)
        br = tk.Frame(body, bg=PANEL)
        br.pack(fill="x", pady=(10, 0))
        Btn(br, "Open folder", lambda: open_path(self.folder_var.get()), padx=14, pady=6).pack(side="left")
        Btn(br, "Clear history", self.clear_history, padx=14, pady=6).pack(side="left", padx=(8, 0))
        tk.Label(br, text="Double-click an entry to open its folder. History is cleared when you close the app.",
                 bg=PANEL, fg=MUTED, font=(FONT, 8)).pack(side="right")

        tk.Frame(self.page, bg=BG, height=20).pack()

    # ----- scrolling / layout -----
    def on_canvas_resize(self, e):
        self.canvas.itemconfigure(self.win, width=min(e.width - 40, 820))
        self.canvas.coords(self.win, e.width // 2, 0)

    def on_wheel(self, e):
        if self.page.winfo_height() <= self.canvas.winfo_height():
            return
        step = -e.delta if sys.platform == "darwin" else -(e.delta // 120)
        self.canvas.yview_scroll(step, "units")

    # ----- status helpers -----
    def set_status(self, text, color=MUTED):
        self.status.config(text=text, fg=color)

    def set_link_msg(self, text, color=MUTED):
        self.link_msg.config(text=text, fg=color)

    # ----- link + preview -----
    def paste(self):
        try:
            text = self.root.clipboard_get().strip()
        except tk.TclError:
            return
        self.url_var.set(text)
        self.load_info()

    def on_url_change(self, *args):
        if self._debounce:
            self.root.after_cancel(self._debounce)
            self._debounce = None
        url = self.url_var.get().strip()
        if re.match(r"https?://\S+$", url) and url != self.loaded_url:
            self._debounce = self.root.after(700, self.load_info)

    def load_info(self):
        if self._debounce:
            self.root.after_cancel(self._debounce)
            self._debounce = None
        url = self.url_var.get().strip()
        if not url:
            self.set_link_msg("Please paste a link first.", DANGER)
            return
        self.loading_url = url
        self.set_link_msg("Checking link…", MUTED)
        threading.Thread(target=self.fetch, args=(url,), daemon=True).start()

    def fetch(self, url):
        try:
            opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
                    "skip_download": True, "logger": QuietLogger(), "socket_timeout": 15}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if info.get("_type") == "playlist":
                raise ValueError("This is a playlist link. Please paste the link of a single video.")
            img = None
            t = info.get("thumbnail") or ((info.get("thumbnails") or [{}])[-1]).get("url")
            if t:
                try:
                    req = urllib.request.Request(t, headers={"User-Agent": "Mozilla/5.0"})
                    img = urllib.request.urlopen(req, timeout=10).read()
                except Exception:
                    img = None
            meta = {"title": info.get("title") or "Untitled",
                    "uploader": info.get("uploader") or info.get("channel") or "",
                    "site": info.get("extractor_key") or "",
                    "duration": info.get("duration")}
            self.q.put(("info", url, meta, img))
        except Exception as e:
            self.q.put(("info_error", url, clean_error(e)))

    def on_info(self, url, meta, img):
        if url != self.loading_url:
            return
        self.loaded_url = url
        self.meta = meta
        self.title_lbl.config(text=meta["title"])
        dur = int(meta["duration"]) if meta.get("duration") else None
        bits = [meta["uploader"], meta["site"], fmt_time(dur) if dur else ""]
        self.meta_lbl.config(text="\n".join(b for b in bits if b))
        self.thumb_img = None
        if img:
            try:
                im = Image.open(io.BytesIO(img)).convert("RGB")
                im.thumbnail((286, 160))
                self.thumb_img = ImageTk.PhotoImage(im)
            except Exception:
                self.thumb_img = None
        if self.thumb_img:
            self.thumb_lbl.config(image=self.thumb_img, text="")
        else:
            self.thumb_lbl.config(image="", text="No preview available")
        self.duration = dur
        self.slider.enabled = bool(dur)
        if dur:
            self.slider.set(0, dur, dur)
            self.sync_trim_text(0, dur)
            self.trim_note.config(text="Drag the handles or type times (m:ss or h:mm:ss).")
        else:
            self.slider.set(0, 1, 1)
            self.trim_note.config(text="This video has no known length, so trimming isn't available.")
        self.set_link_msg("✓ Video found", OK)

    def on_info_error(self, url, msg):
        if url != self.loading_url:
            return
        self.loaded_url = None
        self.duration = None
        self.slider.enabled = False
        self.thumb_img = None
        self.thumb_lbl.config(image="", text="No video loaded yet")
        self.title_lbl.config(text="—")
        self.meta_lbl.config(text="")
        self.set_link_msg("✗ " + msg, DANGER)

    # ----- output settings -----
    def set_format(self, fmt):
        self.fmt = fmt
        if fmt == "mp4":
            self.quality.set_options([("Best", "best"), ("1080p", 1080), ("720p", 720),
                                      ("480p", 480), ("360p", 360)], "best")
        else:
            self.quality.set_options([("320 kbps", "320"), ("192 kbps", "192"),
                                      ("128 kbps", "128")], "320")

    def browse(self):
        d = filedialog.askdirectory(initialdir=self.folder_var.get() or str(Path.home()))
        if d:
            self.folder_var.set(d)

    # ----- trimming -----
    def on_trim_toggle(self, on):
        if on:
            self.trim_box.pack(fill="x", pady=(14, 0))
        else:
            self.trim_box.pack_forget()

    def sync_trim_text(self, s, e):
        self.start_var.set(fmt_time(s))
        self.end_var.set(fmt_time(e))
        self.len_lbl.config(text="Clip length: " + fmt_time(e - s))

    def on_slider(self, lo, hi):
        self.sync_trim_text(lo, hi)

    def commit_trim(self):
        if not self.duration:
            return
        s = parse_time(self.start_var.get())
        e = parse_time(self.end_var.get())
        if s is None:
            s = self.slider.lo
        if e is None:
            e = self.slider.hi
        s = max(0, min(s, self.duration - 1))
        e = max(s + 1, min(e, self.duration))
        self.slider.set(s, e)
        self.sync_trim_text(s, e)

    # ----- downloading -----
    def start_download(self):
        if self.busy:
            return
        url = self.url_var.get().strip()
        if not url or url != self.loaded_url or not self.meta:
            self.set_link_msg("Load the video first (press Load).", DANGER)
            return
        if not self.ffmpeg:
            self.set_status("ffmpeg not found. Run: pip install imageio-ffmpeg", DANGER)
            return
        folder = self.folder_var.get().strip()
        try:
            os.makedirs(folder, exist_ok=True)
        except Exception:
            self.set_status("Can't use that save folder.", DANGER)
            return
        clip = None
        if self.trim_switch.on:
            if not self.duration:
                self.set_status("Trimming isn't available for this video.", DANGER)
                return
            self.commit_trim()
            clip = (self.slider.lo, self.slider.hi)
        cfg = {"folder": folder, "fmt": self.fmt, "quality": self.quality.value, "clip": clip,
               "name": safe_name(self.name_var.get()), "title": self.meta["title"]}
        self.busy = True
        self.go_btn.set_disabled(True)
        self.on_progress(None, "Starting…" if not clip else "Downloading the selected part… this can take a moment.")
        threading.Thread(target=self.worker, args=(url, cfg), daemon=True).start()

    def worker(self, url, cfg):
        fmt, quality, clip = cfg["fmt"], cfg["quality"], cfg["clip"]
        label = ("MP4 · " + ("Best" if quality == "best" else f"{quality}p")) if fmt == "mp4" \
            else f"MP3 · {quality}k"
        entry = {"time": time.strftime("%H:%M"), "title": cfg["title"], "fmt": label,
                 "part": f"{fmt_time(clip[0])}–{fmt_time(clip[1])}" if clip else "Full video",
                 "path": None, "folder": cfg["folder"]}
        last = [0.0]

        def hook(d):
            if d["status"] == "downloading":
                now = time.time()
                if now - last[0] < 0.2:
                    return
                last[0] = now
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                done = d.get("downloaded_bytes", 0)
                speed = d.get("speed")
                sp = f" · {speed / 1e6:.1f} MB/s" if speed else ""
                if total:
                    self.q.put(("progress", done / total, f"Downloading… {done / total * 100:.0f}%{sp}"))
                else:
                    self.q.put(("progress", None, "Downloading…"))
            elif d["status"] == "finished":
                self.q.put(("progress", None, "Converting… almost done."))

        base = cfg["name"] or "%(title).150B"
        tag = f" (clip {tag_time(clip[0])}-{tag_time(clip[1])})" if clip else ""
        opts = {
            "outtmpl": os.path.join(cfg["folder"].replace("%", "%%"), base + tag + ".%(ext)s"),
            "noplaylist": True, "quiet": True, "no_warnings": True, "logger": QuietLogger(),
            "progress_hooks": [hook], "windowsfilenames": True,
            "concurrent_fragment_downloads": 4, "ffmpeg_location": self.ffmpeg,
        }
        if fmt == "mp3":
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                       "preferredquality": quality}]
        else:
            opts["format"] = "bv*+ba/b"
            res = "res" if quality == "best" else f"res:{quality}"
            opts["format_sort"] = [res, "ext:mp4:m4a"]
            opts["merge_output_format"] = "mp4"
            opts["postprocessors"] = [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}]
        if clip:
            opts["download_ranges"] = download_range_func(None, [(clip[0], clip[1])])
            opts["force_keyframes_at_cuts"] = True
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                path = None
                rd = info.get("requested_downloads")
                if rd:
                    path = rd[0].get("filepath")
                if not path:
                    path = ydl.prepare_filename(info)
            entry["path"] = path
            self.q.put(("done", entry))
        except Exception as e:
            self.q.put(("failed", entry, clean_error(e)))

    def on_progress(self, frac, text):
        if frac is None:
            if str(self.progress["mode"]) != "indeterminate":
                self.progress.configure(mode="indeterminate")
                self.progress.start(12)
        else:
            if str(self.progress["mode"]) != "determinate":
                self.progress.stop()
                self.progress.configure(mode="determinate")
            self.progress["value"] = frac * 100
        self.set_status(text)

    def finish_progress(self, value):
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self.progress["value"] = value
        self.busy = False
        self.go_btn.set_disabled(False)

    def on_done(self, entry):
        self.finish_progress(100)
        name = os.path.basename(entry["path"]) if entry["path"] else ""
        self.set_status(f"✓ Done! Saved as: {name}", OK)
        self.add_history(entry, True)

    def on_failed(self, entry, msg):
        self.finish_progress(0)
        self.set_status("✗ " + msg, DANGER)
        self.add_history(entry, False)

    # ----- history -----
    def add_history(self, entry, ok):
        iid = self.tree.insert("", 0, values=(entry["time"], entry["title"], entry["fmt"],
                                              entry["part"], "✓" if ok else "✗"),
                               tags=() if ok else ("fail",))
        self.paths[iid] = entry["path"] or entry["folder"]

    def open_selected(self, e=None):
        sel = self.tree.selection()
        if not sel:
            return
        p = self.paths.get(sel[0]) or self.folder_var.get()
        open_path(p if os.path.isdir(p) else os.path.dirname(p))

    def clear_history(self):
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.paths.clear()

    # ----- thread -> UI messages -----
    def poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                getattr(self, "on_" + msg[0])(*msg[1:])
        except queue.Empty:
            pass
        self.root.after(100, self.poll)


def main():
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharper text on Windows
        except Exception:
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
