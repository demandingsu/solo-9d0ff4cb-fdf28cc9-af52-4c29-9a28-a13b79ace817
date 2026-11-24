"""箱数汇总的后台构建：分块扫描旧账、心跳上报、中断接管。

构建在后台线程进行，每个分块一个短事务（见 ledger.build_summary_chunk），
因此扫描旧账期间其他人仍可正常入账与下载。构建线程随进程退出而终止
（daemon 线程），状态停留在 building；核对包页据此提示可重试，
重试凭心跳超时接管（builder_token 换主），已汇总进度不会重复计数。
"""
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone

from . import ledger

_lock = threading.Lock()
_threads = {}  # token -> Thread（本进程内，仅供测试与状态观察）


def _connect(db_path):
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def request_build(app):
    """请求发起（或重试）箱数汇总构建。

    返回 (started, status)：
    - started=True  本次请求取得了构建权，后台线程已启动；
    - started=False 无需或不能启动，status 为当前状态（building=他处正在构建，
      active=已启用）。
    """
    token = uuid.uuid4().hex
    now = time.time()
    stale_before = now - app.config["SUMMARY_BUILD_STALE_SECONDS"]
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = _connect(app.config["LEDGER_DB"])
    try:
        conn.execute("BEGIN IMMEDIATE")
        # 只有 off、或 building 但心跳已超时（构建中断）时才能取得构建权
        cur = conn.execute(
            """
            UPDATE summary_state
               SET status = ?, builder_token = ?, heartbeat_at = ?, started_at = ?, finished_at = NULL
             WHERE status = ?
                OR (status = ? AND (heartbeat_at IS NULL OR heartbeat_at < ?))
            """,
            (
                ledger.SUMMARY_BUILDING,
                token,
                now,
                started_at,
                ledger.SUMMARY_OFF,
                ledger.SUMMARY_BUILDING,
                stale_before,
            ),
        )
        if cur.rowcount != 1:
            status = conn.execute("SELECT status FROM summary_state WHERE id = 1").fetchone()[
                "status"
            ]
            conn.execute("COMMIT")
            return False, status
        conn.execute("COMMIT")
    finally:
        conn.close()

    thread = threading.Thread(
        target=_build_loop,
        args=(app, token),
        daemon=True,
        name=f"summary-build-{token[:8]}",
    )
    with _lock:
        _threads[token] = thread
    thread.start()
    return True, ledger.SUMMARY_BUILDING


def _build_loop(app, token):
    cfg = app.config
    chunk_size = cfg["SUMMARY_BUILD_CHUNK_SIZE"]
    delay = cfg["SUMMARY_BUILD_DELAY_MS"] / 1000.0
    max_chunks = cfg.get("SUMMARY_BUILD_MAX_CHUNKS")  # 测试钩子：处理 N 块后退出，模拟构建中断
    conn = _connect(cfg["LEDGER_DB"])
    chunks = 0
    try:
        while True:
            if delay:
                time.sleep(delay)
            result = ledger.build_summary_chunk(conn, token, chunk_size)
            if result is None:
                return  # 构建权已被接管，退出
            processed, _hwm = result
            if processed:
                chunks += 1
                if max_chunks is not None and chunks >= max_chunks:
                    return  # 模拟进程崩溃：状态停留在 building，等待重试
                continue
            done = ledger.finalize_summary_build(conn, token)
            if done is None:
                return  # 构建权已被接管，退出
            if done:
                return  # 汇总已启用
            # 收尾瞬间又有新批次入账，继续扫描
    except Exception:
        app.logger.exception("箱数汇总构建线程异常退出；状态保留为 building，可在核对包页重试")
    finally:
        conn.close()
        with _lock:
            _threads.pop(token, None)


def wait_for_idle(timeout=10.0):
    """等待本进程内所有构建线程结束（测试与自测脚本用）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _lock:
            threads = list(_threads.values())
        if not threads:
            return True
        for t in threads:
            t.join(timeout=0.1)
    return False
