"""核对包签发 / 校验 与 报数可行性判定。

核对包：只含果园、生成时间和“去向 + 箱数”汇总条目（条目数只随去向数量变化），
附 HMAC-SHA256 签名，防止市场侧篡改包内容。

报数判定：仅凭核对包与报数，判断整组数字能否由同一套符合原账的产地流向解释。
建模为单源单汇最大流：
    源 -> 果园（容量 = 账面总箱数）
    果园 -> 各去向（容量 = 账面该去向箱数）
    各去向 -> 汇（容量 = 该去向报数）
所有报数都被满足（最大流 = 报数合计）才可解释。
不符时：
  * 报数合计 ≠ 账面合计 -> 给出总数差额；
  * 否则给出超限的去向组合（最小割中位于汇一侧、报数超过账面供给的去向）。
"""
import hashlib
import hmac
import json
from dataclasses import dataclass, field

PACKAGE_TYPE = "orchard-reconciliation"
PACKAGE_VERSION = 1
MAX_PACKAGE_ENTRIES = 16  # 业务上最多 4 个去向，留余量防滥用

_SIGNED_KEYS = ("package_type", "version", "orchard", "generated_at", "entries")


class PackageError(Exception):
    """核对包本身不合法（结构或签名），problems 为问题列表。"""

    def __init__(self, problems):
        super().__init__("; ".join(problems))
        self.problems = list(problems)


# ---------------------------------------------------------------- 签发与校验

def _canonical(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _signature(payload, secret):
    body = {k: payload[k] for k in _SIGNED_KEYS}
    return hmac.new(secret.encode("utf-8"), _canonical(body), hashlib.sha256).hexdigest()


def make_package(orchard, entries, generated_at, secret):
    payload = {
        "package_type": PACKAGE_TYPE,
        "version": PACKAGE_VERSION,
        "orchard": orchard,
        "generated_at": generated_at,
        "entries": entries,
    }
    payload["signature"] = _signature(payload, secret)
    return payload


def package_to_bytes(package):
    return (json.dumps(package, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def load_and_verify_package(raw_bytes, secret):
    """解析并校验核对包；不合法抛 PackageError，合法返回 dict。"""
    try:
        package = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PackageError(["核对包不是有效的 UTF-8 JSON 文件"])

    problems = []
    if not isinstance(package, dict):
        raise PackageError(["核对包顶层必须是 JSON 对象"])

    if package.get("package_type") != PACKAGE_TYPE:
        problems.append("package_type 不正确")
    if package.get("version") != PACKAGE_VERSION:
        problems.append("核对包版本不受支持")
    orchard = package.get("orchard")
    if not isinstance(orchard, str) or not orchard.strip():
        problems.append("果园字段缺失或为空")
    if not isinstance(package.get("generated_at"), str):
        problems.append("生成时间字段缺失")

    entries = package.get("entries")
    if not isinstance(entries, list) or not entries:
        problems.append("核对包缺少去向汇总条目")
        entries = []
    elif len(entries) > MAX_PACKAGE_ENTRIES:
        problems.append(f"去向条目数异常（{len(entries)} 条）")

    seen = set()
    clean_entries = []
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            problems.append(f"第 {i + 1} 条去向汇总不是对象")
            continue
        dest, crates = e.get("destination"), e.get("crates")
        if not isinstance(dest, str) or not dest.strip():
            problems.append(f"第 {i + 1} 条去向名缺失")
            continue
        if not isinstance(crates, int) or isinstance(crates, bool) or crates < 0:
            problems.append(f"去向 {dest} 的箱数不是非负整数")
            continue
        if dest in seen:
            problems.append(f"去向 {dest} 在包中重复出现")
        seen.add(dest)
        clean_entries.append({"destination": dest, "crates": crates})

    signature = package.get("signature")
    if not isinstance(signature, str) or len(signature) != 64:
        problems.append("缺少有效签名")
    elif not problems:
        expected = _signature(package, secret)
        if not hmac.compare_digest(expected, signature):
            problems.append("核对包签名校验失败：包内容被篡改或不属于本库")

    if problems:
        raise PackageError(problems)
    package["entries"] = clean_entries
    return package


# ---------------------------------------------------------------- 报数判定

@dataclass
class Verdict:
    ok: bool
    reason: str = ""                 # "" | "total_difference" | "over_limit"
    total_ledger: int = 0
    total_reported: int = 0
    difference: int = 0              # 报数合计 - 账面合计
    over_limit: list = field(default_factory=list)  # [{destination, reported, ledger, excess}]
    deficits: list = field(default_factory=list)    # [{destination, reported, ledger, shortage}]
    rows: list = field(default_factory=list)        # 全量对照 [{destination, ledger, reported}]


def _flow_feasible(ledger_caps, reports):
    """对 源->果园->去向->汇 网络求最大流，返回所有报数是否都能被满足。

    网络规模极小（去向 ≤ 4），用 Edmonds-Karp。
    """
    dests = list(ledger_caps)
    n = len(dests)
    S, O, T = 0, 1, 2 + n
    N = T + 1
    cap = [[0] * N for _ in range(N)]

    total_ledger = sum(ledger_caps.values())
    cap[S][O] = total_ledger
    for i, d in enumerate(dests):
        cap[O][2 + i] = ledger_caps[d]
        cap[2 + i][T] = reports[d]

    flow = [[0] * N for _ in range(N)]

    def bfs():
        parent = [-1] * N
        parent[S] = S
        queue = [S]
        for u in queue:
            for v in range(N):
                if parent[v] == -1 and cap[u][v] - flow[u][v] > 0:
                    parent[v] = u
                    if v == T:
                        return parent
                    queue.append(v)
        return None

    while True:
        parent = bfs()
        if parent is None:
            break
        bottleneck = None
        v = T
        while v != S:
            u = parent[v]
            residual = cap[u][v] - flow[u][v]
            bottleneck = residual if bottleneck is None else min(bottleneck, residual)
            v = u
        v = T
        while v != S:
            u = parent[v]
            flow[u][v] += bottleneck
            flow[v][u] -= bottleneck
            v = u

    return sum(flow[S][v] for v in range(N)) == sum(reports.values())


def check_reports(entries, reports):
    """仅凭核对包条目与各去向报数，判定整组数字可否由同一套符合原账的产地流向解释。"""
    ledger = {e["destination"]: e["crates"] for e in entries}
    rows = [
        {"destination": d, "ledger": ledger[d], "reported": reports[d]}
        for d in sorted(ledger)
    ]
    total_ledger = sum(ledger.values())
    total_reported = sum(reports.values())
    verdict = Verdict(
        ok=False,
        total_ledger=total_ledger,
        total_reported=total_reported,
        difference=total_reported - total_ledger,
        rows=rows,
    )

    if total_reported != total_ledger:
        verdict.reason = "total_difference"
        return verdict

    if _flow_feasible(ledger, reports):
        verdict.ok = True
        return verdict

    # 该网络中去向的唯一供给边是 果园->去向，故超限组合即报数超过账面供给的去向集合
    verdict.reason = "over_limit"
    verdict.over_limit = [
        {
            "destination": d,
            "reported": reports[d],
            "ledger": ledger[d],
            "excess": reports[d] - ledger[d],
        }
        for d in sorted(ledger)
        if reports[d] > ledger[d]
    ]
    verdict.deficits = [
        {
            "destination": d,
            "reported": reports[d],
            "ledger": ledger[d],
            "shortage": ledger[d] - reports[d],
        }
        for d in sorted(ledger)
        if reports[d] < ledger[d]
    ]
    return verdict
