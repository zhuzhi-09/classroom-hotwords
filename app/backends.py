"""识别后端。统一接口：

    accept(pcm_int16_bytes) -> 本次新识别出的整句列表
    partial                 -> 当前正在识别的内容（给悬浮条底部显示）
    flush()                 -> 停止时把缓冲里剩下的音频识别完
    reset()                 -> 开始新一轮监听前清空状态
"""
import json
import os

import numpy as np


def _ascii_path(path):
    """Kaldi 在 Windows 上打不开含中文的路径，尽量换成 8.3 短路径。"""
    if path.isascii():
        return path
    import ctypes
    buf = ctypes.create_unicode_buffer(1024)
    if ctypes.windll.kernel32.GetShortPathNameW(path, buf, 1024) and buf.value.isascii():
        return buf.value
    raise RuntimeError("Vosk 模型不能放在含中文的路径下，请把程序移到纯英文路径，或改用 SenseVoice 模型")


class VoskBackend:
    def __init__(self, path, sample_rate):
        from vosk import KaldiRecognizer, Model, SetLogLevel
        SetLogLevel(-1)
        self._model = Model(_ascii_path(path))
        self._make = lambda: KaldiRecognizer(self._model, sample_rate)
        self.rec = self._make()
        self.partial = ""

    def reset(self):
        self.rec = self._make()
        self.partial = ""

    def accept(self, data):
        if self.rec.AcceptWaveform(data):
            self.partial = ""
            text = json.loads(self.rec.Result()).get("text", "").replace(" ", "")
            return [text] if text else []
        self.partial = json.loads(self.rec.PartialResult()).get("partial", "").replace(" ", "")
        return []

    def flush(self):
        text = json.loads(self.rec.FinalResult()).get("text", "").replace(" ", "")
        return [text] if text else []


class SenseVoiceBackend:
    """Silero VAD 切出一句话，再交给 SenseVoice 整句识别。"""

    WINDOW = 512  # silero vad 要求 16k 下每次 512 个采样

    def __init__(self, path, sample_rate, language="zh"):
        import sherpa_onnx
        self._so = sherpa_onnx
        self.sample_rate = sample_rate
        threads = max(1, min(4, (os.cpu_count() or 2) // 2))
        self.recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=os.path.join(path, "model.int8.onnx"),
            tokens=os.path.join(path, "tokens.txt"),
            num_threads=threads, use_itn=True, language=language)
        cfg = sherpa_onnx.VadModelConfig()
        cfg.silero_vad.model = os.path.join(path, "silero_vad.onnx")
        cfg.silero_vad.threshold = 0.5
        cfg.silero_vad.min_silence_duration = 0.5
        cfg.silero_vad.min_speech_duration = 0.25
        cfg.silero_vad.max_speech_duration = 15
        cfg.sample_rate = sample_rate
        cfg.num_threads = 1
        self._vad_cfg = cfg
        self.reset()

    def reset(self):
        self.vad = self._so.VoiceActivityDetector(self._vad_cfg, buffer_size_in_seconds=60)
        self.buf = np.zeros(0, dtype=np.float32)
        self.partial = ""

    def _drain(self):
        out = []
        while not self.vad.empty():
            stream = self.recognizer.create_stream()
            stream.accept_waveform(self.sample_rate, self.vad.front.samples)
            self.vad.pop()
            self.recognizer.decode_stream(stream)
            text = stream.result.text.strip()
            if text:
                out.append(text)
        return out

    def accept(self, data):
        samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        self.buf = np.concatenate([self.buf, samples])
        while len(self.buf) >= self.WINDOW:
            self.vad.accept_waveform(self.buf[:self.WINDOW])
            self.buf = self.buf[self.WINDOW:]
        self.partial = "（有人在说话…）" if self.vad.is_speech_detected() else ""
        return self._drain()

    def flush(self):
        self.vad.flush()
        self.partial = ""
        return self._drain()
