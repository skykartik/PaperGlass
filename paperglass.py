# -*- coding: utf-8 -*-
"""
PaperGlass 2 - an e-ink / paper display filter for Windows.

  * Tone (grayscale, paper white, ink black, optional muted colour): Windows' own screen
    colour effect (Magnification API). It is applied by the compositor, so it covers every
    window, the taskbar, the Start menu and Quick Settings, on every monitor.
  * Film grain, edge shading, desk lamp, refresh flash: a click-through overlay on each
    monitor. Windows keeps the Start menu and Quick Settings above ordinary windows, so the
    overlay only reaches them when the app runs with "UIAccess" (see setup_full_coverage.ps1).
  * Quit = everything returns to normal. If the process is killed, Windows drops the effect too.

Run:  pip install PySide6   then   pythonw paperglass.py
"""

import atexit
import copy
import ctypes
import io
import json
import math
import os
import platform
import random
import signal
import struct
import sys
import wave
from ctypes import wintypes
from datetime import datetime

VERSION = "2.0"
APP_NAME = "PaperGlass"
SUPPORT_EMAIL = ""   # e.g. "help@example.com"  (adds an "Email support" button when set)
SUPPORT_URL = ""     # e.g. "https://example.com/paperglass"  (adds a "Website" button when set)

IS_WIN = sys.platform == "win32"

# --------------------------------------------------------------------------- Windows plumbing

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
HWND_TOPMOST = ctypes.c_void_p(-1).value
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x0001, 0x0002, 0x0004, 0x0008, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0x0000

if IS_WIN:
    import winreg
    import winsound

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    WinEventProc = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
                                      wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)

    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [wintypes.HWND, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
    user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.PostThreadMessageW.argtypes = [wintypes.DWORD, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.SetWinEventHook.restype = wintypes.HANDLE
    user32.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, WinEventProc,
                                       wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
    user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                             wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]

NEUTRAL_APPS = {"explorer.exe", "startmenuexperiencehost.exe", "shellexperiencehost.exe", "searchhost.exe",
                "searchapp.exe", "lockapp.exe", "textinputhost.exe", "taskmgr.exe"}
HIDE_APPS = NEUTRAL_APPS | {"applicationframehost.exe", "systemsettings.exe", "widgets.exe"}


def proc_info(hwnd):
    """(pid, lower-case exe name) for a window."""
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return 0, ""
    h = kernel32.OpenProcess(0x1000, False, pid.value)      # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return pid.value, ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = wintypes.DWORD(1024)
        ok = kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n))
        return pid.value, (os.path.basename(buf.value).lower() if ok else "")
    finally:
        kernel32.CloseHandle(h)


def list_windowed_apps():
    names, own = set(), os.getpid()

    def cb(hwnd, _lparam):
        try:
            if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
                pid, name = proc_info(hwnd)
                if name and pid != own and name not in HIDE_APPS:
                    names.add(name)
        except Exception:
            pass
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return sorted(names)


def has_uiaccess():
    """True when this process runs with UIAccess (can draw above the Start menu)."""
    if not IS_WIN:
        return False
    try:
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            return False
        val, ret = wintypes.DWORD(0), wintypes.DWORD(0)
        ok = advapi32.GetTokenInformation(token, 26, ctypes.byref(val), ctypes.sizeof(val), ctypes.byref(ret))
        kernel32.CloseHandle(token)
        return bool(ok and val.value)
    except Exception:
        return False


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launch_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --tray'
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw
    return f'"{exe}" "{os.path.abspath(sys.argv[0])}" --tray'


def is_autostart():
    if not IS_WIN:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(on):
    if not IS_WIN:
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, launch_command())
            else:
                try:
                    winreg.DeleteValue(k, APP_NAME)
                except FileNotFoundError:
                    pass
    except OSError:
        pass


# --------------------------------------------------------------------------- Qt

from PySide6.QtCore import (Qt, QTimer, QThread, Signal, QObject, QPointF, QRectF, QUrl,
                            QVariantAnimation, QEasingCurve)
from PySide6.QtGui import (QAction, QBrush, QColor, QDesktopServices, QGuiApplication, QIcon, QImage,
                           QKeySequence, QPainter, QPainterPath, QPen, QPixmap, QRadialGradient)
from PySide6.QtWidgets import (QAbstractButton, QApplication, QButtonGroup, QComboBox, QFrame, QGridLayout,
                               QHBoxLayout, QKeySequenceEdit, QLabel, QLineEdit, QListWidget, QMenu,
                               QMessageBox, QPushButton, QSlider, QStackedWidget, QSystemTrayIcon,
                               QVBoxLayout, QWidget)

# --------------------------------------------------------------------------- settings

DEFAULT_KEYS = dict(eink="Ctrl+Alt+E", lamp="Ctrl+Alt+L", night="Ctrl+Alt+N",
                    refresh="Ctrl+Alt+R", panel="Ctrl+Alt+P")

DEFAULTS = dict(
    enabled=True, strength=100, contrast=50, brightness=0, warmth=55, ink=70, color=0, texture=50, shade=30,
    lamp_enabled=False, lamp_pos="left", lamp_power=60, lamp_warmth=60, lamp_spread=60, lamp_dim=35,
    night_auto=False, night_start="21:00", night_end="06:30", night_days="1111111",
    night_preset="night", night_dim=6, night_lamp=False,
    exc_apps=[], exc_mode="skip", exc_lamp=True,
    hotkeys_on=True, keys=dict(DEFAULT_KEYS),
    autostart=False, start_hidden=False, close_quits=False,
    sounds=True, sound_volume=60, sound_style="chime",
    flash="soft",
    custom_presets={},
)

PRESET_KEYS = ("warmth", "contrast", "ink", "texture", "brightness", "color", "shade")
PRESETS = {
    "paper":     dict(warmth=55, contrast=50, ink=70, texture=50, brightness=0, color=0, shade=30),
    "carta":     dict(warmth=12, contrast=62, ink=85, texture=30, brightness=0, color=0, shade=18),
    "newsprint": dict(warmth=38, contrast=36, ink=42, texture=85, brightness=-3, color=0, shade=40),
    "kaleido":   dict(warmth=28, contrast=48, ink=60, texture=45, brightness=2, color=35, shade=25),
    "night":     dict(warmth=90, contrast=42, ink=55, texture=35, brightness=-14, color=0, shade=45),
}
PRESET_LABELS = dict(paper="Paper", carta="Carta", newsprint="Newsprint", kaleido="Kaleido", night="Night")
PRESET_TIPS = dict(
    paper="Warm, matte, easy on the eyes", carta="Cool and crisp, like a reader",
    newsprint="Grainy and soft", kaleido="Muted colour, like colour e-ink", night="Dim and warm for late hours")


def settings_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, APP_NAME)
    os.makedirs(folder, exist_ok=True)
    return folder


def settings_path():
    return os.path.join(settings_dir(), "settings.json")


