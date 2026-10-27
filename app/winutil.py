"""Windows 相关：单实例、DPI、全局热键、开机自启、窗口鼠标穿透。"""
import ctypes
import os
import sys
import threading
import winreg
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

APP_ID = "ClassroomHotWords"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def enable_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


_mutex = None


def acquire_single_instance():
    global _mutex
    _mutex = kernel32.CreateMutexW(None, False, "Global\\" + APP_ID)
    return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


# ---------- 开机自启 ----------
def _launch_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    script = os.path.abspath(sys.argv[0])
    return f'"{pyw}" "{script}"'


def set_autostart(enabled):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, APP_ID, 0, winreg.REG_SZ, _launch_command())
        else:
            try:
                winreg.DeleteValue(k, APP_ID)
            except FileNotFoundError:
                pass


# ---------- 鼠标穿透 ----------
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x80000
WS_EX_TRANSPARENT = 0x20
WS_EX_TOOLWINDOW = 0x80
WS_EX_APPWINDOW = 0x40000


def _hwnd(tk_window):
    return user32.GetParent(tk_window.winfo_id()) or tk_window.winfo_id()


def set_click_through(tk_window, enabled):
    hwnd = _hwnd(tk_window)
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    style |= WS_EX_LAYERED
    style = style | WS_EX_TRANSPARENT if enabled else style & ~WS_EX_TRANSPARENT
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)


def hide_from_taskbar(tk_window):
    hwnd = _hwnd(tk_window)
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)


# ---------- 全局热键 ----------
MODS = {"ctrl": 0x2, "control": 0x2, "alt": 0x1, "shift": 0x4, "win": 0x8}
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012


def parse_hotkey(text):
    """'Ctrl+Alt+H' -> (mods, vk)；无法解析返回 None。"""
    parts = [p.strip().lower() for p in text.split("+") if p.strip()]
    if not parts:
        return None
    mods, vk = 0, None
    for p in parts:
        if p in MODS:
            mods |= MODS[p]
        elif len(p) == 1 and p.isalnum():
            vk = ord(p.upper())
        elif p.startswith("f") and p[1:].isdigit() and 1 <= int(p[1:]) <= 24:
            vk = 0x6F + int(p[1:])
        elif p == "space":
            vk = 0x20
        else:
            return None
    return (mods, vk) if vk else None


class HotkeyThread(threading.Thread):
    def __init__(self, hotkey, callback):
        super().__init__(daemon=True)
        self.hotkey = hotkey
        self.callback = callback
        self.thread_id = None
        self.ok = threading.Event()
        self.failed = False

    def run(self):
        self.thread_id = kernel32.GetCurrentThreadId()
        parsed = parse_hotkey(self.hotkey)
        if not parsed or not user32.RegisterHotKey(None, 1, parsed[0] | MOD_NOREPEAT, parsed[1]):
            self.failed = True
            self.ok.set()
            return
        self.ok.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                self.callback()
        user32.UnregisterHotKey(None, 1)

    def stop(self):
        if self.thread_id and not self.failed:
            user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)
