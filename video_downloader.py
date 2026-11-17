"""
Video Downloader
Version: 1.1.0
Author: Mashiroto (with Claude)

Description:
Small desktop app (pop-up window) to downloads videos or audio from
YouTube, TikTok, Instagram and 1000+ other sites using yt-dlp.
Everything runs locally on your PC.

Setup (once):
    py -m pip install yt-dlp pillow imageio-ffmpeg

Start:
    py video_downloader.py

Zoom: Ctrl + mouse wheel, or Ctrl + Plus / Minus, Ctrl + 0 to reset.

Tip: sites change often. If a download suddenly stops working, update with:
    py -m pip install -U yt-dlp

Changelog:
- 1.0.0 (2026.11.16): Initial version (link, preview, MP4/MP3, trim, history).
- 1.1.0 (2026.11.17): GUI update, download folder pop up, more preview informations, zoom (Ctrl + mouse wheel).
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
import traceback 
import urllib.request
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import tkinter.font as tkfont

# ---------- Dependency check (friendly message instead of a crash) ----------
try:
    import yt_dlp
    from yt_dlp.utils import download_range_func
    from PIL import Image, ImageDraw, ImageOps, ImageTk
except ImportError:
    _r = tk.Tk()
    _r.withdraw()
    messagebox.showerror(
        "Missing packages",
        "Please install the required packages first.\n\n"
        "Open a terminal and run:\n\n"
        "py -m pip install yt-dlp pillow imageio-ffmpeg",
    )
    sys.exit(1)

APP_TITLE = "Video Downloader"
RESAMPLE = getattr(Image, "Resampling", Image).LANCZOS

# ---------- Colors ----------
BG = "#08090b"
PANEL = "#111216"
PANEL2 = "#1a1c21"
PANEL3 = "#212429"
BORDER = "#22242a"
BORDER_STRONG = "#3a3d45"
TEXT = "#f3f1ec"
MUTED = "#8c8e96"
ACCENT = "#4aa3ff"          # bright blue
ACCENT_HOVER = "#7bbcff"
ACCENT_SOFT = "#17324f"
ON_ACCENT = "#04172b"       # text on top of the accent color
DANGER = "#ef6461"
OK = "#6fcf97"

if sys.platform.startswith("win"):
    FONT = "Segoe UI"
elif sys.platform == "darwin":
    FONT = "Helvetica Neue"
else:
    FONT = "DejaVu Sans"

ZOOMS = [0.75, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0]
SCALE = 1.0  # current zoom factor, changed by Ctrl + mouse wheel

MP4_Q = [("Best", "best"), ("1080p", 1080), ("720p", 720), ("480p", 480), ("360p", 360)]
MP3_Q = [("320 kbps", "320"), ("192 kbps", "192"), ("128 kbps", "128")]


# ---------- Scaling helpers ----------
def sc(v):
    """Scale a pixel value with the current zoom."""
    return 0 if v == 0 else max(1, int(round(v * SCALE)))


_fonts = {}


def F(size, weight="normal"):
    key = (size, weight, SCALE)
    if key not in _fonts:
        _fonts[key] = tkfont.Font(family=FONT, size=max(7, int(round(size * SCALE))), weight=weight)
    return _fonts[key]


# ---------- Pure helpers (no GUI) ----------
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


def truncate(text, n):
    return text if len(text) <= n else text[: n - 1] + "…"


def clean_error(e):
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).strip().replace("ERROR: ", "")
    msg = msg.splitlines()[0] if msg else "Unknown error"
    low = msg.lower()
    if "login" in low or "cookies" in low or "sign in" in low:
        msg += "  (This site wants a logged-in account.)"
    return msg[:220]


def fmt_size(b):
    if b is None:
        return "—"
    if b >= 1024 ** 3:
        return f"≈ {b / 1024 ** 3:.1f} GB"
    if b >= 100 * 1024 ** 2:
        return f"≈ {b / 1024 ** 2:.0f} MB"
    if b >= 1024 ** 2:
        return f"≈ {b / 1024 ** 2:.1f} MB"
    return f"≈ {max(1, b // 1024)} KB"


def _size_of(f, dur):
    if f.get("size"):
        return f["size"]
    tbr = f.get("tbr") or 0
    if tbr and dur:
        return tbr * 125 * dur  # kbit/s -> bytes
    return None


def estimate_bytes(meta, fmt, quality, clip):
    """Rough size of the file you'd get with the current settings."""
    if not meta:
        return None
    dur = meta.get("duration")
    fl = meta.get("formats") or []
    if fmt == "mp3":
        if not dur:
            return None
        total = int(quality) * 125 * dur
    else:
        vids = [f for f in fl if f["v"] and f["h"]]
        if not vids:
            return None
        if quality != "best":
            capped = [f for f in vids if f["h"] <= quality]
            vids = capped or [min(vids, key=lambda f: f["h"])]
        best = max(vids, key=lambda f: (f["h"], f.get("tbr") or 0))
        total = _size_of(best, dur)
        if total is None:
            return None
        if not best["a"]:
            auds = [f for f in fl if f["a"] and not f["v"]]
            if auds:
                ba = max(auds, key=lambda f: (f.get("abr") or f.get("tbr") or 0))
                extra = _size_of(ba, dur)
                if extra:
                    total += extra
    if clip and dur:
        total = total * max(1, clip[1] - clip[0]) / dur
    return int(total)


def source_quality(meta):
    """Best quality the source offers: ('1080p · 60 fps', '130 kbps')."""
    fl = meta.get("formats") or []
    vids = [f for f in fl if f["v"] and f["h"]]
    if vids:
        b = max(vids, key=lambda f: (f["h"], f.get("fps") or 0))
        q = f"{b['h']}p" + (f" · {int(b['fps'])} fps" if b.get("fps") else "")
    else:
        q = "Audio only" if any(f["a"] for f in fl) else "—"
    auds = [f for f in fl if f["a"] and not f["v"]] or [f for f in fl if f["a"]]
    abr = max((f.get("abr") or 0 for f in auds), default=0)
    return q, (f"{int(abr)} kbps" if abr else "—")


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


