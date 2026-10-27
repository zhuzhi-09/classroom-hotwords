"""对比识别后端的准确率、速度和内存。

用法：py -3.12 tools/compare_asr.py <模型key> <wav> [参考文本.txt] [--noise 信噪比dB]
每个后端单独起一个进程跑，内存数字才互不干扰。
音频按 0.1 秒一块喂给后端，和真实麦克风的流程一样（包括 VAD 断句）。
"""
import ctypes
import ctypes.wintypes as wt
import os
import re
import sys
import time
import wave

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from app.backends import SenseVoiceBackend, VoskBackend  # noqa: E402

PUNCT = re.compile(r"[\s，。！？、,.!?；;：:“”\"'（）()…-]")


def peak_mem_mb():
    class PMC(ctypes.Structure):
        _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t)] + [(f"f{i}", ctypes.c_size_t) for i in range(6)]
    c = PMC()
    c.cb = ctypes.sizeof(PMC)
    fn = ctypes.windll.psapi.GetProcessMemoryInfo
    fn.argtypes = [wt.HANDLE, ctypes.POINTER(PMC), wt.DWORD]
    fn(wt.HANDLE(-1), ctypes.byref(c), c.cb)
    return c.PeakWorkingSetSize / 1048576


def cer(ref, hyp):
    """字错误率：编辑距离 / 参考字数"""
    r, h = PUNCT.sub("", ref), PUNCT.sub("", hyp)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
    return d[len(h)] / max(1, len(r))


def main():
    key, wav_path = sys.argv[1], sys.argv[2]
    ref = None
    args = sys.argv[3:]
    snr = None
    if "--noise" in args:
        i = args.index("--noise")
        snr = float(args[i + 1])
        args = args[:i] + args[i + 2:]
    if args:
        with open(args[0], encoding="utf-8") as f:
            ref = "".join(f.read().split())

    with wave.open(wav_path) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
    audio_sec = len(pcm) / 16000
    # 结尾补 1 秒静音，让最后一句也能被 VAD 断出来
    pcm = np.concatenate([pcm, np.zeros(16000, dtype=np.float32)])
    if snr is not None:
        rng = np.random.default_rng(0)
        # 粉红噪声 + 低频嗡嗡声，比白噪声更接近教室环境
        white = rng.standard_normal(len(pcm))
        pink = np.cumsum(white)
        pink -= np.convolve(pink, np.ones(400) / 400, mode="same")
        hum = np.sin(2 * np.pi * 100 * np.arange(len(pcm)) / 16000)
        noise = pink / pink.std() + 0.3 * hum
        speech_power = np.mean(pcm[np.abs(pcm) > 300] ** 2)
        noise *= np.sqrt(speech_power / 10 ** (snr / 10) / np.mean(noise ** 2))
        pcm = pcm + noise
    pcm = np.clip(pcm, -32768, 32767).astype(np.int16)

    t0 = time.perf_counter()
    path = os.environ.get("MODEL_DIR") or os.path.join(ROOT, "models", key)
    backend = SenseVoiceBackend(path, 16000, "zh") if key == "sensevoice" else VoskBackend(path, 16000)
    load_sec = time.perf_counter() - t0

    t0 = time.perf_counter()
    texts = []
    for i in range(0, len(pcm), 1600):
        texts += backend.accept(pcm[i:i + 1600].tobytes())
    texts += backend.flush()
    proc_sec = time.perf_counter() - t0

    hyp = "".join(texts)
    print(f"模型 {key}  噪声 {'无' if snr is None else f'SNR {snr:g}dB'}")
    print(f"  加载 {load_sec:.1f}s  处理 {audio_sec:.1f}s 音频用了 {proc_sec:.2f}s (实时率 {proc_sec / audio_sec:.3f})"
          f"  峰值内存 {peak_mem_mb():.0f}MB")
    for t in texts:
        print("  >", t)
    if ref:
        print(f"  字错误率 CER = {cer(ref, hyp) * 100:.1f}%")


if __name__ == "__main__":
    main()
