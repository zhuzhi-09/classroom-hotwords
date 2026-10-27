"""配置读写。所有可调参数都在 DEFAULTS 里，管理页按这里的键生成表单。"""
import json
import os
import sys
import threading

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
    RES_DIR = getattr(sys, "_MEIPASS", BASE_DIR)
else:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    RES_DIR = BASE_DIR

DATA_DIR = os.path.join(BASE_DIR, "data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
DB_PATH = os.path.join(DATA_DIR, "records.db")
DEFAULT_MODELS_DIR = os.path.join(BASE_DIR, "models")
LEGACY_VOSK_DIR = os.path.join(BASE_DIR, "model")  # v1.0 的 Vosk 模型位置
WEB_DIR = os.path.join(RES_DIR, "web")

DEFAULT_STOPWORDS = """的 了 是 我 你 他 她 它 们 我们 你们 他们 她们 这 那 这个 那个 这些 那些 这样 那样 这里 那里
就 都 也 还 又 再 很 太 更 最 啊 吧 呢 吗 嘛 哦 噢 呀 哈 嗯 呃 额 唉 哎 喂 么 啦 咯
和 跟 与 及 或 而 但 但是 因为 所以 如果 然后 那么 就是 还是 或者 而且 不过 可是 只是
在 有 没 没有 不 不是 要 会 能 可以 去 来 说 看 想 让 给 把 被 对 到 从 向 为 以 于
一 一个 一下 一些 一点 什么 怎么 怎么样 为什么 哪 哪个 哪里 谁 多少 几 个 些 种 样
上 下 里 中 时候 现在 今天 已经 正在 自己 大家 东西 事情 知道 觉得 好 对吧 是不是 其实 反正 这么 那个 一样
进行 出来 起来 应该 可能 非常 比较 还有 以后 之后 同学 同学们 老师""".split()

DEFAULTS = {
    # 识别
    "asr_model": "sensevoice",  # 见 engine.MODELS
    "models_dir": "",           # 空 = 程序目录下 models
    "language": "zh",           # SenseVoice 识别语言：zh / auto
    "download_source": "auto",  # 模型下载源，见 engine.SOURCES
    "download_fallback": True,  # 选定的源失败时自动换其他源
    "device": None,             # 麦克风编号，None = 系统默认
    "start_listening": True,    # 启动后自动开始监听
    # 热词
    "top_n": 8,
    "window_minutes": 10,       # 0 = 本节课全部
    "min_count": 2,
    "min_word_len": 2,
    "stopwords": DEFAULT_STOPWORDS,
    "custom_words": [],         # 自定义词典（学科术语、同学名字等）
    # 悬浮条
    "overlay_visible": True,
    "alpha": 0.75,
    "width": 680,
    "height": 48,
    "font_size": 15,
    "font_family": "Microsoft YaHei UI",
    "bg_color": "#1e1e1e",
    "fg_color": "#ffffff",
    "accent_color": "#ffcc33",  # 前 3 名高亮
    "show_count": True,
    "show_partial": False,      # 底部显示正在识别的句子（会把原话投到屏幕上，默认关）
    "click_through": False,     # 鼠标穿透（锁定后不可拖动）
    "x": None,
    "y": None,
    # 系统
    "hotkey": "Ctrl+Alt+H",     # 显示/隐藏悬浮条
    "autostart": False,
    "web_port": 18765,
    "open_admin_on_start": False,
}


class Config:
    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        self.lock = threading.Lock()
        self.data = dict(DEFAULTS)
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            self.data.update({k: v for k, v in saved.items() if k in DEFAULTS})
        except (OSError, ValueError):
            pass

    def __getitem__(self, key):
        with self.lock:
            return self.data[key]

    def snapshot(self):
        with self.lock:
            return dict(self.data)

    def update(self, changes):
        """只接受已知键，按默认值类型做简单校验，返回实际改动的键。"""
        changed = []
        with self.lock:
            for k, v in changes.items():
                if k not in DEFAULTS:
                    continue
                v = self._coerce(k, v)
                if self.data.get(k) != v:
                    self.data[k] = v
                    changed.append(k)
            if changed:
                self._save()
        return changed

    @staticmethod
    def _coerce(key, v):
        default = DEFAULTS[key]
        if key == "device":
            return None if v in (None, "", "default") else int(v)
        if key in ("x", "y"):
            return None if v is None else int(v)
        if isinstance(default, bool):
            return bool(v)
        if isinstance(default, int):
            return int(v)
        if isinstance(default, float):
            return max(0.1, min(1.0, float(v))) if key == "alpha" else float(v)
        if isinstance(default, list):
            if isinstance(v, str):
                v = v.split()
            return [str(x).strip() for x in v if str(x).strip()]
        return str(v)

    def _save(self):
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)

    def models_dir(self):
        return self["models_dir"] or DEFAULT_MODELS_DIR