def reveal(path, fallback_folder):
    """Open the folder of a saved file (and highlight the file on Windows/Mac)."""
    try:
        if path and os.path.exists(path):
            if sys.platform.startswith("win"):
                subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            else:
                open_path(os.path.dirname(path))
        else:
            open_path(fallback_folder)
    except Exception:
        open_path(fallback_folder)


class QuietLogger:
    """Keeps yt-dlp from printing into the console."""
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


# ---------- Smooth (anti-aliased) rounded drawing ----------
_cache = {}


def rr_image(w, h, r, fill, outline=None, bg=BG, border=1):
    """Rounded rectangle as an image. Drawn big and scaled down = smooth edges."""
    w, h = max(2, int(w)), max(2, int(h))
    key = (w, h, r, fill, outline, bg, border)
    img = _cache.get(key)
    if img is not None:
        return img
    if len(_cache) > 400:
        _cache.clear()  # widgets keep their own reference, so this is safe
    ss = 3 if w * h < 120000 else 2
    r = max(0, min(int(r), h // 2, w // 2))
    im = Image.new("RGB", (w * ss, h * ss), bg)
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, w * ss - 1, h * ss - 1], radius=r * ss, fill=outline or fill)
    if outline:
        b = border * ss
        d.rounded_rectangle([b, b, w * ss - 1 - b, h * ss - 1 - b],
                            radius=max(0, r * ss - b), fill=fill)
    img = ImageTk.PhotoImage(im.resize((w, h), RESAMPLE))
    _cache[key] = img
    return img


def icon_image(size, color, bgc):
    """The classic 'open in new window' icon (box with an arrow)."""
    key = ("icon", size, color, bgc)
    img = _cache.get(key)
    if img is not None:
        return img
    ss = 4
    S = size * ss
    im = Image.new("RGB", (S, S), bgc)
    d = ImageDraw.Draw(im)
    lw = max(2, int(S * 0.11))

    def P(x, y):
        return (x * S, y * S)

    box = [P(.46, .18), P(.18, .18), P(.18, .82), P(.82, .82), P(.82, .54)]
    arrow1 = [P(.46, .54), P(.86, .14)]
    arrow2 = [P(.62, .14), P(.86, .14), P(.86, .38)]
    for pts in (box, arrow1, arrow2):
        d.line(pts, fill=color, width=lw, joint="curve")
        for x, y in (pts[0], pts[-1]):  # round line ends
            d.ellipse([x - lw / 2, y - lw / 2, x + lw / 2, y + lw / 2], fill=color)
    img = ImageTk.PhotoImage(im.resize((size, size), RESAMPLE))
    _cache[key] = img
    return img


def rounded_photo(pil, w, h, r, bg):
    """A picture cropped to w x h with rounded corners."""
    ss = 2
    im = ImageOps.fit(pil, (w * ss, h * ss), RESAMPLE)
    mask = Image.new("L", (w * ss, h * ss), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w * ss - 1, h * ss - 1], radius=r * ss, fill=255)
    base = Image.new("RGB", (w * ss, h * ss), bg)
    base.paste(im, (0, 0), mask)
    return ImageTk.PhotoImage(base.resize((w, h), RESAMPLE))


# ---------- Custom widgets ----------
class RoundFrame(tk.Canvas):
    """A rounded 'card'. Put your widgets into .body"""
    def __init__(self, parent, fill=PANEL, outline=BORDER, radius=18, padx=20, pady=16):
        super().__init__(parent, bg=parent.cget("bg"), highlightthickness=0, bd=0, height=10)
        self.fill, self.outline, self.radius = fill, outline, sc(radius)
        self.px, self.py = sc(padx), sc(pady)
        self._img = None
        self.body = tk.Frame(self, bg=fill)
        # Top/bottom padding lives INSIDE the body (spacer frames). The body must start at y=0:
        # a canvas window that lies outside the visible canvas is never shown, so it never
        # gets a size and the card would stay a thin strip.
        tk.Frame(self.body, bg=fill, height=self.py).pack(side="top", fill="x")
        tk.Frame(self.body, bg=fill, height=self.py).pack(side="bottom", fill="x")
        self._win = self.create_window(self.px, 0, window=self.body, anchor="nw")
        self.bind("<Configure>", self._on_resize)
        self.body.bind("<Configure>", self._sync)

    def _sync(self, e=None):
        h = self.body.winfo_reqheight()
        if h > 1 and int(float(self.cget("height"))) != h:
            self.configure(height=h)

    def _on_resize(self, e):
        self.itemconfigure(self._win, width=max(1, e.width - 2 * self.px))
        self._sync()
        if e.width > 4 and e.height > 4:
            self._img = rr_image(e.width, e.height, self.radius, self.fill, self.outline, self.cget("bg"))
            self.delete("bg")
            self.create_image(0, 0, image=self._img, anchor="nw", tags="bg")
            self.tag_lower("bg")


