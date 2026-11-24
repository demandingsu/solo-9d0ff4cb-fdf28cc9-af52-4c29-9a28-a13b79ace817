"""页面路由：上传台账、下载核对包、市场核对。"""
import json
from datetime import datetime, timezone

from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from . import checker, ledger
from .db import get_db

bp = Blueprint("main", __name__)


@bp.get("/")
def index():
    conn = get_db()
    return render_template(
        "index.html",
        overview=ledger.overview(conn),
        orchards=ledger.orchard_summaries(conn),
        destinations=sorted(ledger.existing_destinations(conn)),
        max_destinations=ledger.MAX_DESTINATIONS,
    )


# ---------------------------------------------------------------- 上传台账

@bp.route("/upload", methods=["GET", "POST"])
def upload():
    problems = []
    if request.method == "POST":
        files = {
            "inbound": request.files.get("inbound"),
            "transfers": request.files.get("transfers"),
            "deliveries": request.files.get("deliveries"),
        }
        missing = [name for name, f in files.items() if f is None or f.filename == ""]
        if missing:
            problems.append("三个 CSV 都必须上传：" + ", ".join(missing) + " 缺失")
        else:
            raw = {name: f.read() for name, f in files.items()}
            try:
                batch = ledger.parse_batch(raw["inbound"], raw["transfers"], raw["deliveries"])
                conn = get_db()
                session_orchard = ledger.validate_batch(
                    batch, ledger.existing_sessions(conn), ledger.existing_destinations(conn)
                )
                digest = ledger.batch_hash(raw["inbound"], raw["transfers"], raw["deliveries"])
                ledger.persist_batch(conn, digest, batch, session_orchard)
            except ledger.BatchRejected as exc:
                problems = exc.problems
            else:
                flash(
                    f"入账成功：入库 {len(batch.inbound)} 行、转仓 {len(batch.transfers)} 行、"
                    f"交货/余箱 {len(batch.deliveries)} 行。",
                    "ok",
                )
                return redirect(url_for("main.index"))
        # 校验未通过：本次上传全部退回，不入账
        for p in problems:
            flash(p, "error")
        if problems:
            flash("本次上传已退回，请修正后重新提交。", "error")
    return render_template(
        "upload.html",
        inbound_headers=",".join(ledger.INBOUND_HEADERS),
        transfer_headers=",".join(ledger.TRANSFER_HEADERS),
        delivery_headers=",".join(ledger.DELIVERY_HEADERS),
        max_destinations=ledger.MAX_DESTINATIONS,
    )


# ---------------------------------------------------------------- 核对包

@bp.get("/packages")
def packages():
    conn = get_db()
    summaries = ledger.orchard_summaries(conn)
    for s in summaries:
        s["entries"] = ledger.orchard_entries(conn, s["orchard"])
    return render_template("packages.html", orchards=summaries)


@bp.get("/packages/<path:orchard>/download")
def download_package(orchard):
    conn = get_db()
    entries = ledger.orchard_entries(conn, orchard)
    if not entries:
        flash(f"果园 {orchard} 没有任何去向汇总，无法生成核对包。", "error")
        return redirect(url_for("main.packages"))
    package = checker.make_package(
        orchard=orchard,
        entries=entries,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        secret=current_app.config["SECRET_KEY"],
    )
    payload = checker.package_to_bytes(package)
    filename = _urlquote(f"package_{orchard}.json")
    return Response(
        payload,
        mimetype="application/json; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename=package.json; filename*=UTF-8''{filename}"
        },
    )


def _urlquote(text):
    from urllib.parse import quote

    return quote(text)


# ---------------------------------------------------------------- 市场核对

@bp.route("/verify", methods=["GET", "POST"])
def verify():
    if request.method == "GET":
        return render_template("verify_upload.html")

    upload_file = request.files.get("package")
    if upload_file is None or upload_file.filename == "":
        flash("请上传核对包文件。", "error")
        return render_template("verify_upload.html"), 400
    try:
        package = checker.load_and_verify_package(
            upload_file.read(), current_app.config["SECRET_KEY"]
        )
    except checker.PackageError as exc:
        for p in exc.problems:
            flash(p, "error")
        return render_template("verify_upload.html"), 400

    return render_template(
        "verify_report.html",
        package=package,
        package_json=json.dumps(package, ensure_ascii=False),
    )


@bp.post("/verify/check")
def verify_check():
    package_raw = request.form.get("package", "")
    try:
        package = checker.load_and_verify_package(
            package_raw.encode("utf-8"), current_app.config["SECRET_KEY"]
        )
    except checker.PackageError as exc:
        for p in exc.problems:
            flash(p, "error")
        return render_template("verify_upload.html"), 400

    destinations = request.form.getlist("destination")
    reports_raw = request.form.getlist("report")
    entries = package["entries"]
    expected = {e["destination"] for e in entries}

    problems = []
    if set(destinations) != expected or len(destinations) != len(reports_raw):
        problems.append("报数去向与核对包不一致，请重新上传核对包。")
    reports = {}
    for dest, raw in zip(destinations, reports_raw):
        text = (raw or "").strip()
        if not text.isdigit():
            problems.append(f"去向 {dest} 的报数 “{raw}” 不是非负整数（按整箱记数）")
            continue
        reports[dest] = int(text)
    if problems:
        for p in problems:
            flash(p, "error")
        return render_template(
            "verify_report.html",
            package=package,
            package_json=json.dumps(package, ensure_ascii=False),
        ), 400

    verdict = checker.check_reports(entries, reports)
    return render_template(
        "verify_result.html", package=package, verdict=verdict
    )
