"""Upload über die YouTube Data API v3.

- OAuth 2.0 (Desktop-App) für den eigenen Kanal, Token in data/token.json
- videos.insert immer mit privacyStatus "private" (optional mit publishAt)
- thumbnails.set für das Vorschaubild
- Veröffentlichen/Planen nur über publish_video()/schedule_video() – beide prüfen die Freigaben erneut

Jede Funktion, die etwas an YouTube schickt, ruft zuerst gate.assert_upload_allowed() auf.
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone
from pathlib import Path

from . import uploadlog
from .config import Settings
from .creators import CreatorRegistry
from .export import load_manifest
from .gate import assert_upload_allowed, find_blockers
from .models import Project, ProjectError, now_utc

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube"]
RETRIABLE_STATUS = {500, 502, 503, 504}
MAX_RETRIES = 8


class YouTubeError(RuntimeError):
    pass


def get_service(settings: Settings):
    """Baut den API-Client; öffnet beim ersten Mal den Browser für den OAuth-Login."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise YouTubeError(
            "Google-API-Pakete fehlen: pip install 'publisher[youtube]'"
        ) from exc

    yt = settings["youtube"]
    token_file = settings.path(yt["token_file"])
    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            secret = settings.path(yt["client_secret_file"])
            if not secret.exists():
                raise YouTubeError(
                    f"OAuth-Client-Datei fehlt: {secret}. In der Google Cloud Console einen "
                    "OAuth-Client vom Typ „Desktop-App“ anlegen und die JSON-Datei dort ablegen."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
            creds = flow.run_local_server(port=0)
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(creds.to_json(), encoding="utf-8")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def _rfc3339(when: datetime) -> str:
    if when.tzinfo is None:
        raise ValueError("Veröffentlichungszeitpunkt braucht eine Zeitzone")
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _check_future(when: datetime) -> None:
    if when <= now_utc():
        raise ValueError("Geplanter Zeitpunkt muss in der Zukunft liegen")


def _resumable_upload(request) -> dict:
    from googleapiclient.errors import HttpError

    response, retry = None, 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                log.info("Upload: %d%%", int(status.progress() * 100))
        except HttpError as exc:
            if exc.resp.status not in RETRIABLE_STATUS or retry >= MAX_RETRIES:
                raise
            retry += 1
            time.sleep(min(2**retry, 64) + random.random())
        except (ConnectionError, TimeoutError):
            if retry >= MAX_RETRIES:
                raise
            retry += 1
            time.sleep(min(2**retry, 64) + random.random())
    return response


def upload_project(
    project: Project,
    registry: CreatorRegistry,
    settings: Settings,
    service=None,
    publish_at: datetime | None = None,
) -> dict:
    """Lädt das zuletzt exportierte Video privat hoch. Gesperrt, solange ein Clip nicht freigegeben ist."""
    assert_upload_allowed(project, registry)  # harte Sperre – vor jedem API-Aufruf

    manifest = load_manifest(project)
    if not manifest or not manifest.get("video"):
        raise ProjectError("Kein exportiertes Video vorhanden – zuerst exportieren.")
    video = Path(manifest["video"])
    if not video.exists():
        raise ProjectError(f"Exportiertes Video fehlt: {video}")
    if publish_at is None and project.meta.publish_at:
        publish_at = datetime.fromisoformat(project.meta.publish_at)
    if publish_at is not None:
        _check_future(publish_at)

    from googleapiclient.http import MediaFileUpload

    service = service or get_service(settings)
    yt = settings["youtube"]
    status = {"privacyStatus": "private", "selfDeclaredMadeForKids": False}
    if publish_at is not None:
        status["publishAt"] = _rfc3339(publish_at)
    body = {
        "snippet": {
            "title": manifest["title"],
            "description": manifest["description"],
            "tags": manifest["tags"],
            "categoryId": manifest["category_id"],
            "defaultLanguage": yt.get("default_language", "de"),
            "defaultAudioLanguage": yt.get("default_language", "de"),
        },
        "status": status,
    }
    media = MediaFileUpload(str(video), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    response = _resumable_upload(service.videos().insert(part="snippet,status", body=body, media_body=media))
    video_id = response["id"]

    thumb_ok = None
    if project.meta.thumbnail:
        thumb = settings.path(project.meta.thumbnail)
        if thumb.exists():
            service.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(str(thumb))).execute()
            thumb_ok = str(thumb)
        else:
            log.warning("Thumbnail %s nicht gefunden – übersprungen", thumb)

    record = {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "uploaded_at": now_utc().isoformat(),
        "privacy": "private",
        "publish_at": status.get("publishAt"),
        "published_at": None,
        "manifest": project.last_export["manifest"],
        "thumbnail": thumb_ok,
    }
    project.uploads.append(record)
    project.save()
    uploadlog.append(
        settings.upload_log_file,
        "upload",
        project=project.name,
        video_id=video_id,
        url=record["url"],
        title=manifest["title"],
        is_short=manifest["is_short"],
        privacy="private",
        publish_at=record["publish_at"],
        video_file=str(video),
        sources=manifest["clips"],
        creators=sorted({c["creator_name"] for c in manifest["clips"]}),
    )
    return record


def _current_upload(project: Project) -> dict:
    upload = project.last_upload
    if not upload:
        raise ProjectError("Dieses Projekt wurde noch nicht hochgeladen.")
    return upload


def _update_status(service, video_id: str, changes: dict, drop: tuple[str, ...] = ()) -> dict:
    items = service.videos().list(part="status", id=video_id).execute().get("items", [])
    if not items:
        raise YouTubeError(f"Video {video_id} nicht gefunden")
    status = items[0]["status"]
    for key in drop:
        status.pop(key, None)
    status.update(changes)
    keep = {"privacyStatus", "publishAt", "selfDeclaredMadeForKids", "license", "embeddable", "publicStatsViewable"}
    status = {k: v for k, v in status.items() if k in keep}
    return service.videos().update(part="status", body={"id": video_id, "status": status}).execute()


def publish_video(project: Project, registry: CreatorRegistry, settings: Settings, service=None) -> dict:
    """Sofort öffentlich schalten – nur nach Bestätigung in der Review-UI aufrufen."""
    assert_upload_allowed(project, registry)
    upload = _current_upload(project)
    service = service or get_service(settings)
    _update_status(service, upload["video_id"], {"privacyStatus": "public"}, drop=("publishAt",))
    upload.update(privacy="public", publish_at=None, published_at=now_utc().isoformat())
    project.save()
    uploadlog.append(settings.upload_log_file, "publish", project=project.name, video_id=upload["video_id"])
    return upload


def schedule_video(
    project: Project, registry: CreatorRegistry, settings: Settings, when: datetime, service=None
) -> dict:
    """Privat lassen und zum Zeitpunkt `when` automatisch veröffentlichen."""
    assert_upload_allowed(project, registry)
    _check_future(when)
    upload = _current_upload(project)
    service = service or get_service(settings)
    _update_status(service, upload["video_id"], {"privacyStatus": "private", "publishAt": _rfc3339(when)})
    upload.update(privacy="private", publish_at=_rfc3339(when))
    project.save()
    uploadlog.append(
        settings.upload_log_file, "schedule", project=project.name, video_id=upload["video_id"], publish_at=upload["publish_at"]
    )
    return upload


def revoke_schedule_if_blocked(project: Project, registry: CreatorRegistry, settings: Settings, service=None) -> bool:
    """Wurde eine Freigabe nach dem Planen entzogen, wird die geplante Veröffentlichung aufgehoben."""
    upload = project.last_upload
    if not upload or not upload.get("publish_at") or upload.get("published_at"):
        return False
    blockers = find_blockers(project, registry)
    if not blockers:
        return False
    service = service or get_service(settings)
    _update_status(service, upload["video_id"], {"privacyStatus": "private"}, drop=("publishAt",))
    upload.update(publish_at=None)
    project.save()
    uploadlog.append(
        settings.upload_log_file,
        "schedule_revoked",
        project=project.name,
        video_id=upload["video_id"],
        reasons=[b.describe() for b in blockers],
    )
    return True
