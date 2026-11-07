import os
from datetime import timedelta

from flask import Flask, flash, redirect, render_template, request, session, url_for
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFError, CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

from .mealie import MealieAuthError, MealieError
from .models import db

csrf = CSRFProtect()
migrate = Migrate(render_as_batch=True)  # batch mode so SQLite can alter columns


def create_app(config=None):
    app = Flask(__name__)
    data_dir = os.environ.get("GRAVYBOAT_DATA", os.path.join(os.getcwd(), "data"))
    app.config.update(
        SECRET_KEY=os.environ.get("GRAVYBOAT_SECRET_KEY"),
        SQLALCHEMY_DATABASE_URI=os.environ.get("GRAVYBOAT_DATABASE_URL", f"sqlite:///{os.path.join(data_dir, 'gravyboat.db')}"),
        MEALIE_URL=os.environ.get("MEALIE_URL", "http://mealie:9000"),
        MEALIE_PUBLIC_URL=os.environ.get("MEALIE_PUBLIC_URL") or os.environ.get("MEALIE_URL", ""),
        MEALIE_GROUP_SLUG=os.environ.get("MEALIE_GROUP_SLUG", "home"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("GRAVYBOAT_INSECURE_COOKIES") != "1",
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
        WTF_CSRF_TIME_LIMIT=None,
    )
    if config:
        app.config.update(config)
    if not app.config["SECRET_KEY"]:
        raise RuntimeError("GRAVYBOAT_SECRET_KEY must be set")
    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:///") and "memory" not in app.config["SQLALCHEMY_DATABASE_URI"]:
        os.makedirs(os.path.dirname(app.config["SQLALCHEMY_DATABASE_URI"][len("sqlite:///") :]), exist_ok=True)

    # Number of reverse proxies in front of the app whose X-Forwarded-* headers to trust (HTTPS
    # detection, client address for the sign-in limit). Set 0 when nothing sits in front of it.
    proxies = int(os.environ.get("GRAVYBOAT_TRUSTED_PROXIES", "1"))
    if proxies > 0:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=proxies, x_proto=proxies, x_host=proxies)
    db.init_app(app)
    migrate.init_app(app, db, directory=os.path.join(os.path.dirname(os.path.dirname(__file__)), "migrations"))
    csrf.init_app(app)

    from sqlalchemy import event as sa_event

    with app.app_context():
        engine = db.engine

        @sa_event.listens_for(engine, "connect")
        def _sqlite_pragmas(conn, _record):
            cur = conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

    from . import filters
    from .auth import bp as auth_bp
    from .guest import bp as guest_bp
    from .host import bp as host_bp

    filters.register(app)
    app.register_blueprint(auth_bp)
    app.register_blueprint(host_bp)
    app.register_blueprint(guest_bp)

    @app.errorhandler(MealieAuthError)
    def _expired(_e):
        guest_ids = {k: v for k, v in session.items() if k.startswith("guest_")}
        session.clear()
        session.update(guest_ids)
        flash("Your Mealie sign-in expired. Sign in again.", "warn")
        return redirect(url_for("auth.login", next=request.path))

    @app.errorhandler(MealieError)
    def _mealie_down(e):
        return render_template("error.html", title="Mealie problem", message=str(e)), 502

    @app.errorhandler(CSRFError)
    def _csrf(_e):
        return render_template("error.html", title="Form expired", message="That form was stale. Go back, reload and try again."), 400

    @app.errorhandler(500)
    def _500(_e):
        return render_template("error.html", title="Something broke", message="Gravy Boat hit an error. Try again, and if it keeps happening, check the logs."), 500

    @app.errorhandler(404)
    def _404(_e):
        return render_template("error.html", title="Not found", message="There's nothing here."), 404

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' https: http: data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; font-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'self'",
        )
        return resp

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app
