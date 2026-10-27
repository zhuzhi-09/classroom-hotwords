"""本地管理页服务。只监听 127.0.0.1。

所有 /api 请求必须带 X-Token 头（令牌只写在本服务返回的页面里），
防止别的网站在浏览器里偷偷调用本机接口。
"""
import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from . import engine as engine_mod
from .config import DEFAULTS, WEB_DIR


class WebServer:
    def __init__(self, app):
        self.app = app
        self.token = secrets.token_hex(16)
        port = app.cfg["web_port"]
        handler = type("Handler", (Handler,), {"app": app, "token": self.token})
        try:
            self.httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        except OSError:
            self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)  # 端口被占用就随机
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/"

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()


class ApiError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


class Handler(BaseHTTPRequestHandler):
    app = None
    token = ""

    def log_message(self, *args):
        pass

    # ---------- 基础 ----------
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            raise ApiError("请求格式错误")

    def _dispatch(self, method):
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        if method == "GET" and not parts:
            return self._index()
        if not parts or parts[0] != "api":
            return self._send(404, "not found", "text/plain")
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        # 导出文件用浏览器直接下载，令牌放在 query 里
        token = self.headers.get("X-Token") or query.get("token")
        if not secrets.compare_digest(token or "", self.token):
            return self._json({"error": "forbidden"}, 403)
        try:
            result = self._route(method, parts[1:], query)
            if result is not None:
                self._json(result)
        except ApiError as e:
            self._json({"error": str(e)}, e.code)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _index(self):
        with open(os.path.join(WEB_DIR, "index.html"), "r", encoding="utf-8") as f:
            html = f.read().replace("__TOKEN__", self.token)
        self._send(200, html, "text/html; charset=utf-8")

    # ---------- 路由 ----------
    def _route(self, method, p, q):
        app, eng, st = self.app, self.app.engine, self.app.storage
        route = (method, p[0] if p else "", len(p))

        if route == ("GET", "state", 1):
            s = eng.state()
            s.update(overlay_visible=app.cfg["overlay_visible"], hotkey_ok=app.hotkey_ok(),
                     summary=st.summary(), download=eng.download)
            return s

        if route == ("GET", "config", 1):
            return {"config": app.cfg.snapshot(), "defaults": DEFAULTS}
        if route == ("POST", "config", 1):
            try:
                changed = app.apply_config(self._body())
            except (ValueError, TypeError) as e:
                raise ApiError(f"参数不合法：{e}")
            except RuntimeError as e:
                raise ApiError(str(e))
            return {"changed": changed, "config": app.cfg.snapshot(), "hotkey_ok": app.hotkey_ok()}
        if route == ("POST", "reset", 1):
            keep = {"x", "y"}
            app.apply_config({k: v for k, v in DEFAULTS.items() if k not in keep})
            return {"config": app.cfg.snapshot()}

        if route == ("GET", "devices", 1):
            return engine_mod.list_input_devices()
        if route == ("GET", "models", 1):
            return {"models": engine_mod.MODELS, "download": eng.download,
                    "ready": engine_mod.model_ready(app.cfg.model_dir()), "dir": app.cfg.model_dir()}
        if route == ("POST", "models", 2):
            if not eng.download_model(p[1]):
                raise ApiError("已有下载任务在进行，或模型名称无效")
            return {"ok": True}

        if route == ("POST", "control", 2):
            action = p[1]
            if action == "start":
                eng.start()
            elif action == "stop":
                threading.Thread(target=eng.stop, daemon=True).start()
            elif action == "toggle_overlay":
                app.call_ui(app.toggle_overlay)
            elif action == "clear":
                eng.clear_window()
            elif action == "new_session":
                return {"session_id": app.new_session(self._body().get("name") or None)}
            else:
                raise ApiError("未知操作")
            return {"ok": True}

        if route == ("POST", "tokenize", 1):
            text = str(self._body().get("text", ""))
            tokens = eng.segment(text)
            return {"tokens": tokens, "kept": [t for t in tokens if eng.keep(t)]}

        if route == ("GET", "sessions", 1):
            return {"sessions": st.list_sessions(), "current": eng.session_id}
        if p and p[0] == "sessions" and len(p) >= 2:
            try:
                sid = int(p[1])
            except ValueError:
                raise ApiError("课堂编号错误")
            if not st.get_session(sid):
                raise ApiError("课堂不存在", 404)
            sub = p[2] if len(p) > 2 else ""
            if method == "GET" and sub == "utterances":
                return st.list_utterances(sid, q.get("q", ""), int(q.get("limit", 500)))
            if method == "GET" and sub == "words":
                c = st.word_counts(sid, eng.keep)
                return [{"word": w, "count": n} for w, n in c.most_common(int(q.get("limit", 100)))]
            if method == "GET" and sub == "timeline":
                return st.timeline(sid, q.get("word", ""), int(q.get("bucket", 5)))
            if method == "GET" and sub == "export":
                s = st.get_session(sid)
                name = quote(f"{s['name']}.csv")
                self._send(200, st.export_csv(sid, eng.keep), "text/csv; charset=utf-8",
                           {"Content-Disposition": f"attachment; filename*=UTF-8''{name}"})
                return None
            if method == "POST" and sub == "rename":
                name = str(self._body().get("name", "")).strip()
                if not name:
                    raise ApiError("名称不能为空")
                st.rename_session(sid, name)
                return {"ok": True}
            if method == "DELETE" and not sub:
                if sid == eng.session_id:
                    raise ApiError("不能删除正在进行的课堂，请先开始新课堂")
                st.delete_session(sid)
                return {"ok": True}

        if method == "DELETE" and p and p[0] == "utterances" and len(p) == 2:
            st.delete_utterance(int(p[1]))
            return {"ok": True}

        raise ApiError("接口不存在", 404)
