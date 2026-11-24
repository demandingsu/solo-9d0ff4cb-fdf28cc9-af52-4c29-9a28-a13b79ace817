"""SQLite 存取层：台账（入库 / 转仓 / 交货与余箱）持久化。"""
import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_hash    TEXT NOT NULL UNIQUE,        -- 三个 CSV 内容的整体哈希，防止同一批重复入账
    uploaded_at   TEXT NOT NULL,
    inbound_count INTEGER NOT NULL,
    transfer_count INTEGER NOT NULL,
    delivery_count INTEGER NOT NULL
);

-- 每个仓次唯一归属一个果园（校验阶段保证）
CREATE TABLE IF NOT EXISTS sessions (
    name    TEXT PRIMARY KEY,
    orchard TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inbound (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id INTEGER NOT NULL REFERENCES uploads(id),
    session  TEXT NOT NULL,
    orchard  TEXT NOT NULL,
    crates   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS transfers (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id INTEGER NOT NULL REFERENCES uploads(id),
    from_session TEXT NOT NULL,
    to_session   TEXT NOT NULL,
    crates   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id INTEGER NOT NULL REFERENCES uploads(id),
    session  TEXT NOT NULL,
    kind     TEXT NOT NULL CHECK (kind IN ('交货', '余箱')),
    destination TEXT NOT NULL,
    crates   INTEGER NOT NULL
);
"""


def get_db():
    if "db" not in g:
        path = current_app.config["LEDGER_DB"]
        g.db = sqlite3.connect(path)
        g.db.row_factory = sqlite3.Row
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    conn.commit()


def init_app(app):
    import os

    db_path = app.config["LEDGER_DB"]
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db()
