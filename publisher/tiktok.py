"""TikTok verbinden (Login Kit for Desktop, OAuth 2.0 + PKCE) und Videos direkt posten (Content Posting API).

Einmalige Einrichtung (TikTok-Vorgabe, geht nicht ohne):
  1. Auf developers.tiktok.com eine App anlegen, Produkte „Login Kit“ und „Content Posting API“
     (Direct Post) hinzufügen, Scopes user.info.basic + video.publish.
  2. Bei Login Kit → Desktop als Redirect-URI eintragen:  http://127.0.0.1:3455/callback/
  3. Client Key + Client Secret in der App unter „Verbindungen“ eintragen.
Solange TikTok die App nicht geprüft hat, erlaubt TikTok nur private Posts („Nur ich“).
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import secrets
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable

from .config import Settings
from .models import now_utc

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
USER_URL = "https://open.tiktokapis.com/v2/user/info/?fields=display_name,avatar_url"
CREATOR_INFO_URL = "https://open.tiktokapis.com/v2/post/publish/creator_info/query/"
INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
SCOPES = "user.info.basic,video.publish"

MIN_CHUNK = 5 * 1024 * 1024
DEFAULT_CHUNK = 10 * 1024 * 1024
MAX_CHUNK = 64 * 1024 * 1024


class TikTokError(RuntimeError):
    pass


# ---------------------------------------------------------------- Konfiguration / Token

def _cfg(settings: Settings) -> dict:
    return settings.raw.setdefault("tiktok", {})


def credentials_file(settings: Settings) -> Path:
    return settings.data_dir / "tiktok_app.json"


def token_file(settings: Settings) -> Path:
    return settings.data_dir / "tiktok_token.json"


def save_app_credentials(settings: Settings, client_key: str, client_secret: str) -> None:
    f = credentials_file(settings)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"client_key": client_key.strip(), "client_secret": client_secret.strip()}), encoding="utf-8")


def app_credentials(settings: Settings) -> tuple[str, str] | None:
    f = credentials_file(settings)
    if not f.exists():
        return None
    data = json.loads(f.read_text(encoding="utf-8"))
    return (data["client_key"], data["client_secret"]) if data.get("client_key") else None


def is_connected(settings: Settings) -> bool:
    return token_file(settings).exists()


def disconnect(settings: Settings) -> None:
    token_file(settings).unlink(missing_ok=True)


def redirect_uri(settings: Settings) -> str:
    port = int(_cfg(settings).get("redirect_port", 3455))
    return f"http://127.0.0.1:{port}/callback/"


def pkce_pair(encoding: str = "hex") -> tuple[str, str]:
    """TikTok Desktop verlangt laut Doku SHA256 als Hex; base64url (RFC 7636) per Einstellung möglich."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    verifier = "".join(secrets.choice(alphabet) for _ in range(64))
    digest = hashlib.sha256(verifier.encode()).digest()
    if encoding == "base64url":
        challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    else:
        challenge = digest.hex()
    return verifier, challenge


def _http(url: str, *, data: bytes | None = None, headers: dict | None = None, method: str | None = None) -> dict:
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            payload = json.loads(body)
        except ValueError:
            raise TikTokError(f"TikTok-Fehler {exc.code}: {body[:300]!r}") from exc
        raise TikTokError(_error_text(payload) or f"TikTok-Fehler {exc.code}") from exc
    return json.loads(body) if body else {}


def _error_text(payload: dict) -> str | None:
    err = payload.get("error")
    if isinstance(err, dict) and err.get("code") not in (None, "ok"):
        return f"TikTok: {err.get('code')} – {err.get('message', '')}"
    if isinstance(err, str) and err:
        return f"TikTok: {err} – {payload.get('error_description', '')}"
    return None


def _form(url: str, fields: dict) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    payload = _http(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    if "access_token" not in payload:
        raise TikTokError(_error_text(payload) or f"Kein Token erhalten: {payload}")
    return payload


def _store_token(settings: Settings, payload: dict) -> dict:
    now = time.time()
    token = {
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token", ""),
        "open_id": payload.get("open_id", ""),
        "expires_at": now + int(payload.get("expires_in", 86400)) - 60,
        "refresh_expires_at": now + int(payload.get("refresh_expires_in", 0)),
        "scope": payload.get("scope", ""),
    }
    f = token_file(settings)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(token), encoding="utf-8")
    return token


def access_token(settings: Settings) -> str:
    f = token_file(settings)
    if not f.exists():
        raise TikTokError("TikTok ist nicht verbunden.")
    token = json.loads(f.read_text(encoding="utf-8"))
    if time.time() < token["expires_at"]:
        return token["access_token"]
    creds = app_credentials(settings)
    if not creds or not token.get("refresh_token"):
        raise TikTokError("TikTok-Anmeldung abgelaufen – bitte neu verbinden.")
    payload = _form(TOKEN_URL, {
        "client_key": creds[0], "client_secret": creds[1],
        "grant_type": "refresh_token", "refresh_token": token["refresh_token"],
    })
    return _store_token(settings, payload)["access_token"]


# ---------------------------------------------------------------- Login (Browser + lokaler Callback)

