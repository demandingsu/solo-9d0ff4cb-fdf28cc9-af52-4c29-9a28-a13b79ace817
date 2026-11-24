"""台账核心逻辑：CSV 解析、整批校验、入账、核对包汇总、箱数汇总构建。

业务规则
--------
1. 按整箱记数：所有箱数必须是正整数。
2. 入库注明果园；同一仓次的入库只能涉及一个果园；经转仓相连的仓次必须同属一个果园。
3. 转仓注明起讫仓次，起讫不得相同。
4. 交货与余箱合计最多 MAX_DESTINATIONS 个去向（全库口径）。
5. 每个仓次收发必须平衡：入库 + 转入 = 转出 + 交货 + 余箱。
   有差额则指出仓次，整批退回（不入账）。
6. 批次整体去重（内容哈希），仓次名全局唯一。

箱数汇总
--------
台账变厚后，核对包页不再每次扫描明细，而是读 crate_summary / orchard_summary 两张
汇总表。首次汇总由库管在核对包页发起，后台分块构建（summary_state 状态机）：

- off      未构建：列表与下载按原账（deliveries/sessions 明细）出数。
- building 构建中：仍按原账出数；构建线程每块一个短事务，推进 high_water_mark 并
           上报心跳。进程中断后状态停留在 building，凭心跳超时由重试接管，已汇总
           进度随事务保留，不会重复计数。
- active   已启用：列表与下载只查汇总表；此后每批入账在同一事务内把箱数增量并入
           汇总表，入账失败则增量随批次一起回滚。

恰好计一次：批次是否计入汇总由 upload id 与 high_water_mark 决定（构建期），或由
入账事务自己并入（启用后）。入账事务与构建事务都持有 SQLite 写锁，状态读取与增量
写入在同一事务内完成，因此扫描期间入账的批次也不会漏计或重计。
"""
import csv
import hashlib
import io
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

MAX_DESTINATIONS = 4
MAX_NAME_LEN = 64

KIND_DELIVERY = "交货"
KIND_REMAINING = "余箱"
DELIVERY_KINDS = (KIND_DELIVERY, KIND_REMAINING)

INBOUND_HEADERS = ["仓次", "果园", "箱数"]
TRANSFER_HEADERS = ["起仓次", "讫仓次", "箱数"]
DELIVERY_HEADERS = ["仓次", "类型", "去向", "箱数"]


class BatchRejected(Exception):
    """整批校验未通过：problems 为面向用户的问题列表，本次上传全部退回。"""

    def __init__(self, problems):
        super().__init__("; ".join(problems))
        self.problems = list(problems)


@dataclass
class Batch:
    inbound: list = field(default_factory=list)    # {session, orchard, crates}
    transfers: list = field(default_factory=list)  # {from_session, to_session, crates}
    deliveries: list = field(default_factory=list) # {session, kind, destination, crates}


# ---------------------------------------------------------------- CSV 解析

def _parse_crates(raw, label, line_no, problems):
    text = (raw or "").strip()
    if not text.isdigit():
        problems.append(f"{label} 第 {line_no} 行：箱数 “{raw}” 不是正整数（须按整箱记数）")
        return None
    value = int(text)
    if value < 1:
        problems.append(f"{label} 第 {line_no} 行：箱数必须为正整数")
        return None
    return value


def _parse_name(raw, label, line_no, col, problems):
    text = (raw or "").strip()
    if not text:
        problems.append(f"{label} 第 {line_no} 行：{col} 不能为空")
        return None
    if len(text) > MAX_NAME_LEN:
        problems.append(f"{label} 第 {line_no} 行：{col} 超长（>{MAX_NAME_LEN} 字符）")
        return None
    return text


def _read_csv(raw_bytes, required_headers, label, problems):
    """解析单个 CSV，返回 dict 行列表；表头不符或文件不可读时记入 problems。"""
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        problems.append(f"{label}：文件不是有效的 UTF-8 编码")
        return []
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not rows:
        problems.append(f"{label}：文件为空")
        return []
    headers = [h.strip() for h in rows[0]]
    if headers != required_headers:
        problems.append(
            f"{label}：表头应为 {','.join(required_headers)}，实际为 {','.join(headers) or '（空）'}"
        )
        return []
    out = []
    for i, row in enumerate(rows[1:], start=2):  # 第 1 行是表头
        if len(row) != len(required_headers):
            problems.append(f"{label} 第 {i} 行：列数应为 {len(required_headers)}，实际 {len(row)}")
            continue
        out.append(dict(zip(required_headers, row)))
    return out