class Settings:
    def __init__(self):
        self.__dict__.update(copy.deepcopy(DEFAULTS))

    def load(self):
        try:
            with open(settings_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        for k, v in data.items():
            if k not in DEFAULTS or not isinstance(v, type(DEFAULTS[k])):
                continue
            if k == "keys":
                merged = dict(DEFAULT_KEYS)
                merged.update({a: t for a, t in v.items() if a in DEFAULT_KEYS and isinstance(t, str)})
                v = merged
            setattr(self, k, v)

    def save(self):
        try:
            with open(settings_path(), "w", encoding="utf-8") as f:
                json.dump({k: getattr(self, k) for k in DEFAULTS}, f, indent=2)
        except Exception:
            pass


# --------------------------------------------------------------------------- colour maths

LUMA = (0.2126, 0.7152, 0.0722)
PAPER_COOL = (0.925, 0.940, 0.950)
PAPER_WARM = (0.965, 0.905, 0.760)
IDENTITY = [[1.0 if r == c else 0.0 for c in range(5)] for r in range(5)]


def build_matrix(look, k_eink, k_lamp):
    """5x5 colour matrix in the row-vector convention of the Magnification API.

    gray = contrast * luma + offset;  colour = ink + (paper - ink) * gray
    `color` mixes some of the original hue back in (colour e-ink). The result is blended with
    the identity by effect strength * fade progress. While the lamp is on, a small R-B
    pass-through lets its warm light keep some colour on an otherwise grayscale screen.
    """
    st = max(0.0, min(1.0, look.strength / 100.0)) * k_eink
    cf = 0.6 + 0.9 * look.contrast / 100.0
    off = 0.5 * (1.0 - cf) + look.brightness / 100.0
    t = look.warmth / 100.0
    paper = [PAPER_COOL[i] + (PAPER_WARM[i] - PAPER_COOL[i]) * t for i in range(3)]
    kk = 0.02 + (100 - look.ink) / 100.0 * 0.16
    ink = [kk * p for p in paper]
    ca = max(0.0, min(1.0, look.color / 100.0))

    m = [[0.0] * 5 for _ in range(5)]
    for i in range(3):
        for j in range(3):
            gray_part = (paper[j] - ink[j]) * cf * LUMA[i]
            col_part = (paper[j] - ink[j]) * cf if i == j else 0.0
            m[i][j] = (1.0 - ca) * gray_part + ca * col_part
    for j in range(3):
        m[4][j] = ink[j] + (paper[j] - ink[j]) * off
    m[3][3] = 1.0
    m[4][4] = 1.0

    out = [[(1.0 - st) * IDENTITY[r][c] + st * m[r][c] for c in range(5)] for r in range(5)]
    w = 0.55 * (look.lamp_power / 100.0) * k_lamp * st
    if w > 0.0:
        out[0][0] += w
        out[2][0] -= w
        out[0][2] -= w
        out[2][2] += w
    return out


def schedule_active(start, end, days, now):
    """Is `now` inside the night window? `days` is a 7-char string, Monday first."""
    try:
        sh, sm = (int(x) for x in start.split(":"))
        eh, em = (int(x) for x in end.split(":"))
    except Exception:
        return False
    a, b, t = sh * 60 + sm, eh * 60 + em, now.hour * 60 + now.minute
    days = (days + "1111111")[:7]
    today, yesterday = now.weekday(), (now.weekday() - 1) % 7
    if a == b:
        return False
    if a < b:
        return days[today] == "1" and a <= t < b
    return (t >= a and days[today] == "1") or (t < b and days[yesterday] == "1")


VK_NAMES = {
    "space": 0x20, "tab": 0x09, "return": 0x0D, "enter": 0x0D, "esc": 0x1B, "escape": 0x1B,
    "backspace": 0x08, "del": 0x2E, "delete": 0x2E, "ins": 0x2D, "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pgup": 0x21, "pageup": 0x21, "pgdown": 0x22, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
}


def parse_hotkey(text):
    """'Ctrl+Alt+E' -> (modifiers, virtual-key) or None if it isn't a usable global hotkey."""
    if not text:
        return None
    mods, key = 0, None
    for part in (p.strip() for p in text.split("+")):
        low = part.lower()
        if not low:
            continue
        if low == "ctrl":
            mods |= MOD_CONTROL
        elif low == "alt":
            mods |= MOD_ALT
        elif low == "shift":
            mods |= MOD_SHIFT
        elif low in ("meta", "win"):
            mods |= MOD_WIN
        else:
            key = low
    if key is None or not (mods & (MOD_CONTROL | MOD_ALT | MOD_WIN)):
        return None
    if len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x6F + int(key[1:])
    elif key in VK_NAMES:
        vk = VK_NAMES[key]
    else:
        return None
    return mods | MOD_NOREPEAT, vk


# --------------------------------------------------------------------------- sounds

SR = 22050
CHIMES = {   # (frequency, start, length)
    "on": [(659.25, 0.00, 0.40), (880.00, 0.09, 0.50)],
    "off": [(880.00, 0.00, 0.30), (659.25, 0.09, 0.45)],
    "lamp_on": [(523.25, 0.00, 0.40), (783.99, 0.08, 0.55)],
    "lamp_off": [(783.99, 0.00, 0.30), (523.25, 0.08, 0.50)],
    "night_on": [(659.25, 0.00, 0.40), (523.25, 0.12, 0.50), (392.00, 0.24, 0.65)],
    "night_off": [(392.00, 0.00, 0.40), (523.25, 0.12, 0.50), (659.25, 0.24, 0.65)],
    "refresh": [(1046.5, 0.00, 0.25), (1318.5, 0.06, 0.35)],
    "save": [(880.00, 0.00, 0.20), (1174.7, 0.07, 0.30)],
    "tick": [(1500.0, 0.00, 0.06)],
}
PAPER_SPECS = {   # (length, brightness)
    "on": (0.30, 0.50), "off": (0.22, 0.25), "lamp_on": (0.18, 0.70), "lamp_off": (0.15, 0.35),
    "night_on": (0.35, 0.20), "night_off": (0.30, 0.60), "refresh": (0.45, 0.60),
    "save": (0.12, 0.70), "tick": (0.03, 0.80),
}


def _bell(freq, dur, vol):
    out = []
    for i in range(int(SR * dur)):
        t = i / SR
        env = min(1.0, t / 0.004) * math.exp(-7.0 * t)
        v = (math.sin(2 * math.pi * freq * t) + 0.30 * math.sin(4 * math.pi * freq * t)
             + 0.10 * math.sin(6 * math.pi * freq * t))
        out.append(v * env * vol * 0.45)
    return out


def _rustle(dur, vol, bright):
    n = int(SR * dur)
    rnd, y, gain, a, out = random.Random(3), 0.0, 1.0, 0.15 + 0.7 * bright, []
    for i in range(n):
        if i % 40 == 0:
            gain = 0.55 + 0.45 * rnd.random()
        y += a * (rnd.uniform(-1, 1) - y)
        out.append(y * (math.sin(math.pi * i / n) ** 1.4) * gain * vol * 1.6)
    return out


def synth(kind, style, volume):
    vol = max(0.0, min(1.0, volume / 100.0)) * 0.55
    if style == "paper":
        dur, bright = PAPER_SPECS[kind]
        buf = _rustle(dur, vol, bright)
    else:
        notes = CHIMES[kind]
        total = max(st + d for _f, st, d in notes)
        buf = [0.0] * int(SR * total)
        for f, st, d in notes:
            i0 = int(st * SR)
            for i, v in enumerate(_bell(f, d, vol)):
                if i0 + i < len(buf):
                    buf[i0 + i] += v
    bio = io.BytesIO()
    w = wave.open(bio, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes(b"".join(struct.pack("<h", int(max(-1.0, min(1.0, x)) * 32767)) for x in buf))
    w.close()
    return bio.getvalue()


class Sounds:
    """Plays the synthesised cues. winsound cannot play in-memory audio asynchronously, so each cue is
    written once to a small WAV file in the settings folder and played from there."""

    def __init__(self, engine):
        self.engine = engine
        self.last_error = ""

    def _file(self, kind, style, vol):
        folder = os.path.join(settings_dir(), "sounds")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{style}_{kind}_{vol}.wav")
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(synth(kind, style, vol))
        return path

    def play(self, kind):
        s = self.engine.s
        if not (IS_WIN and s.sounds) or s.sound_volume <= 0:
            return
        try:
            vol = max(10, min(100, int(round(s.sound_volume / 10.0)) * 10))
            path = self._file(kind, s.sound_style, vol)
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
            self.last_error = ""
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- paper textures

def _noise_pixmap(n, white, max_alpha=255, bell=False):
    """n x n noise as a black or white layer whose alpha is the noise.
    bell=True gives a bell-shaped distribution (mostly mid values, a few strong specks), like film grain."""
    raw = os.urandom(n * n)
    if bell:
        raw = bytes((x + y) >> 1 for x, y in zip(raw, os.urandom(n * n)))
    if max_alpha < 255:
        raw = bytes(v * max_alpha // 255 for v in raw)
    buf = bytearray(n * n * 4)
    if white:
        buf[0::4] = raw
        buf[1::4] = raw
        buf[2::4] = raw
    buf[3::4] = raw
    img = QImage(bytes(buf), n, n, n * 4, QImage.Format.Format_ARGB32_Premultiplied).copy()
    return QPixmap.fromImage(img)


def _soft_tile(n, factor, white, max_alpha=255, bell=True):
    """Seamless soft noise: tile n x n noise 3x3, scale it up smoothly and keep the middle tile."""
    small = _noise_pixmap(n, white, max_alpha, bell)
    big = QPixmap(n * 3, n * 3)
    big.fill(Qt.GlobalColor.transparent)
    p = QPainter(big)
    for i in range(3):
        for j in range(3):
            p.drawPixmap(i * n, j * n, small)
    p.end()
    scaled = big.scaled(n * 3 * factor, n * 3 * factor, Qt.AspectRatioMode.IgnoreAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)
    return scaled.copy(n * factor, n * factor, n * factor, n * factor)


def make_textures():
    """Film-grain layers: fine dark and light grain, a softer coarse grain and a very faint cloudy unevenness."""
    return {
        "grain_dark": _noise_pixmap(512, False, bell=True),
        "grain_light": _noise_pixmap(512, True, bell=True),
        "coarse": _soft_tile(128, 2, False),
        "mottle": _soft_tile(12, 48, False, 44, bell=False),
    }


def flash_layer(t, mode):
    """(r, g, b, alpha 0..1) for the refresh flash at progress t, or None."""
    if t <= 0.0 or t >= 1.0:
        return None
    if mode == "full":
        if t < 0.14:
            return 0, 0, 0, t / 0.14
        if t < 0.30:
            return 0, 0, 0, 1.0
        if t < 0.50:
            return 255, 255, 255, 1.0
        return 255, 255, 255, 1.0 - (t - 0.50) / 0.50
    return 250, 250, 248, 0.62 * math.sin(math.pi * t)


# --------------------------------------------------------------------------- screen colour effect

class MAGCOLOREFFECT(ctypes.Structure):
    _fields_ = [("transform", (ctypes.c_float * 5) * 5)]


class Magnifier:
    """Thin wrapper around Magnification.dll's full-screen colour effect."""

    def __init__(self):
        self.ok = False
        self._up = False
        if not IS_WIN:
            return
        try:
            self.dll = ctypes.WinDLL("Magnification.dll", use_last_error=True)
            self.dll.MagInitialize.restype = wintypes.BOOL
            self.dll.MagUninitialize.restype = wintypes.BOOL
            self.dll.MagSetFullscreenColorEffect.restype = wintypes.BOOL
            self.dll.MagSetFullscreenColorEffect.argtypes = [ctypes.POINTER(MAGCOLOREFFECT)]
            self.ok = bool(self.dll.MagInitialize())
            self._up = self.ok
        except Exception:
            self.ok = False

    def apply(self, matrix):
        if not self.ok:
            return
        eff = MAGCOLOREFFECT()
        for r in range(5):
            for c in range(5):
                eff.transform[r][c] = float(matrix[r][c])
        self.dll.MagSetFullscreenColorEffect(ctypes.byref(eff))

    def shutdown(self):
        if self._up:
            try:
                self.apply(IDENTITY)
                self.dll.MagUninitialize()
            except Exception:
                pass
            self._up = False
            self.ok = False


# --------------------------------------------------------------------------- overlay

LAMP_POS = {
    "left": (-0.04, 0.45), "top_left": (0.02, -0.05), "top": (0.50, -0.10),
    "top_right": (0.98, -0.05), "right": (1.04, 0.45),
}


class Overlay(QWidget):
    """Click-through, always-on-top, per-pixel-alpha window that covers one monitor."""

    def __init__(self, engine, screen):
        super().__init__(None)
        self.engine = engine
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.NoDropShadowWindowHint)
        for a in (Qt.WidgetAttribute.WA_TranslucentBackground, Qt.WidgetAttribute.WA_ShowWithoutActivating,
                  Qt.WidgetAttribute.WA_TransparentForMouseEvents, Qt.WidgetAttribute.WA_NoSystemBackground):
            self.setAttribute(a, True)
        self.setScreen(screen)
        self.setGeometry(screen.geometry())
        screen.geometryChanged.connect(self.setGeometry)

    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(0, self._native_setup)

    def _native_setup(self):
        if not IS_WIN:
            return
        hwnd = int(self.winId())
        style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        style |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style)
        self.keep_on_top()

    def keep_on_top(self):
        if IS_WIN and self.isVisible():
            user32.SetWindowPos(int(self.winId()), HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def paintEvent(self, _):
        e = self.engine
        s = e.eff
        w, h = self.width(), self.height()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        fade = (s.strength / 100.0) * e.k_eink

        # film grain: faint cloudy unevenness, soft coarse grain, then fine dark and light grain
        tex = (s.texture / 100.0) * fade
        if tex > 0.004:
            p.setOpacity(min(1.0, 0.8 * tex))
            p.drawTiledPixmap(0, 0, w, h, e.tex["mottle"])
            p.setOpacity(min(1.0, 0.16 * tex))
            p.drawTiledPixmap(0, 0, w, h, e.tex["coarse"])
            p.setOpacity(min(1.0, 0.34 * tex))
            p.drawTiledPixmap(0, 0, w, h, e.tex["grain_dark"])
            p.setOpacity(min(1.0, 0.22 * tex))
            p.drawTiledPixmap(0, 0, w, h, e.tex["grain_light"])
            p.setOpacity(1.0)

        # edge shading, like a front-lit panel that is a touch darker toward the corners
        sh = (s.shade / 100.0) * fade
        if sh > 0.004:
            g = QRadialGradient(QPointF(w / 2.0, h / 2.0), 0.5 * math.hypot(w, h))
            g.setColorAt(0.55, QColor(0, 0, 0, 0))
            g.setColorAt(1.0, QColor(22, 15, 6, int(255 * 0.34 * sh)))
            p.fillRect(self.rect(), QBrush(g))

        # desk lamp
        kl = e.k_lamp
        if kl > 0.005:
            fx, fy = LAMP_POS.get(s.lamp_pos, LAMP_POS["left"])
            centre = QPointF(fx * w, fy * h)
            radius = (0.35 + 0.9 * s.lamp_spread / 100.0) * max(w, h)
            power = s.lamp_power / 100.0 * kl
            dim = s.lamp_dim / 100.0 * 0.5 * kl
            if dim > 0.003:
                g1 = QRadialGradient(centre, radius * 1.35)
                g1.setColorAt(0.0, QColor(0, 0, 0, 0))
                g1.setColorAt(0.5, QColor(0, 0, 0, int(255 * dim * 0.35)))
                g1.setColorAt(1.0, QColor(0, 0, 0, int(255 * dim)))
                p.fillRect(self.rect(), QBrush(g1))
            t = s.lamp_warmth / 100.0
            r, gg, b = 255, int(222 - 72 * t), int(182 - 122 * t)
            a = 0.45 * power
            g2 = QRadialGradient(centre, radius)
            for stop, f in ((0.0, 1.0), (0.25, 0.72), (0.55, 0.32), (0.8, 0.10), (1.0, 0.0)):
                g2.setColorAt(stop, QColor(r, gg, b, int(255 * a * f)))
            p.fillRect(self.rect(), QBrush(g2))

        # refresh flash
        fl = flash_layer(e.flash_t, e.flash_mode)
        if fl:
            p.fillRect(self.rect(), QColor(fl[0], fl[1], fl[2], int(255 * fl[3])))
        p.end()


# --------------------------------------------------------------------------- hotkeys

class HotkeyThread(QThread):
    fired = Signal(str)
    registered = Signal(list)          # actions that could not be registered

    def __init__(self, bindings):
        super().__init__()
        self.bindings = bindings        # [(action, modifiers, vk)]
        self._tid = 0
        self._ready = __import__("threading").Event()

    def run(self):
        if not IS_WIN:
            return
        self._tid = kernel32.GetCurrentThreadId()
        ok, failed, ids = [], [], {}
        for i, (action, mods, vk) in enumerate(self.bindings, start=1):
            if user32.RegisterHotKey(None, i, mods, vk):
                ok.append(i)
                ids[i] = action
            else:
                failed.append(action)
        self._ready.set()
        self.registered.emit(failed)
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and int(msg.wParam) in ids:
                self.fired.emit(ids[int(msg.wParam)])
        for i in ok:
            user32.UnregisterHotKey(None, i)

    def stop(self):
        if IS_WIN and self.isRunning():
            self._ready.wait(1.5)
            if self._tid:
                user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            self.wait(2000)


# --------------------------------------------------------------------------- engine

class Engine(QObject):
    changed = Signal()            # settings / modes changed (refresh the UI)
    status = Signal()             # foreground app / pause status changed
    panel_requested = Signal()

    def __init__(self):
        super().__init__()
        self.s = Settings()
        self.s.load()
        self.eff = self.s
        self.mag = Magnifier()
        self.uiaccess = has_uiaccess()
        self.k_eink = 0.0
        self.k_lamp = 0.0
        self.flash_t = 0.0
        self.flash_mode = "soft"
        self.excepted = False
        self._last_app = ""
        self.night_override = None
        self.sched_active = False
        self.hk = None
        self.hk_failed = []
        self._down = False
        self._targets = {}
        self.overlays = []
        self._hook = None
        self._hook_proc = None
        self.tex = make_textures()
        self.sounds = Sounds(self)

        self._anims = {}
        for key in ("eink", "lamp"):
            a = QVariantAnimation(self)
            a.setDuration(500)
            a.setEasingCurve(QEasingCurve.Type.InOutCubic)
            a.valueChanged.connect(lambda v, k=key: self._on_k(k, float(v)))
            self._anims[key] = a

        self._flash_anim = QVariantAnimation(self)
        self._flash_anim.setStartValue(0.0)
        self._flash_anim.setEndValue(1.0)
        self._flash_anim.valueChanged.connect(self._on_flash)
        self._flash_anim.finished.connect(lambda: self._on_flash(0.0))

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self.s.save)

        self._keep = QTimer(self)
        self._keep.setInterval(1500)
        self._keep.timeout.connect(self._keep_on_top)

        self._reapply = QTimer(self)
        self._reapply.setInterval(4000)
        self._reapply.timeout.connect(self._apply_mag)

        self._sched = QTimer(self)
        self._sched.setInterval(15000)
        self._sched.timeout.connect(self._tick_schedule)

        self._poll = QTimer(self)
        self._poll.setInterval(1000)
        self._poll.timeout.connect(self._on_foreground)

        app = QGuiApplication.instance()
        app.screenAdded.connect(self.rebuild_overlays)
        app.screenRemoved.connect(self.rebuild_overlays)

    # ---- lifecycle
    def start(self, intro=True):
        if IS_WIN:
            self.s.autostart = is_autostart()      # the Run key is the source of truth (the installer may have set it)
        self.sched_active = self._schedule_now()
        self.rebuild_overlays()
        self.reload_hotkeys()
        if IS_WIN:
            try:
                self._hook_proc = WinEventProc(self._win_event)
                self._hook = user32.SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None,
                                                    self._hook_proc, 0, 0, WINEVENT_OUTOFCONTEXT)
            except Exception:
                self._hook = None
        for t in (self._keep, self._reapply, self._sched, self._poll):
            t.start()
        self._on_foreground()
        self._recompute()
        if intro and self.s.enabled:
            self.flash()

    def shutdown(self):
        if self._down:
            return
        self._down = True
        try:
            if self._hook:
                user32.UnhookWinEvent(self._hook)
            if self.hk:
                self.hk.stop()
            for o in self.overlays:
                o.hide()
            self.mag.shutdown()                      # colours back to normal
            self.s.save()
        except Exception:
            pass

    # ---- effective look, targets, animation
    def night_active(self):
        return self.sched_active if self.night_override is None else self.night_override

    def preset_values(self, key):
        if key.startswith("custom:"):
            vals = dict(PRESETS["paper"])
            vals.update(self.s.custom_presets.get(key[7:], PRESETS["night"]))
            return vals
        return dict(PRESETS.get(key, PRESETS["paper"]))

    def effective(self):
        e = copy.copy(self.s)
        if self.night_active():
            for k, v in self.preset_values(self.s.night_preset).items():
                setattr(e, k, v)
            e.brightness = max(-30, e.brightness - self.s.night_dim)
            if self.s.night_lamp:
                e.lamp_enabled = True
        return e

    def _recompute(self):
        if self._down:
            return
        self.eff = self.effective()
        ex = self.excepted
        eink = 1.0 if (self.s.enabled and not ex) else 0.0
        lamp = 1.0 if (self.eff.lamp_enabled and not (ex and self.s.exc_lamp)) else 0.0
        self._go("eink", eink)
        self._go("lamp", lamp)
        self.refresh()

    def _go(self, key, target):
        if self._targets.get(key) == target:
            return
        self._targets[key] = target
        a = self._anims[key]
        cur = self.k_eink if key == "eink" else self.k_lamp
        a.stop()
        if abs(cur - target) < 1e-3:
            self._on_k(key, target)
            return
        a.setStartValue(cur)
        a.setEndValue(target)
        a.start()

    def _on_k(self, key, v):
        if key == "eink":
            self.k_eink = v
        else:
            self.k_lamp = v
        self.refresh()

    def _on_flash(self, v):
        self.flash_t = float(v)
        self.refresh()

    def flash(self, force=False):
        mode = self.s.flash
        if mode == "off" and not force:
            return
        self.flash_mode = "soft" if mode == "off" else mode
        self._flash_anim.stop()
        self._flash_anim.setDuration(650 if self.flash_mode == "full" else 480)
        self._flash_anim.start()

    def _apply_mag(self):
        if not self._down:
            self.mag.apply(build_matrix(self.eff, self.k_eink, self.k_lamp))

    def refresh(self):
        if self._down:
            return
        self.eff = self.effective()
        self._apply_mag()
        vis = self.k_eink > 0.002 or self.k_lamp > 0.002 or self.flash_t > 0.0
        for o in self.overlays:
            if o.isVisible() != vis:
                o.setVisible(vis)
            o.update()

    def _keep_on_top(self):
        for o in self.overlays:
            o.keep_on_top()

    def rebuild_overlays(self, *_):
        for o in self.overlays:
            o.hide()
            o.deleteLater()
        self.overlays = [Overlay(self, sc) for sc in QGuiApplication.screens()]
        self.refresh()

    # ---- schedule / night mode
    def _schedule_now(self):
        s = self.s
        return bool(s.night_auto) and schedule_active(s.night_start, s.night_end, s.night_days, datetime.now())

    def _tick_schedule(self):
        active = self._schedule_now()
        if active != self.sched_active:
            self.sched_active = active
            self.night_override = None
            self.sounds.play("night_on" if active else "night_off")
            self.flash()
            self._recompute()
            self.changed.emit()

    def set_night(self, on):
        self.night_override = bool(on)
        self.sounds.play("night_on" if on else "night_off")
        self.flash()
        self._recompute()
        self.changed.emit()

    # ---- app exceptions
    def _win_event(self, *_args):
        try:
            QTimer.singleShot(0, self._on_foreground)
        except Exception:
            pass

    def _on_foreground(self):
        if not IS_WIN or self._down:
            return
        try:
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return
            pid, name = proc_info(hwnd)
        except Exception:
            return
        if not name or pid == os.getpid() or name in NEUTRAL_APPS:
            return
        self._last_app = name
        self._evaluate()

    def _evaluate(self):
        lst = {x.lower() for x in self.s.exc_apps}
        if not lst:
            excepted = False
        elif self.s.exc_mode == "skip":
            excepted = self._last_app in lst
        else:
            excepted = self._last_app not in lst
        if excepted != self.excepted:
            self.excepted = excepted
            self._recompute()
            self.status.emit()

    # ---- hotkeys
    def reload_hotkeys(self):
        if self.hk:
            self.hk.stop()
            self.hk = None
        self.hk_failed = []
        if not (IS_WIN and self.s.hotkeys_on):
            self.status.emit()
            return
        bindings = []
        for action, text in self.s.keys.items():
            parsed = parse_hotkey(text)
            if parsed:
                bindings.append((action, parsed[0], parsed[1]))
        self.hk = HotkeyThread(bindings)
        self.hk.fired.connect(self._on_hotkey)
        self.hk.registered.connect(self._on_registered)
        self.hk.start()

    def _on_registered(self, failed):
        self.hk_failed = list(failed)
        self.status.emit()

    def _on_hotkey(self, name):
        if name == "eink":
            self.set_enabled(not self.s.enabled)
        elif name == "lamp":
            self.set_lamp(not self.s.lamp_enabled)
        elif name == "night":
            self.set_night(not self.night_active())
        elif name == "refresh":
            self.sounds.play("refresh")
            self.flash(force=True)
        elif name == "panel":
            self.panel_requested.emit()

    def set_key(self, action, text):
        self.s.keys[action] = text
        self.reload_hotkeys()
        self._save_timer.start()

    # ---- UI-facing API
    def set(self, name, value):
        setattr(self.s, name, value)
        if name.startswith("night_"):
            self._tick_schedule()
            self._recompute()
        elif name.startswith("exc_"):
            self._evaluate()
            self._recompute()
            self.status.emit()
        elif name == "hotkeys_on":
            self.reload_hotkeys()
        elif name == "autostart":
            set_autostart(bool(value))
        else:
            self.refresh()
        self._save_timer.start()

    def set_enabled(self, on):
        if self.s.enabled == on:
            return
        self.s.enabled = on
        self.sounds.play("on" if on else "off")
        if on:
            self.flash()
        self._recompute()
        self._save_timer.start()
        self.changed.emit()

    def set_lamp(self, on):
        if self.s.lamp_enabled == on:
            return
        self.s.lamp_enabled = on
        self.sounds.play("lamp_on" if on else "lamp_off")
        self._recompute()
        self._save_timer.start()
        self.changed.emit()

    def apply_preset(self, key):
        for k, v in self.preset_values(key).items():
            setattr(self.s, k, v)
        self.sounds.play("tick")
        self.refresh()
        self._save_timer.start()
        self.changed.emit()

    def save_look(self, name):
        self.s.custom_presets[name] = {k: getattr(self.s, k) for k in PRESET_KEYS}
        self.sounds.play("save")
        self._save_timer.start()
        self.changed.emit()

    def delete_look(self, name):
        self.s.custom_presets.pop(name, None)
        if self.s.night_preset == "custom:" + name:
            self.s.night_preset = "night"
        self._recompute()
        self._save_timer.start()
        self.changed.emit()

    def look_choices(self):
        items = [(k, PRESET_LABELS[k]) for k in PRESETS]
        items += [("custom:" + n, n + " (mine)") for n in sorted(self.s.custom_presets)]
        return items

    def add_exception(self, name):
        name = name.strip().lower()
        if not name:
            return False
        if "." not in name:
            name += ".exe"
        if name in [x.lower() for x in self.s.exc_apps]:
            return False
        self.set("exc_apps", list(self.s.exc_apps) + [name])
        return True

    def remove_exception(self, name):
        self.set("exc_apps", [x for x in self.s.exc_apps if x.lower() != name.lower()])

    def restore_now(self):
        self.s.enabled = False
        self.s.lamp_enabled = False
        self.night_override = False
        self._recompute()
        self._save_timer.start()
        self.changed.emit()

    def reset_all(self):
        self.s.__dict__.update(copy.deepcopy(DEFAULTS))
        set_autostart(False)
        self.night_override = None
        self.sched_active = False
        self.reload_hotkeys()
        self._recompute()
        self.s.save()
        self.changed.emit()

    def diagnostics(self):
        screens = ", ".join(f"{sc.name()} {sc.geometry().width()}x{sc.geometry().height()} "
                            f"@{sc.devicePixelRatio():.2f}x" for sc in QGuiApplication.screens())
        return "\n".join([
            f"{APP_NAME} {VERSION}",
            f"Windows: {platform.platform()}",
            f"Python {sys.version.split()[0]} ({'packaged exe' if getattr(sys, 'frozen', False) else 'script'})",
            f"Screen colour effect available: {self.mag.ok}",
            f"Draws above Start menu / Quick Settings (UIAccess): {self.uiaccess}",
            f"Monitors: {screens}",
            f"Start with Windows: {is_autostart()}",
            f"Hotkeys that could not be registered: {self.hk_failed or 'none'}",
            f"Paused by app rule: {self.excepted} (last app: {self._last_app or 'n/a'})",
            f"Night mode active: {self.night_active()}",
            f"Last sound error: {self.sounds.last_error or 'none'}",
            f"Settings file: {settings_path()}",
        ])


