"""屏幕顶部半透明热词悬浮条（Tk，必须在主线程里使用）。"""
import tkinter as tk

from . import winutil


class Overlay:
    def __init__(self, root, app):
        self.app = app
        self.cfg = app.cfg
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.words_frame = tk.Frame(self.win)
        self.words_frame.pack(fill="both", expand=True, padx=10)
        self.status = tk.Label(self.win, anchor="w")
        self.status.pack(fill="x", padx=10, pady=(0, 2))
        self._labels = []
        self._last = None

        self.menu = tk.Menu(self.win, tearoff=0)
        self.menu.add_command(label="打开管理页", command=app.open_admin)
        self.menu.add_command(label="暂停 / 继续监听", command=app.toggle_listening)
        self.menu.add_command(label="开始新课堂", command=app.new_session)
        self.menu.add_command(label="清空当前热词", command=app.engine.clear_window)
        self.menu.add_command(label="锁定位置（鼠标穿透）", command=lambda: app.apply_config({"click_through": True}))
        self.menu.add_command(label="恢复到屏幕顶部居中", command=self.reset_position)
        self.menu.add_separator()
        self.menu.add_command(label="隐藏悬浮条", command=app.toggle_overlay)
        self.menu.add_command(label="退出", command=app.quit)

        self._bind(self.win)
        self._bind(self.words_frame)
        self._bind(self.status)
        self.win.update_idletasks()
        winutil.hide_from_taskbar(self.win)
        self.apply_style()
        self.set_visible(self.cfg["overlay_visible"])

    def _bind(self, w):
        w.bind("<ButtonPress-1>", self._drag_start)
        w.bind("<B1-Motion>", self._drag_move)
        w.bind("<ButtonRelease-1>", self._drag_end)
        w.bind("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))

    def _drag_start(self, e):
        self._dx, self._dy = e.x_root - self.win.winfo_x(), e.y_root - self.win.winfo_y()

    def _drag_move(self, e):
        self.win.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _drag_end(self, e):
        self.app.apply_config({"x": self.win.winfo_x(), "y": self.win.winfo_y()})

    def reset_position(self):
        self.app.apply_config({"x": None, "y": None})

    def apply_style(self):
        c = self.cfg.snapshot()
        w, h = c["width"], c["height"]
        x = c["x"] if c["x"] is not None else (self.win.winfo_screenwidth() - w) // 2
        y = c["y"] if c["y"] is not None else 4
        self.win.geometry(f"{w}x{h}+{x}+{y}")
        self.win.attributes("-alpha", c["alpha"])
        for widget in (self.win, self.words_frame):
            widget.configure(bg=c["bg_color"])
        self.status.configure(bg=c["bg_color"], fg="#9a9a9a",
                              font=(c["font_family"], max(7, c["font_size"] // 2)))
        if c["show_partial"]:
            self.status.pack(fill="x", padx=10, pady=(0, 2))
        else:
            self.status.pack_forget()
        winutil.set_click_through(self.win, c["click_through"])
        self._last = None
        self.refresh()

    def set_visible(self, visible):
        if visible:
            self.win.deiconify()
            self.win.attributes("-topmost", True)
            winutil.set_click_through(self.win, self.cfg["click_through"])
        else:
            self.win.withdraw()

    def refresh(self):
        c = self.cfg.snapshot()
        eng = self.app.engine
        top = eng.top_words()
        if not top:
            items = [("等待热词…" if eng.listening else eng.status, None)]
        else:
            items = top
        key = (tuple(items), c["show_count"])
        if key != self._last:
            self._last = key
            for lbl in self._labels:
                lbl.destroy()
            self._labels = []
            inner = tk.Frame(self.words_frame, bg=c["bg_color"])
            inner.place(relx=0.5, rely=0.5, anchor="center")
            self._labels.append(inner)
            self._bind(inner)
            for i, (word, count) in enumerate(items):
                text = f"{word}×{count}" if count and c["show_count"] else word
                color = c["accent_color"] if count and i < 3 else c["fg_color"]
                lbl = tk.Label(inner, text=text, bg=c["bg_color"], fg=color,
                               font=(c["font_family"], c["font_size"], "bold" if count else "normal"))
                lbl.pack(side="left", padx=8)
                self._bind(lbl)
        scope = f"近{c['window_minutes']}分钟" if c["window_minutes"] else "本节课"
        partial = f"  ▸ {eng.partial[-40:]}" if eng.partial else ""
        self.status.configure(text=f"{eng.status} · {scope}{partial}")
