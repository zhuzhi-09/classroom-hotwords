"""识别引擎：麦克风 -> 识别后端(SenseVoice / Vosk) -> jieba -> 存储 + 实时热词统计。"""
import array
import hashlib
import math
import os
import queue
import shutil
import tarfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from collections import Counter, deque

import jieba
import sounddevice as sd

from .backends import SenseVoiceBackend, VoskBackend
from .config import LEGACY_VOSK_DIR

def _ms(repo, path):
    return f"https://modelscope.cn/models/{repo}/resolve/master/{path}"


def _hf(repo, path, host="huggingface.co"):
    return f"https://{host}/{repo}/resolve/main/{path}"


SV_REPO = "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"
GH = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"

# 每个文件按顺序尝试多个下载源：魔搭（国内快）→ hf-mirror（国内）→ HuggingFace → GitHub。
# 魔搭上是第三方转存，所以必须校验 sha256（取自 k2-fsa 官方发布的文件），不一致就换下一个源。
MODELS = {
    "sensevoice": {
        "name": "SenseVoice-Small（推荐）", "engine": "sensevoice", "size": "230MB",
        "desc": "阿里 FunASR 出品，中文准确率高、速度快，抗噪声能力强。说完一句识别一句。有国内下载源",
        "files": ["model.int8.onnx", "tokens.txt", "silero_vad.onnx"],
        "downloads": [
            {"save": "model.int8.onnx",
             "bytes": 237115547,
             "sha256": "12ca1a2ae7ecf3e0019ef2822307ee0b5cadc9196569e379b4c4026f8205276d",
             "urls": [_ms("Mr7Cat/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09", "model.int8.onnx"),
                      _ms("aistoy/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09", "model.int8.onnx"),
                      _hf(SV_REPO, "model.int8.onnx", "hf-mirror.com"),
                      _hf(SV_REPO, "model.int8.onnx")]},
            {"save": "tokens.txt",
             "bytes": 315894,
             "sha256": "f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc",
             "urls": [_ms("Mr7Cat/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09", "tokens.txt"),
                      _ms("aistoy/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09", "tokens.txt"),
                      _hf(SV_REPO, "tokens.txt", "hf-mirror.com"),
                      _hf(SV_REPO, "tokens.txt")]},
            {"save": "silero_vad.onnx",
             "bytes": 643854,
             "sha256": "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6",
             "urls": [_ms("xnnehang/k2-fsa-silero-vad", "silero_vad.onnx"),
                      _ms("liaowenbin/silero_vad", "silero_vad.onnx"),
                      _ms("WEAAEW/silero-vad-onnx", "v4-sherpa/silero_vad.onnx"),
                      GH + "silero_vad.onnx"]},
        ],
    },
    "vosk-small": {
        "name": "Vosk 中文小模型", "engine": "vosk", "size": "42MB",
        "desc": "最轻，可以边说边出字，但准确率明显不如 SenseVoice。从国外官网下载，国内可能较慢",
        "files": ["conf/model.conf"],
        "downloads": [{"urls": ["https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip"]}],
    },
    "vosk-large": {
        "name": "Vosk 中文大模型", "engine": "vosk", "size": "1.3GB",
        "desc": "比 Vosk 小模型准，但占内存约 3GB、加载慢。从国外官网下载，国内可能较慢",
        "files": ["conf/model.conf"],
        "downloads": [{"urls": ["https://alphacephei.com/vosk/models/vosk-model-cn-0.22.zip"]}],
    },
}
CONNECT_TIMEOUT = 30     # 秒：连不上或长时间收不到数据就换源
STALL_SECONDS = 20       # 连续这么久平均速度低于 MIN_SPEED 就换源
MIN_SPEED = 30 * 1024    # 字节/秒
SAMPLE_RATE = 16000


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