# --------------------------------------------------------------------------- look & feel

INK, MUTED, LINE = "#232733", "#6F7686", "#E2E5EC"
BG, SIDE, ACCENT, SOFT = "#F6F7FA", "#262A3D", "#7B6CF6", "#ECE9FF"
AMBER = "#F5A524"


def arrow_path():
    path = os.path.join(settings_dir(), "arrow.png")
    pm = QPixmap(24, 24)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(MUTED), 2.4)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.drawPolyline([QPointF(7, 9.5), QPointF(12, 14.5), QPointF(17, 9.5)])
    p.end()
    pm.save(path, "PNG")
    return path.replace("\\", "/")


QSS_TEMPLATE = """
QWidget { font-family: "Segoe UI Variable Text", "Segoe UI"; font-size: 13px; color: @INK@; }
#shell { background: @BG@; border: 1px solid #D5D9E4; border-radius: 24px; }
#sidebar { background: @SIDE@; border-top-left-radius: 23px; border-bottom-left-radius: 23px;
           border-top-right-radius: 0px; border-bottom-right-radius: 0px; }
#sidecard { background: #30354C; border-radius: 16px; }
QLabel { background: transparent; }
QLabel#brand { color: #FFFFFF; font-size: 17px; font-weight: 700; }
QLabel#brandsub { color: #9AA0BC; font-size: 11px; }
QLabel#sidelabel { color: #E7E9F5; font-size: 13px; font-weight: 600; }
QLabel#title { font-size: 25px; font-weight: 700; }
QLabel#sub { color: @MUTED@; font-size: 13px; }
QLabel#h2 { font-size: 15px; font-weight: 600; }
QLabel#rowlabel { font-size: 13px; }
QLabel#muted { color: @MUTED@; font-size: 12px; }
QLabel#note { background: @SOFT@; color: #4B41C7; border-radius: 12px; padding: 8px 12px; }
QLabel#good { color: #1E8F6A; font-weight: 600; }
QLabel#bad { color: #C2405A; font-weight: 600; }
#card { background: #FFFFFF; border: 1px solid @LINE@; border-radius: 18px; }
QPushButton { outline: none; }
QPushButton#nav { text-align: left; padding: 10px 14px; border: none; border-radius: 14px;
                  color: #B9BED3; background: transparent; font-size: 14px; }
QPushButton#nav:hover { background: #31364D; color: #FFFFFF; }
QPushButton#nav:checked { background: @BG@; color: @INK@; font-weight: 600; }
QPushButton#quit { border: 1px solid #4A506B; border-radius: 14px; padding: 9px 12px; color: #D7DAEA; background: transparent; }
QPushButton#quit:hover { background: #3A4059; }
QPushButton#wbtn { border: none; border-radius: 15px; background: transparent; color: @MUTED@; font-size: 14px; }
QPushButton#wbtn:hover { background: #E7E9F1; color: @INK@; }
QPushButton#wclose:hover { background: #FFD9DF; color: #B3243F; }
QPushButton#seg { background: transparent; border: 1px solid #D3D7E2; border-radius: 12px; padding: 8px 4px; }
QPushButton#seg:hover { background: #F1F2F7; }
QPushButton#seg:checked { background: @ACCENT@; color: #FFFFFF; border-color: @ACCENT@; font-weight: 600; }
QPushButton#seg:disabled { color: #A3A8B6; border-color: #E6E8EE; }
QPushButton#day { background: transparent; border: 1px solid #D3D7E2; border-radius: 17px; font-weight: 600; color: @MUTED@; }
QPushButton#day:hover { background: #F1F2F7; }
QPushButton#day:checked { background: @ACCENT@; color: #FFFFFF; border-color: @ACCENT@; }
QPushButton#primary { background: @ACCENT@; color: #FFFFFF; border: none; border-radius: 14px; padding: 10px 16px; font-weight: 600; }
QPushButton#primary:hover { background: #6A5AEA; }
QPushButton#soft { background: @SOFT@; color: #4B41C7; border: none; border-radius: 14px; padding: 10px 16px; font-weight: 600; }
QPushButton#soft:hover { background: #E1DCFF; }
QPushButton#ghost { background: transparent; border: 1px solid #D3D7E2; border-radius: 14px; padding: 10px 16px; }
QPushButton#ghost:hover { background: #F1F2F7; }
QPushButton#danger { background: #FFF0F2; color: #B3243F; border: none; border-radius: 14px; padding: 10px 16px; font-weight: 600; }
QPushButton#danger:hover { background: #FFE0E5; }
QPushButton:disabled { color: #A3A8B6; }
QSlider::groove:horizontal { height: 6px; background: #E3E6EE; border-radius: 3px; }
QSlider::sub-page:horizontal { background: @ACCENT@; border-radius: 3px; }
QSlider::handle:horizontal { width: 18px; height: 18px; margin: -7px 0; border-radius: 10px; background: #FFFFFF; border: 2px solid @ACCENT@; }
QSlider::sub-page:horizontal:disabled { background: #C9CCD8; }
QSlider::handle:horizontal:disabled { border-color: #C9CCD8; }
QLabel:disabled { color: #A3A8B6; }
QComboBox { background: #FFFFFF; border: 1px solid #D3D7E2; border-radius: 12px; padding: 6px 30px 6px 12px; min-height: 22px; }
QComboBox:hover { border-color: #B9BFD0; }
QComboBox:disabled { color: #A3A8B6; background: #F4F5F8; }
QComboBox::drop-down { border: none; width: 28px; subcontrol-origin: padding; subcontrol-position: center right; }
QComboBox::down-arrow { image: url(@ARROW@); width: 14px; height: 14px; }
QComboBox QAbstractItemView { background: #FFFFFF; border: 1px solid #D3D7E2; selection-background-color: @SOFT@;
                              selection-color: @INK@; outline: 0; padding: 4px; }
QLineEdit, QKeySequenceEdit { background: #FFFFFF; border: 1px solid #D3D7E2; border-radius: 12px; padding: 6px 10px; min-height: 22px; }
QLineEdit:focus { border-color: @ACCENT@; }
QKeySequenceEdit QLineEdit { border: none; padding: 0px; }
QListWidget { background: #FFFFFF; border: 1px solid #D3D7E2; border-radius: 14px; padding: 4px; outline: 0; }
QListWidget::item { padding: 7px 10px; border-radius: 9px; }
QListWidget::item:selected { background: @SOFT@; color: @INK@; }
QToolTip { background: @SIDE@; color: #FFFFFF; border: none; padding: 5px 9px; }
"""