def parse_batch(inbound_bytes, transfer_bytes, delivery_bytes):
    """把三个 CSV 的字节流解析成 Batch；有任何问题抛 BatchRejected。"""
    problems = []
    inbound_raw = _read_csv(inbound_bytes, INBOUND_HEADERS, "入库 CSV", problems)
    transfer_raw = _read_csv(transfer_bytes, TRANSFER_HEADERS, "转仓 CSV", problems)
    delivery_raw = _read_csv(delivery_bytes, DELIVERY_HEADERS, "交货 CSV", problems)

    batch = Batch()
    # 逐行解析（行号 = CSV 中的实际行号，含表头偏移）
    for idx, r in enumerate(inbound_raw, start=2):
        session = _parse_name(r["仓次"], "入库 CSV", idx, "仓次", problems)
        orchard = _parse_name(r["果园"], "入库 CSV", idx, "果园", problems)
        crates = _parse_crates(r["箱数"], "入库 CSV", idx, problems)
        if session and orchard and crates:
            batch.inbound.append({"session": session, "orchard": orchard, "crates": crates})

    for idx, r in enumerate(transfer_raw, start=2):
        frm = _parse_name(r["起仓次"], "转仓 CSV", idx, "起仓次", problems)
        to = _parse_name(r["讫仓次"], "转仓 CSV", idx, "讫仓次", problems)
        crates = _parse_crates(r["箱数"], "转仓 CSV", idx, problems)
        if frm and to and frm == to:
            problems.append(f"转仓 CSV 第 {idx} 行：起讫仓次相同（{frm}）")
            frm = to = None
        if frm and to and crates:
            batch.transfers.append({"from_session": frm, "to_session": to, "crates": crates})

    for idx, r in enumerate(delivery_raw, start=2):
        session = _parse_name(r["仓次"], "交货 CSV", idx, "仓次", problems)
        kind = (r["类型"] or "").strip()
        if kind not in DELIVERY_KINDS:
            problems.append(f"交货 CSV 第 {idx} 行：类型只能是 交货 或 余箱，实际为 “{r['类型']}”")
            kind = None
        dest = _parse_name(r["去向"], "交货 CSV", idx, "去向", problems)
        crates = _parse_crates(r["箱数"], "交货 CSV", idx, problems)
        if session and kind and dest and crates:
            batch.deliveries.append(
                {"session": session, "kind": kind, "destination": dest, "crates": crates}
            )

    if not batch.inbound and not batch.transfers and not batch.deliveries and not problems:
        problems.append("三个 CSV 都没有有效数据行")
    if problems:
        raise BatchRejected(problems)
    return batch


# ---------------------------------------------------------------- 批次校验

class _UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def resolve_session_orchards(batch, problems):
    """确定每个仓次归属的果园；冲突记入 problems。返回 {session: orchard}。"""
    uf = _UnionFind()
    pinned = {}  # session -> orchard（来自入库行）
    for row in batch.inbound:
        s, o = row["session"], row["orchard"]
        if s in pinned and pinned[s] != o:
            problems.append(f"仓次 {s} 的入库涉及多个果园（{pinned[s]}、{o}），同一仓次只能归属一个果园")
        pinned.setdefault(s, o)
        uf.find(s)
    for t in batch.transfers:
        uf.union(t["from_session"], t["to_session"])
    for d in batch.deliveries:
        uf.find(d["session"])

    # 每个连通分量至多一个果园
    comp_orchard = {}
    for s in list(uf.parent):
        root = uf.find(s)
        o = pinned.get(s)
        if o is None:
            continue
        if root in comp_orchard and comp_orchard[root] != o:
            problems.append(
                f"仓次经转仓关联到不同果园（{comp_orchard[root]}、{o}），转仓只能在同一果园的仓次间进行"
            )
        comp_orchard.setdefault(root, o)

    return {s: comp_orchard.get(uf.find(s)) for s in uf.parent}


