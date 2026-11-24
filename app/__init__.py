"""苹果箱数核对平台 —— Flask 应用工厂。"""
import os

from flask import Flask

from . import db
from .routes import bp


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        # 用于核对包 HMAC 签名的密钥，生产环境必须通过 APP_SECRET_KEY 注入
        SECRET_KEY=os.environ.get("APP_SECRET_KEY", "dev-secret-change-me"),
        LEDGER_DB=os.environ.get("LEDGER_DB", os.path.join(os.getcwd(), "data", "ledger.db")),
        # 上传的 CSV / 核对包都很小，限制 2MB 防止滥用
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
        # 箱数汇总后台构建：每块处理的批数、块间停顿（演示/调压用）、心跳超时（超时后可接管重试）
        SUMMARY_BUILD_CHUNK_SIZE=int(os.environ.get("SUMMARY_BUILD_CHUNK_SIZE", "100")),
        SUMMARY_BUILD_DELAY_MS=int(os.environ.get("SUMMARY_BUILD_DELAY_MS", "0")),
        SUMMARY_BUILD_STALE_SECONDS=float(os.environ.get("SUMMARY_BUILD_STALE_SECONDS", "15")),
        SUMMARY_BUILD_MAX_CHUNKS=None,  # 测试钩子：处理 N 块后退出，模拟构建中断
    )
    if test_config:
        app.config.update(test_config)

    db.init_app(app)
    app.register_blueprint(bp)
    return app