def build_qss():
    qss = QSS_TEMPLATE
    for k, v in dict(INK=INK, MUTED=MUTED, LINE=LINE, BG=BG, SIDE=SIDE, ACCENT=ACCENT, SOFT=SOFT,
                     ARROW=arrow_path()).items():
        qss = qss.replace(f"@{k}@", v)
    return qss


def draw_mascot(p, r):
    """A tiny, friendly e-reader with a face."""
    w = r.width()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor(SIDE), w * 0.05))
    p.setBrush(QColor("#FFFFFF"))
    p.drawRoundedRect(r.adjusted(w * 0.03, w * 0.03, -w * 0.03, -w * 0.03), w * 0.24, w * 0.24)
    inner = r.adjusted(w * 0.14, w * 0.14, -w * 0.14, -w * 0.24)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(SOFT))
    p.drawRoundedRect(inner, w * 0.12, w * 0.12)
    p.setBrush(QColor(SIDE))
    for cx in (0.37, 0.63):
        p.drawEllipse(QRectF(r.x() + w * cx - w * 0.04, r.y() + w * 0.36, w * 0.08, w * 0.11))
    p.setBrush(QColor(255, 140, 165, 190))
    for cx in (0.27, 0.73):
        p.drawEllipse(QRectF(r.x() + w * cx - w * 0.06, r.y() + w * 0.50, w * 0.12, w * 0.07))
    pen = QPen(QColor(SIDE), w * 0.04)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawArc(QRectF(r.x() + w * 0.42, r.y() + w * 0.44, w * 0.16, w * 0.12), 200 * 16, 140 * 16)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(SIDE))
    p.drawEllipse(QPointF(r.center().x(), r.bottom() - w * 0.10), w * 0.028, w * 0.028)