def check_balance(batch, problems):
    """仓次收发平衡：入库 + 转入 = 转出 + 交货 + 余箱。有差额指出仓次。"""
    inflow = defaultdict(int)
    outflow = defaultdict(int)
    for r in batch.inbound:
        inflow[r["session"]] += r["crates"]
    for t in batch.transfers:
        outflow[t["from_session"]] += t["crates"]
        inflow[t["to_session"]] += t["crates"]
    for d in batch.deliveries:
        outflow[d["session"]] += d["crates"]
    for s in sorted(set(inflow) | set(outflow)):
        if inflow[s] != outflow[s]:
            diff = inflow[s] - outflow[s]
            side = "收大于支" if diff > 0 else "支大于收"
            problems.append(
                f"仓次 {s} 收发有差额：收入 {inflow[s]} 箱，支出 {outflow[s]} 箱，{side} {abs(diff)} 箱"
            )


def validate_batch(batch, existing_sessions, existing_destinations):
    """整批校验，返回 {session: orchard}；未通过抛 BatchRejected。"""
    problems = []

    batch_sessions = (
        {r["session"] for r in batch.inbound}
        | {t["from_session"] for t in batch.transfers}
        | {t["to_session"] for t in batch.transfers}
        | {d["session"] for d in batch.deliveries}
    )
    reused = sorted(batch_sessions & set(existing_sessions))
    if reused:
        problems.append(f"以下仓次已入账，不得重复上传：{', '.join(reused)}")

    session_orchard = resolve_session_orchards(batch, problems)
    check_balance(batch, problems)

    destinations = {d["destination"] for d in batch.deliveries} | set(existing_destinations)
    if len(destinations) > MAX_DESTINATIONS:
        problems.append(
            f"交货和余箱合计最多 {MAX_DESTINATIONS} 个去向，当前共 {len(destinations)} 个："
            f"{', '.join(sorted(destinations))}"
        )

    if problems:
        raise BatchRejected(problems)
    return session_orchard


# ---------------------------------------------------------------- 入账

def batch_hash(inbound_bytes, transfer_bytes, delivery_bytes):
    h = hashlib.sha256()
    for chunk in (inbound_bytes, transfer_bytes, delivery_bytes):
        h.update(len(chunk).to_bytes(8, "big"))
        h.update(chunk)
    return h.hexdigest()


