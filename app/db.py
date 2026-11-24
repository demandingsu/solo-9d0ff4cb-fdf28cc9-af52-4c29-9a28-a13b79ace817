"""SQLite 存取层：台账（入库 / 转仓 / 交货与余箱）与箱数汇总持久化。

汇总相关表：
- summary_state    单行状态机：off（未构建）-> building（后台分块扫描中）-> active（已启用）。
                   high_water_mark 记录已汇总到的最大 upload id；builder_token + heartbeat_at
                   用于构建中断后的接管重试。
- crate_summary    按（果园, 去向）合计的交货/余箱箱数，启用后列表与下载只查此表。
- orchard_summary  按果园合计的有交货/余箱记录的仓次数（与 orchard_summaries 原口径一致）。

数据库使用 WAL 模式：构建线程分块写入时，入账（写）与下载（读）仍可并发完成。
"""
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

-- 箱数汇总状态机（单行，id 固定为 1）
CREATE TABLE IF NOT EXISTS summary_state (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    status          TEXT NOT NULL CHECK (status IN ('off', 'building', 'active')),
    high_water_mark INTEGER NOT NULL DEFAULT 0,  -- 已汇总到的最大 upload id
    builder_token   TEXT,                        -- 当前构建线程的令牌，崩溃后凭心跳超时接管
    heartbeat_at    REAL,                        -- 构建线程心跳（epoch 秒）
    started_at      TEXT,
    finished_at     TEXT
);

-- 按（果园, 去向）合计的交货/余箱箱数
CREATE TABLE IF NOT EXISTS crate_summary (
    orchard     TEXT NOT NULL,
    destination TEXT NOT NULL,
    crates      INTEGER NOT NULL,
    PRIMARY KEY (orchard, destination)
);

-- 按果园合计的有交货/余箱记录的仓次数
CREATE TABLE IF NOT EXISTS orchard_summary (
    orchard  TEXT PRIMARY KEY,
    sessions INTEGER NOT NULL
);
"""


def get_db():
    if "db" not in g:
        path = current_app.config["LEDGER_DB"]
        # busy_timeout：汇总构建的分块事务与入账写事务相互等待时，短暂阻塞而非立即报错
        g.db = sqlite3.connect(path, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA busy_timeout = 10000")
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    # WAL：允许一个写事务与多个读事务并发，构建扫描期间入账与下载不互相阻塞
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("INSERT OR IGNORE INTO summary_state (id, status) VALUES (1, 'off')")
    conn.commit()


def init_app(app):
    import os

    db_path = app.config["LEDGER_DB"]
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db()