def mascot_pixmap(size=64):
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    draw_mascot(p, QRectF(0, 0, size, size))
    p.end()
    return pm


def make_icon():
    ic = QIcon()
    for s in (16, 24, 32, 48, 64, 128):
        ic.addPixmap(mascot_pixmap(s))
    return ic


def make_shadow(size):
    pm = QPixmap(size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    base = QRectF(20, 26, size.width() - 40, size.height() - 40)
    for i in range(18, 0, -1):
        p.setBrush(QColor(20, 22, 44, 3))
        p.drawRoundedRect(base.adjusted(-i, -i, i, i), 24 + i, 24 + i)
    p.end()
    return pm


# --------------------------------------------------------------------------- widgets

class Switch(QAbstractButton):
    def __init__(self, off="#CDD1DB"):
        super().__init__()
        self.setCheckable(True)
        self.setFixedSize(52, 30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._off = QColor(off)
        self._t = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(150)
        self._anim.valueChanged.connect(self._tick)
        self.toggled.connect(self._run)

    def _run(self, on):
        self._anim.stop()
        self._anim.setStartValue(self._t)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def _tick(self, v):
        self._t = float(v)
        self.update()

    def set_silent(self, on):
        self.blockSignals(True)
        self.setChecked(on)
        self.blockSignals(False)
        self._t = 1.0 if on else 0.0
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        on, t = QColor(ACCENT), self._t
        col = QColor(int(self._off.red() + (on.red() - self._off.red()) * t),
                     int(self._off.green() + (on.green() - self._off.green()) * t),
                     int(self._off.blue() + (on.blue() - self._off.blue()) * t))
        if not self.isEnabled():
            col.setAlpha(120)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(QRectF(1, 1, self.width() - 2, self.height() - 2), 14, 14)
        d = 22
        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(QRectF(4 + (self.width() - d - 8) * t, (self.height() - d) / 2, d, d))


class Segmented(QWidget):
    changed = Signal(str)

    def __init__(self, options):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons = {}
        for key, label, tip in options:
            b = QPushButton(label)
            b.setObjectName("seg")
            b.setCheckable(True)
            b.setToolTip(tip)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setProperty("key", key)
            self.group.addButton(b)
            self.buttons[key] = b
            lay.addWidget(b, 1)
        self.group.buttonClicked.connect(lambda b: self.changed.emit(b.property("key")))

    def set_current(self, key):
        if key is None or key not in self.buttons:
            self.group.setExclusive(False)
            for b in self.buttons.values():
                b.setChecked(False)
            self.group.setExclusive(True)
        else:
            self.buttons[key].setChecked(True)


class SliderRow(QWidget):
    changed = Signal(int)

    def __init__(self, title, lo, hi, suffix=""):
        super().__init__()
        self.suffix = suffix
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(3)
        top = QHBoxLayout()
        name = QLabel(title)
        name.setObjectName("rowlabel")
        self.val = QLabel("0")
        self.val.setObjectName("muted")
        top.addWidget(name)
        top.addStretch(1)
        top.addWidget(self.val)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(lo, hi)
        self.slider.setFixedHeight(22)
        self.slider.valueChanged.connect(self._on)
        v.addLayout(top)
        v.addWidget(self.slider)

    def _on(self, v):
        self.val.setText(f"{v}{self.suffix}")
        self.changed.emit(v)

    def set_value(self, v):
        self.slider.blockSignals(True)
        self.slider.setValue(int(v))
        self.slider.blockSignals(False)
        self.val.setText(f"{int(v)}{self.suffix}")


class Card(QFrame):
    def __init__(self, title=None, subtitle=None):
        super().__init__()
        self.setObjectName("card")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(18, 16, 18, 16)
        self.lay.setSpacing(10)
        if title:
            t = QLabel(title)
            t.setObjectName("h2")
            self.lay.addWidget(t)
        if subtitle:
            s = QLabel(subtitle)
            s.setObjectName("muted")
            s.setWordWrap(True)
            self.lay.addWidget(s)


def switch_row(title, desc=None):
    """A title (+ optional description) on the left and a Switch on the right."""
    row = QHBoxLayout()
    row.setSpacing(12)
    text = QVBoxLayout()
    text.setSpacing(1)
    t = QLabel(title)
    t.setObjectName("rowlabel")
    text.addWidget(t)
    if desc:
        d = QLabel(desc)
        d.setObjectName("muted")
        d.setWordWrap(True)
        text.addWidget(d)
    sw = Switch()
    row.addLayout(text, 1)
    row.addWidget(sw, 0, Qt.AlignmentFlag.AlignVCenter)
    return row, sw


def page_layout(widget):
    lay = QVBoxLayout(widget)
    lay.setContentsMargins(26, 4, 26, 20)
    lay.setSpacing(14)
    return lay


class LampPicker(QWidget):
    """A mini monitor: tap where your lamp sits and see the glow."""
    changed = Signal(str)
    SPOTS = {"left": (16, 100), "top_left": (52, 16), "top": (150, 12), "top_right": (248, 16), "right": (284, 100)}

    def __init__(self):
        super().__init__()
        self.setFixedSize(300, 200)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pos_key, self.on = "left", False

    def set_state(self, pos, on):
        self.pos_key, self.on = pos, on
        self.update()

    def mousePressEvent(self, e):
        x, y = e.position().x(), e.position().y()
        best = min(self.SPOTS.items(), key=lambda kv: (kv[1][0] - x) ** 2 + (kv[1][1] - y) ** 2)
        if (best[1][0] - x) ** 2 + (best[1][1] - y) ** 2 < 46 ** 2:
            self.pos_key = best[0]
            self.update()
            self.changed.emit(best[0])

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        screen = QRectF(34, 34, 232, 138)
        path = QPainterPath()
        path.addRoundedRect(screen, 16, 16)
        p.setPen(QPen(QColor("#CBD0DD"), 2))
        p.setBrush(QColor("#EEF0F6"))
        p.drawPath(path)
        # pretend page lines
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#D9DCE7"))
        for i, wd in enumerate((150, 170, 120, 160, 90)):
            p.drawRoundedRect(QRectF(54, 56 + i * 20, wd, 7), 3.5, 3.5)
        cx, cy = self.SPOTS[self.pos_key]
        if self.on:
            p.save()
            p.setClipPath(path)
            g = QRadialGradient(QPointF(cx, cy), 190)
            g.setColorAt(0.0, QColor(255, 196, 96, 230))
            g.setColorAt(0.45, QColor(255, 206, 130, 110))
            g.setColorAt(1.0, QColor(255, 214, 160, 0))
            p.fillRect(self.rect(), QBrush(g))
            p.restore()
        for key, (x, y) in self.SPOTS.items():
            sel = key == self.pos_key
            p.setPen(QPen(QColor(AMBER if (sel and self.on) else ("#8C92A8" if sel else "#C3C8D6")), 2))
            p.setBrush(QColor("#FFD37A") if (sel and self.on) else (QColor("#FFFFFF") if not sel else QColor("#E8EAF2")))
            p.drawEllipse(QPointF(x, y), 11 if sel else 8, 11 if sel else 8)


class TimeBox(QWidget):
    changed = Signal(str)

    def __init__(self):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.h, self.m = QComboBox(), QComboBox()
        for i in range(24):
            self.h.addItem(f"{i:02d}")
        for i in range(0, 60, 5):
            self.m.addItem(f"{i:02d}")
        colon = QLabel(":")
        lay.addWidget(self.h)
        lay.addWidget(colon)
        lay.addWidget(self.m)
        self.h.activated.connect(self._emit)
        self.m.activated.connect(self._emit)

    def _emit(self, *_):
        self.changed.emit(f"{self.h.currentText()}:{self.m.currentText()}")

    def set_time(self, text):
        try:
            hh, mm = (int(x) for x in text.split(":"))
        except Exception:
            hh, mm = 0, 0
        self.h.blockSignals(True)
        self.m.blockSignals(True)
        self.h.setCurrentIndex(max(0, min(23, hh)))
        self.m.setCurrentIndex(max(0, min(11, round(mm / 5))))
        self.h.blockSignals(False)
        self.m.blockSignals(False)


class DayChips(QWidget):
    changed = Signal(str)
    NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

    def __init__(self):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.btns = []
        for letter, name in zip("MTWTFSS", self.NAMES):
            b = QPushButton(letter)
            b.setObjectName("day")
            b.setCheckable(True)
            b.setFixedSize(34, 34)
            b.setToolTip(name)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(self._emit)
            self.btns.append(b)
            lay.addWidget(b)
        lay.addStretch(1)

    def _emit(self, *_):
        self.changed.emit("".join("1" if b.isChecked() else "0" for b in self.btns))

    def set_days(self, text):
        text = (text + "1111111")[:7]
        for i, b in enumerate(self.btns):
            b.blockSignals(True)
            b.setChecked(text[i] == "1")
            b.blockSignals(False)


# --------------------------------------------------------------------------- pages

class LookPage(QWidget):
    SLIDERS = [("strength", "Effect strength", 0, 100, "%"), ("contrast", "Contrast", 0, 100, ""),
               ("brightness", "Brightness", -30, 30, ""), ("warmth", "Paper tone (cool to warm)", 0, 100, ""),
               ("ink", "Ink depth", 0, 100, ""), ("color", "Colour (0 is pure e-ink)", 0, 100, ""),
               ("texture", "Film grain", 0, 100, ""), ("shade", "Edge shading", 0, 100, "")]

    def __init__(self, engine):
        super().__init__()
        self.e = engine
        lay = page_layout(self)
        self.note = QLabel("Night mode is on. These sliders change your day look.")
        self.note.setObjectName("note")
        self.note.setWordWrap(True)
        lay.addWidget(self.note)

        c1 = Card()
        self.presets = Segmented([(k, PRESET_LABELS[k], PRESET_TIPS[k]) for k in PRESETS])
        self.presets.changed.connect(self.e.apply_preset)
        c1.lay.addWidget(self.presets)
        r1 = QHBoxLayout()
        r1.setSpacing(8)
        self.saved = QComboBox()
        self.saved.activated.connect(self._pick)
        self.delete = QPushButton("Delete")
        self.delete.setObjectName("ghost")
        self.delete.setCursor(Qt.CursorShape.PointingHandCursor)
        self.delete.clicked.connect(self._delete)
        r1.addWidget(self.saved, 1)
        r1.addWidget(self.delete)
        c1.lay.addLayout(r1)
        r2 = QHBoxLayout()
        r2.setSpacing(8)
        self.name = QLineEdit()
        self.name.setPlaceholderText("Name this look, then save it")
        self.name.setMaxLength(24)
        self.name.returnPressed.connect(self._save)
        save = QPushButton("Save look")
        save.setObjectName("soft")
        save.setCursor(Qt.CursorShape.PointingHandCursor)
        save.clicked.connect(self._save)
        r2.addWidget(self.name, 1)
        r2.addWidget(save)
        c1.lay.addLayout(r2)
        lay.addWidget(c1)

        c2 = Card()
        grid = QGridLayout()
        grid.setHorizontalSpacing(26)
        grid.setVerticalSpacing(12)
        self.rows = {}
        for i, (key, title, lo, hi, suf) in enumerate(self.SLIDERS):
            r = SliderRow(title, lo, hi, suf)
            r.changed.connect(lambda v, k=key: self._slider(k, v))
            self.rows[key] = r
            grid.addWidget(r, i // 2, i % 2)
        c2.lay.addLayout(grid)
        lay.addWidget(c2)
        lay.addStretch(1)

    def _slider(self, key, value):
        self.e.set(key, value)
        if key in PRESET_KEYS:
            self.presets.set_current(None)
            self.saved.setCurrentIndex(0)

    def _pick(self, idx):
        key = self.saved.itemData(idx)
        if key:
            self.e.apply_preset(key)

    def _save(self):
        name = self.name.text().strip()
        if not name:
            self.name.setFocus()
            self.name.setPlaceholderText("Give your look a name first")
            return
        self.e.save_look(name)
        self.name.clear()

    def _delete(self):
        key = self.saved.currentData()
        if key:
            self.e.delete_look(key[7:])

    def sync(self):
        s = self.e.s
        for k, r in self.rows.items():
            r.set_value(getattr(s, k))
        self.note.setVisible(self.e.night_active())
        cur = {k: getattr(s, k) for k in PRESET_KEYS}
        match = next((n for n, v in PRESETS.items() if v == cur), None)
        self.presets.set_current(match)
        self.saved.blockSignals(True)
        self.saved.clear()
        self.saved.addItem("My saved looks", None)
        sel = 0
        for n in sorted(s.custom_presets):
            self.saved.addItem(n, "custom:" + n)
            vals = dict(PRESETS["paper"])
            vals.update(s.custom_presets[n])
            if match is None and vals == cur:
                sel = self.saved.count() - 1
        self.saved.setCurrentIndex(sel)
        self.saved.blockSignals(False)
        self.delete.setEnabled(sel > 0)


class LampPage(QWidget):
    SLIDERS = [("lamp_power", "Light"), ("lamp_warmth", "Warmth (soft white to amber)"),
               ("lamp_spread", "Spread"), ("lamp_dim", "Room dimming")]

    def __init__(self, engine):
        super().__init__()
        self.e = engine
        lay = page_layout(self)
        top = Card()
        row, self.sw = switch_row("Desk lamp", "Warms one side of the screen, like a lamp beside you.")
        self.sw.toggled.connect(self.e.set_lamp)
        top.lay.addLayout(row)
        lay.addWidget(top)

        body = QHBoxLayout()
        body.setSpacing(14)
        left = Card("Where is your lamp?", "Tap a spot around the screen.")
        self.picker = LampPicker()
        self.picker.changed.connect(lambda k: self.e.set("lamp_pos", k))
        left.lay.addWidget(self.picker, 0, Qt.AlignmentFlag.AlignHCenter)
        right = Card("Light")
        self.rows = {}
        for key, title in self.SLIDERS:
            r = SliderRow(title, 0, 100)
            r.changed.connect(lambda v, k=key: self.e.set(k, v))
            self.rows[key] = r
            right.lay.addWidget(r)
        right.lay.addStretch(1)
        body.addWidget(left, 0)
        body.addWidget(right, 1)
        lay.addLayout(body)
        lay.addStretch(1)

    def sync(self):
        s = self.e.s
        self.sw.set_silent(s.lamp_enabled)
        self.picker.set_state(s.lamp_pos, s.lamp_enabled)
        for k, r in self.rows.items():
            r.set_value(getattr(s, k))


class NightPage(QWidget):
    def __init__(self, engine):
        super().__init__()
        self.e = engine
        lay = page_layout(self)
        c = Card()
        row, self.sw_now = switch_row("Night mode now", "A dimmer, warmer look until you switch it off.")
        self.sw_now.toggled.connect(self.e.set_night)
        c.lay.addLayout(row)
        row2, self.sw_auto = switch_row("Turn on automatically", "Uses the times and days below.")
        self.sw_auto.toggled.connect(lambda v: self.e.set("night_auto", v))
        c.lay.addLayout(row2)

        self.when = QWidget()
        wl = QVBoxLayout(self.when)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(10)
        tr = QHBoxLayout()
        tr.setSpacing(8)
        self.t_from, self.t_to = TimeBox(), TimeBox()
        self.t_from.changed.connect(lambda v: self.e.set("night_start", v))
        self.t_to.changed.connect(lambda v: self.e.set("night_end", v))
        for text, box in (("From", self.t_from), ("until", self.t_to)):
            lab = QLabel(text)
            lab.setObjectName("rowlabel")
            tr.addWidget(lab)
            tr.addWidget(box)
        tr.addStretch(1)
        wl.addLayout(tr)
        self.days = DayChips()
        self.days.changed.connect(lambda v: self.e.set("night_days", v))
        wl.addWidget(self.days)
        c.lay.addWidget(self.when)

        lr = QHBoxLayout()
        lab = QLabel("Night look")
        lab.setObjectName("rowlabel")
        self.look = QComboBox()
        self.look.activated.connect(lambda i: self.e.set("night_preset", self.look.itemData(i)))
        lr.addWidget(lab)
        lr.addWidget(self.look, 1)
        c.lay.addLayout(lr)
        self.dim = SliderRow("Extra dimming", 0, 30)
        self.dim.changed.connect(lambda v: self.e.set("night_dim", v))
        c.lay.addWidget(self.dim)
        row3, self.sw_lamp = switch_row("Turn the desk lamp on at night")
        self.sw_lamp.toggled.connect(lambda v: self.e.set("night_lamp", v))
        c.lay.addLayout(row3)
        lay.addWidget(c)
        self.state = QLabel("")
        self.state.setObjectName("muted")
        self.state.setWordWrap(True)
        lay.addWidget(self.state)
        lay.addStretch(1)

    def sync(self):
        s = self.e.s
        self.sw_now.set_silent(self.e.night_active())
        self.sw_auto.set_silent(s.night_auto)
        self.when.setEnabled(s.night_auto)
        self.t_from.set_time(s.night_start)
        self.t_to.set_time(s.night_end)
        self.days.set_days(s.night_days)
        self.look.blockSignals(True)
        self.look.clear()
        for key, label in self.e.look_choices():
            self.look.addItem(label, key)
        idx = self.look.findData(s.night_preset)
        self.look.setCurrentIndex(max(0, idx))
        self.look.blockSignals(False)
        self.dim.set_value(s.night_dim)
        self.sw_lamp.set_silent(s.night_lamp)
        if s.night_auto:
            self.state.setText(f"Night mode runs from {s.night_start} until {s.night_end} on the days you picked. "
                               "Switching it by hand lasts until the next scheduled change.")
        else:
            self.state.setText("Automatic night mode is off. Use the switch above whenever you like.")


class AppsPage(QWidget):
    def __init__(self, engine):
        super().__init__()
        self.e = engine
        lay = page_layout(self)
        c = Card("Apps that need true colour",
                 "While one of these is in front, PaperGlass steps aside. When you switch away, it fades back in.")
        self.mode = QComboBox()
        self.mode.addItem("Turn the filter off in these apps", "skip")
        self.mode.addItem("Only filter these apps", "only")
        self.mode.activated.connect(lambda i: self.e.set("exc_mode", self.mode.itemData(i)))
        c.lay.addWidget(self.mode)
        self.list = QListWidget()
        self.list.setMinimumHeight(120)
        c.lay.addWidget(self.list, 1)
        ar = QHBoxLayout()
        ar.setSpacing(8)
        self.pick = QComboBox()
        self.pick.setEditable(True)
        self.pick.lineEdit().setPlaceholderText("Pick a running app, or type a name like vlc.exe")
        add = QPushButton("Add")
        add.setObjectName("soft")
        add.setCursor(Qt.CursorShape.PointingHandCursor)
        add.clicked.connect(self._add)
        rem = QPushButton("Remove")
        rem.setObjectName("ghost")
        rem.setCursor(Qt.CursorShape.PointingHandCursor)
        rem.clicked.connect(self._remove)
        ar.addWidget(self.pick, 1)
        ar.addWidget(add)
        ar.addWidget(rem)
        c.lay.addLayout(ar)
        row, self.sw_lamp = switch_row("Pause the desk lamp too")
        self.sw_lamp.toggled.connect(lambda v: self.e.set("exc_lamp", v))
        c.lay.addLayout(row)
        lay.addWidget(c, 1)
        self.state = QLabel("")
        self.state.setObjectName("muted")
        lay.addWidget(self.state)
        self.refresh_running()

    def refresh_running(self):
        self.pick.clear()
        if IS_WIN:
            for n in list_windowed_apps():
                self.pick.addItem(n)
        self.pick.setCurrentIndex(-1)
        self.pick.setEditText("")

    def showEvent(self, e):
        super().showEvent(e)
        self.refresh_running()

    def _add(self):
        if self.e.add_exception(self.pick.currentText()):
            self.pick.setEditText("")

    def _remove(self):
        it = self.list.currentItem()
        if it:
            self.e.remove_exception(it.text())

    def sync(self):
        s = self.e.s
        self.mode.setCurrentIndex(max(0, self.mode.findData(s.exc_mode)))
        self.list.clear()
        if s.exc_apps:
            self.list.addItems(s.exc_apps)
        self.sw_lamp.set_silent(s.exc_lamp)
        self.update_state()

    def update_state(self):
        e = self.e
        if e.excepted:
            self.state.setText(f"Paused while {e._last_app} is in front.")
        elif not e.s.exc_apps:
            self.state.setText("No apps yet. Try adding a video player or photo editor.")
        else:
            self.state.setText("Filter is active.")


class KeysPage(QWidget):
    ACTIONS = [("eink", "Turn e-ink on or off"), ("lamp", "Turn the desk lamp on or off"),
               ("night", "Turn night mode on or off"), ("refresh", "Refresh flash"),
               ("panel", "Open this window")]

    def __init__(self, engine):
        super().__init__()
        self.e = engine
        lay = page_layout(self)
        top = Card()
        row, self.sw = switch_row("Global hotkeys", "They work in any app, even while this window is hidden.")
        self.sw.toggled.connect(lambda v: self.e.set("hotkeys_on", v))
        top.lay.addLayout(row)
        lay.addWidget(top)

        c = Card("Shortcuts", "Click a box, then press the keys you want. Use Ctrl, Alt or Win plus a key.")
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(10)
        self.grid.setVerticalSpacing(8)
        self.edits, self.stat = {}, {}
        for i, (action, label) in enumerate(self.ACTIONS):
            lab = QLabel(label)
            lab.setObjectName("rowlabel")
            ed = QKeySequenceEdit()
            ed.setFixedWidth(170)
            ed.editingFinished.connect(lambda a=action: self._edited(a))
            reset = QPushButton("Reset")
            reset.setObjectName("ghost")
            reset.setCursor(Qt.CursorShape.PointingHandCursor)
            reset.clicked.connect(lambda _c=False, a=action: self._reset(a))
            st = QLabel("")
            st.setObjectName("muted")
            self.edits[action], self.stat[action] = ed, st
            self.grid.addWidget(lab, i, 0)
            self.grid.addWidget(ed, i, 1)
            self.grid.addWidget(reset, i, 2)
            self.grid.addWidget(st, i, 3)
        self.grid.setColumnStretch(0, 1)
        c.lay.addLayout(self.grid)
        self.msg = QLabel("")
        self.msg.setObjectName("bad")
        self.msg.setWordWrap(True)
        c.lay.addWidget(self.msg)
        lay.addWidget(c)
        tips = Card("Also handy")
        t = QLabel("Click the tray icon to open PaperGlass. Right-click it for quick switches and Quit.")
        t.setObjectName("muted")
        t.setWordWrap(True)
        tips.lay.addWidget(t)
        lay.addWidget(tips)
        lay.addStretch(1)

    def _seq(self, ed):
        return ed.keySequence().toString(QKeySequence.SequenceFormat.PortableText).split(",")[0].strip()

    def _edited(self, action):
        ed = self.edits[action]
        txt = self._seq(ed)
        if not txt:
            ed.setKeySequence(QKeySequence(self.e.s.keys[action]))
            return
        if parse_hotkey(txt) is None:
            self.msg.setText("Use Ctrl, Alt or Win together with a key, for example Ctrl+Alt+E.")
        elif any(a != action and t.lower() == txt.lower() for a, t in self.e.s.keys.items()):
            self.msg.setText("That combination is already used by another shortcut.")
        else:
            self.msg.setText("")
            self.e.set_key(action, txt)
            return
        ed.setKeySequence(QKeySequence(self.e.s.keys[action]))

    def _reset(self, action):
        self.msg.setText("")
        self.e.set_key(action, DEFAULT_KEYS[action])
        self.sync()

    def sync(self):
        s = self.e.s
        self.sw.set_silent(s.hotkeys_on)
        for a, ed in self.edits.items():
            ed.blockSignals(True)
            ed.setKeySequence(QKeySequence(s.keys.get(a, DEFAULT_KEYS[a])))
            ed.blockSignals(False)
            ed.setEnabled(s.hotkeys_on)
        self.update_state()

    def update_state(self):
        for a, st in self.stat.items():
            st.setText("In use elsewhere" if a in self.e.hk_failed else "")


class SettingsPage(QWidget):
    def __init__(self, engine, on_help):
        super().__init__()
        self.e = engine
        lay = QHBoxLayout(self)
        lay.setContentsMargins(26, 4, 26, 20)
        lay.setSpacing(14)
        left, right = QVBoxLayout(), QVBoxLayout()
        left.setSpacing(14)
        right.setSpacing(14)

        c1 = Card("Startup")
        r, self.sw_auto = switch_row("Start with Windows", "Opens quietly in the tray when you sign in.")
        self.sw_auto.toggled.connect(lambda v: self.e.set("autostart", v))
        c1.lay.addLayout(r)
        r, self.sw_hidden = switch_row("Start hidden", "Skip this window when PaperGlass opens.")
        self.sw_hidden.toggled.connect(lambda v: self.e.set("start_hidden", v))
        c1.lay.addLayout(r)
        r, self.sw_close = switch_row("Close button quits", "Off: the X hides PaperGlass to the tray.")
        self.sw_close.toggled.connect(lambda v: self.e.set("close_quits", v))
        c1.lay.addLayout(r)
        left.addWidget(c1)

        c2 = Card("Start menu and Quick Settings")
        self.shell = QLabel("")
        self.shell.setWordWrap(True)
        self.shell.setObjectName("muted")
        self.shell_state = QLabel("")
        c2.lay.addWidget(self.shell_state)
        c2.lay.addWidget(self.shell)
        how = QPushButton("How to cover them")
        how.setObjectName("soft")
        how.setCursor(Qt.CursorShape.PointingHandCursor)
        how.clicked.connect(on_help)
        c2.lay.addWidget(how)
        left.addWidget(c2)
        left.addStretch(1)

        c3 = Card("Sounds")
        r, self.sw_snd = switch_row("Play sounds", "Soft cues when you switch things.")
        self.sw_snd.toggled.connect(lambda v: self.e.set("sounds", v))
        c3.lay.addLayout(r)
        self.vol = SliderRow("Volume", 0, 100, "%")
        self.vol.changed.connect(lambda v: self.e.set("sound_volume", v))
        c3.lay.addWidget(self.vol)
        self.style = Segmented([("chime", "Chimes", "Gentle bell tones"), ("paper", "Paper", "Page-rustle sounds")])
        self.style.changed.connect(self._style)
        c3.lay.addWidget(self.style)
        test = QPushButton("Play a sample")
        test.setObjectName("ghost")
        test.setCursor(Qt.CursorShape.PointingHandCursor)
        test.clicked.connect(lambda: self.e.sounds.play("on"))
        c3.lay.addWidget(test)
        right.addWidget(c3)

        c4 = Card("Refresh flash", "Like a real e-ink panel clearing itself.")
        self.flash = Segmented([("off", "Off", "No flash"), ("soft", "Soft", "A gentle paper wipe"),
                                ("full", "Full", "Black-then-white, like the real thing")])
        self.flash.changed.connect(lambda k: self.e.set("flash", k))
        c4.lay.addWidget(self.flash)
        now = QPushButton("Refresh screen now")
        now.setObjectName("soft")
        now.setCursor(Qt.CursorShape.PointingHandCursor)
        now.clicked.connect(self._refresh_now)
        c4.lay.addWidget(now)
        right.addWidget(c4)
        right.addStretch(1)

        lay.addLayout(left, 1)
        lay.addLayout(right, 1)

    def _style(self, key):
        self.e.set("sound_style", key)
        self.e.sounds.play("on")

    def _refresh_now(self):
        self.e.sounds.play("refresh")
        self.e.flash(force=True)

    def sync(self):
        s = self.e.s
        self.sw_auto.set_silent(s.autostart)
        self.sw_hidden.set_silent(s.start_hidden)
        self.sw_close.set_silent(s.close_quits)
        self.sw_snd.set_silent(s.sounds)
        self.vol.set_value(s.sound_volume)
        self.style.set_current(s.sound_style)
        self.flash.set_current(s.flash)
        if self.e.uiaccess:
            self.shell_state.setText("Covered")
            self.shell_state.setObjectName("good")
            self.shell.setText("Grain, lamp and flash reach the Start menu and Quick Settings too.")
        else:
            self.shell_state.setText("Tone only")
            self.shell_state.setObjectName("bad")
            self.shell.setText("Gray and paper tone already cover them. Grain, lamp and flash stop "
                               "underneath because Windows keeps those panels above normal windows.")
        self.shell_state.style().unpolish(self.shell_state)
        self.shell_state.style().polish(self.shell_state)


class SupportPage(QWidget):
    FAQ = (
        "<p><b>The screen is stuck in gray.</b><br>Press Restore screen now below, or quit PaperGlass. "
        "Windows also drops the effect whenever the app closes.</p>"
        "<p><b>Nothing happens, or a warning showed up.</b><br>Turn off Windows Magnifier and "
        "Settings, Accessibility, Color filters, then restart PaperGlass.</p>"
        "<p><b>Grain or lamp is missing over the Start menu.</b><br>The gray tone covers it already. To add the rest, "
        "use the optional full-coverage setup (steps below).</p>"
        "<p><b>Video or photos look wrong.</b><br>Add that app on the Apps tab and PaperGlass steps aside while it is in front.</p>"
    )
    COVER = ("Full coverage (advanced, optional): run build_full_coverage.bat from the PaperGlass folder. "
             "It builds a special copy, signs it with a certificate made on your PC and installs it to Program "
             "Files so Windows lets it draw above the Start menu. remove_full_coverage.ps1 undoes it. "
             "Do not run that special copy from the build folder.")

    def __init__(self, engine, quit_cb):
        super().__init__()
        self.e = engine
        lay = page_layout(self)
        about = Card()
        ar = QHBoxLayout()
        ar.setSpacing(14)
        logo = QLabel()
        logo.setPixmap(mascot_pixmap(56))
        logo.setFixedSize(56, 56)
        txt = QVBoxLayout()
        txt.setSpacing(1)
        t = QLabel(f"{APP_NAME} {VERSION}")
        t.setObjectName("h2")
        d = QLabel("Makes your whole screen feel like paper.")
        d.setObjectName("muted")
        txt.addWidget(t)
        txt.addWidget(d)
        ar.addWidget(logo)
        ar.addLayout(txt, 1)
        if SUPPORT_EMAIL:
            b = QPushButton("Email support")
            b.setObjectName("soft")
            b.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("mailto:" + SUPPORT_EMAIL)))
            ar.addWidget(b)
        if SUPPORT_URL:
            b = QPushButton("Website")
            b.setObjectName("ghost")
            b.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(SUPPORT_URL)))
            ar.addWidget(b)
        about.lay.addLayout(ar)
        lay.addWidget(about)

        faq = Card("Quick answers")
        f = QLabel(self.FAQ)
        f.setWordWrap(True)
        f.setTextFormat(Qt.TextFormat.RichText)
        f.setStyleSheet("font-size: 12px;")
        faq.lay.addWidget(f)
        cv = QLabel(self.COVER)
        cv.setWordWrap(True)
        cv.setObjectName("note")
        faq.lay.addWidget(cv)
        lay.addWidget(faq)

        br = QHBoxLayout()
        br.setSpacing(8)
        for text, obj, fn in (("Copy diagnostics", "soft", self._copy), ("Open settings folder", "ghost", self._folder),
                              ("Restore screen now", "ghost", self.e.restore_now),
                              ("Reset everything", "danger", self._reset)):
            b = QPushButton(text)
            b.setObjectName(obj)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _c=False, f=fn: f())
            br.addWidget(b)
        lay.addLayout(br)
        self.done = QLabel("")
        self.done.setObjectName("muted")
        lay.addWidget(self.done)
        lay.addStretch(1)

    def _copy(self):
        QApplication.clipboard().setText(self.e.diagnostics())
        self.done.setText("Copied. Paste it into your message when you ask for help.")

    def _folder(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(settings_dir()))

    def _reset(self):
        r = QMessageBox.question(self, APP_NAME, "Reset every setting to its default?")
        if r == QMessageBox.StandardButton.Yes:
            self.e.reset_all()
            self.done.setText("Everything is back to defaults.")

    def sync(self):
        pass


