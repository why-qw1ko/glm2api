"""SQLite 元数据与脱敏统计；不存放请求/响应正文。"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS api_keys (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, digest TEXT UNIQUE NOT NULL,
                    prefix TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY, created REAL NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY, created REAL NOT NULL, protocol TEXT NOT NULL,
                    model TEXT NOT NULL, key_id TEXT NOT NULL, account TEXT NOT NULL,
                    status INTEGER NOT NULL, outcome TEXT NOT NULL, error TEXT NOT NULL,
                    duration_ms REAL NOT NULL, first_ms REAL, input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL, images INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS requests_created ON requests(created);
                CREATE INDEX IF NOT EXISTS requests_key_created ON requests(key_id, created);
            """)
        try:
            path.chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, name, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE name=?", (name,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, name, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (name, json.dumps(value)))

    def save_config(self, value, changed):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('runtime_config',?)", (json.dumps(value),))
            db.execute("INSERT INTO audit(created,action,detail) VALUES (?,?,?)",
                       (time.time(), "config.update", json.dumps(changed, ensure_ascii=False)))

    def audit(self, action, detail):
        with self.connect() as db:
            db.execute("INSERT INTO audit(created,action,detail) VALUES (?,?,?)",
                       (time.time(), action, json.dumps(detail, ensure_ascii=False)))

    def audits(self, page=1):
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM audit ORDER BY id DESC LIMIT 50 OFFSET ?", ((page - 1) * 50,))]

    def create_key(self, name):
        token = "glm_" + secrets.token_urlsafe(32)
        key_id = secrets.token_hex(8)
        with self.connect() as db:
            db.execute("INSERT INTO api_keys VALUES (?,?,?,?,1,?)",
                       (key_id, name, hashlib.sha256(token.encode()).hexdigest(), token[:8], time.time()))
            db.execute("INSERT INTO audit(created,action,detail) VALUES (?,?,?)",
                       (time.time(), "key.create", json.dumps({"id": key_id, "name": name}, ensure_ascii=False)))
        return {"id": key_id, "key": token}

    def keys(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT id,name,prefix,enabled,created FROM api_keys ORDER BY created DESC")]

    def authenticate_key(self, token):
        with self.connect() as db:
            row = db.execute("SELECT id FROM api_keys WHERE digest=? AND enabled=1",
                             (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        return row[0] if row else None

    def toggle_key(self, key_id, enabled):
        with self.connect() as db:
            count = db.execute("UPDATE api_keys SET enabled=? WHERE id=?", (int(enabled), key_id)).rowcount
            if count:
                db.execute("INSERT INTO audit(created,action,detail) VALUES (?,?,?)",
                           (time.time(), "key.update", json.dumps({"id": key_id, "enabled": enabled})))
        return bool(count)

    def record(self, record):
        columns = ("id", "created", "protocol", "model", "key_id", "account", "status", "outcome",
                   "error", "duration_ms", "first_ms", "input_tokens", "output_tokens", "images")
        with self.connect() as db:
            db.execute("INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(record[c] for c in columns))

    def usage(self, since, until, model="", protocol="", key_id="", outcome="", page=1):
        filters = ["created>=?", "created<=?"]
        values = [since, until]
        for field, value in (("model", model), ("protocol", protocol), ("key_id", key_id), ("outcome", outcome)):
            if value:
                filters.append(f"{field}=?")
                values.append(value)
        where = " WHERE " + " AND ".join(filters)
        aggregate = """COUNT(*) AS requests, COALESCE(SUM(outcome='success'),0) AS success,
            COALESCE(SUM(outcome='failed'),0) AS failed, COALESCE(SUM(outcome='cancelled'),0) AS cancelled,
            COALESCE(SUM(input_tokens),0) AS input_tokens, COALESCE(SUM(output_tokens),0) AS output_tokens,
            COALESCE(SUM(images),0) AS images, AVG(duration_ms) AS avg_ms, AVG(first_ms) AS first_ms"""
        with self.connect() as db:
            summary = dict(db.execute("SELECT " + aggregate + " FROM requests" + where, values).fetchone())
            rows = [dict(r) for r in db.execute("SELECT * FROM requests" + where +
                    " ORDER BY created DESC LIMIT 50 OFFSET ?", values + [(page - 1) * 50])]
            trends = [dict(r) for r in db.execute(
                "SELECT CAST(created/3600 AS INTEGER)*3600 AS bucket," + aggregate +
                " FROM requests" + where + " GROUP BY bucket ORDER BY bucket", values)]
            groups = [dict(r) for r in db.execute("SELECT model,protocol,key_id," + aggregate +
                " FROM requests" + where + " GROUP BY model,protocol,key_id ORDER BY requests DESC LIMIT 100", values)]
        if trends and until - since <= 366 * 86400:
            indexed = {row["bucket"]: row for row in trends}
            empty = {name: (None if name in ("avg_ms", "first_ms") else 0) for name in summary}
            trends = [indexed.get(bucket, {**empty, "bucket": bucket})
                      for bucket in range(int(since // 3600) * 3600, int(until // 3600) * 3600 + 1, 3600)]
        return {"summary": summary, "rows": rows, "trends": trends, "groups": groups, "page": page,
                "page_size": 50, "estimated": True, "official_quota": None}

    def prune(self, days):
        with self.connect() as db:
            db.execute("DELETE FROM requests WHERE created<?", (time.time() - days * 86400,))
            db.execute("DELETE FROM audit WHERE created<?", (time.time() - days * 86400,))
