"""SQLite 存储：课堂(session) 和 句子(utterance)。

句子保存 jieba 的全部分词结果，停用词在查询时再过滤，
这样修改停用词后历史统计也会随之更新。
"""
import csv
import io
import sqlite3
import threading
import time
from collections import Counter
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL
);
CREATE TABLE IF NOT EXISTS utterances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    ts REAL NOT NULL,
    text TEXT NOT NULL,
    tokens TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_utt_session ON utterances(session_id, ts);
"""


class Storage:
    def __init__(self, path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def _exec(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def _all(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    # ---------- 课堂 ----------
    def new_session(self, name=None):
        now = time.time()
        self._exec("UPDATE sessions SET ended_at=? WHERE ended_at IS NULL", (now,))
        name = name or "课堂 " + datetime.now().strftime("%Y-%m-%d %H:%M")
        return self._exec("INSERT INTO sessions(name, started_at) VALUES(?, ?)", (name, now)).lastrowid

    def end_open_sessions(self):
        self._exec("UPDATE sessions SET ended_at=? WHERE ended_at IS NULL", (time.time(),))

    def get_session(self, sid):
        rows = self._all("SELECT * FROM sessions WHERE id=?", (sid,))
        return rows[0] if rows else None

    def list_sessions(self):
        return self._all("""
            SELECT s.*, COUNT(u.id) AS utterance_count, MAX(u.ts) AS last_ts
            FROM sessions s LEFT JOIN utterances u ON u.session_id = s.id
            GROUP BY s.id ORDER BY s.started_at DESC""")

    def rename_session(self, sid, name):
        self._exec("UPDATE sessions SET name=? WHERE id=?", (name, sid))

    def delete_session(self, sid):
        self._exec("DELETE FROM sessions WHERE id=?", (sid,))

    # ---------- 句子 ----------
    def add_utterance(self, sid, text, tokens):
        return self._exec("INSERT INTO utterances(session_id, ts, text, tokens) VALUES(?,?,?,?)",
                          (sid, time.time(), text, " ".join(tokens))).lastrowid

    def list_utterances(self, sid, q="", limit=500):
        if q:
            return self._all("SELECT * FROM utterances WHERE session_id=? AND text LIKE ? "
                             "ORDER BY ts DESC LIMIT ?", (sid, f"%{q}%", limit))
        return self._all("SELECT * FROM utterances WHERE session_id=? ORDER BY ts DESC LIMIT ?",
                         (sid, limit))

    def delete_utterance(self, uid):
        self._exec("DELETE FROM utterances WHERE id=?", (uid,))

    def word_counts(self, sid, keep):
        """keep(word) -> bool，由调用方按当前停用词配置提供。"""
        c = Counter()
        for r in self._all("SELECT tokens FROM utterances WHERE session_id=?", (sid,)):
            c.update(w for w in r["tokens"].split() if keep(w))
        return c

    def timeline(self, sid, word, bucket_minutes=5):
        """某个词在课堂内随时间的出现次数。"""
        s = self.get_session(sid)
        if not s:
            return []
        buckets = Counter()
        for r in self._all("SELECT ts, tokens FROM utterances WHERE session_id=?", (sid,)):
            n = r["tokens"].split().count(word)
            if n:
                buckets[max(0, int((r["ts"] - s["started_at"]) // (bucket_minutes * 60)))] += n
        if not buckets:
            return []
        return [{"minute": i * bucket_minutes, "count": buckets.get(i, 0)}
                for i in range(max(buckets) + 1)]

    def export_csv(self, sid, keep):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["时间", "原句", "关键词"])
        for r in reversed(self.list_utterances(sid, limit=1_000_000)):
            w.writerow([datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M:%S"), r["text"],
                        " ".join(t for t in r["tokens"].split() if keep(t))])
        return "﻿" + buf.getvalue()

    def summary(self):
        with self.lock:
            r = self.db.execute("SELECT (SELECT COUNT(*) FROM sessions) AS sessions, "
                                "(SELECT COUNT(*) FROM utterances) AS utterances").fetchone()
        return dict(r)