# --------------------------------------------------------------------------- main window

class MainWindow(QWidget):
    minimized_to_tray = Signal()
    NAV = [("look", "\U0001F3A8  Look", "Look", "Make your screen feel like paper."),
           ("lamp", "\U0001F4A1  Lamp", "Desk lamp", "A warm pool of light from beside your screen."),
           ("night", "\U0001F319  Night", "Night mode", "Dim and warm things up when it gets late."),
           ("apps", "\U0001F9E9  Apps", "App exceptions", "Pause the filter for apps that need true colour."),
           ("keys", "\u2328\uFE0F  Hotkeys", "Hotkeys", "Control PaperGlass without opening it."),
           ("settings", "\u2699\uFE0F  Settings", "Settings", "Startup, sounds and screen refresh."),
           ("support", "\U0001F4AC  Support", "Support", "Help, tips and diagnostics.")]

    def __init__(self, engine, quit_cb):
        super().__init__()
        self.e = engine
        self.quit_cb = quit_cb
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(make_icon())
        self.setFixedSize(840, 640)
        self.setStyleSheet(build_qss())
        self._shadow = make_shadow(self.size())

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)
        shell = QFrame()
        shell.setObjectName("shell")
        outer.addWidget(shell)
        row = QHBoxLayout(shell)
        row.setContentsMargins(1, 1, 1, 1)
        row.setSpacing(0)
        row.addWidget(self._build_sidebar())

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        head = QHBoxLayout()
        head.setContentsMargins(28, 20, 16, 8)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.title = QLabel("")
        self.title.setObjectName("title")
        self.sub = QLabel("")
        self.sub.setObjectName("sub")
        titles.addWidget(self.title)
        titles.addWidget(self.sub)
        head.addLayout(titles, 1)
        mn = QPushButton("\u2013")
        mn.setObjectName("wbtn")
        mn.setFixedSize(30, 30)
        mn.setToolTip("Hide to tray. The filter keeps running.")
        mn.setCursor(Qt.CursorShape.PointingHandCursor)
        mn.clicked.connect(self._hide_to_tray)
        cl = QPushButton("\u2715")
        cl.setObjectName("wbtn")
        cl.setProperty("class", "close")
        cl.setStyleSheet("QPushButton:hover { background: #FFD9DF; color: #B3243F; }")
        cl.setFixedSize(30, 30)
        cl.setToolTip("Close")
        cl.setCursor(Qt.CursorShape.PointingHandCursor)
        cl.clicked.connect(self.close)
        head.addWidget(mn, 0, Qt.AlignmentFlag.AlignTop)
        head.addWidget(cl, 0, Qt.AlignmentFlag.AlignTop)
        right.addLayout(head)

        self.stack = QStackedWidget()
        self.pages = {
            "look": LookPage(engine), "lamp": LampPage(engine), "night": NightPage(engine),
            "apps": AppsPage(engine), "keys": KeysPage(engine),
            "settings": SettingsPage(engine, lambda: self._go("support")),
            "support": SupportPage(engine, quit_cb),
        }
        for key, *_ in self.NAV:
            self.stack.addWidget(self.pages[key])
        right.addWidget(self.stack, 1)
        row.addLayout(right, 1)

        self.engine_hooks()
        self._go("look")
        self.sync()

    def engine_hooks(self):
        self.e.changed.connect(self.sync)
        self.e.status.connect(self._status)

    def _build_sidebar(self):
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(188)
        lay = QVBoxLayout(side)
        lay.setContentsMargins(14, 18, 14, 16)
        lay.setSpacing(4)
        logo_row = QHBoxLayout()
        logo_row.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(mascot_pixmap(44))
        logo.setFixedSize(44, 44)
        names = QVBoxLayout()
        names.setSpacing(0)
        b1 = QLabel(APP_NAME)
        b1.setObjectName("brand")
        b2 = QLabel(f"version {VERSION}")
        b2.setObjectName("brandsub")
        names.addWidget(b1)
        names.addWidget(b2)
        logo_row.addWidget(logo)
        logo_row.addLayout(names, 1)
        lay.addLayout(logo_row)
        lay.addSpacing(16)

        self.navgroup = QButtonGroup(self)
        self.navgroup.setExclusive(True)
        self.navbtns = {}
        for key, label, *_ in self.NAV:
            b = QPushButton(label)
            b.setObjectName("nav")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _c=False, k=key: self._go(k))
            self.navgroup.addButton(b)
            self.navbtns[key] = b
            lay.addWidget(b)
        lay.addStretch(1)

        card = QFrame()
        card.setObjectName("sidecard")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 12, 14, 12)
        cl.setSpacing(10)
        r = QHBoxLayout()
        lab = QLabel("E-ink filter")
        lab.setObjectName("sidelabel")
        self.master = Switch(off="#535A78")
        self.master.toggled.connect(self.e.set_enabled)
        r.addWidget(lab, 1)
        r.addWidget(self.master)
        cl.addLayout(r)
        self.side_state = QLabel("")
        self.side_state.setObjectName("brandsub")
        self.side_state.setWordWrap(True)
        cl.addWidget(self.side_state)
        lay.addWidget(card)
        lay.addSpacing(6)
        q = QPushButton("Quit and restore")
        q.setObjectName("quit")
        q.setCursor(Qt.CursorShape.PointingHandCursor)
        q.clicked.connect(self.quit_cb)
        lay.addWidget(q)
        return side

    def _go(self, key):
        keys = [k for k, *_ in self.NAV]
        idx = keys.index(key)
        self.stack.setCurrentIndex(idx)
        self.navbtns[key].setChecked(True)
        self.title.setText(self.NAV[idx][2])
        self.sub.setText(self.NAV[idx][3])

    def sync(self):
        for page in self.pages.values():
            page.sync()
        self.master.set_silent(self.e.s.enabled)
        self._status()

    def _status(self):
        e = self.e
        if not e.s.enabled:
            txt = "Off"
        elif e.excepted:
            txt = f"Paused for {e._last_app}"
        elif e.night_active():
            txt = "On, night mode"
        else:
            txt = "On"
        self.side_state.setText(txt)
        self.pages["apps"].update_state()
        self.pages["keys"].update_state()

    # ---- window behaviour
    def paintEvent(self, _):
        QPainter(self).drawPixmap(0, 0, self._shadow)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and ev.position().y() < 96:
            h = self.windowHandle()
            if h:
                h.startSystemMove()
                ev.accept()
                return
        super().mousePressEvent(ev)

    def _hide_to_tray(self):
        self.hide()
        self.minimized_to_tray.emit()

    def changeEvent(self, ev):
        if self.isMinimized():
            QTimer.singleShot(0, self._hide_to_tray)
        super().changeEvent(ev)

    def closeEvent(self, ev):
        if self.e._down or self.e.s.close_quits:
            ev.accept()
            self.quit_cb()
        else:
            ev.ignore()
            self._hide_to_tray()


