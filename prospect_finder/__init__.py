"""Roofing SMMA Prospect Finder."""

from __future__ import annotations

import secrets

from flask import Flask

from .config import Config


def create_app(config: Config | None = None) -> Flask:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:  # python-dotenv is optional
        pass
    config = config or Config.from_env()
    app = Flask(__name__)
    app.config["PF"] = config
    app.config["SECRET_KEY"] = config.secret_key
    if config.secret_key == Config.secret_key:
        app.config["SECRET_KEY"] = secrets.token_hex(16)  # random per process when unset
    app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024

    from . import cli, web

    web.init_app(app)
    cli.init_app(app)
    return app
