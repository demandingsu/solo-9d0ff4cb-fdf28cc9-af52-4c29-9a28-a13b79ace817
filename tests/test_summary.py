"""箱数汇总：构建生命周期、恰好计一次、中断重试、入账回滚、查询切换。"""
import json
import threading

import pytest

from app import builder, create_app, ledger
from app.db import get_db
from tests.conftest import upload_batch, upload_good_batch, download_package


@pytest.fixture()
def app(tmp_path):
    return_app = create_app(
        {
            "SECRET_KEY": "test-secret",
            "LEDGER_DB": str(tmp_path / "ledger.db"),
            "SUMMARY_BUILD_CHUNK_SIZE": 2,
            "SUMMARY_BUILD_DELAY_MS": 0,
            "SUMMARY_BUILD_STALE_SECONDS": 0,
        }
    )
    yield return_app


@pytest.fixture()
def client(app):
    return app.test_client()


def start_build(client):
    resp = client.post("/packages/summary/build", follow_redirects=False)
    assert resp.status_code == 302
    return resp


def finish_build(client):
    start_build(client)
    assert builder.wait_for_idle(), "构建线程未在限时内结束"


def status(app):
    with app.app_context():
        return ledger.summary_status(get_db())


def raw_crates(app):
    """绕过汇总表，直接从明细聚合（对照组）。"""
    with app.app_context():
        rows = get_db().execute(
            """
            SELECT s.orchard AS orchard, d.destination AS destination, SUM(d.crates) AS crates
            FROM deliveries d JOIN sessions s ON s.name = d.session
            GROUP BY s.orchard, d.destination
            """
        ).fetchall()
        return {(r["orchard"], r["destination"]): r["crates"] for r in rows}


def summary_crates(app):
    with app.app_context():
        rows = get_db().execute("SELECT orchard, destination, crates FROM crate_summary").fetchall()
        return {(r["orchard"], r["destination"]): r["crates"] for r in rows}


def raw_sessions(app):
    with app.app_context():
        rows = get_db().execute(
            """
            SELECT s.orchard AS orchard, COUNT(DISTINCT d.session) AS sessions
            FROM deliveries d JOIN sessions s ON s.name = d.session
            GROUP BY s.orchard
            """
        ).fetchall()
        return {r["orchard"]: r["sessions"] for r in rows}


def summary_sessions(app):
    with app.app_context():
        rows = get_db().execute("SELECT orchard, sessions FROM orchard_summary").fetchall()
        return {r["orchard"]: r["sessions"] for r in rows}


def upload_extra(client, session, orchard, destination, crates):
    return upload_batch(
        client,
        inbound=f"仓次,果园,箱数\n{session},{orchard},{crates}\n",
        transfers="起仓次,讫仓次,箱数\n",
        deliveries=f"仓次,类型,去向,箱数\n{session},交货,{destination},{crates}\n",
    )


# ---------------------------------------------------------------- 生命周期

def test_build_lifecycle_and_numbers_unchanged(app, client):
    upload_good_batch(client)
    assert status(app)["status"] == "off"

    before = download_package(client, "果园A")
    finish_build(client)

    st = status(app)
    assert st["status"] == "active"
    assert st["high_water_mark"] == st["total_uploads"] == 1

    # 启用前后核对包条目一致（格式不变，仅 generated_at/signature 不同）
    after = download_package(client, "果园A")
    assert after["entries"] == before["entries"]
    assert set(after.keys()) == set(before.keys())

    # 汇总表与明细聚合一致
    assert summary_crates(app) == raw_crates(app)
    assert summary_sessions(app) == raw_sessions(app)

    # 页面提示已启用
    body = client.get("/packages").get_data(as_text=True)
    assert "汇总已启用" in body


def test_packages_page_offers_first_build_when_off(client):
    upload_good_batch(client)
    body = client.get("/packages").get_data(as_text=True)
    assert "发起首次箱数汇总" in body


def test_rebuild_request_when_active_is_noop(app, client):
    upload_good_batch(client)
    finish_build(client)
    start_build(client)  # 已启用：不应再构建
    assert builder.wait_for_idle()
    assert status(app)["status"] == "active"


# ---------------------------------------------------------------- 恰好计一次