class Btn(tk.Canvas):
    """Rounded button. Use icon=True + size=... for the square 'open' icon button."""
    def __init__(self, parent, text="", command=None, primary=False, padx=16, pady=8,
                 font=None, icon=False, size=None, radius=11):
        self.f = font or F(10, "bold")
        self.text, self.command, self.primary, self.icon = text, command, primary, icon
        self.r = sc(radius)
        self.active = self.disabled = self.hover = False
        if size:
            w = h = size
        else:
            w = self.f.measure(text) + 2 * sc(padx)
            h = self.f.metrics("linespace") + 2 * sc(pady)
        super().__init__(parent, width=w, height=h, bg=parent.cget("bg"),
                         highlightthickness=0, bd=0, cursor="hand2")
        self._pbg = parent.cget("bg")
        self._img = self._ico = None
        self.bind("<Configure>", lambda e: self._paint())
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<Button-1>", self._click)
        self._paint()

    def _colors(self):
        if self.disabled:
            return PANEL3, MUTED
        if self.icon:
            return (ACCENT_SOFT, ACCENT_HOVER) if self.hover else (PANEL3, ACCENT)
        if self.primary or self.active:
            return (ACCENT_HOVER if self.hover else ACCENT), ON_ACCENT
        return ("#2b2e35" if self.hover else PANEL3), TEXT

    def _paint(self):
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1:
            w = int(float(self.cget("width")))
        if h <= 1:
            h = int(float(self.cget("height")))
        fill, fg = self._colors()
        self.delete("all")
        self._img = rr_image(w, h, min(self.r, h // 2), fill, None, self._pbg)
        self.create_image(0, 0, image=self._img, anchor="nw")
        if self.icon:
            self._ico = icon_image(max(8, int(h * 0.5)), fg, fill)
            self.create_image(w // 2, h // 2, image=self._ico)
        else:
            self.create_text(w // 2, h // 2, text=self.text, fill=fg, font=self.f)

    def _set_hover(self, on):
        self.hover = on
        self._paint()

    def _click(self, e):
        if not self.disabled and self.command:
            self.command()

    def set_active(self, on):
        self.active = on
        self._paint()

    def set_disabled(self, on):
        self.disabled = on
        self.configure(cursor="arrow" if on else "hand2")
        self._paint()


class Chips(tk.Frame):
    """A row of buttons where exactly one is selected (toggle / presets)."""
    def __init__(self, parent, command=None, padx=14, pady=6):
        super().__init__(parent, bg=parent.cget("bg"))
        self.command = command
        self.padx, self.pady = padx, pady
        self.btns = []
        self.value = None

    def set_options(self, options, default):
        for b in self.btns:
            b.destroy()
        self.btns = []
        for label, val in options:
            b = Btn(self, label, lambda v=val: self.select(v), padx=self.padx, pady=self.pady, radius=14)
            b.val = val
            b.pack(side="left", padx=(0, sc(6)))
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
    def __init__(self, parent, command=None, on=False):
        self.w, self.h = sc(46), sc(26)
        super().__init__(parent, width=self.w, height=self.h, bg=parent.cget("bg"),
                         highlightthickness=0, bd=0, cursor="hand2")
        self._pbg = parent.cget("bg")
        self.on = on
        self.command = command
        self._refs = []
        self.bind("<Button-1>", self.toggle)
        self.draw()

    def draw(self):
        self.delete("all")
        col = ACCENT if self.on else PANEL3
        pad = sc(3)
        kn = self.h - 2 * pad
        pill = rr_image(self.w, self.h, self.h // 2, col, None if self.on else "#33363d", self._pbg)
        knob = rr_image(kn, kn, kn // 2, ON_ACCENT if self.on else MUTED, None, col)
        self._refs = [pill, knob]
        self.create_image(0, 0, image=pill, anchor="nw")
        self.create_image(self.w - kn - pad if self.on else pad, pad, image=knob, anchor="nw")

    def toggle(self, e=None):
        self.on = not self.on
        self.draw()
        if self.command:
            self.command(self.on)


class Pill(tk.Canvas):
    """Small rounded label."""
    def __init__(self, parent, text, fg, font, padx=14, pady=5):
        w = font.measure(text) + 2 * sc(padx)
        h = font.metrics("linespace") + 2 * sc(pady)
        super().__init__(parent, width=w, height=h, bg=parent.cget("bg"), highlightthickness=0, bd=0)
        self._img = rr_image(w, h, h // 2, PANEL, BORDER_STRONG, parent.cget("bg"))
        self.create_image(0, 0, image=self._img, anchor="nw")
        self.create_text(w // 2, h // 2, text=text, fill=fg, font=font)


class RoundEntry(tk.Canvas):
    """Rounded text input. The real Entry is in .entry"""
    def __init__(self, parent, var, width=None):
        font = F(11)
        h = font.metrics("linespace") + sc(22)
        kw = {}
        if width:
            kw["width"] = font.measure("0") * width + sc(32)
        super().__init__(parent, height=h, bg=parent.cget("bg"), highlightthickness=0, bd=0, **kw)
        self._pbg = parent.cget("bg")
        self.focused = False
        self._img = None
        self.entry = tk.Entry(self, textvariable=var, bg=PANEL2, fg=TEXT, insertbackground=TEXT,
                              relief="flat", bd=0, highlightthickness=0, font=font,
                              selectbackground=ACCENT_SOFT, selectforeground=ACCENT)
        self._win = self.create_window(sc(14), h // 2, window=self.entry, anchor="w")
        self.entry.bind("<FocusIn>", lambda e: self._focus(True))
        self.entry.bind("<FocusOut>", lambda e: self._focus(False))
        self.bind("<Configure>", self._on_resize)
        self.bind("<Button-1>", lambda e: self.entry.focus_set())

    def _focus(self, on):
        self.focused = on
        self._paint()

    def _on_resize(self, e):
        self.itemconfigure(self._win, width=max(10, e.width - sc(28)))
        self._paint()

    def _paint(self):
        w, h = self.winfo_width(), int(float(self.cget("height")))
        if w < 8:
            return
        self._img = rr_image(w, h, sc(12), PANEL2, ACCENT if self.focused else BORDER,
                             self._pbg, border=2 if self.focused else 1)
        self.delete("bg")
        self.create_image(0, 0, image=self._img, anchor="nw", tags="bg")
        self.tag_lower("bg")


class RangeSlider(tk.Canvas):
    """One bar with two handles: start and end of the part you want to keep."""
    def __init__(self, parent, command):
        super().__init__(parent, height=sc(34), bg=parent.cget("bg"), highlightthickness=0, bd=0,
                         cursor="hand2")
        self._pbg = parent.cget("bg")
        self.pad = sc(14)
        self.command = command
        self.max, self.lo, self.hi = 1, 0, 1
        self.enabled = False
        self.drag = None
        self._refs = []
        self.bind("<Configure>", lambda e: self.draw())
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._move)

    def _x(self, v):
        span = max(1, self.winfo_width() - 2 * self.pad)
        return self.pad + span * (v / self.max)

    def _v(self, x):
        span = max(1, self.winfo_width() - 2 * self.pad)
        return round(min(max((x - self.pad) / span, 0), 1) * self.max)

    def set(self, lo, hi, mx=None):
        if mx is not None:
            self.max = max(1, mx)
        self.lo, self.hi = lo, hi
        self.draw()

    def draw(self):
        W = self.winfo_width()
        if W < 4 * self.pad:
            return
        self.delete("all")
        H = int(float(self.cget("height")))
        y = H // 2
        th = sc(6)
        col = ACCENT if self.enabled else "#4a4d55"
        x1, x2 = int(self._x(self.lo)), int(self._x(self.hi))
        track = rr_image(W - 2 * self.pad, th, th // 2, PANEL3, None, self._pbg)
        rng = rr_image(max(th, x2 - x1), th, th // 2, col, None, PANEL3)
        d = sc(20)
        handle = rr_image(d, d, d // 2, TEXT, col, self._pbg, border=sc(3))
        self._refs = [track, rng, handle]
        self.create_image(self.pad, y - th // 2, image=track, anchor="nw")
        self.create_image(x1, y - th // 2, image=rng, anchor="nw")
        for x in (x1, x2):
            self.create_image(x - d // 2, y - d // 2, image=handle, anchor="nw")

    def _press(self, e):
        if not self.enabled:
            return
        d1, d2 = abs(e.x - self._x(self.lo)), abs(e.x - self._x(self.hi))
        if d1 < d2:
            self.drag = "lo"
        elif d1 > d2:
            self.drag = "hi"
        else:
            self.drag = "lo" if e.x < self._x(self.lo) else "hi"
        self._move(e)

    def _move(self, e):
        if not self.enabled or not self.drag:
            return
        v = self._v(e.x)
        if self.drag == "lo":
            self.lo = min(v, self.hi - 1)
        else:
            self.hi = max(v, self.lo + 1)
        self.draw()
        self.command(self.lo, self.hi)


class ProgressBar(tk.Canvas):
    """Rounded progress bar (with a moving bar when the progress is unknown)."""
    def __init__(self, parent):
        super().__init__(parent, height=sc(8), bg=parent.cget("bg"), highlightthickness=0, bd=0)
        self._pbg = parent.cget("bg")
        self.mode, self.frac = "det", 0.0
        self._pos, self._dir, self._job = 0, 1, None
        self._refs = []
        self.bind("<Configure>", lambda e: self.draw())

    def draw(self):
        w, h = self.winfo_width(), int(float(self.cget("height")))
        if w < 8:
            return
        self.delete("all")
        track = rr_image(w, h, h // 2, PANEL3, None, self._pbg)
        self._refs = [track]
        self.create_image(0, 0, image=track, anchor="nw")
        if self.mode == "det" and self.frac > 0:
            bar = rr_image(max(h, int(w * self.frac)), h, h // 2, ACCENT, None, PANEL3)
            self._refs.append(bar)
            self.create_image(0, 0, image=bar, anchor="nw")
        elif self.mode == "ind":
            seg = int(w * 0.3)
            bar = rr_image(seg, h, h // 2, ACCENT, None, PANEL3)
            self._refs.append(bar)
            self.create_image(self._pos, 0, image=bar, anchor="nw")

    def _cancel(self):
        if self._job:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def set_fraction(self, f):
        self._cancel()
        self.mode, self.frac = "det", max(0.0, min(1.0, f))
        self.draw()

    def start(self):
        if self.mode == "ind" and self._job:
            return
        self._cancel()
        self.mode = "ind"
        self._pos, self._dir = 0, 1
        self._tick()

    def _tick(self):
        w = self.winfo_width()
        seg = int(w * 0.3)
        step = max(4, w // 60)
        self._pos += self._dir * step
        if self._pos + seg >= w:
            self._pos, self._dir = max(0, w - seg), -1
        elif self._pos <= 0:
            self._pos, self._dir = 0, 1
        self.draw()
        self._job = self.after(30, self._tick)

    def destroy(self):
        self._cancel()
        super().destroy()


class ThumbBox(tk.Canvas):
    """Rounded thumbnail area."""
    def __init__(self, parent):
        self.w, self.h = sc(288), sc(162)
        super().__init__(parent, width=self.w, height=self.h, bg=parent.cget("bg"),
                         highlightthickness=0, bd=0)
        self._pbg = parent.cget("bg")
        self._img = None

    def show(self, pil, text="No video loaded yet"):
        self.delete("all")
        if pil is None:
            self._img = rr_image(self.w, self.h, sc(14), "#0a0b0d", BORDER, self._pbg)
            self.create_image(0, 0, image=self._img, anchor="nw")
            self.create_text(self.w // 2, self.h // 2, text=text, fill=MUTED, font=F(9))
        else:
            self._img = rounded_photo(pil, self.w, self.h, sc(14), self._pbg)
            self.create_image(0, 0, image=self._img, anchor="nw")


def badge(parent, num):
    d = sc(28)
    bg = parent.cget("bg")
    c = tk.Canvas(parent, width=d, height=d, bg=bg, highlightthickness=0, bd=0)
    c._img = rr_image(d, d, d // 2, PANEL3, "#2e3138", bg)
    c.create_image(0, 0, image=c._img, anchor="nw")
    c.create_text(d // 2, d // 2, text=str(num), fill=ACCENT, font=F(9, "bold"))
    return c


def autowrap(label, container, margin=0):
    container.bind("<Configure>",
                   lambda e: label.configure(wraplength=max(60, e.width - margin)), add="+")


# ---------- The app ----------
class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.zoom = 1.0
        # values that must survive a zoom (the window is rebuilt on zoom)
        dl = Path.home() / "Downloads"
        self.url_var = tk.StringVar()
        self.folder_var = tk.StringVar(value=str(dl if dl.exists() else Path.home()))
        self.name_var = tk.StringVar()
        self.start_var = tk.StringVar(value="0:00")
        self.end_var = tk.StringVar(value="0:00")
        self.fmt, self.qval = "mp4", "best"
        self.trim_on = False
        self.t_lo = self.t_hi = 0
        self.meta = None
        self.thumb_pil = None
        self.duration = None
        self.loading_url = self.loaded_url = None
        self.link_text = "Works with YouTube, TikTok, Instagram and 1000+ other sites."
        self.link_color = MUTED
        self.status_text, self.status_color = "Ready.", MUTED
        self.prog_mode, self.prog_frac = "det", 0.0
        self.history = []
        self.busy = False
        self._debounce = self._zoom_job = self._toast_job = self.toast = None
        self.ffmpeg = find_ffmpeg()
        if not self.ffmpeg:
            self.status_text, self.status_color = "ffmpeg not found. Run: py -m pip install imageio-ffmpeg", DANGER

        root.title(APP_TITLE)
        root.geometry("860x960")
        root.minsize(640, 600)
        root.configure(bg=BG)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Vertical.TScrollbar", background=PANEL3, troughcolor=BG, bordercolor=BG,
                        arrowcolor=MUTED, lightcolor=PANEL3, darkcolor=PANEL3)

        self.url_var.trace_add("write", self.on_url_change)
        root.bind_all("<MouseWheel>", self.on_wheel)
        root.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-1, "units"))
        root.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(1, "units"))
        root.bind_all("<Control-MouseWheel>", lambda e: self.zoom_step(1 if e.delta > 0 else -1))
        root.bind_all("<Control-Button-4>", lambda e: self.zoom_step(1))
        root.bind_all("<Control-Button-5>", lambda e: self.zoom_step(-1))
        for key in ("<Control-plus>", "<Control-equal>", "<Control-KP_Add>"):
            root.bind_all(key, lambda e: self.zoom_step(1))
        for key in ("<Control-minus>", "<Control-KP_Subtract>"):
            root.bind_all(key, lambda e: self.zoom_step(-1))
        root.bind_all("<Control-0>", lambda e: self.set_zoom(1.0))

        self.build_ui()
        root.after(100, self.poll)

    # ----- zoom -----
    def zoom_step(self, direction):
        i = min(range(len(ZOOMS)), key=lambda k: abs(ZOOMS[k] - self.zoom))
        self.set_zoom(ZOOMS[max(0, min(len(ZOOMS) - 1, i + direction))])
        return "break"

    def set_zoom(self, z):
        global SCALE
        if abs(z - self.zoom) > 1e-6:
            self.zoom = SCALE = z
            if self._zoom_job:
                self.root.after_cancel(self._zoom_job)
            self._zoom_job = self.root.after(150, self.rebuild)  # wait until you stop scrolling
        self.flash_toast()

    def flash_toast(self):
        if self.toast:
            self.toast.destroy()
        if self._toast_job:
            self.root.after_cancel(self._toast_job)
        self.toast = Pill(self.root, f"{int(round(self.zoom * 100))}%", TEXT, F(11, "bold"), padx=16, pady=7)
        self.toast.place(relx=1, rely=1, x=-26, y=-26, anchor="se")
        self._toast_job = self.root.after(1200, self._hide_toast)

    def _hide_toast(self):
        if self.toast:
            self.toast.destroy()
            self.toast = None

    def rebuild(self):
        self._zoom_job = None
        try:
            y = self.canvas.yview()[0]
        except Exception:
            y = 0
        self.canvas.destroy()
        self.sb.destroy()
        _cache.clear()
        self.build_ui()
        self.root.update_idletasks()
        self.canvas.yview_moveto(y)
        if self.toast:
            tk.Misc.tkraise(self.toast)

    # ----- small UI builders -----
    def card(self, num, title):
        rf = RoundFrame(self.page, fill=PANEL, outline=BORDER, radius=22, padx=24, pady=20)
        rf.pack(fill="x", pady=(0, sc(14)))
        head = tk.Frame(rf.body, bg=PANEL)
        head.pack(fill="x", pady=(0, sc(14)))
        badge(head, num).pack(side="left")
        tk.Label(head, text=title, bg=PANEL, fg=TEXT, font=F(12, "bold")).pack(side="left", padx=sc(12))
        body = tk.Frame(rf.body, bg=PANEL)
        body.pack(fill="x")
        return body

    def row(self, parent, label):
        r = tk.Frame(parent, bg=PANEL)
        r.pack(fill="x", pady=sc(5))
        tk.Label(r, text=label, bg=PANEL, fg=MUTED, font=F(10), width=9, anchor="w").pack(side="left")
        inner = tk.Frame(r, bg=PANEL)
        inner.pack(side="left", fill="x", expand=True)
        return inner

    # ----- main layout -----
    def build_ui(self):
        self.canvas = tk.Canvas(self.root, bg=BG, highlightthickness=0)
        self.sb = ttk.Scrollbar(self.root, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.sb.set)
        self.sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.page = tk.Frame(self.canvas, bg=BG)
        self.win = self.canvas.create_window((0, 0), window=self.page, anchor="n")
        self.page.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self.on_canvas_resize)

        # Header
        head = tk.Frame(self.page, bg=BG)
        head.pack(fill="x", pady=(sc(30), sc(22)))
        tk.Label(head, text=APP_TITLE, font=F(30, "bold"), bg=BG, fg=ACCENT).pack()
        tk.Label(head, text="Paste a link, pick a format, grab the video.",
                 font=F(10), bg=BG, fg=MUTED).pack(pady=(sc(4), sc(10)))
        Pill(head, "●  Runs locally on your PC · powered by yt-dlp", OK, F(9)).pack()

        # 1) Link
        body = self.card(1, "Video link")
        r = tk.Frame(body, bg=PANEL)
        r.pack(fill="x")
        self.url_entry = RoundEntry(r, self.url_var)
        self.url_entry.pack(side="left", fill="x", expand=True)
        self.url_entry.entry.bind("<Return>", lambda e: self.load_info())
        Btn(r, "Paste", self.paste, padx=16).pack(side="left", fill="y", padx=(sc(8), 0))
        Btn(r, "Load", self.load_info, primary=True, padx=16).pack(side="left", fill="y", padx=(sc(8), 0))
        self.link_msg = tk.Label(body, text=self.link_text, fg=self.link_color, bg=PANEL, font=F(9),
                                 anchor="w", justify="left")
        self.link_msg.pack(fill="x", pady=(sc(10), 0))
        autowrap(self.link_msg, body)

        # 2) Preview
        body = self.card(2, "Preview")
        box = tk.Frame(body, bg=PANEL)
        box.pack(fill="x")
        self.thumb = ThumbBox(box)
        self.thumb.pack(side="left", anchor="n")
        info = tk.Frame(box, bg=PANEL)
        info.pack(side="left", fill="both", expand=True, padx=(sc(20), 0))
        self.title_lbl = tk.Label(info, text="—", bg=PANEL, fg=TEXT, font=F(12, "bold"),
                                  anchor="w", justify="left")
        self.title_lbl.pack(fill="x")
        autowrap(self.title_lbl, info)
        meta = tk.Frame(info, bg=PANEL)
        meta.pack(anchor="w", pady=(sc(10), 0))
        left = tk.Frame(meta, bg=PANEL)           # Account / Platform / Length
        left.pack(side="left", anchor="n")
        self.l_user = tk.Label(left, text="", bg=PANEL, fg=MUTED, font=F(10), anchor="w")
        self.l_site = tk.Label(left, text="", bg=PANEL, fg=MUTED, font=F(10), anchor="w")
        self.l_len = tk.Label(left, text="", bg=PANEL, fg=MUTED, font=F(10), anchor="w")
        for lb in (self.l_user, self.l_site, self.l_len):
            lb.pack(fill="x", anchor="w")
        tk.Frame(meta, bg=BORDER_STRONG, width=1).pack(side="left", fill="y", padx=sc(18))
        right = tk.Frame(meta, bg=PANEL)          # Quality / Audio / Size (right next to it)
        right.pack(side="left", anchor="n")
        self.v_quality, self.v_audio, self.v_size = (
            tk.Label(right, text="—", bg=PANEL, fg=TEXT, font=F(10), anchor="w"),
            tk.Label(right, text="—", bg=PANEL, fg=TEXT, font=F(10), anchor="w"),
            tk.Label(right, text="—", bg=PANEL, fg=ACCENT, font=F(10, "bold"), anchor="w"))
        for i, (cap, lb) in enumerate((("Quality", self.v_quality), ("Audio", self.v_audio),
                                       ("Est. size", self.v_size))):
            tk.Label(right, text=cap, bg=PANEL, fg=MUTED, font=F(9), anchor="w").grid(
                row=i, column=0, sticky="w", padx=(0, sc(12)))
            lb.grid(row=i, column=1, sticky="w")

        # 3) Output
        body = self.card(3, "Output")
        self.fmt_chips = Chips(self.row(body, "Format"), command=self.on_format, padx=18, pady=7)
        self.fmt_chips.pack(anchor="w")
        self.fmt_chips.set_options([("MP4 · Video", "mp4"), ("MP3 · Audio", "mp3")], self.fmt)
        self.quality = Chips(self.row(body, "Quality"), command=self.on_quality)
        self.quality.pack(anchor="w")
        self.apply_quality_options()

        fr = self.row(body, "Save to")
        Btn(fr, "Browse", self.browse, padx=16).pack(side="right", fill="y", padx=(sc(8), 0))
        RoundEntry(fr, self.folder_var).pack(side="left", fill="x", expand=True)
        nr = self.row(body, "Filename")
        RoundEntry(nr, self.name_var).pack(fill="x")
        tk.Label(body, text="Filename is optional. Leave it empty to use the video title.",
                 bg=PANEL, fg=MUTED, font=F(9), anchor="w").pack(fill="x", pady=(sc(4), 0))

        # 4) Trim
        body = self.card(4, "Trim (optional)")
        top = tk.Frame(body, bg=PANEL)
        top.pack(fill="x")
        self.trim_switch = Switch(top, self.on_trim_toggle, on=self.trim_on)
        self.trim_switch.pack(side="left")
        tk.Label(top, text="Download only a part of the video", bg=PANEL, fg=TEXT,
                 font=F(10)).pack(side="left", padx=sc(12))
        self.trim_box = tk.Frame(body, bg=PANEL)
        self.trim_note = tk.Label(self.trim_box, text=self.trim_note_text(), bg=PANEL, fg=MUTED,
                                  font=F(9), anchor="w")
        self.trim_note.pack(fill="x", pady=(0, sc(6)))
        self.slider = RangeSlider(self.trim_box, self.on_slider)
        self.slider.pack(fill="x")
        tr = tk.Frame(self.trim_box, bg=PANEL)
        tr.pack(fill="x", pady=(sc(8), 0))
        tk.Label(tr, text="Start", bg=PANEL, fg=MUTED, font=F(10)).pack(side="left", padx=(0, sc(8)))
        self.start_entry = RoundEntry(tr, self.start_var, width=8)
        self.start_entry.pack(side="left")
        self.end_entry = RoundEntry(tr, self.end_var, width=8)
        self.end_entry.pack(side="right")
        tk.Label(tr, text="End", bg=PANEL, fg=MUTED, font=F(10)).pack(side="right", padx=(0, sc(8)))
        self.len_lbl = tk.Label(tr, text="Clip length: " + fmt_time(max(0, self.t_hi - self.t_lo)),
                                bg=PANEL, fg=ACCENT, font=F(10, "bold"))
        self.len_lbl.pack(expand=True)
        for ent in (self.start_entry.entry, self.end_entry.entry):
            ent.bind("<Return>", lambda e: self.commit_trim(), add="+")
            ent.bind("<FocusOut>", lambda e: self.commit_trim(), add="+")
        self.slider.enabled = bool(self.duration)
        self.slider.set(self.t_lo, self.t_hi or 1, self.duration or 1)
        if self.trim_on:
            self.trim_box.pack(fill="x", pady=(sc(14), 0))

        # 5) Convert
        body = self.card(5, "Convert")
        self.go_btn = Btn(body, "Convert & Download", self.start_download, primary=True,
                          font=F(12, "bold"), pady=12, radius=14)
        self.go_btn.pack(fill="x")
        self.go_btn.set_disabled(self.busy)
        self.progress = ProgressBar(body)
        self.progress.pack(fill="x", pady=(sc(14), 0))
        self.status = tk.Label(body, text=self.status_text, bg=PANEL, fg=self.status_color,
                               font=F(9), anchor="w", justify="left")
        self.status.pack(fill="x", pady=(sc(8), 0))
        autowrap(self.status, body)

        # 6) History
        body = self.card(6, "History")
        self.hist_holder = tk.Frame(body, bg=PANEL)
        self.hist_holder.pack(fill="x")
        br = tk.Frame(body, bg=PANEL)
        br.pack(fill="x", pady=(sc(6), 0))
        Btn(br, "Open folder", lambda: open_path(self.folder_var.get()), padx=14, pady=6).pack(side="left")
        Btn(br, "Clear history", self.clear_history, padx=14, pady=6).pack(side="left", padx=(sc(8), 0))
        tk.Label(body, text="Click the icon on the right of a download to open its folder. "
                            "History is cleared when you close the app.",
                 bg=PANEL, fg=MUTED, font=F(8), anchor="w", justify="left").pack(fill="x", pady=(sc(10), 0))
        tk.Frame(self.page, bg=BG, height=sc(20)).pack()

        # fill everything with the saved state
        self.show_meta()
        self.render_history()
        if self.prog_mode == "ind":
            self.progress.start()
        else:
            self.progress.set_fraction(self.prog_frac)

    # ----- scrolling / layout -----
    def on_canvas_resize(self, e):
        self.canvas.itemconfigure(self.win, width=min(e.width - sc(40), sc(820)))
        self.canvas.coords(self.win, e.width // 2, 0)

    def on_wheel(self, e):
        if self.page.winfo_height() <= self.canvas.winfo_height():
            return
        step = -e.delta if sys.platform == "darwin" else -(e.delta // 120)
        self.canvas.yview_scroll(step, "units")

    # ----- status helpers -----
    def set_status(self, text, color=MUTED):
        self.status_text, self.status_color = text, color
        self.status.config(text=text, fg=color)

    def set_link_msg(self, text, color=MUTED):
        self.link_text, self.link_color = text, color
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
            formats = []
            for f in info.get("formats") or []:
                formats.append({
                    "h": f.get("height"), "fps": f.get("fps"),
                    "v": (f.get("vcodec") or "none") != "none",
                    "a": (f.get("acodec") or "none") != "none",
                    "size": f.get("filesize") or f.get("filesize_approx"),
                    "tbr": f.get("tbr"), "abr": f.get("abr"),
                })
            meta = {"title": info.get("title") or "Untitled",
                    "uploader": info.get("uploader") or info.get("channel") or "",
                    "site": info.get("extractor_key") or "",
                    "duration": info.get("duration"),
                    "formats": formats}
            self.q.put(("info", url, meta, img))
        except Exception as e:
            self.q.put(("info_error", url, clean_error(e)))

    def on_info(self, url, meta, img):
        if url != self.loading_url:
            return
        self.loaded_url = url
        self.meta = meta
        self.thumb_pil = None
        if img:
            try:
                self.thumb_pil = Image.open(io.BytesIO(img)).convert("RGB")
            except Exception:
                self.thumb_pil = None
        dur = int(meta["duration"]) if meta.get("duration") else None
        self.duration = dur
        self.slider.enabled = bool(dur)
        self.slider.set(0, dur or 1, dur or 1)
        self.sync_trim_text(0, dur or 0)
        self.trim_note.config(text=self.trim_note_text())
        self.show_meta()
        self.set_link_msg("✓ Video found", OK)

    def on_info_error(self, url, msg):
        if url != self.loading_url:
            return
        self.loaded_url = None
        self.meta = None
        self.duration = None
        self.thumb_pil = None
        self.slider.enabled = False
        self.slider.set(0, 1, 1)
        self.t_lo = self.t_hi = 0
        self.trim_note.config(text=self.trim_note_text())
        self.show_meta()
        self.set_link_msg("✗ " + msg, DANGER)

    def show_meta(self):
        m = self.meta
        if not m:
            self.thumb.show(None)
            self.title_lbl.config(text="—")
            for lb in (self.l_user, self.l_site, self.l_len):
                lb.config(text="")
            self.v_quality.config(text="—")
            self.v_audio.config(text="—")
            self.v_size.config(text="—")
            return
        self.thumb.show(self.thumb_pil, "No preview available")
        self.title_lbl.config(text=m["title"])
        self.l_user.config(text=m["uploader"] or "—")
        self.l_site.config(text=m["site"] or "—")
        self.l_len.config(text=fmt_time(m["duration"]) if m.get("duration") else "Unknown length")
        q, a = source_quality(m)
        self.v_quality.config(text=q)
        self.v_audio.config(text=a)
        self.update_size()

    def update_size(self):
        if not hasattr(self, "v_size"):
            return
        clip = (self.t_lo, self.t_hi) if self.trim_on and self.duration else None
        b = estimate_bytes(self.meta, self.fmt, self.qval, clip)
        self.v_size.config(text=fmt_size(b))

    # ----- output settings -----
    def apply_quality_options(self):
        opts = MP4_Q if self.fmt == "mp4" else MP3_Q
        if self.qval not in [v for _, v in opts]:
            self.qval = opts[0][1]
        self.quality.set_options(opts, self.qval)

    def on_format(self, val):
        self.fmt = val
        self.qval = "best" if val == "mp4" else "320"
        self.apply_quality_options()
        self.update_size()

    def on_quality(self, val):
        self.qval = val
        self.update_size()

    def browse(self):
        d = filedialog.askdirectory(initialdir=self.folder_var.get() or str(Path.home()))
        if d:
            self.folder_var.set(d)

    # ----- trimming -----
    def trim_note_text(self):
        if not self.meta:
            return "Load a video first to choose a start and end."
        if not self.duration:
            return "This video has no known length, so trimming isn't available."
        return "Drag the handles or type times (m:ss or h:mm:ss)."

    def on_trim_toggle(self, on):
        self.trim_on = on
        if on:
            self.trim_box.pack(fill="x", pady=(sc(14), 0))
        else:
            self.trim_box.pack_forget()
        self.update_size()

    def sync_trim_text(self, s, e):
        self.t_lo, self.t_hi = s, e
        self.start_var.set(fmt_time(s))
        self.end_var.set(fmt_time(e))
        self.len_lbl.config(text="Clip length: " + fmt_time(max(0, e - s)))
        self.update_size()

    def on_slider(self, lo, hi):
        self.sync_trim_text(lo, hi)

    def commit_trim(self):
        if not self.duration:
            return
        s = parse_time(self.start_var.get())
        e = parse_time(self.end_var.get())
        if s is None:
            s = self.t_lo
        if e is None:
            e = self.t_hi
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
            self.set_status("ffmpeg not found. Run: py -m pip install imageio-ffmpeg", DANGER)
            return
        folder = self.folder_var.get().strip()
        try:
            os.makedirs(folder, exist_ok=True)
        except Exception:
            self.set_status("Can't use that save folder.", DANGER)
            return
        clip = None
        if self.trim_on:
            if not self.duration:
                self.set_status("Trimming isn't available for this video.", DANGER)
                return
            self.commit_trim()
            clip = (self.t_lo, self.t_hi)
        cfg = {"folder": folder, "fmt": self.fmt, "quality": self.qval, "clip": clip,
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
                 "path": None, "folder": cfg["folder"], "ok": True, "err": ""}
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
            entry["ok"] = False
            entry["err"] = clean_error(e)
            self.q.put(("failed", entry, entry["err"]))

    def on_progress(self, frac, text):
        if frac is None:
            self.prog_mode = "ind"
            self.progress.start()
        else:
            self.prog_mode, self.prog_frac = "det", frac
            self.progress.set_fraction(frac)
        self.set_status(text)

    def finish_progress(self, value):
        self.prog_mode, self.prog_frac = "det", value
        self.progress.set_fraction(value)
        self.busy = False
        self.go_btn.set_disabled(False)

    def on_done(self, entry):
        self.finish_progress(1.0)
        name = os.path.basename(entry["path"]) if entry["path"] else ""
        self.set_status(f"✓ Done! Saved as: {name}", OK)
        self.history.append(entry)
        self.render_history()

    def on_failed(self, entry, msg):
        self.finish_progress(0.0)
        self.set_status("✗ " + msg, DANGER)
        self.history.append(entry)
        self.render_history()

    # ----- history -----
    def render_history(self):
        for w in self.hist_holder.winfo_children():
            w.destroy()
        if not self.history:
            tk.Label(self.hist_holder, text="Nothing downloaded yet.", bg=PANEL, fg=MUTED,
                     font=F(10)).pack(anchor="w", pady=(0, sc(4)))
            return
        for e in reversed(self.history):
            row = RoundFrame(self.hist_holder, fill=PANEL2, outline=BORDER, radius=14, padx=14, pady=10)
            row.pack(fill="x", pady=(0, sc(8)))
            b = tk.Frame(row.body, bg=PANEL2)
            b.pack(fill="x")
            b.grid_columnconfigure(1, weight=1)
            tk.Label(b, text=e["time"], bg=PANEL2, fg=MUTED, font=F(9)).grid(
                row=0, column=0, rowspan=2, sticky="w", padx=(0, sc(14)))
            tk.Label(b, text=truncate(e["title"], 70), bg=PANEL2, fg=TEXT, font=F(10, "bold"),
                     anchor="w").grid(row=0, column=1, sticky="we")
            sub = f'{e["fmt"]} · {e["part"]}' if e["ok"] else e["err"]
            tk.Label(b, text=truncate(sub, 90), bg=PANEL2, fg=MUTED if e["ok"] else DANGER,
                     font=F(9), anchor="w").grid(row=1, column=1, sticky="we")
            if e["ok"]:
                Btn(b, command=lambda p=e["path"], f=e["folder"]: reveal(p, f), icon=True,
                    size=sc(36), radius=11).grid(row=0, column=2, rowspan=2, sticky="e",
                                                 padx=(sc(12), 0))
            else:
                tk.Label(b, text="✗", bg=PANEL2, fg=DANGER, font=F(13, "bold")).grid(
                    row=0, column=2, rowspan=2, sticky="e", padx=(sc(12), 0))

    def clear_history(self):
        self.history.clear()
        self.render_history()

    # ----- thread -> UI messages -----
    def poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                try:
                    getattr(self, "on_" + msg[0])(*msg[1:])
                except Exception:
                    traceback.print_exc()  # one error must not stop the app
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
