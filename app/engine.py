"""识别引擎：麦克风 -> Vosk -> jieba -> 存储 + 实时热词统计。"""
import array
import json
import math
import os
import queue
import shutil
import threading
import time
import urllib.request
import zipfile
from collections import Counter, deque

import jieba
import sounddevice as sd
from vosk import KaldiRecognizer, Model, SetLogLevel

SetLogLevel(-1)

MODELS = {
    "small": {"name": "中文小模型 vosk-model-small-cn-0.22", "size": "42MB",
              "url": "https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip"},
    "large": {"name": "中文大模型 vosk-model-cn-0.22（更准，占内存约 3GB）", "size": "1.3GB",
              "url": "https://alphacephei.com/vosk/models/vosk-model-cn-0.22.zip"},
}


def list_input_devices():
    out = []
    try:
        default_in = sd.default.device[0]
        hostapis = sd.query_hostapis()
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                out.append({"id": i, "name": d["name"], "api": hostapis[d["hostapi"]]["name"],
                            "default": i == default_in})
    except Exception:
        pass
    return out


def model_ready(path):
    return os.path.isdir(os.path.join(path, "am")) or os.path.isfile(os.path.join(path, "conf", "model.conf"))


class Engine:
    def __init__(self, config, storage):
        self.cfg = config
        self.storage = storage
        self.model = None
        self.loaded_model_dir = None
        self.thread = None
        self.stop_flag = threading.Event()
        self.listening = False
        self.status = "未启动"
        self.error = ""
        self.partial = ""
        self.level = 0.0               # 麦克风音量 0~1
        self.recent = deque(maxlen=50)  # 最近的句子
        self.events = deque()           # (ts, token) 本节课，用于时间窗口统计
        self.lock = threading.Lock()
        self.session_id = None
        self.download = {"state": "idle", "progress": 0, "message": ""}
        self.reload_dictionary()

    # ---------- 分词 ----------
    def reload_dictionary(self):
        jieba.initialize()
        words = set(self.cfg["custom_words"])
        for w in getattr(self, "_custom", set()) - words:
            jieba.del_word(w)
        for w in words:
            jieba.add_word(w, freq=100000)
        self._custom = words
        self._stop = set(self.cfg["stopwords"])
        self._minlen = self.cfg["min_word_len"]

    def keep(self, w):
        if len(w) < self._minlen or w in self._stop:
            return False
        return any("一" <= ch <= "鿿" or ch.isalnum() for ch in w)

    @staticmethod
    def segment(text):
        return [w for w in (t.strip() for t in jieba.lcut(text)) if w]

    # ---------- 课堂 ----------
    def new_session(self, name=None):
        self.session_id = self.storage.new_session(name)
        with self.lock:
            self.events.clear()
            self.recent.clear()
        return self.session_id

    def clear_window(self):
        with self.lock:
            self.events.clear()

    def top_words(self, n=None, window=None):
        n = n or self.cfg["top_n"]
        window = self.cfg["window_minutes"] if window is None else window
        cutoff = time.time() - window * 60 if window else 0
        with self.lock:
            c = Counter(w for t, w in self.events if t >= cutoff and self.keep(w))
        return [(w, k) for w, k in c.most_common(n) if k >= self.cfg["min_count"]]

    # ---------- 监听 ----------
    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_flag.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_flag.set()
        if self.thread:
            self.thread.join(timeout=3)
        self.listening = False
        self.partial = ""
        self.level = 0.0
        if not self.error:
            self.status = "已暂停"

    def restart(self):
        self.stop()
        self.start()

    def _load_model(self):
        path = self.cfg.model_dir()
        if self.model is not None and self.loaded_model_dir == path:
            return True
        if not model_ready(path):
            self.status = "未安装语音模型"
            self.error = "请在管理页「模型」中下载模型，或在设置里指定模型目录"
            return False
        self.status = "加载模型中…"
        self.model = Model(path)
        self.loaded_model_dir = path
        return True

    def _run(self):
        self.error = ""
        try:
            if not self._load_model():
                return
            audio_q = queue.Queue(maxsize=200)

            def callback(indata, frames, t, status):
                samples = array.array("h", bytes(indata))
                if samples:
                    rms = math.sqrt(sum(s * s for s in samples[::8]) / len(samples[::8]))
                    self.level = min(1.0, rms / 6000)
                try:
                    audio_q.put_nowait(bytes(indata))
                except queue.Full:
                    pass

            rec = KaldiRecognizer(self.model, 16000)
            with sd.RawInputStream(samplerate=16000, blocksize=4000, dtype="int16", channels=1,
                                   device=self.cfg["device"], callback=callback):
                self.listening = True
                self.status = "监听中"
                while not self.stop_flag.is_set():
                    try:
                        data = audio_q.get(timeout=0.3)
                    except queue.Empty:
                        continue
                    if rec.AcceptWaveform(data):
                        text = json.loads(rec.Result()).get("text", "").replace(" ", "")
                        self.partial = ""
                        if text:
                            self._handle_text(text)
                    else:
                        self.partial = json.loads(rec.PartialResult()).get("partial", "").replace(" ", "")
                text = json.loads(rec.FinalResult()).get("text", "").replace(" ", "")
                if text:
                    self._handle_text(text)
        except Exception as e:
            self.status = "出错"
            self.error = str(e)
        finally:
            self.listening = False

    def _handle_text(self, text):
        tokens = self.segment(text)
        now = time.time()
        if self.session_id is None:
            self.new_session()
        self.storage.add_utterance(self.session_id, text, tokens)
        with self.lock:
            for w in tokens:
                self.events.append((now, w))
            keep_after = now - 4 * 3600
            while self.events and self.events[0][0] < keep_after:
                self.events.popleft()
            self.recent.appendleft({"ts": now, "text": text,
                                    "words": [w for w in tokens if self.keep(w)]})

    def state(self):
        with self.lock:
            recent = list(self.recent)[:20]
        return {"listening": self.listening, "status": self.status, "error": self.error,
                "partial": self.partial, "level": round(self.level, 3),
                "session_id": self.session_id, "recent": recent,
                "top": self.top_words(), "model_ready": model_ready(self.cfg.model_dir()),
                "model_dir": self.cfg.model_dir()}

    # ---------- 模型下载 ----------
    def download_model(self, key):
        if self.download["state"] == "running" or key not in MODELS:
            return False
        threading.Thread(target=self._download, args=(key,), daemon=True).start()
        return True

    def _download(self, key):
        info = MODELS[key]
        target = self.cfg.model_dir()
        tmp_zip = target.rstrip("\\/") + ".download.zip"
        tmp_dir = target.rstrip("\\/") + ".extract"
        d = self.download
        d.update(state="running", progress=0, message="下载中：" + info["name"])
        try:
            with urllib.request.urlopen(info["url"], timeout=30) as r, open(tmp_zip, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                done = 0
                while chunk := r.read(1 << 16):
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        d["progress"] = round(done * 100 / total, 1)
            d["message"] = "解压中…"
            shutil.rmtree(tmp_dir, ignore_errors=True)
            with zipfile.ZipFile(tmp_zip) as z:
                z.extractall(tmp_dir)
            inner = [os.path.join(tmp_dir, n) for n in os.listdir(tmp_dir)]
            src = inner[0] if len(inner) == 1 and os.path.isdir(inner[0]) else tmp_dir
            was_listening = self.listening
            self.stop()
            self.model = None
            shutil.rmtree(target, ignore_errors=True)
            shutil.move(src, target)
            d.update(state="done", progress=100, message="模型已安装")
            if was_listening or self.cfg["start_listening"]:
                self.start()
        except Exception as e:
            d.update(state="error", message=f"下载失败：{e}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            if os.path.exists(tmp_zip):
                os.remove(tmp_zip)