# --------------------------------------------------------------------------- main

def main():
    if not IS_WIN:
        print("PaperGlass only runs on Windows.")
        return 1

    if "--make-icon" in sys.argv:                  # used by make_installer.bat
        i = sys.argv.index("--make-icon")
        out = sys.argv[i + 1] if len(sys.argv) > i + 1 else "paperglass.ico"
        _app = QApplication(sys.argv)
        return 0 if mascot_pixmap(256).save(out, "ICO") else 1

    mutex = kernel32.CreateMutexW(None, False, "Local\\PaperGlass_single_instance")
    if ctypes.get_last_error() == 183:      # ERROR_ALREADY_EXISTS
        user32.MessageBoxW(None, "PaperGlass is already running. Look for it in the system tray.", APP_NAME, 0x40)
        return 0

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(make_icon())

    engine = Engine()
    quitting = {"done": False}

    def quit_all():
        if quitting["done"]:
            return
        quitting["done"] = True
        engine.shutdown()
        tray.hide()
        app.quit()

    win = MainWindow(engine, quit_all)

    tray = QSystemTrayIcon(make_icon(), app)
    tray.setToolTip(APP_NAME)
    menu = QMenu()
    act_show = QAction("Open PaperGlass", menu)
    act_eink = QAction("E-ink filter", menu, checkable=True)
    act_lamp = QAction("Desk lamp", menu, checkable=True)
    act_night = QAction("Night mode", menu, checkable=True)
    act_refresh = QAction("Refresh screen", menu)
    act_quit = QAction("Quit and restore screen", menu)
    for a in (act_show, None, act_eink, act_lamp, act_night, act_refresh, None, act_quit):
        menu.addSeparator() if a is None else menu.addAction(a)
    tray.setContextMenu(menu)

    def show_panel():
        win.showNormal()
        win.raise_()
        win.activateWindow()

    def sync_tray(*_):
        for act, val in ((act_eink, engine.s.enabled), (act_lamp, engine.s.lamp_enabled),
                         (act_night, engine.night_active())):
            act.blockSignals(True)
            act.setChecked(val)
            act.blockSignals(False)

    act_show.triggered.connect(show_panel)
    act_eink.toggled.connect(engine.set_enabled)
    act_lamp.toggled.connect(engine.set_lamp)
    act_night.toggled.connect(engine.set_night)
    act_refresh.triggered.connect(lambda: (engine.sounds.play("refresh"), engine.flash(force=True)))
    act_quit.triggered.connect(quit_all)
    tray.activated.connect(lambda r: show_panel() if r in (
        QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick) else None)
    engine.changed.connect(sync_tray)
    engine.panel_requested.connect(show_panel)
    sync_tray()
    tray.show()

    tip = {"shown": False}

    def tray_tip():
        if not tip["shown"]:
            tip["shown"] = True
            tray.showMessage(APP_NAME, "Still running. Click the tray icon to open it again.",
                             QSystemTrayIcon.MessageIcon.Information, 3000)

    win.minimized_to_tray.connect(tray_tip)

    app.aboutToQuit.connect(engine.shutdown)
    atexit.register(engine.shutdown)
    signal.signal(signal.SIGINT, lambda *_: quit_all())
    signal.signal(signal.SIGTERM, lambda *_: quit_all())
    pulse = QTimer()
    pulse.timeout.connect(lambda: None)      # lets Python handle signals while Qt runs
    pulse.start(250)

    hidden = "--tray" in sys.argv[1:] or engine.s.start_hidden
    engine.start(intro=not hidden)
    if not hidden:
        win.show()

    if not engine.mag.ok:
        QMessageBox.warning(
            win, APP_NAME,
            "Windows would not start its screen colour effect, so the gray filter is off "
            "(only the grain and the lamp will show).\n\n"
            "Another app may be using it: close Windows Magnifier, turn off Settings > Accessibility > "
            "Color filters, then restart PaperGlass.")

    code = app.exec()
    engine.shutdown()
    del mutex
    return code


if __name__ == "__main__":
    sys.exit(main())
