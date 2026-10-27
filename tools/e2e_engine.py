"""端到端测试识别引擎：用 wav 文件代替麦克风，跑完整流程（识别→分词→入库→热词统计）。

用法：py -3.12 tools/e2e_engine.py [模型key] [wav]
"""
import os
import sys
import tempfile
import threading
import time
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from app import config  # noqa: E402

tmp = tempfile.mkdtemp(prefix="classroom_e2e_")
config.DATA_DIR, config.CONFIG_PATH = tmp, os.path.join(tmp, "config.json")

import sounddevice as sd  # noqa: E402

from app import engine as engine_mod  # noqa: E402
from app.storage import Storage  # noqa: E402

key = sys.argv[1] if len(sys.argv) > 1 else "sensevoice"
wav = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "tools", "testdata", "lesson_clean.wav")
with wave.open(wav) as w:
    PCM = w.readframes(w.getnframes()) + b"\0" * 32000


class FakeStream:
    """按真实速度的 10 倍把 wav 推给回调，接口与 sd.RawInputStream 一致。"""

    def __init__(self, samplerate, blocksize, dtype, channels, device, callback):
        self.cb, self.block = callback, blocksize * 2
        self.done = threading.Event()

    def __enter__(self):
        def pump():
            for i in range(0, len(PCM), self.block):
                self.cb(PCM[i:i + self.block], self.block // 2, None, None)
                time.sleep(self.block / 2 / 16000 / 10)
            self.done.set()
        threading.Thread(target=pump, daemon=True).start()
        FakeStream.current = self
        return self

    def __exit__(self, *a):
        pass


sd.RawInputStream = FakeStream
cfg = config.Config()
cfg.update({"asr_model": key, "models_dir": os.path.join(ROOT, "models"), "custom_words": ["光合作用", "叶绿体"]})
eng = engine_mod.Engine(cfg, Storage(os.path.join(tmp, "records.db")))
eng.new_session("e2e")
eng.start()
for _ in range(200):
    time.sleep(0.1)
    if getattr(FakeStream, "current", None) and FakeStream.current.done.is_set():
        break
    if eng.error:
        break
time.sleep(1)
eng.stop()
s = eng.state()
print("状态:", s["status"], s["error"])
print("句子数:", len(s["recent"]))
for u in reversed(s["recent"]):
    print("  >", u["text"], "|", " ".join(u["words"]))
print("热词:", s["top"])
print("入库:", eng.storage.summary())
