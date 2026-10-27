"""对每个下载源各做一次完整的 SenseVoice 下载，确认能装好、文件校验通过，并打印每个文件的实际来源。

用法：py -3.12 tools/test_download_sources.py [源 ...]   （默认 modelscope hf-mirror huggingface）
每个源大约下载 240MB。
"""
import os
import shutil
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from app import config  # noqa: E402

tmp = tempfile.mkdtemp(prefix="classroom_dl_")
config.DATA_DIR, config.CONFIG_PATH = tmp, os.path.join(tmp, "config.json")

from app.engine import Engine  # noqa: E402
from app.storage import Storage  # noqa: E402

for source in sys.argv[1:] or ["modelscope", "hf-mirror", "huggingface"]:
    cfg = config.Config()
    models = os.path.join(tmp, "models-" + source)
    cfg.update({"models_dir": models, "download_source": source, "download_fallback": False})
    eng = Engine(cfg, Storage(os.path.join(tmp, source + ".db")))
    seen = []

    def watch():
        while eng.download["state"] in ("idle", "running"):
            m = eng.download["message"]
            if "来源" in m and (not seen or seen[-1] != m):
                seen.append(m)
            time.sleep(0.1)
    threading.Thread(target=watch, daemon=True).start()
    t = time.time()
    eng._download("sensevoice", None)
    dt = time.time() - t
    time.sleep(0.3)
    ok = eng.model_ready("sensevoice")
    print(f"[{source}] {'成功' if ok else '失败'}  {dt:.0f}s  {eng.download['message']}")
    for m in seen:
        print("    ", m.rsplit("（", 1)[1].rstrip("）"))
    shutil.rmtree(models, ignore_errors=True)