def persist_batch(conn, digest, batch, session_orchard):
    """单事务入账；批次哈希冲突（重复上传）抛 BatchRejected。

    汇总已启用（active）时，本批的箱数增量在同一事务内并入汇总表：
    批次与增量一起提交，入账失败则一起撤回。
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO uploads (batch_hash, uploaded_at, inbound_count, transfer_count, delivery_count)"
                " VALUES (?, ?, ?, ?, ?)",
                (digest, now, len(batch.inbound), len(batch.transfers), len(batch.deliveries)),
            )
            upload_id = cur.lastrowid
            conn.executemany(
                "INSERT INTO inbound (upload_id, session, orchard, crates) VALUES (?, ?, ?, ?)",
                [(upload_id, r["session"], r["orchard"], r["crates"]) for r in batch.inbound],
            )
            conn.executemany(
                "INSERT INTO transfers (upload_id, from_session, to_session, crates) VALUES (?, ?, ?, ?)",
                [(upload_id, t["from_session"], t["to_session"], t["crates"]) for t in batch.transfers],
            )
            conn.executemany(
                "INSERT INTO deliveries (upload_id, session, kind, destination, crates) VALUES (?, ?, ?, ?, ?)",
                [(upload_id, d["session"], d["kind"], d["destination"], d["crates"]) for d in batch.deliveries],
            )
            conn.executemany(
                "INSERT INTO sessions (name, orchard) VALUES (?, ?)",
                [(s, o) for s, o in sorted(session_orchard.items()) if o is not None],
            )
            # 状态读取必须在写事务内：与构建线程的收尾事务互斥，保证每批恰好计一次
            if _summary_status_value(conn) == SUMMARY_ACTIVE:
                apply_batch_to_summary(conn, batch, session_orchard)
    except Exception as exc:
        if "UNIQUE" in str(exc) and "batch_hash" in str(exc):
            raise BatchRejected(["该批次已上传并入账，请勿重复提交"]) from exc
        raise


# ---------------------------------------------------------------- 箱数汇总

SUMMARY_OFF = "off"
SUMMARY_BUILDING = "building"
SUMMARY_ACTIVE = "active"


def _summary_status_value(conn):
    row = conn.execute("SELECT status FROM summary_state WHERE id = 1").fetchone()
    return row["status"] if row else SUMMARY_OFF


def summary_status(conn):
    """汇总构建状态（含进度），供核对包页展示。"""
    row = conn.execute(
        "SELECT status, high_water_mark, heartbeat_at, started_at, finished_at"
        " FROM summary_state WHERE id = 1"
    ).fetchone()
    status = dict(row) if row else {"status": SUMMARY_OFF, "high_water_mark": 0}
    status["total_uploads"] = conn.execute("SELECT COUNT(*) FROM uploads").fetchone()[0]
    return status


def summary_active(conn):
    return _summary_status_value(conn) == SUMMARY_ACTIVE


def _apply_summary_deltas(conn, crate_deltas, session_deltas):
    """把（果园, 去向）箱数增量与果园仓次数增量并入汇总表（调用方负责事务）。"""
    conn.executemany(
        """
        INSERT INTO crate_summary (orchard, destination, crates) VALUES (?, ?, ?)
        ON CONFLICT (orchard, destination) DO UPDATE SET crates = crates + excluded.crates
        """,
        [(orchard, dest, crates) for (orchard, dest), crates in sorted(crate_deltas.items())],
    )
    conn.executemany(
        """
        INSERT INTO orchard_summary (orchard, sessions) VALUES (?, ?)
        ON CONFLICT (orchard) DO UPDATE SET sessions = sessions + excluded.sessions
        """,
        sorted(session_deltas.items()),
    )


def apply_batch_to_summary(conn, batch, session_orchard):
    """单个批次的汇总增量（入账事务内调用，与批次一起提交/回滚）。"""
    crate_deltas = defaultdict(int)
    delivery_sessions = defaultdict(set)  # 果园 -> 有交货/余箱记录的仓次
    for d in batch.deliveries:
        orchard = session_orchard.get(d["session"])
        if orchard is None:
            # 交货仓次必有所属果园（收发平衡校验已排除无果园分量），防御性跳过
            continue
        crate_deltas[(orchard, d["destination"])] += d["crates"]
        delivery_sessions[orchard].add(d["session"])
    session_deltas = {o: len(ss) for o, ss in delivery_sessions.items()}
    _apply_summary_deltas(conn, crate_deltas, session_deltas)


def _apply_uploads_to_summary(conn, upload_ids):
    """把一批 upload（按 id 连续的一段）的箱数并入汇总表（调用方负责事务）。"""
    marks = ", ".join("?" for _ in upload_ids)
    crate_deltas = {}
    rows = conn.execute(
        f"""
        SELECT s.orchard AS orchard, d.destination AS destination, SUM(d.crates) AS crates
        FROM deliveries d JOIN sessions s ON s.name = d.session
        WHERE d.upload_id IN ({marks})
        GROUP BY s.orchard, d.destination
        """,
        upload_ids,
    ).fetchall()
    for r in rows:
        crate_deltas[(r["orchard"], r["destination"])] = r["crates"]
    session_deltas = {}
    rows = conn.execute(
        f"""
        SELECT s.orchard AS orchard, COUNT(DISTINCT d.session) AS sessions
        FROM deliveries d JOIN sessions s ON s.name = d.session
        WHERE d.upload_id IN ({marks})
        GROUP BY s.orchard
        """,
        upload_ids,
    ).fetchall()
    for r in rows:
        session_deltas[r["orchard"]] = r["sessions"]
    _apply_summary_deltas(conn, crate_deltas, session_deltas)


def build_summary_chunk(conn, token, chunk_size):
    """构建线程的一个分块：把 high_water_mark 之后的至多 chunk_size 批并入汇总表。

    整个分块（增量 + 水位推进 + 心跳）在一个事务内提交，中断后重试从水位继续，
    不会重复计数。conn 须为自动提交模式（isolation_level=None）。

    返回 (处理批数, 新水位)；构建权已被接管（令牌不符）时返回 None。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        state = conn.execute(
            "SELECT status, builder_token, high_water_mark FROM summary_state WHERE id = 1"
        ).fetchone()
        if state["status"] != SUMMARY_BUILDING or state["builder_token"] != token:
            conn.execute("ROLLBACK")
            return None
        hwm = state["high_water_mark"]
        ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM uploads WHERE id > ? ORDER BY id LIMIT ?", (hwm, chunk_size)
            ).fetchall()
        ]
        if ids:
            _apply_uploads_to_summary(conn, ids)
            hwm = ids[-1]
        conn.execute(
            "UPDATE summary_state SET high_water_mark = ?, heartbeat_at = ? WHERE id = 1",
            (hwm, time.time()),
        )
        conn.execute("COMMIT")
        return len(ids), hwm
    except Exception:
        conn.execute("ROLLBACK")
        raise


