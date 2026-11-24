"""端到端：上传 -> 下载核对包 -> 市场核对。"""
import io
import json

from app import checker
from tests.conftest import (
    GOOD_DELIVERIES,
    download_package,
    upload_batch,
    upload_good_batch,
    verify_with_reports,
)


def post_package(client, pkg):
    return client.post(
        "/verify",
        data={"package": (io.BytesIO(json.dumps(pkg, ensure_ascii=False).encode("utf-8")), "pkg.json")},
        content_type="multipart/form-data",
    )


def test_upload_ok_and_package_entries(client):
    upload_good_batch(client)
    pkg_a = download_package(client, "果园A")
    assert pkg_a["orchard"] == "果园A"
    entries = {e["destination"]: e["crates"] for e in pkg_a["entries"]}
    # 果园A：S1 交货市场甲55 + 余箱仓存5，S2（S1 转入）交货市场乙40
    assert entries == {"市场甲": 55, "市场乙": 40, "仓存": 5}
    # 条目数只随去向数量变化
    assert len(pkg_a["entries"]) == 3

    pkg_b = download_package(client, "果园B")
    entries_b = {e["destination"]: e["crates"] for e in pkg_b["entries"]}
    assert entries_b == {"市场甲": 30, "市场丙": 45, "仓存": 5}


def test_unbalanced_upload_rejected_and_nothing_persisted(client):
    resp = upload_batch(
        client,
        inbound="仓次,果园,箱数\nS9,果园A,50\n",
        transfers="起仓次,讫仓次,箱数\nS9,S10,20\n",
        deliveries="仓次,类型,去向,箱数\nS9,交货,市场甲,60\nS10,交货,市场乙,25\n",
    )
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200  # 退回上传页
    assert "仓次 S9 收发有差额" in body
    assert "仓次 S10 收发有差额" in body
    assert "本次上传已退回" in body
    # 未入账：总览仍为空
    index = client.get("/").get_data(as_text=True)
    assert "台账为空" in index


def test_duplicate_batch_rejected(client):
    upload_good_batch(client)
    resp = upload_batch(client)
    body = resp.get_data(as_text=True)
    assert "请勿重复提交" in body or "已入账" in body


def test_verify_ok(client):
    upload_good_batch(client)
    pkg = download_package(client, "果园A")
    resp = verify_with_reports(client, pkg, {"市场甲": 55, "市场乙": 40, "仓存": 5})
    body = resp.get_data(as_text=True)
    assert "相符" in body


def test_verify_total_difference(client):
    upload_good_batch(client)
    pkg = download_package(client, "果园A")
    resp = verify_with_reports(client, pkg, {"市场甲": 55, "市场乙": 40, "仓存": 3})
    body = resp.get_data(as_text=True)
    assert "总数差额" in body
    assert "-2" in body  # 98 - 100


def test_verify_over_limit_combination(client):
    upload_good_batch(client)
    pkg = download_package(client, "果园A")
    # 总数不变（100），市场甲多报 10，市场乙少报 10
    resp = verify_with_reports(client, pkg, {"市场甲": 65, "市场乙": 30, "仓存": 5})
    body = resp.get_data(as_text=True)
    assert "超限" in body
    assert "市场甲" in body
    assert "+10" in body


def test_verify_tampered_package_rejected(client):
    upload_good_batch(client)
    pkg = download_package(client, "果园A")
    pkg["entries"][0]["crates"] += 1  # 篡改箱数
    resp = post_package(client, pkg)
    assert resp.status_code == 400
    assert "签名校验失败" in resp.get_data(as_text=True)


def test_verify_wrong_secret_rejected(client):
    upload_good_batch(client)
    pkg = download_package(client, "果园A")
    # 用别的密钥重签 -> 仍应失败
    pkg["signature"] = checker._signature(pkg, "another-secret")
    resp = post_package(client, pkg)
    assert resp.status_code == 400
    assert "签名校验失败" in resp.get_data(as_text=True)


def test_package_is_self_contained_summary(client):
    """核对包只含去向与箱数汇总，不泄露仓次/批次细节。"""
    upload_good_batch(client)
    pkg = download_package(client, "果园A")
    assert set(pkg.keys()) == {"package_type", "version", "orchard", "generated_at", "entries", "signature"}
    assert all(set(e.keys()) == {"destination", "crates"} for e in pkg["entries"])
    body = json.dumps(pkg, ensure_ascii=False)
    assert "S1" not in body and "S2" not in body


def test_second_upload_accumulates(client):
    upload_good_batch(client)
    resp = upload_batch(
        client,
        inbound="仓次,果园,箱数\nT1,果园A,20\n",
        transfers="起仓次,讫仓次,箱数\n",
        deliveries="仓次,类型,去向,箱数\nT1,交货,市场甲,20\n",
    )
    assert resp.status_code == 302
    pkg = download_package(client, "果园A")
    entries = {e["destination"]: e["crates"] for e in pkg["entries"]}
    assert entries["市场甲"] == 75  # 55 + 20


def test_missing_file_rejected(client):
    resp = client.post(
        "/upload",
        data={"inbound": (__import__("io").BytesIO(GOOD_DELIVERIES.encode()), "x.csv")},
        content_type="multipart/form-data",
    )
    assert "都必须上传" in resp.get_data(as_text=True)
