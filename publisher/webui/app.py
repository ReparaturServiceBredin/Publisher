"""Review-UI (Flask). Nur für den lokalen Betrieb gedacht (Standard: 127.0.0.1)."""

from __future__ import annotations

import secrets
from datetime import datetime

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for

from .. import uploadlog
from ..config import load_settings
from ..creators import OWN_VOD, CreatorRegistry
from ..gate import UploadBlocked, find_blockers
from ..models import CLIP_STATUSES, ProjectError, ProjectStore
from ..timefmt import format_de_datetime, format_hms, parse_timestamp, vod_link_at


def create_app(config_path: str | None = None, youtube_service_factory=None) -> Flask:
    app = Flask(__name__)
    app.secret_key = secrets.token_hex(32)
    settings = load_settings(config_path)
    store = ProjectStore(settings.projects_dir)
    app.config["PUBLISHER_SETTINGS"] = settings

    def registry() -> CreatorRegistry:
        # bei jedem Request frisch laden, damit Status-Änderungen sofort greifen
        return CreatorRegistry.load(settings.creators_file)

    def yt_service():
        if youtube_service_factory:
            return youtube_service_factory()
        from ..youtube import get_service

        return get_service(settings)

    def load_project(name: str):
        try:
            return store.load(name)
        except ProjectError:
            abort(404)

    app.jinja_env.filters["hms"] = lambda s: "–" if s is None else format_hms(s)
    app.jinja_env.filters["de_dt"] = format_de_datetime
    app.jinja_env.globals["vod_link_at"] = vod_link_at
    app.jinja_env.globals["OWN_VOD"] = OWN_VOD

    @app.before_request
    def csrf_protect():
        token = session.setdefault("csrf", secrets.token_hex(16))
        if request.method == "POST" and request.form.get("csrf") != token:
            abort(400, "Ungültiges Formular-Token – Seite neu laden.")

    @app.context_processor
    def inject_csrf():
        return {"csrf_token": session.get("csrf", "")}

    @app.get("/")
    def index():
        projects = []
        for name in store.list():
            p = store.load(name)
            projects.append((p, find_blockers(p, registry())))
        return render_template("index.html", projects=projects, log=uploadlog.read(settings.upload_log_file)[-20:])

    @app.post("/projects")
    def create_project():
        try:
            store.create(request.form.get("name", "").strip())
        except ProjectError as exc:
            flash(str(exc), "error")
        return redirect(url_for("index"))

    @app.get("/creators")
    def creators():
        return render_template("creators.html", creators=list(registry()), path=settings.creators_file)

    @app.get("/p/<name>")
    def project(name: str):
        p = load_project(name)
        reg = registry()
        blockers = find_blockers(p, reg)
        blocked_ids = {b.clip_id for b in blockers}
        manifest = None
        stale = False
        from ..export import load_manifest

        manifest = load_manifest(p)
        if manifest:
            exported = [(c["clip_id"], c["vod_start"], c["vod_end"]) for c in manifest["clips"]]
            current = [(c.id, c.trim_start, c.trim_end) for c in p.selected_clips()]
            stale = exported != current
        return render_template(
            "project.html",
            p=p,
            reg=reg,
            clips=sorted(p.candidates, key=lambda c: (c.order, c.trim_start)),
            statuses=CLIP_STATUSES,
            blockers=blockers,
            blocked_ids=blocked_ids,
            manifest=manifest,
            stale=stale,
            upload=p.last_upload,
            log=uploadlog.read(settings.upload_log_file, p.name),
        )

    @app.post("/p/<name>/meta")
    def update_meta(name: str):
        p = load_project(name)
        f = request.form
        p.meta.title = f.get("title", "").strip()
        p.meta.tags = [t.strip() for t in f.get("tags", "").split(",") if t.strip()]
        p.meta.category_id = f.get("category_id", "").strip()
        p.meta.is_short = f.get("is_short") == "on"
        p.meta.thumbnail = f.get("thumbnail", "").strip()
        p.meta.intro_text = f.get("intro_text", "").strip()
        publish_at = f.get("publish_at", "").strip()
        p.meta.publish_at = (
            datetime.fromisoformat(publish_at).replace(tzinfo=settings.tz).isoformat() if publish_at else ""
        )
        p.save()
        flash("Metadaten gespeichert.", "ok")
        return redirect(url_for("project", name=name))

    @app.post("/p/<name>/clip/<clip_id>")
    def edit_clip(name: str, clip_id: str):
        p = load_project(name)
        f = request.form
        try:
            p.apply_review_edit(
                clip_id,
                {
                    "title": f["title"],
                    "status": f["status"],
                    "order": int(f["order"]),
                    "trim_start": parse_timestamp(f["trim_start"]),
                    "trim_end": parse_timestamp(f["trim_end"]),
                },
            )
            p.save()
            flash("Clip gespeichert.", "ok")
        except (ProjectError, ValueError, KeyError) as exc:
            flash(f"Clip nicht gespeichert: {exc}", "error")
        return redirect(url_for("project", name=name) + f"#{clip_id}")

    @app.post("/p/<name>/add-clip")
    def add_clip(name: str):
        p = load_project(name)
        f = request.form
        try:
            p.add_candidate(
                f["vod_id"], parse_timestamp(f["start"]), parse_timestamp(f["end"]), f.get("title", "")
            )
            p.save()
        except (ProjectError, ValueError, KeyError) as exc:
            flash(f"Clip nicht angelegt: {exc}", "error")
        return redirect(url_for("project", name=name))

    @app.post("/p/<name>/intro")
    def regenerate_intro(name: str):
        from ..llm import generate_intro

        p = load_project(name)
        text, warning = generate_intro(p, p.selected_clips(), settings)
        p.meta.intro_text = text
        p.save()
        flash(warning or "Intro neu generiert.", "error" if warning else "ok")
        return redirect(url_for("project", name=name))

    @app.post("/p/<name>/export")
    def export(name: str):
        from ..export import export_project

        p = load_project(name)
        try:
            manifest = export_project(p, registry(), settings, do_render=request.form.get("render") == "on")
            flash("Export fertig.", "ok")
            for w in manifest["warnings"]:
                flash(w, "error")
        except Exception as exc:  # Render-/Validierungsfehler anzeigen statt 500
            flash(f"Export fehlgeschlagen: {exc}", "error")
        return redirect(url_for("project", name=name))

    def _guarded(name: str, action):
        p = load_project(name)
        try:
            action(p)
        except UploadBlocked as exc:
            flash(str(exc), "error")
        except Exception as exc:
            flash(f"Fehlgeschlagen: {exc}", "error")
        return redirect(url_for("project", name=name))

    @app.post("/p/<name>/upload")
    def upload(name: str):
        from ..youtube import upload_project

        def act(p):
            rec = upload_project(p, registry(), settings, service=yt_service())
            flash(f"Privat hochgeladen: {rec['url']}", "ok")

        return _guarded(name, act)

    @app.post("/p/<name>/publish")
    def publish(name: str):
        from ..youtube import publish_video

        if request.form.get("confirm") != "ja":
            flash("Bitte die Veröffentlichung ausdrücklich bestätigen.", "error")
            return redirect(url_for("project", name=name))

        def act(p):
            publish_video(p, registry(), settings, service=yt_service())
            flash("Video ist jetzt öffentlich.", "ok")

        return _guarded(name, act)

    @app.post("/p/<name>/schedule")
    def schedule(name: str):
        from ..youtube import schedule_video

        def act(p):
            when = datetime.fromisoformat(request.form["when"]).replace(tzinfo=settings.tz)
            rec = schedule_video(p, registry(), settings, when, service=yt_service())
            flash(f"Veröffentlichung geplant: {rec['publish_at']}", "ok")

        return _guarded(name, act)

    return app