class Engine:
    def __init__(self, config, storage):
        self.cfg = config
        self.storage = storage
        self.backend = None
        self.backend_key = None
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
        self.download = {"state": "idle", "progress": 0, "message": "", "model": None}
        self.reload_dictionary()

    # ---------- 模型 ----------
    def model_path(self, key):
        path = os.path.join(self.cfg.models_dir(), key)
        if key == "vosk-small" and not self._has_files(key, path) and self._has_files(key, LEGACY_VOSK_DIR):
            return LEGACY_VOSK_DIR
        return path

    @staticmethod
    def _has_files(key, path):
        return all(os.path.isfile(os.path.join(path, f)) for f in MODELS[key]["files"])

    def model_ready(self, key=None):
        key = key or self.cfg["asr_model"]
        return key in MODELS and self._has_files(key, self.model_path(key))

    def models(self):
        active = self.cfg["asr_model"]
        return [dict(key=k, active=k == active, installed=self.model_ready(k), path=self.model_path(k),
                     **{f: m[f] for f in ("name", "engine", "size", "desc")})
                for k, m in MODELS.items()]

    def _load_backend(self):
        key = self.cfg["asr_model"]
        lang = self.cfg["language"]
        if self.backend is not None and self.backend_key == (key, lang):
            return True
        if key not in MODELS or not self.model_ready(key):
            self.status = "未安装语音模型"
            self.error = "请在管理页「语音模型」中下载并选用一个模型"
            return False
        self.status = "加载模型中…"
        self.backend = None
        path = self.model_path(key)
        if MODELS[key]["engine"] == "sensevoice":
            self.backend = SenseVoiceBackend(path, SAMPLE_RATE, lang)
        else:
            self.backend = VoskBackend(path, SAMPLE_RATE)
        self.backend_key = (key, lang)
        return True

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
            self.thread.join(timeout=5)
        self.listening = False
        self.partial = ""
        self.level = 0.0
        if not self.error:
            self.status = "已暂停"

    def restart(self):
        self.stop()
        self.start()

    def _run(self):
        self.error = ""
        try:
            if not self._load_backend():
                return
            audio_q = queue.Queue(maxsize=400)

            def callback(indata, frames, t, status):
                samples = array.array("h", bytes(indata))
                if samples:
                    sub = samples[::8]
                    self.level = min(1.0, math.sqrt(sum(s * s for s in sub) / len(sub)) / 6000)
                try:
                    audio_q.put_nowait(bytes(indata))
                except queue.Full:
                    pass

            backend = self.backend
            backend.reset()
            with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=1600, dtype="int16", channels=1,
                                   device=self.cfg["device"], callback=callback):
                self.listening = True
                self.status = "监听中"
                while not self.stop_flag.is_set():
                    try:
                        data = audio_q.get(timeout=0.3)
                    except queue.Empty:
                        continue
                    for text in backend.accept(data):
                        self._handle_text(text)
                    self.partial = backend.partial
                for text in backend.flush():
                    self._handle_text(text)
        except Exception as e:
            self.status = "出错"
            self.error = str(e)
        finally:
            self.listening = False

    def _handle_text(self, text):
        text = text.strip()
        if not text:
            return
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
        key = self.cfg["asr_model"]
        return {"listening": self.listening, "status": self.status, "error": self.error,
                "partial": self.partial, "level": round(self.level, 3),
                "session_id": self.session_id, "recent": recent, "top": self.top_words(),
                "model_ready": self.model_ready(), "model_name": MODELS.get(key, {}).get("name", key)}

    # ---------- 模型下载 / 删除 ----------
    def download_model(self, key, on_done=None):
        if self.download["state"] == "running" or key not in MODELS:
            return False
        threading.Thread(target=self._download, args=(key, on_done), daemon=True).start()
        return True

    def _download(self, key, on_done):
        info = MODELS[key]
        target = os.path.join(self.cfg.models_dir(), key)
        work = target + ".download"
        d = self.download
        d.update(state="running", progress=0, message="下载中：" + info["name"], model=key)
        try:
            shutil.rmtree(work, ignore_errors=True)
            os.makedirs(work)
            items = info["downloads"]
            weights = [it.get("bytes", 1) for it in items]
            for i, item in enumerate(items):
                fname = os.path.join(work, item.get("save") or item["urls"][0].rsplit("/", 1)[-1])
                errors = []
                for url in item["urls"]:
                    host = urllib.parse.urlparse(url).hostname
                    d["message"] = f"下载中：{info['name']}（{i + 1}/{len(items)}，来源 {host}）"

                    def progress(frac):
                        d["progress"] = round((sum(weights[:i]) + frac * weights[i]) * 100 / sum(weights), 1)
                    try:
                        self._fetch(url, fname, item.get("sha256"), progress)
                        break
                    except Exception as e:
                        errors.append(f"{host}: {e}")
                else:
                    raise RuntimeError("所有下载源都失败了 —— " + "；".join(errors))
                if fname.endswith((".zip", ".tar.bz2")):
                    d["message"] = "解压中…"
                    self._extract(fname, work)
                    os.remove(fname)
            if not self._has_files(key, work):
                raise RuntimeError("下载的文件不完整")
            if self.backend_key and self.backend_key[0] == key:
                self.stop()
                self.backend = self.backend_key = None
            shutil.rmtree(target, ignore_errors=True)
            os.replace(work, target)
            d.update(state="done", progress=100, message=info["name"] + " 已安装")
            if on_done:
                on_done(key)
        except Exception as e:
            d.update(state="error", message=f"下载失败：{e}")
        finally:
            shutil.rmtree(work, ignore_errors=True)

    @staticmethod
    def _fetch(url, path, sha256, progress):
        """下载单个文件；连不上、太慢或校验不通过都抛异常，由调用方换下一个源。"""
        req = urllib.request.Request(url, headers={"User-Agent": "ClassroomWords/1.2"})
        h = hashlib.sha256()
        with urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT) as r, open(path, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done, window_start, window_bytes = 0, time.monotonic(), 0
            while chunk := r.read(1 << 16):
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                window_bytes += len(chunk)
                if total:
                    progress(done / total)
                elapsed = time.monotonic() - window_start
                if elapsed >= STALL_SECONDS:
                    if window_bytes / elapsed < MIN_SPEED:
                        raise RuntimeError(f"速度太慢（{window_bytes / elapsed / 1024:.0f}KB/s）")
                    window_start, window_bytes = time.monotonic(), 0
        if total and done != total:
            raise RuntimeError("下载不完整")
        if sha256 and h.hexdigest() != sha256:
            raise RuntimeError("文件校验不一致")

    @staticmethod
    def _extract(archive, dest):
        """解压到 dest；压缩包里只有一层目录时把里面的内容提上来。"""
        tmp = archive + ".x"
        if archive.endswith(".zip"):
            with zipfile.ZipFile(archive) as z:
                z.extractall(tmp)
        else:
            with tarfile.open(archive, "r:bz2") as t:
                t.extractall(tmp, filter="data")
        entries = os.listdir(tmp)
        src = os.path.join(tmp, entries[0]) if len(entries) == 1 and os.path.isdir(os.path.join(tmp, entries[0])) else tmp
        for name in os.listdir(src):
            if name == "test_wavs" or name.endswith(".onnx") and name.startswith("model.") and "int8" not in name:
                continue  # 示例音频和非量化模型用不到
            shutil.move(os.path.join(src, name), os.path.join(dest, name))
        shutil.rmtree(tmp, ignore_errors=True)

    def delete_model(self, key):
        if key not in MODELS:
            raise ValueError("模型名称无效")
        if key == self.cfg["asr_model"]:
            raise ValueError("不能删除正在使用的模型，请先切换到其他模型")
        path = self.model_path(key)
        if self.backend_key and self.backend_key[0] == key:
            self.backend = self.backend_key = None
        shutil.rmtree(path, ignore_errors=True)