def connect(settings: Settings, open_browser: Callable[[str], object] = webbrowser.open, timeout: float = 300) -> str:
    creds = app_credentials(settings)
    if not creds:
        raise TikTokError("Bitte zuerst Client Key und Client Secret der TikTok-App eintragen.")
    client_key, client_secret = creds
    verifier, challenge = pkce_pair(_cfg(settings).get("pkce_encoding", "hex"))
    state = secrets.token_urlsafe(16)
    redirect = redirect_uri(settings)
    port = int(urllib.parse.urlparse(redirect).port)
    result: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in q.items()})
            ok = "code" in result and result.get("state") == state
            msg = "TikTok ist verbunden – du kannst dieses Fenster schließen." if ok else "Anmeldung fehlgeschlagen."
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<html><body style='font-family:sans-serif'><h2>{msg}</h2></body></html>".encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = 1
    params = {
        "client_key": client_key, "response_type": "code", "scope": SCOPES, "redirect_uri": redirect,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    }
    open_browser(f"{AUTH_URL}?{urllib.parse.urlencode(params)}")
    deadline = time.time() + timeout
    try:
        while not result and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if not result:
        raise TikTokError("Zeitüberschreitung bei der TikTok-Anmeldung.")
    if result.get("state") != state:
        raise TikTokError("Sicherheitsprüfung (state) fehlgeschlagen – bitte erneut verbinden.")
    if "code" not in result:
        raise TikTokError(f"TikTok hat die Anmeldung abgelehnt: {result.get('error_description') or result.get('error')}")
    payload = _form(TOKEN_URL, {
        "client_key": client_key, "client_secret": client_secret, "code": result["code"],
        "grant_type": "authorization_code", "redirect_uri": redirect, "code_verifier": verifier,
    })
    _store_token(settings, payload)
    return display_name(settings)


def _auth_headers(settings: Settings) -> dict:
    return {"Authorization": f"Bearer {access_token(settings)}", "Content-Type": "application/json; charset=UTF-8"}


def _post_json(settings: Settings, url: str, body: dict) -> dict:
    payload = _http(url, data=json.dumps(body).encode(), headers=_auth_headers(settings), method="POST")
    err = _error_text(payload)
    if err:
        raise TikTokError(err)
    return payload.get("data", {})


def display_name(settings: Settings) -> str:
    payload = _http(USER_URL, headers={"Authorization": f"Bearer {access_token(settings)}"})
    return payload.get("data", {}).get("user", {}).get("display_name", "TikTok")


def creator_info(settings: Settings) -> dict:
    """Erlaubte Sichtbarkeiten, maximale Videolänge usw. – TikTok verlangt die Abfrage vor jedem Post."""
    return _post_json(settings, CREATOR_INFO_URL, {})


# ---------------------------------------------------------------- Hochladen

def chunk_plan(size: int, chunk: int = DEFAULT_CHUNK) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) nach TikTok-Regeln: < 5 MB am Stück, sonst 5–64 MB, Rest an den letzten."""
    if size < MIN_CHUNK:
        return size, 1
    chunk = max(MIN_CHUNK, min(chunk, MAX_CHUNK, size))
    return chunk, max(1, math.floor(size / chunk))


def upload_video(
    path: str | Path,
    settings: Settings,
    *,
    title: str,
    privacy_level: str = "SELF_ONLY",
    disable_comment: bool = False,
    disable_duet: bool = False,
    disable_stitch: bool = False,
    progress: Callable[[float], None] | None = None,
    wait: bool = True,
) -> dict:
    path = Path(path)
    info = creator_info(settings)
    options = info.get("privacy_level_options") or ["SELF_ONLY"]
    if privacy_level not in options:
        raise TikTokError(f"Sichtbarkeit {privacy_level} ist für dieses Konto nicht erlaubt (möglich: {', '.join(options)}).")
    size = path.stat().st_size
    chunk, count = chunk_plan(size)
    data = _post_json(settings, INIT_URL, {
        "post_info": {
            "title": title[:2200],
            "privacy_level": privacy_level,
            "disable_comment": disable_comment,
            "disable_duet": disable_duet,
            "disable_stitch": disable_stitch,
            "video_cover_timestamp_ms": 1000,
        },
        "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count},
    })
    publish_id, upload_url = data["publish_id"], data["upload_url"]
    with path.open("rb") as fh:
        for i in range(count):
            start = i * chunk
            end = size - 1 if i == count - 1 else start + chunk - 1  # letzter Chunk nimmt den Rest
            fh.seek(start)
            body = fh.read(end - start + 1)
            req = urllib.request.Request(upload_url, data=body, method="PUT", headers={
                "Content-Type": "video/mp4", "Content-Length": str(len(body)),
                "Content-Range": f"bytes {start}-{end}/{size}",
            })
            try:
                urllib.request.urlopen(req, timeout=300).read()
            except urllib.error.HTTPError as exc:
                raise TikTokError(f"Upload-Teil {i + 1}/{count} fehlgeschlagen: HTTP {exc.code}") from exc
            if progress:
                progress((i + 1) / count)
    status = "PROCESSING_UPLOAD"
    if wait:
        for _ in range(60):
            st = _post_json(settings, STATUS_URL, {"publish_id": publish_id})
            status = st.get("status", status)
            if status in ("PUBLISH_COMPLETE", "FAILED", "SEND_TO_USER_INBOX"):
                if status == "FAILED":
                    raise TikTokError(f"TikTok-Verarbeitung fehlgeschlagen: {st.get('fail_reason', '')}")
                break
            time.sleep(3)
    return {
        "platform": "tiktok",
        "publish_id": publish_id,
        "privacy": privacy_level,
        "status": status,
        "uploaded_at": now_utc().isoformat(),
    }