def test_posting_during_interrupted_build_counted_once(app, client):
    """构建中断 -> 继续按原账出包 -> 入账 -> 重试 -> 扫描期间入账的批次恰好计一次。"""
    app.config["SUMMARY_BUILD_MAX_CHUNKS"] = 1  # 处理 1 块后模拟崩溃
    app.config["SUMMARY_BUILD_CHUNK_SIZE"] = 1
    upload_good_batch(client)  # 第 1 批

    start_build(client)
    assert builder.wait_for_idle()
    st = status(app)
    assert st["status"] == "building"      # 中断：状态停留 building
    assert st["high_water_mark"] == 1

    # 中断期间：页面仍按原账出数，下载照常
    body = client.get("/packages").get_data(as_text=True)
    assert "重试构建" in body
    pkg = download_package(client, "果园A")
    assert {e["destination"]: e["crates"] for e in pkg["entries"]} == {
        "市场甲": 55, "市场乙": 40, "仓存": 5,
    }

    # 中断期间入账两批（building 状态：入账不写汇总，由续扫负责）
    assert upload_extra(client, "T1", "果园A", "市场甲", 20).status_code == 302
    assert upload_extra(client, "T2", "果园B", "市场丙", 7).status_code == 302

    # 重试：从水位继续，不重复计数
    app.config["SUMMARY_BUILD_MAX_CHUNKS"] = None
    finish_build(client)
    st = status(app)
    assert st["status"] == "active"
    assert st["high_water_mark"] == 3

    assert summary_crates(app) == raw_crates(app)
    assert summary_sessions(app) == raw_sessions(app)
    pkg = download_package(client, "果园A")
    assert {e["destination"]: e["crates"] for e in pkg["entries"]}["市场甲"] == 75


def test_posting_when_active_updates_summary_in_same_txn(app, client):
    upload_good_batch(client)
    finish_build(client)

    assert upload_extra(client, "T1", "果园A", "市场甲", 20).status_code == 302
    assert summary_crates(app) == raw_crates(app)
    pkg = download_package(client, "果园A")
    assert {e["destination"]: e["crates"] for e in pkg["entries"]}["市场甲"] == 75


def test_failed_post_rolls_back_summary_delta(app, client):
    upload_good_batch(client)
    finish_build(client)
    before = summary_crates(app)

    # 重复上传同一批：入账被拒，汇总增量也必须一起撤回（不变）
    resp = upload_batch(client)
    assert "请勿重复提交" in resp.get_data(as_text=True) or "已入账" in resp.get_data(as_text=True)
    assert summary_crates(app) == before

    # 不平衡批次：整批退回，汇总不变
    resp = upload_batch(
        client,
        inbound="仓次,果园,箱数\nT9,果园A,50\n",
        transfers="起仓次,讫仓次,箱数\n",
        deliveries="仓次,类型,去向,箱数\nT9,交货,市场甲,40\n",
    )
    assert "收发有差额" in resp.get_data(as_text=True)
    assert summary_crates(app) == before


def test_concurrent_posts_during_build(app, client):
    """构建与入账并发（不同批次）：最终汇总与明细聚合一致，每批恰好计一次。"""
    upload_good_batch(client)
    app.config["SUMMARY_BUILD_CHUNK_SIZE"] = 1
    app.config["SUMMARY_BUILD_DELAY_MS"] = 2

    errors = []

    def post_batches():
        c = app.test_client()
        for i in range(20):
            try:
                resp = upload_extra(c, f"T{i}", "果园A" if i % 2 else "果园B", "市场甲", 3)
                assert resp.status_code == 302, resp.get_data(as_text=True)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    poster = threading.Thread(target=post_batches)
    poster.start()
    start_build(client)
    poster.join()
    assert builder.wait_for_idle()
    assert not errors
    assert status(app)["status"] == "active"
    assert summary_crates(app) == raw_crates(app)
    assert summary_sessions(app) == raw_sessions(app)


# ---------------------------------------------------------------- 口径一致

def test_orchard_summaries_match_raw_semantics(app, client):
    """转仓链中间仓次无交货：仓次数口径与明细查询一致。"""
    upload_batch(
        client,
        inbound="仓次,果园,箱数\nS1,果园A,100\n",
        transfers="起仓次,讫仓次,箱数\nS1,S2,100\n",
        deliveries="仓次,类型,去向,箱数\nS2,交货,市场甲,100\n",
    )
    with app.app_context():
        before = ledger.orchard_summaries(get_db())
    finish_build(client)
    with app.app_context():
        after = ledger.orchard_summaries(get_db())
    assert before == after
    # S1 无交货记录，仓次数只计 S2
    assert after[0]["sessions"] == 1
    assert after[0]["total_crates"] == 100


def test_multiple_orchards_and_remaining_kind(app, client):
    upload_good_batch(client)
    finish_build(client)
    pkg_b = download_package(client, "果园B")
    assert {e["destination"]: e["crates"] for e in pkg_b["entries"]} == {
        "市场甲": 30, "市场丙": 45, "仓存": 5,
    }
    body = client.get("/").get_data(as_text=True)
    assert "果园B" in body
