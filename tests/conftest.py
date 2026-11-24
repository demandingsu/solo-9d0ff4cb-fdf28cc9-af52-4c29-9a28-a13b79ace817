import io
import json

import pytest

from app import create_app


@pytest.fixture()
def app(tmp_path):
    return_app = create_app(
        {
            "SECRET_KEY": "test-secret",
            "LEDGER_DB": str(tmp_path / "ledger.db"),
        }
    )
    yield return_app


@pytest.fixture()
def client(app):
    return app.test_client()


def csv_bytes(text):
    return io.BytesIO(text.encode("utf-8"))


GOOD_INBOUND = "仓次,果园,箱数\nS1,果园A,100\nS3,果园B,80\n"
GOOD_TRANSFERS = "起仓次,讫仓次,箱数\nS1,S2,40\n"
GOOD_DELIVERIES = (
    "仓次,类型,去向,箱数\n"
    "S1,交货,市场甲,55\n"
    "S1,余箱,仓存,5\n"
    "S2,交货,市场乙,40\n"
    "S3,交货,市场甲,30\n"
    "S3,交货,市场丙,45\n"
    "S3,余箱,仓存,5\n"
)


def upload_batch(client, inbound=GOOD_INBOUND, transfers=GOOD_TRANSFERS, deliveries=GOOD_DELIVERIES):
    return client.post(
        "/upload",
        data={
            "inbound": (csv_bytes(inbound), "inbound.csv"),
            "transfers": (csv_bytes(transfers), "transfers.csv"),
            "deliveries": (csv_bytes(deliveries), "deliveries.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )


def upload_good_batch(client):
    resp = upload_batch(client)
    assert resp.status_code == 302, resp.get_data(as_text=True)
    return resp


def download_package(client, orchard="果园A"):
    resp = client.get(f"/packages/{orchard}/download")
    assert resp.status_code == 200
    return json.loads(resp.get_data(as_text=True))


def verify_with_reports(client, package, reports):
    """走完整两步核对流程，reports 为 {去向: 箱数}，返回结果页响应。"""
    resp = client.post(
        "/verify",
        data={"package": (io.BytesIO(json.dumps(package, ensure_ascii=False).encode("utf-8")), "pkg.json")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    # 多值字段用 MultiDict 传递（dict 会覆盖重复键）
    from werkzeug.datastructures import MultiDict

    form = MultiDict()
    form.add("package", json.dumps(package, ensure_ascii=False))
    for e in package["entries"]:
        form.add("destination", e["destination"])
        form.add("report", str(reports[e["destination"]]))
    return client.post("/verify/check", data=form)
