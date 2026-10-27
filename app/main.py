"""程序入口：Tk 主线程 + 托盘 + 本地管理服务 + 识别引擎。"""
import os
import queue
import threading
import webbrowser
import tkinter as tk
from tkinter import messagebox

import pystray
from PIL import Image, ImageDraw

from . import winutil
from .config import DB_PATH, Config
from .engine import Engine
from .overlay import Overlay
from .storage import Storage
from .webserver import WebServer

STYLE_KEYS = {"alpha", "width", "height", "font_size", "font_family", "bg_color", "fg_color",
              "accent_color", "show_count", "show_partial", "click_through", "x", "y"}
DICT_KEYS = {"stopwords", "custom_words", "min_word_len"}


def tray_image(active):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = (52, 152, 219, 255) if active else (150, 150, 150, 255)
    d.rounded_rectangle((20, 4, 44, 40), radius=12, fill=color)
    d.arc((12, 18, 52, 50), start=0, end=180, fill=color, width=5)
    d.line((32, 50, 32, 58), fill=color, width=5)
    d.line((20, 59, 44, 59), fill=color, width=5)
    return img


class App:
    def __init__(self):
        self.cfg = Config()
        self.storage = Storage(DB_PATH)
        self.engine = Engine(self.cfg, self.storage)
        self.ui_q = queue.Queue()
        self.hotkey_thread = None

        self.root = tk.Tk()
        self.root.withdraw()
        self.overlay = Overlay(self.root, self)

        self.web = WebServer(self)
        self.web.start()

        self.tray = pystray.Icon("classroom_words", tray_image(False), "课堂热词", self._tray_menu())
        self.tray.run_detached()

        self._register_hotkey()
        self.engine.new_session()
        if self.cfg["start_listening"]:
            self.engine.start()
        if self.cfg["open_admin_on_start"]:
            self.open_admin()

        self.root.after(100, self._poll)
        self.root.after(500, self._tick)

    # ---------- 线程间调度 ----------
    def call_ui(self, fn):
        """其他线程（托盘/热键/网页）要动 Tk 时都走这里。"""
        self.ui_q.put(fn)

    def _poll(self):
        try:
            while True:
                self.ui_q.get_nowait()()
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _tick(self):
        self.overlay.refresh()
        active = self.engine.listening
        if getattr(self, "_tray_active", None) != active:
            self._tray_active = active
            self.tray.icon = tray_image(active)
            self.tray.update_menu()
        self.root.after(500, self._tick)

    # ---------- 动作 ----------
    def toggle_overlay(self):
        self.apply_config({"overlay_visible": not self.cfg["overlay_visible"]})

    def toggle_listening(self):
        if self.engine.listening:
            threading.Thread(target=self.engine.stop, daemon=True).start()
        else:
            self.engine.start()

    def new_session(self, name=None):
        return self.engine.new_session(name)

    def open_admin(self):
        webbrowser.open(self.web.url)

    def apply_config(self, changes):
        """统一的配置入口：保存并让改动立即生效。可从任何线程调用。"""
        changed = set(self.cfg.update(changes))
        if not changed:
            return []
        if changed & DICT_KEYS:
            self.engine.reload_dictionary()
        if changed & {"device", "model_dir"} and (self.engine.listening or self.engine.error):
            threading.Thread(target=self.engine.restart, daemon=True).start()
        if "hotkey" in changed:
            self._register_hotkey()
        if "autostart" in changed:
            try:
                winutil.set_autostart(self.cfg["autostart"])
            except OSError as e:
                self.cfg.update({"autostart": not self.cfg["autostart"]})
                raise RuntimeError(f"设置开机自启失败：{e}")
        if changed & STYLE_KEYS:
            self.call_ui(self.overlay.apply_style)
        if "overlay_visible" in changed:
            self.call_ui(lambda: self.overlay.set_visible(self.cfg["overlay_visible"]))
        self.tray.update_menu()
        return sorted(changed)

    def _register_hotkey(self):
        if self.hotkey_thread:
            self.hotkey_thread.stop()
        self.hotkey_thread = winutil.HotkeyThread(self.cfg["hotkey"],
                                                  lambda: self.call_ui(self.toggle_overlay))
        self.hotkey_thread.start()
        self.hotkey_thread.ok.wait(1)

    def hotkey_ok(self):
        return bool(self.hotkey_thread) and not self.hotkey_thread.failed

    # ---------- 托盘 ----------
    def _tray_menu(self):
        M = pystray.MenuItem

        def scope(minutes, label):
            return M(label, lambda: self.apply_config({"window_minutes": minutes}),
                     checked=lambda i: self.cfg["window_minutes"] == minutes, radio=True)

        return pystray.Menu(
            M("打开管理页", lambda: self.open_admin(), default=True),
            M(lambda i: f"显示悬浮条（{self.cfg['hotkey']}）",
              lambda: self.call_ui(self.toggle_overlay), checked=lambda i: self.cfg["overlay_visible"]),
            M("监听麦克风", lambda: self.call_ui(self.toggle_listening),
              checked=lambda i: self.engine.listening),
            M("鼠标穿透（锁定悬浮条）", lambda: self.apply_config({"click_through": not self.cfg["click_through"]}),
              checked=lambda i: self.cfg["click_through"]),
            M("统计范围", pystray.Menu(scope(5, "最近 5 分钟"), scope(10, "最近 10 分钟"),
                                   scope(30, "最近 30 分钟"), scope(0, "本节课全部"))),
            M("开始新课堂", lambda: self.new_session()),
            pystray.Menu.SEPARATOR,
            M("退出", lambda: self.call_ui(self.quit)),
        )

    def quit(self):
        self.engine.stop_flag.set()
        self.storage.end_open_sessions()
        if self.hotkey_thread:
            self.hotkey_thread.stop()
        self.web.stop()
        self.tray.stop()
        self.root.destroy()
        os._exit(0)

    def run(self):
        self.root.mainloop()


def main():
    winutil.enable_dpi_awareness()
    if not winutil.acquire_single_instance():
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("课堂热词", "程序已经在运行了，请在任务栏右下角托盘里找到麦克风图标。")
        return
    App().run()
