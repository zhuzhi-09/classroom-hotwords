"""生成 README 用的截图（docs/*.png）。

用临时数据目录和演示数据启动程序，不碰真实记录，也不打开麦克风：
悬浮条用 ImageGrab 截取，管理页用无头 Edge 截取。
用法：py -3.12 tools/screenshots.py
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DOCS = os.path.join(ROOT, "docs")
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

from app import config  # noqa: E402

tmp = tempfile.mkdtemp(prefix="classroom_shots_")
config.DATA_DIR = tmp
config.CONFIG_PATH = os.path.join(tmp, "config.json")
config.DB_PATH = os.path.join(tmp, "records.db")
with open(config.CONFIG_PATH, "w", encoding="utf-8") as f:
    json.dump({"start_listening": False, "alpha": 1.0, "show_partial": True, "web_port": 18799,
               "custom_words": ["光合作用", "叶绿体", "二氧化碳"]}, f)

from PIL import ImageGrab  # noqa: E402

from app import main as main_mod, winutil  # noqa: E402

LESSON = [
    "今天我们来学习光合作用", "光合作用需要阳光和二氧化碳", "叶绿体是光合作用的场所",
    "老师这个实验什么时候做", "二氧化碳和水在叶绿体里生成葡萄糖", "叶绿体里面有叶绿素",
    "光合作用会释放氧气", "实验要用到淀粉和碘液", "为什么叶子是绿色的", "因为叶绿素吸收红光和蓝光",
    "没有阳光植物还能进行光合作用吗", "晚上植物只进行呼吸作用", "呼吸作用会消耗氧气",
    "这个实验要把叶片放在黑暗里一天", "滴上碘液看有没有变蓝", "变蓝说明有淀粉",
]
OLD = [("七年级二班 · 数学", ["勾股定理怎么证明", "直角三角形的两条直角边", "勾股定理的逆定理"] * 4),
       ("七年级二班 · 语文", ["这首诗的作者是谁", "诗人想表达思乡之情", "月亮在古诗里代表思乡"] * 4)]


def seed(app):
    st, eng = app.storage, app.engine
    for name, lines in OLD:
        sid = st.new_session(name)
        for line in lines:
            st.add_utterance(sid, line, eng.segment(line))
    app.new_session("七年级二班 · 生物")
    for line in LESSON * 2:
        eng._handle_text(line)
    eng.listening, eng.status, eng.level = True, "监听中", 0.42
    eng.partial = "滴上碘液看有没有变"


def edge_shot(url, out, w=1280, h=860):
    subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    "--force-color-profile=srgb", "--force-device-scale-factor=1",
                    f"--window-size={w},{h}", "--virtual-time-budget=4000",
                    f"--screenshot={out}", url], check=True, timeout=60,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_shots(app):
    try:
        time.sleep(1.5)
        ov = app.overlay.win
        x, y = ov.winfo_rootx(), ov.winfo_rooty()
        ImageGrab.grab((x, y, x + ov.winfo_width(), y + ov.winfo_height())).save(
            os.path.join(DOCS, "overlay.png"))
        for page, h in [("live", 760), ("records", 900), ("dict", 760), ("overlay", 900),
                        ("settings", 1100), ("model", 520)]:
            edge_shot(app.web.url + "#" + page, os.path.join(DOCS, f"admin-{page}.png"), h=h)
            print("ok", page)
    finally:
        app.call_ui(app.quit)


if __name__ == "__main__":
    os.makedirs(DOCS, exist_ok=True)
    winutil.enable_dpi_awareness()
    app = main_mod.App()
    # 截图里展示默认值；此时引擎没启动，不会真的打开麦克风
    app.cfg.update({"start_listening": True, "web_port": 18765})
    seed(app)
    threading.Thread(target=run_shots, args=(app,), daemon=True).start()
    app.run()