def finalize_summary_build(conn, token):
    """收尾：确认没有漏网的已入账批次后启用汇总。

    与入账写事务互斥（同一 SQLite 写锁）：启用前一刻入账的批次要么已被本函数看到
    （继续扫描），要么在启用之后入账并由入账事务自己并入汇总。conn 为自动提交模式。

    返回 True 已启用；False 扫描期间又有新批次入账，需继续扫描；None 构建权被接管。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        state = conn.execute(
            "SELECT status, builder_token, high_water_mark FROM summary_state WHERE id = 1"
        ).fetchone()
        if state["status"] != SUMMARY_BUILDING or state["builder_token"] != token:
            conn.execute("ROLLBACK")
            return None
        max_upload = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM uploads").fetchone()["m"]
        if max_upload > state["high_water_mark"]:
            conn.execute("ROLLBACK")
            return False
        conn.execute(
            """
            UPDATE summary_state
               SET status = ?, finished_at = ?, builder_token = NULL, heartbeat_at = ?
             WHERE id = 1
            """,
            (SUMMARY_ACTIVE, datetime.now(timezone.utc).isoformat(timespec="seconds"), time.time()),
        )
        conn.execute("COMMIT")
        return True
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- 查询 / 汇总

def existing_sessions(conn):
    return {r["name"] for r in conn.execute("SELECT name FROM sessions")}


def existing_destinations(conn):
    return {r["destination"] for r in conn.execute("SELECT DISTINCT destination FROM deliveries")}


def orchard_summaries(conn):
    """各果园汇总：总箱数、去向数、仓次数。汇总启用后只查汇总表，否则扫描明细。"""
    if summary_active(conn):
        rows = conn.execute(
            """
            SELECT o.orchard AS orchard,
                   COALESCE(c.total_crates, 0) AS total_crates,
                   COALESCE(c.destinations, 0) AS destinations,
                   o.sessions AS sessions
            FROM orchard_summary o
            LEFT JOIN (
                SELECT orchard, SUM(crates) AS total_crates, COUNT(*) AS destinations
                FROM crate_summary GROUP BY orchard
            ) c ON c.orchard = o.orchard
            ORDER BY o.orchard
            """
        ).fetchall()
        return [dict(r) for r in rows]
    rows = conn.execute(
        """
        SELECT s.orchard AS orchard,
               COALESCE(SUM(d.crates), 0) AS total_crates,
               COUNT(DISTINCT d.destination) AS destinations,
               COUNT(DISTINCT d.session) AS sessions
        FROM sessions s
        LEFT JOIN deliveries d ON d.session = s.name
        GROUP BY s.orchard
        ORDER BY s.orchard
        """
    ).fetchall()
    return [dict(r) for r in rows]


def orchard_entries(conn, orchard):
    """某果园按去向的箱数汇总 —— 核对包的全部内容，条目数只随去向数量变化。

    汇总启用后只查汇总表，否则扫描明细；两种路径的结果一致。
    """
    if summary_active(conn):
        rows = conn.execute(
            "SELECT destination, crates FROM crate_summary WHERE orchard = ? ORDER BY destination",
            (orchard,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT d.destination AS destination, SUM(d.crates) AS crates
            FROM deliveries d JOIN sessions s ON d.session = s.name
            WHERE s.orchard = ?
            GROUP BY d.destination
            ORDER BY d.destination
            """,
            (orchard,),
        ).fetchall()
    return [{"destination": r["destination"], "crates": r["crates"]} for r in rows]


def overview(conn):
    row = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM uploads)   AS uploads,
          (SELECT COUNT(*) FROM sessions)  AS sessions,
          (SELECT COUNT(DISTINCT orchard) FROM sessions) AS orchards,
          (SELECT COUNT(DISTINCT destination) FROM deliveries) AS destinations
        """
    ).fetchone()
    return dict(row)
