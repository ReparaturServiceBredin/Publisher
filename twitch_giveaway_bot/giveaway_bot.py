#!/usr/bin/env python3
"""Twitch-Giveaway-Bot.

Läuft mit deinem eigenen Twitch-Account in einer festen Liste von Kanälen, erkennt
Chat-Giveaways, trägt dich einmal ein und schickt dir eine Push-Nachricht aufs Handy,
wenn du eine Flüsternachricht bekommst oder im Chat als Gewinner genannt wirst.

Benötigt Python 3.9+ und als einzige externe Abhängigkeit `websockets`
(pip install websockets). HTTP läuft nur über die Standardbibliothek (urllib).

Start:  python giveaway_bot.py            (liest config.json im selben Ordner)
        python giveaway_bot.py pfad.json  (andere Konfigurationsdatei)
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from pathlib import Path
from typing import Deque, Dict, Optional, Set, Tuple

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
except ImportError:  # pragma: no cover - verständliche Meldung statt Traceback
    print("Das Paket 'websockets' fehlt. Bitte installieren mit:  pip install websockets")
    sys.exit(1)

IRC_URL = "wss://irc-ws.chat.twitch.tv:443"
EVENTSUB_URL = "wss://eventsub.wss.twitch.tv/ws"
HELIX_URL = "https://api.twitch.tv/helix"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
DEVICE_URL = "https://id.twitch.tv/oauth2/device"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
SCOPES = "chat:read chat:edit user:read:whispers"
NTFY_URL = "https://ntfy.sh/"

RECONNECT_DELAY_SEC = 10
EXIT_LOGIN = 2  # Beendet, weil die Twitch-Anmeldung abgelaufen ist (Fenster zeigt dann wieder „Anmelden“)
JOIN_BATCH_SIZE = 20
JOIN_BATCH_INTERVAL_SEC = 10
HEARTBEAT_SEC = 600
LOGIN_TIMEOUT_SEC = 30
JOIN_CHECK_SEC = 15  # so lange auf Twitchs JOIN-Bestätigung warten
USER_AGENT = "TwitchGiveawayBot/1.0 (+python-urllib)"

ENTRY_CONFIRM_WORDS = ("entered", "joined", "eingetragen", "teilgenommen", "dabei", "ticket", "entry", "registriert")
NOTICE_REASONS = {
    "msg_banned": "du bist in diesem Kanal gebannt",
    "msg_timedout": "du hast gerade einen Timeout",
    "msg_duplicate": "gleiche Nachricht zu schnell wiederholt",
    "msg_ratelimit": "zu viele Nachrichten in kurzer Zeit",
    "msg_followersonly": "nur Follower dürfen schreiben",
    "msg_followersonly_followed": "du folgst noch nicht lange genug",
    "msg_followersonly_zero": "nur Follower dürfen schreiben",
    "msg_subsonly": "nur Abonnenten dürfen schreiben",
    "msg_emoteonly": "nur Emotes erlaubt",
    "msg_slowmode": "Slow-Mode aktiv",
    "msg_r9k": "Unique-Chat aktiv (Nachricht schon geschrieben)",
    "msg_verified_email": "deine E-Mail-Adresse ist nicht bestätigt",
    "msg_requires_verified_phone_number": "der Kanal verlangt eine bestätigte Telefonnummer",
    "msg_channel_suspended": "der Kanal ist gesperrt",
    "msg_suspended": "dein Account ist gesperrt",
    "msg_channel_blocked": "du kannst hier nicht schreiben",
}

GIVEAWAY_WORDS = ("giveaway", "raffle", "gewinnspiel", "verlosung", "ticket")
WIN_WORDS = ("won", "winner", "gewonnen", "gewinner", "gratul", "glückwunsch", "congrat")

DEFAULTS = {
    "username": "",
    "client_id": "",
    "access_token": "",
    "channels": [],
    "ntfy_topic": "",
    "discord_webhook": "",
    "dry_run": True,
    "notify_on_entry": True,
    "entry_delay_sec": [3, 12],
    "entry_cooldown_sec": 600,
    "burst_min_users": 15,
    "burst_window_sec": 30,
    "giveaway_bots": ["fossabot", "nightbot", "streamelements", "streamlabs", "moobot", "wizebot", "sery_bot"],
    "blocked_commands": [
        "!discord", "!insta", "!instagram", "!youtube", "!yt", "!tiktok", "!twitter", "!socials", "!social",
        "!uptime", "!followage", "!watchtime", "!points", "!song", "!songrequest", "!sr", "!setup", "!specs",
        "!pc", "!code", "!prime", "!sub", "!subs", "!merch", "!commands", "!cmds", "!help", "!lurk", "!unlurk",
        "!clip", "!so", "!shoutout", "!donate", "!tip", "!rules", "!schedule", "!emotes", "!hug", "!love",
    ],
}

log = logging.getLogger("giveaway_bot")


# ====================================================================== Logging (Deutsch, mit Uhrzeit)

class GermanFormatter(logging.Formatter):
    LEVELS = {"DEBUG": "DEBUG", "INFO": "INFO", "WARNING": "WARNUNG", "ERROR": "FEHLER", "CRITICAL": "KRITISCH"}

    def format(self, record: logging.LogRecord) -> str:
        record.levelname_de = self.LEVELS.get(record.levelname, record.levelname)
        return super().format(record)


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(GermanFormatter("%(asctime)s [%(levelname_de)s] %(message)s", datefmt="%d.%m.%Y %H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


# ====================================================================== Konfiguration

class FatalError(Exception):
    """Fehler, nach dem der Bot nicht weiterlaufen kann (z. B. abgelaufener Token)."""


def load_config(path: Path) -> dict:
    if not path.exists():
        raise FatalError(
            f"Konfigurationsdatei {path} nicht gefunden. Kopiere config.example.json zu config.json und trage deine Daten ein."
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FatalError(f"config.json ist kein gültiges JSON (Zeile {exc.lineno}, Spalte {exc.colno}): {exc.msg}")
    return normalize_config(raw)


def normalize_config(raw: dict) -> dict:
    """Prüft und vereinheitlicht die Einstellungen (aus config.json oder aus dem Fenster)."""
    cfg = {**DEFAULTS, **{k: v for k, v in raw.items() if not k.startswith("_")}}

    cfg["username"] = str(cfg["username"]).strip().lstrip("@").lower()
    token = str(cfg["access_token"]).strip()
    cfg["access_token"] = token[6:] if token.lower().startswith("oauth:") else token
    cfg["client_id"] = str(cfg["client_id"]).strip()
    cfg["channels"], bad_channels = clean_channels(cfg["channels"])
    if bad_channels:
        raise FatalError("Ungültige Kanalnamen: " + ", ".join(bad_channels)
                         + " – bitte nur den Namen eintragen, z. B. montanablack88.")
    cfg["giveaway_bots"] = {str(b).strip().lower() for b in cfg["giveaway_bots"]}
    cfg["blocked_commands"] = {
        ("!" + str(c).strip().lower().lstrip("!")) for c in cfg["blocked_commands"] if str(c).strip()
    }
    delay = cfg["entry_delay_sec"]
    if not (isinstance(delay, (list, tuple)) and len(delay) == 2 and 0 <= float(delay[0]) <= float(delay[1])):
        raise FatalError("entry_delay_sec muss eine Liste [min, max] mit 0 <= min <= max sein, z. B. [3, 12].")
    cfg["entry_delay_sec"] = (float(delay[0]), float(delay[1]))
    for key in ("entry_cooldown_sec", "burst_window_sec"):
        cfg[key] = float(cfg[key])
    cfg["burst_min_users"] = int(cfg["burst_min_users"])

    placeholders = {"dein_twitch_name", "hier_deine_client_id", "hier_dein_access_token",
                    "geheimer-name-nur-fuer-dich-8f3k2"}
    unfilled = [k for k in ("username", "client_id", "access_token", "ntfy_topic") if str(cfg[k]) in placeholders]
    unfilled += ["channels"] if cfg["channels"] == ["kanal1", "kanal2"] else []  # unveränderte Vorlage
    if unfilled:
        raise FatalError(f"config.json ist noch nicht ausgefüllt (Beispielwerte bei: {', '.join(unfilled)}). "
                         "Bitte eigene Daten eintragen – siehe ANLEITUNG.md.")
    missing = [k for k in ("username", "client_id", "access_token") if not cfg[k]]
    if missing:
        raise FatalError(f"In config.json fehlt: {', '.join(missing)}")
    if not cfg["channels"]:
        raise FatalError("In config.json ist keine Kanalliste (channels) eingetragen.")
    if not cfg["ntfy_topic"] and not cfg["discord_webhook"]:
        log.warning("Kein ntfy_topic und kein discord_webhook eingetragen – es werden keine Push-Nachrichten verschickt!")
    return cfg


# ====================================================================== HTTP (nur urllib)

def http_json(url: str, *, method: str = "GET", headers: Optional[dict] = None,
              body: Optional[dict] = None, timeout: float = 15) -> Tuple[int, dict]:
    """Einfacher JSON-Request. Gibt (Statuscode, JSON) zurück – auch bei HTTP-Fehlern, ohne Exception."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": USER_AGENT, **(headers or {})})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(text) if text.strip() else {})
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(text) if text.strip() else {}
        except ValueError:
            return exc.code, {"message": text[:300]}


def http_form(url: str, fields: dict, timeout: float = 15) -> Tuple[int, dict]:
    """POST application/x-www-form-urlencoded (für die Twitch-Anmeldung)."""
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(text) if text.strip() else {}
        except ValueError:
            return exc.code, {"message": text[:300]}


# ---------------------------------------------------------------- Anmeldung per Gerätecode (ohne Passwort im Bot)

def start_device_login(client_id: str) -> dict:
    """Schritt 1: Code holen. Rückgabe enthält user_code und verification_uri (im Browser öffnen)."""
    status, data = http_form(DEVICE_URL, {"client_id": client_id, "scopes": SCOPES})
    if status != 200:
        raise RuntimeError(f"Twitch-Anmeldung konnte nicht gestartet werden: {data.get('message', status)}")
    return data


def poll_device_login(client_id: str, device: dict, cancelled=lambda: False) -> dict:
    """Schritt 2: warten, bis im Browser bestätigt wurde. Gibt die Token-Antwort zurück."""
    interval = max(1, int(device.get("interval", 5)))
    deadline = time.time() + int(device.get("expires_in", 1800))
    while time.time() < deadline:
        if cancelled():
            raise RuntimeError("Anmeldung abgebrochen")
        status, data = http_form(TOKEN_URL, {
            "client_id": client_id, "scopes": SCOPES, "device_code": device["device_code"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        })
        if status == 200 and data.get("access_token"):
            return data
        message = str(data.get("message", "")).lower()
        if "pending" in message:
            time.sleep(interval)
            continue
        if "slow_down" in message:
            interval += 2
            time.sleep(interval)
            continue
        raise RuntimeError("Anmeldung abgelehnt oder abgelaufen – bitte erneut versuchen."
                           if "invalid" in message or "denied" in message or "expired" in message
                           else f"Anmeldung fehlgeschlagen: {data.get('message', status)}")
    raise RuntimeError("Die Anmeldung hat zu lange gedauert – bitte erneut versuchen.")


class TokenManager:
    """Erneuert den Token automatisch über den Refresh-Token (wenn vorhanden)."""

    def __init__(self, cfg: dict, on_update=None):
        self.cfg = cfg
        self.on_update = on_update

    @property
    def can_refresh(self) -> bool:
        return bool(self.cfg.get("refresh_token") and self.cfg.get("client_id"))

    def refresh(self) -> bool:
        if not self.can_refresh:
            return False
        fields = {"client_id": self.cfg["client_id"], "grant_type": "refresh_token",
                  "refresh_token": self.cfg["refresh_token"]}
        if self.cfg.get("client_secret"):
            fields["client_secret"] = self.cfg["client_secret"]
        status, data = http_form(TOKEN_URL, fields)
        if status != 200 or not data.get("access_token"):
            log.error("Token konnte nicht erneuert werden: %s", data.get("message", status))
            return False
        self.cfg["access_token"] = data["access_token"]
        self.cfg["refresh_token"] = data.get("refresh_token", self.cfg["refresh_token"])
        log.info("Twitch-Anmeldung automatisch erneuert ✔")
        if self.on_update:
            try:
                self.on_update(self.cfg["access_token"], self.cfg["refresh_token"])
            except Exception as exc:  # noqa: BLE001
                log.error("Neuer Token konnte nicht gespeichert werden: %s", exc)
        return True


def keep_awake(on: bool) -> None:
    """Windows: PC nicht in den Standby gehen lassen, solange der Bot läuft (Bildschirm darf ausgehen)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
        if on:
            log.info("Standby ist deaktiviert, solange der Bot läuft (Bildschirm darf ausgehen).")
    except Exception as exc:  # noqa: BLE001
        log.warning("Standby konnte nicht verhindert werden: %s", exc)


def describe_error(exc: BaseException) -> str:
    """Verständlicher Text statt leerer Klammern bei Verbindungsabbrüchen."""
    if isinstance(exc, ConnectionClosed):
        rcvd = getattr(exc, "rcvd", None)
        code = getattr(rcvd, "code", None) if rcvd is not None else getattr(exc, "code", None)
        return f"Verbindung von Twitch geschlossen (Code {code})" if code else "Verbindung abgebrochen (Internet weg?)"
    if isinstance(exc, asyncio.TimeoutError):
        return "Zeitüberschreitung – keine Antwort"
    if isinstance(exc, OSError):
        return f"Netzwerkfehler: {exc.strerror or exc}" if (exc.strerror or str(exc)) else "Netzwerkfehler"
    return str(exc) or type(exc).__name__


async def sleep_watch(stop: asyncio.Event) -> None:
    """Erkennt, wenn der PC im Standby war (die Uhr springt) – dann konnte der Bot nichts sehen."""
    last = time.time()
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=20)
            return
        except asyncio.TimeoutError:
            pass
        now = time.time()
        gap = now - last - 20
        if gap > 60:
            log.warning("⚠ Der PC war ca. %d Minuten im Standby/Ruhezustand – in dieser Zeit konnte der Bot "
                        "nichts mitbekommen. Er verbindet sich jetzt neu.", gap // 60 or 1)
        last = now


async def run_blocking(func, *args, **kwargs):
    """Blockierende urllib-Aufrufe im Thread ausführen, damit der Chat-Empfang nie stockt."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))


# ====================================================================== Push-Nachrichten

class Notifier:
    def __init__(self, cfg: dict):
        self.topic = str(cfg.get("ntfy_topic") or "").strip()
        self.discord = str(cfg.get("discord_webhook") or "").strip()

    def _send_ntfy(self, title: str, message: str, urgent: bool, click: Optional[str]) -> None:
        body = {"topic": self.topic, "title": title, "message": message, "priority": 5 if urgent else 3,
                "tags": ["rotating_light"] if urgent else ["gift"]}
        if click:
            body["click"] = click
        status, data = http_json(NTFY_URL, method="POST", body=body)
        if status >= 300:
            raise RuntimeError(f"ntfy antwortete mit {status}: {data}")

    def _send_discord(self, title: str, message: str, urgent: bool, click: Optional[str]) -> None:
        content = f"{'@everyone ' if urgent else ''}**{title}**\n{message}" + (f"\n{click}" if click else "")
        body = {"content": content[:1990], "allowed_mentions": {"parse": ["everyone"] if urgent else []}}
        status, data = http_json(self.discord, method="POST", body=body)
        if status >= 300:
            raise RuntimeError(f"Discord antwortete mit {status}: {data}")

    async def send(self, title: str, message: str, urgent: bool = False, click: Optional[str] = None) -> None:
        """Fehler beim Senden werden nur geloggt – der Bot läuft immer weiter."""
        targets = []
        if self.topic:
            targets.append(("ntfy", self._send_ntfy))
        if self.discord:
            targets.append(("Discord", self._send_discord))
        for name, func in targets:
            try:
                await run_blocking(func, title, message, urgent, click)
                log.info("Push über %s gesendet: %s", name, title)
            except Exception as exc:  # noqa: BLE001
                log.error("Push über %s fehlgeschlagen: %s", name, exc)


# ====================================================================== Twitch-API

class TwitchApi:
    def __init__(self, cfg: dict):
        self.cfg = cfg  # Token immer frisch aus cfg lesen – er kann zwischendurch erneuert werden

    @property
    def client_id(self) -> str:
        return self.cfg["client_id"]

    @property
    def token(self) -> str:
        return self.cfg["access_token"]

    def _headers(self) -> dict:
        return {"Client-Id": self.client_id, "Authorization": f"Bearer {self.token}"}

    def validate(self) -> dict:
        status, data = http_json(VALIDATE_URL, headers={"Authorization": f"OAuth {self.token}"})
        if status == 401:
            raise FatalError("Token abgelaufen oder ungültig")
        if status != 200:
            raise RuntimeError(f"Token-Prüfung fehlgeschlagen ({status}): {data}")
        return data

    def get_user_id(self, login: str) -> str:
        status, data = http_json(f"{HELIX_URL}/users?login={urllib.parse.quote(login)}", headers=self._headers())
        if status == 401:
            raise FatalError("Token abgelaufen oder ungültig")
        if status != 200 or not data.get("data"):
            raise RuntimeError(f"Twitch-Nutzer „{login}“ nicht gefunden ({status}): {data.get('message', data)}")
        return data["data"][0]["id"]

    def subscribe_whispers(self, user_id: str, session_id: str) -> Tuple[int, dict]:
        body = {
            "type": "user.whisper.message",
            "version": "1",
            "condition": {"user_id": user_id},
            "transport": {"method": "websocket", "session_id": session_id},
        }
        return http_json(f"{HELIX_URL}/eventsub/subscriptions", method="POST", headers=self._headers(), body=body)


# ====================================================================== IRC-Hilfen

CHANNEL_NAME_RE = re.compile(r"^[a-z0-9_]{3,25}$")
CHANNEL_URL_RE = re.compile(r"twitch\.tv/(?:popout/)?([A-Za-z0-9_]{3,25})", re.IGNORECASE)


def clean_channel(raw: str) -> Optional[str]:
    """„https://www.twitch.tv/MontanaBlack88“, „@name“, „#name“ → „montanablack88“. Ungültig → None."""
    text = str(raw).strip()
    m = CHANNEL_URL_RE.search(text)
    if m:
        text = m.group(1)
    text = text.strip().strip("/").lstrip("@#").lower()
    return text if CHANNEL_NAME_RE.match(text) else None


def clean_channels(raws) -> Tuple[list, list]:
    """(gültige Kanalnamen ohne Doppelte, ungültige Eingaben)."""
    good, bad = [], []
    for raw in raws:
        if not str(raw).strip():
            continue
        name = clean_channel(raw)
        if name:
            if name not in good:
                good.append(name)
        else:
            bad.append(str(raw).strip())
    return good, bad


IRC_LINE = re.compile(r"^(?:@(?P<tags>\S+) )?(?::(?P<prefix>\S+) )?(?P<command>\S+)(?P<params>.*)$")
COMMAND_RE = re.compile(r"(?<!\S)(![A-Za-z0-9_]+)")
SINGLE_COMMAND_RE = re.compile(r"^![A-Za-z0-9_]+$")
SINGLE_WORD_RE = re.compile(r"^[^\s@/:]{2,25}$")
# „Schreibt 'Monte' in den Chat“, „type join to enter“, „Stichwort: Kekse“
KEYWORD_RE = re.compile(
    r"(?:\btype|\btipp(?:e|t)?|\bschreib(?:t|e)?|\benter with|\bstichwort|\bkeyword|\bmit dem wort)"
    r"\s*[:\-]?\s*[\"'„“‚‘`*]?(!?[A-Za-z0-9_äöüÄÖÜß]{2,25})",
    re.IGNORECASE,
)
KEYWORD_STOPWORDS = {"in", "im", "den", "the", "a", "an", "to", "ein", "eine", "einfach", "jetzt", "mal", "now",
                     "into", "chat", "hier", "folgendes", "das", "dies", "this", "und", "and", "es", "it"}
HINT_WINDOW_SEC = 600  # so lange nach einem Giveaway-Hinweis zählen auch Wort-Bursts ohne „!“


def parse_irc(line: str) -> Optional[dict]:
    m = IRC_LINE.match(line)
    if not m:
        return None
    tags = {}
    if m.group("tags"):
        for part in m.group("tags").split(";"):
            key, _, value = part.partition("=")
            tags[key] = value
    params = m.group("params").strip()
    trailing = None
    if params.startswith(":"):
        params, trailing = "", params[1:]
    elif " :" in params:
        params, trailing = params.split(" :", 1)
    prefix = m.group("prefix") or ""
    return {
        "tags": tags,
        "nick": prefix.split("!", 1)[0].lower() if "!" in prefix else prefix.lower(),
        "command": m.group("command"),
        "params": params.split() if params else [],
        "text": trailing,
    }


# ====================================================================== Erkennung

class Detector:
    """Reine Logik ohne Netzwerk: Giveaway- und Gewinn-Erkennung (gut testbar)."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.me = cfg["username"]
        self.me_re = re.compile(rf"(?<![\w@])@?{re.escape(self.me)}(?!\w)", re.IGNORECASE)
        self.burst: Dict[Tuple[str, str], Deque[Tuple[float, str]]] = defaultdict(deque)
        self.last_entry: Dict[str, float] = {}
        self.hint: Dict[str, float] = {}  # Kanal → Zeitpunkt der letzten Giveaway-Ansage von Bot/Mod/Streamer
        self.on_skip = lambda channel, command, reason: None
        self.on_hint = lambda channel, sender, text: None

    def first_command(self, text: str) -> Optional[str]:
        for cmd in COMMAND_RE.findall(text):
            cmd = cmd.lower()
            if cmd not in self.cfg["blocked_commands"]:
                return cmd
        return None

    def keyword(self, text: str) -> Optional[str]:
        for m in KEYWORD_RE.finditer(text):
            word = m.group(1)
            if word.lower() in KEYWORD_STOPWORDS:
                continue
            if word.startswith("!") and word.lower() in self.cfg["blocked_commands"]:
                continue
            return word
        return None

    def check_giveaway(self, channel: str, sender: str, text: str, now: Optional[float] = None,
                       privileged: bool = False) -> Optional[Tuple[str, str]]:
        """Gibt (Befehl, Grund) zurück, wenn ein Eintrag fällig ist – sonst None.

        privileged = Nachricht vom Streamer oder einem Mod (aus den Twitch-Badges)."""
        now = time.monotonic() if now is None else now
        if sender == self.me:
            return None
        lower = text.lower()
        found: Optional[Tuple[str, str]] = None
        # 1) Giveaway-Bot, Streamer oder Mod kündigt an
        if (sender in self.cfg["giveaway_bots"] or privileged) and any(w in lower for w in GIVEAWAY_WORDS):
            self.hint[channel] = now
            self.on_hint(channel, sender, text)
            cmd = self.first_command(text) or self.keyword(text)
            who = sender if sender in self.cfg["giveaway_bots"] else f"{sender} (Mod/Streamer)"
            if cmd:
                found = (cmd, f"Ansage von {who}")
        # 2) Burst: viele verschiedene Nutzer schreiben denselben einzelnen Befehl
        stripped = text.strip()
        if SINGLE_COMMAND_RE.match(stripped):
            cmd = stripped.lower()
            if cmd not in self.cfg["blocked_commands"]:
                q = self.burst[(channel, cmd)]
                q.append((now, sender))
                window = self.cfg["burst_window_sec"]
                while q and now - q[0][0] > window:
                    q.popleft()
                users = {u for _, u in q}
                if len(users) >= self.cfg["burst_min_users"] and found is None:
                    found = (cmd, f"{len(users)} Nutzer schrieben {cmd} in {window:.0f} s")
                    q.clear()
        # 3) Wort-Giveaway ohne „!“: nur kurz nach einer Giveaway-Ansage, sonst wäre Emote-Spam ein „Giveaway“
        elif (now - self.hint.get(channel, -1e9) <= HINT_WINDOW_SEC and SINGLE_WORD_RE.match(stripped)
              and not stripped.startswith("!")):
            word = stripped
            q = self.burst[(channel, "word:" + word.lower())]
            q.append((now, sender))
            window = self.cfg["burst_window_sec"]
            while q and now - q[0][0] > window:
                q.popleft()
            users = {u for _, u in q}
            if len(users) >= self.cfg["burst_min_users"] and found is None:
                found = (word, f"{len(users)} Nutzer schrieben „{word}“ kurz nach einer Giveaway-Ansage")
                q.clear()
        if found is None:
            return None
        if not self.cooldown_ok(channel, now):
            log.info("#%s: Giveaway erkannt (%s), aber Cooldown aktiv – kein Eintrag.", channel, found[1])
            self.on_skip(channel, found[0], found[1])
            return None
        self.last_entry[channel] = now  # sofort sperren, damit keine doppelten Einträge geplant werden
        return found

    def cooldown_ok(self, channel: str, now: float) -> bool:
        last = self.last_entry.get(channel)
        return last is None or now - last >= self.cfg["entry_cooldown_sec"]

    def is_entry_confirmation(self, sender: str, text: str) -> bool:
        """Giveaway-Bot nennt dich zusammen mit „entered/eingetragen/…“ (ohne Gewinn-Wörter)."""
        if sender not in self.cfg["giveaway_bots"] or not self.me_re.search(text):
            return False
        lower = text.lower()
        return any(w in lower for w in ENTRY_CONFIRM_WORDS) and not any(w in lower for w in WIN_WORDS)

    def is_win(self, sender: str, text: str) -> bool:
        if sender == self.me:
            return False
        lower = text.lower()
        return bool(self.me_re.search(text)) and any(w in lower for w in WIN_WORDS)


# ====================================================================== Chat (IRC über WebSocket)

def _no_event(kind: str, **data) -> None:
    pass


class ChatBot:
    def __init__(self, cfg: dict, notifier: Notifier, stop: asyncio.Event, tokens: Optional[TokenManager] = None,
                 emit=None):
        self.cfg = cfg
        self.tokens = tokens or TokenManager(cfg)
        self.emit = emit or _no_event  # Rückmeldungen fürs Fenster (Status, Einträge, Kanäle)
        self.entry_seq = 0
        self.awaiting: Dict[str, int] = {}
        self.joined: Set[str] = set()
        self.logged_in = False
        self.heartbeat_task: Optional[asyncio.Task] = None  # Kanal → Eintrags-Nr., auf deren Bestätigung durch Twitch gewartet wird
        self.notifier = notifier
        self.stop = stop
        self.detector = Detector(cfg)
        self.detector.on_skip = lambda ch, cmd, why: self.emit("skipped", channel=ch, command=cmd, reason=why)
        self.detector.on_hint = lambda ch, who, text: log.info("💬 Giveaway-Hinweis in #%s von %s: %s", ch, who, text)
        self.msg_counts: Dict[str, int] = {}
        self.ws = None
        self.tasks: Set[asyncio.Task] = set()
        self.join_task: Optional[asyncio.Task] = None

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def send_raw(self, line: str) -> None:
        if self.ws is None:
            raise ConnectionError("nicht verbunden")
        await self.ws.send(line)

    async def run(self) -> None:
        while not self.stop.is_set():
            try:
                await self._session()
            except FatalError:
                raise
            except (ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
                log.warning("Chat-Verbindung getrennt: %s", describe_error(exc))
            except Exception as exc:  # noqa: BLE001
                log.exception("Unerwarteter Fehler in der Chat-Verbindung: %s", exc)
            finally:
                self.ws = None
                self.joined.clear()
                self.emit("chat", state="getrennt")
                if self.join_task:
                    self.join_task.cancel()
                    self.join_task = None
            if self.stop.is_set():
                break
            log.info("Verbinde in %d Sekunden neu mit dem Chat …", RECONNECT_DELAY_SEC)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=RECONNECT_DELAY_SEC)
            except asyncio.TimeoutError:
                pass

    async def _session(self) -> None:
        log.info("Verbinde mit dem Twitch-Chat …")
        self.emit("chat", state="verbinde …")
        async with websockets.connect(IRC_URL, ping_interval=None, max_size=2 ** 20) as ws:
            self.ws = ws
            await ws.send("CAP REQ :twitch.tv/tags twitch.tv/commands")
            await ws.send(f"PASS oauth:{self.cfg['access_token']}")
            await ws.send(f"NICK {self.cfg['username']}")
            self.logged_in = False
            while True:
                # Twitch schickt etwa alle 5 Minuten ein PING – nach 6 Minuten Stille gilt die Verbindung als tot.
                # Bis zur Anmeldung höchstens 30 s warten (z. B. nach dem Aufwachen aus dem Standby).
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=360 if self.logged_in else LOGIN_TIMEOUT_SEC)
                except asyncio.TimeoutError:
                    raise ConnectionError("keine Antwort vom Chat-Server" if not self.logged_in
                                          else "6 Minuten keine Daten vom Chat-Server")
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", "replace")
                for line in raw.split("\r\n"):
                    if line:
                        await self._handle_line(line)

    async def _handle_line(self, line: str) -> None:
        msg = parse_irc(line)
        if msg is None:
            return
        cmd = msg["command"]
        if cmd == "PING":
            await self.send_raw(f"PONG :{msg['text'] or 'tmi.twitch.tv'}")
        elif cmd == "001":
            log.info("Im Chat angemeldet als %s.", self.cfg["username"])
            self.logged_in = True
            self.emit("chat", state="verbunden")
            self.join_task = self._spawn(self._join_channels())
            if self.heartbeat_task is None or self.heartbeat_task.done():
                self.heartbeat_task = self._spawn(self._heartbeat())
        elif cmd == "NOTICE" and msg["text"] and ("authentication failed" in msg["text"].lower()
                                                   or "improperly formatted auth" in msg["text"].lower()):
            log.error("Chat-Anmeldung fehlgeschlagen: %s", msg["text"])
            if await run_blocking(self.tokens.refresh):
                raise ConnectionError("Token erneuert – verbinde neu")
            await self.notifier.send("Token abgelaufen",
                                     "Die Anmeldung im Twitch-Chat ist fehlgeschlagen. Bitte im Bot neu mit Twitch "
                                     "anmelden. Der Bot wurde beendet.", urgent=True)
            raise FatalError("Chat-Anmeldung fehlgeschlagen (Token abgelaufen oder falsch)")
        elif cmd == "NOTICE":
            channel = msg["params"][0].lstrip("#").lower() if msg["params"] else "?"
            log.info("Hinweis von Twitch (#%s): %s", channel, msg["text"])
            entry_id = self.awaiting.pop(channel, None)
            if entry_id is not None:
                msg_id = msg["tags"].get("msg-id", "")
                reason = NOTICE_REASONS.get(msg_id) or (msg["text"] or msg_id or "unbekannter Grund")
                log.error("❌ Eintrag in #%s abgelehnt: %s", channel, reason)
                self.emit("entry", id=entry_id, channel=channel, status="abgelehnt", detail=reason)
                self.detector.last_entry.pop(channel, None)  # bei nächster Gelegenheit erneut versuchen
                self._spawn(self.notifier.send(f"Eintrag in #{channel} abgelehnt", f"Grund: {reason}"))
        elif cmd == "USERSTATE" and msg["params"]:
            channel = msg["params"][0].lstrip("#").lower()
            entry_id = self.awaiting.pop(channel, None)
            if entry_id is not None:  # Twitch bestätigt eine gesendete Nachricht mit USERSTATE
                log.info("✔ Twitch hat deinen Eintrag in #%s angenommen.", channel)
                self.emit("entry", id=entry_id, channel=channel, status="angenommen", detail="von Twitch angenommen")
        elif cmd == "RECONNECT":
            log.info("Twitch bittet um Neuverbindung.")
            raise ConnectionError("RECONNECT von Twitch")
        elif cmd == "JOIN" and msg["nick"] == self.cfg["username"]:
            channel = msg["params"][0].lstrip("#").lower() if msg["params"] else "?"
            log.info("Kanal #%s betreten.", channel)
            self.joined.add(channel)
            self.emit("joined", channel=channel)
        elif cmd == "PRIVMSG" and msg["params"] and msg["text"] is not None:
            channel = msg["params"][0].lstrip("#").lower()
            sender = (msg["tags"].get("login") or msg["nick"] or "").lower()
            badges = msg["tags"].get("badges", "")
            privileged = "broadcaster/" in badges or "moderator/" in badges or msg["tags"].get("mod") == "1" \
                or sender == channel
            await self._on_message(channel, sender, msg["tags"].get("display-name") or sender, msg["text"], privileged)

    async def _heartbeat(self) -> None:
        """Alle 10 Minuten ein Lebenszeichen ins Protokoll – so sieht man, dass der Bot wirklich mitliest."""
        while True:
            await asyncio.sleep(HEARTBEAT_SEC)
            counts, self.msg_counts = self.msg_counts, {}
            if not self.joined:
                log.warning("Lebenszeichen: in keinem Kanal – Verbindung wird wiederhergestellt …")
                continue
            parts = [f"#{c}: {counts.get(c, 0):,} Nachrichten".replace(",", ".") for c in sorted(self.joined)]
            log.info("Läuft ✔ – letzte %d Min: %s", HEARTBEAT_SEC // 60, ", ".join(parts))

    async def _check_joined(self, batch: list) -> None:
        """Twitch ignoriert unbekannte Kanäle stillschweigend – nach 15 s melden, was nicht geklappt hat."""
        await asyncio.sleep(JOIN_CHECK_SEC)
        for channel in batch:
            if channel not in self.joined:
                log.warning("Kanal #%s konnte nicht betreten werden – Name richtig geschrieben?", channel)
                self.emit("join_failed", channel=channel)

    async def _join_channels(self) -> None:
        """Höchstens 20 Kanäle pro 10 Sekunden (Twitch-Limit) – läuft im Hintergrund."""
        channels = self.cfg["channels"]
        for i in range(0, len(channels), JOIN_BATCH_SIZE):
            batch = channels[i:i + JOIN_BATCH_SIZE]
            await self.send_raw("JOIN " + ",".join(f"#{c}" for c in batch))
            log.info("Betrete %d Kanal/Kanäle: %s", len(batch), ", ".join(batch))
            self._spawn(self._check_joined(batch))
            if i + JOIN_BATCH_SIZE < len(channels):
                await asyncio.sleep(JOIN_BATCH_INTERVAL_SEC)

    async def _on_message(self, channel: str, sender: str, display: str, text: str, privileged: bool = False) -> None:
        self.emit("message", channel=channel)
        self.msg_counts[channel] = self.msg_counts.get(channel, 0) + 1
        if self.detector.is_entry_confirmation(sender, text):
            log.info("✔ %s bestätigt deinen Eintrag in #%s: %s", display, channel, text)
            self.emit("confirmed", channel=channel, sender=display, text=text)
        if self.detector.is_win(sender, text):
            self.emit("win", channel=channel, sender=display, text=text)
            log.warning("🎉 MÖGLICHER GEWINN in #%s von %s: %s", channel, display, text)
            self._spawn(self.notifier.send(
                f"🎉 Gewinn in #{channel}?",
                f"Kanal: {channel}\nVon: {display}\nNachricht: {text}\nhttps://twitch.tv/{channel}",
                urgent=True, click=f"https://twitch.tv/{channel}",
            ))
        hit = self.detector.check_giveaway(channel, sender, text, privileged=privileged)
        if hit:
            command, reason = hit
            log.info("Giveaway in #%s erkannt (%s) → Eintrag mit %s geplant.", channel, reason, command)
            self.entry_seq += 1
            self._spawn(self._enter(channel, command, reason, self.entry_seq))

    async def _enter(self, channel: str, command: str, reason: str, entry_id: int = 0) -> None:
        low, high = self.cfg["entry_delay_sec"]
        delay = random.uniform(low, high)
        log.info("#%s: warte %.1f s vor dem Eintrag …", channel, delay)
        self.emit("entry", id=entry_id, channel=channel, command=command, status="erkannt",
                  detail=f"{reason} – sende in {delay:.0f} s")
        await asyncio.sleep(delay)
        if self.cfg["dry_run"]:
            log.info("[DRY-RUN] Würde in #%s senden: %s", channel, command)
            self.emit("entry", id=entry_id, channel=channel, status="testmodus",
                      detail="Testmodus – nicht gesendet")
            await self.notifier.send(f"Dry-Run: Giveaway in #{channel}",
                                     f"Würde „{command}“ senden ({reason}).", click=f"https://twitch.tv/{channel}")
            return
        try:
            self.awaiting[channel] = entry_id
            await self.send_raw(f"PRIVMSG #{channel} :{command}")
        except Exception as exc:  # noqa: BLE001
            self.awaiting.pop(channel, None)
            log.error("Eintrag in #%s fehlgeschlagen (%s) – Verbindung weg?", channel, exc)
            self.emit("entry", id=entry_id, channel=channel, status="fehler", detail=f"nicht gesendet: {exc}")
            self.detector.last_entry.pop(channel, None)  # später erneut erlauben
            return
        log.info("✅ In #%s eingetragen mit %s.", channel, command)
        self.emit("entry", id=entry_id, channel=channel, status="gesendet", detail="gesendet, warte auf Twitch …")
        self._spawn(self._confirm_timeout(channel, entry_id))
        if self.cfg["notify_on_entry"]:
            await self.notifier.send(f"Eingetragen in #{channel}", f"Gesendet: {command} ({reason})",
                                     click=f"https://twitch.tv/{channel}")


    async def _confirm_timeout(self, channel: str, entry_id: int) -> None:
        await asyncio.sleep(6)
        if self.awaiting.get(channel) == entry_id:
            self.awaiting.pop(channel, None)
            self.emit("entry", id=entry_id, channel=channel, status="gesendet",
                      detail="gesendet (keine Rückmeldung von Twitch)")


# ====================================================================== Flüsternachrichten (EventSub WebSocket)

class WhisperListener:
    def __init__(self, cfg: dict, api: TwitchApi, notifier: Notifier, stop: asyncio.Event,
                 tokens: Optional[TokenManager] = None, emit=None):
        self.cfg = cfg
        self.emit = emit or _no_event
        self.tokens = tokens or TokenManager(cfg)
        self.api = api
        self.notifier = notifier
        self.stop = stop
        self.user_id: Optional[str] = None
        self.disabled = False

    async def run(self) -> None:
        try:
            self.user_id = await run_blocking(self.api.get_user_id, self.cfg["username"])
        except FatalError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.error("Konnte deine Twitch-User-ID nicht holen (%s) – Flüsternachrichten sind deaktiviert.", exc)
            return
        log.info("Deine Twitch-User-ID: %s", self.user_id)
        url, ws, subscribe = EVENTSUB_URL, None, True
        while not self.stop.is_set() and not self.disabled:
            try:
                if ws is None:
                    log.info("Verbinde mit EventSub (Flüsternachrichten) …")
                    ws = await websockets.connect(url, ping_interval=None, max_size=2 ** 20)
                new_ws = await self._session(ws, subscribe)
                # session_reconnect: neue Verbindung steht schon, alte jetzt schließen, NICHT neu abonnieren
                old, ws, subscribe = ws, new_ws, False
                await old.close()
                log.info("EventSub-Verbindung gewechselt, Abo bleibt bestehen.")
            except FatalError:
                raise
            except Exception as exc:  # noqa: BLE001
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception:  # noqa: BLE001
                        pass
                ws, url, subscribe = None, EVENTSUB_URL, True
                if self.stop.is_set() or self.disabled:
                    break
                log.warning("EventSub getrennt (%s) – neuer Versuch in %d Sekunden.", describe_error(exc),
                            RECONNECT_DELAY_SEC)
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=RECONNECT_DELAY_SEC)
                except asyncio.TimeoutError:
                    pass
        if ws is not None:
            await ws.close()

    async def _session(self, ws, subscribe: bool):
        """Verarbeitet Nachrichten bis zu einem session_reconnect. Gibt dann die NEUE Verbindung zurück."""
        keepalive = 30.0
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=keepalive + 15)
            msg = json.loads(raw)
            meta, payload = msg.get("metadata", {}), msg.get("payload", {})
            kind = meta.get("message_type")
            if kind == "session_welcome":
                session = payload.get("session", {})
                keepalive = float(session.get("keepalive_timeout_seconds") or 30)
                log.info("EventSub verbunden (Session %s…).", str(session.get("id", ""))[:8])
                if subscribe:
                    await self._subscribe(session["id"])
            elif kind == "session_keepalive":
                pass
            elif kind == "notification":
                await self._on_notification(payload)
            elif kind == "session_reconnect":
                url = payload.get("session", {}).get("reconnect_url")
                log.info("EventSub bittet um Wechsel der Verbindung.")
                return await websockets.connect(url, ping_interval=None, max_size=2 ** 20)
            elif kind == "revocation":
                sub = payload.get("subscription", {})
                log.error("Abo für Flüsternachrichten wurde von Twitch beendet: %s", sub.get("status"))
                await self.notifier.send("Flüsternachrichten-Abo beendet",
                                         f"Twitch hat das Abo beendet ({sub.get('status')}). Token prüfen und Bot neu starten.",
                                         urgent=True)
                raise ConnectionError("Abo widerrufen")

    async def _subscribe(self, session_id: str) -> None:
        status, data = await run_blocking(self.api.subscribe_whispers, self.user_id, session_id)
        if status in (200, 202):
            log.info("Flüsternachrichten abonniert ✔")
            self.emit("whispers", state="aktiv")
        elif status == 409:
            log.info("Flüsternachrichten sind bereits abonniert.")
            self.emit("whispers", state="aktiv")
        elif status == 401:
            if await run_blocking(self.tokens.refresh):
                raise ConnectionError("Token erneuert – abonniere neu")
            await self.notifier.send("Token abgelaufen", "Flüsternachrichten konnten nicht abonniert werden – "
                                                         "bitte im Bot neu mit Twitch anmelden. Der Bot wurde beendet.",
                                     urgent=True)
            raise FatalError("Token abgelaufen (EventSub)")
        elif status == 403:
            self.disabled = True
            self.emit("whispers", state="keine Berechtigung")
            log.error("Kein Zugriff auf Flüsternachrichten (%s). Fehlt der Scope user:read:whispers oder gehört der "
                      "Token zu einer anderen Client-ID? Chat-Erkennung läuft weiter.", data.get("message", ""))
            await self.notifier.send("Flüsternachrichten deaktiviert",
                                     "Der Token hat keine Berechtigung user:read:whispers. Bitte neuen Token holen.")
            raise ConnectionError("keine Berechtigung")
        else:
            raise ConnectionError(f"Abo fehlgeschlagen ({status}): {data.get('message', data)}")

    async def _on_notification(self, payload: dict) -> None:
        if payload.get("subscription", {}).get("type") != "user.whisper.message":
            return
        ev = payload.get("event", {})
        name = ev.get("from_user_name") or ev.get("from_user_login") or "Unbekannt"
        text = (ev.get("whisper") or {}).get("text", "")
        log.warning("✉ Flüsternachricht von %s: %s", name, text)
        self.emit("whisper", sender=name, text=text)
        await self.notifier.send(f"Flüsternachricht von {name}", text or "(leer)", urgent=True,
                                 click="https://www.twitch.tv/popout/moderator/whispers")


# ====================================================================== Start

async def main_async(config_path: Path) -> int:
    try:
        cfg = load_config(config_path)
    except FatalError as exc:
        log.error("%s", exc)
        return 1
    return await run_bot(cfg)


async def token_watch(api: TwitchApi, tokens: TokenManager, notifier: Notifier, stop: asyncio.Event) -> None:
    """Twitch verlangt eine stündliche Token-Prüfung; kurz vor Ablauf wird automatisch erneuert."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=3600)
            return
        except asyncio.TimeoutError:
            pass
        try:
            info = await run_blocking(api.validate)
            if int(info.get("expires_in", 99999)) < 7200:
                await run_blocking(tokens.refresh)
        except FatalError:
            if not await run_blocking(tokens.refresh):
                await notifier.send("Token abgelaufen", "Bitte im Bot neu mit Twitch anmelden.", urgent=True)
                raise
        except Exception as exc:  # noqa: BLE001
            log.warning("Stündliche Token-Prüfung fehlgeschlagen: %s", exc)


async def run_bot(cfg: dict, stop: Optional[asyncio.Event] = None, on_token_update=None, on_event=None) -> int:
    """Startet Chat + Flüsternachrichten mit einer fertigen Konfiguration (auch vom Fenster aus genutzt)."""
    notifier = Notifier(cfg)
    api = TwitchApi(cfg)
    tokens = TokenManager(cfg, on_token_update)
    stop = stop or asyncio.Event()

    try:
        try:
            info = await run_blocking(api.validate)
        except FatalError:
            if not await run_blocking(tokens.refresh):
                raise
            info = await run_blocking(api.validate)
        login = info.get("login", "")
        log.info("Token gültig für %s, läuft noch ca. %d Stunden.", login, int(info.get("expires_in", 0)) // 3600)
        if login and login.lower() != cfg["username"]:
            log.warning("Der Token gehört zu „%s“, in config.json steht „%s“!", login, cfg["username"])
        if info.get("client_id") and info["client_id"] != cfg["client_id"]:
            log.warning("Der Token wurde mit einer anderen Client-ID erstellt – Flüsternachrichten funktionieren dann nicht.")
        scopes = set(info.get("scopes") or [])
        missing = {"chat:read", "chat:edit", "user:read:whispers"} - scopes
        if missing:
            log.warning("Dem Token fehlen Berechtigungen: %s", ", ".join(sorted(missing)))
    except FatalError:
        log.error("Der Token ist abgelaufen oder ungültig.")
        await notifier.send("Token abgelaufen", "Der Twitch-Token ist abgelaufen oder ungültig. Bitte im Bot neu mit "
                                                "Twitch anmelden. Der Bot wurde beendet.", urgent=True)
        return EXIT_LOGIN
    except Exception as exc:  # noqa: BLE001 – ohne Prüfung trotzdem weitermachen
        log.warning("Token konnte nicht geprüft werden (%s) – versuche es trotzdem.", exc)

    mode = "DRY-RUN (es wird nichts in den Chat geschrieben)" if cfg["dry_run"] else "SCHARF (trägt sich wirklich ein)"
    log.info("Bot startet für %s in %d Kanal/Kanälen – Modus: %s", cfg["username"], len(cfg["channels"]), mode)
    await notifier.send("Bot gestartet", f"Überwache {len(cfg['channels'])} Kanäle als {cfg['username']}. Modus: {mode}")

    chat = ChatBot(cfg, notifier, stop, tokens, emit=on_event)
    whispers = WhisperListener(cfg, api, notifier, stop, tokens, emit=on_event)
    tasks = [asyncio.ensure_future(chat.run()), asyncio.ensure_future(whispers.run()),
             asyncio.ensure_future(token_watch(api, tokens, notifier, stop)), asyncio.ensure_future(sleep_watch(stop))]
    keep_awake(True)
    exit_code = 0
    try:
        pending = set(tasks)
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                exc = t.exception()
                if isinstance(exc, FatalError):
                    log.error("Bot wird beendet: %s", exc)
                    exit_code = EXIT_LOGIN if "token" in str(exc).lower() or "anmeldung" in str(exc).lower() else 1
                    stop.set()
                elif exc is not None:
                    log.error("Unerwarteter Fehler: %s", exc)
            if exit_code:
                break
    finally:
        keep_awake(False)
        stop.set()
        for t in tasks + list(chat.tasks):
            t.cancel()
        await asyncio.gather(*tasks, *chat.tasks, return_exceptions=True)
    return exit_code


def is_frozen() -> bool:
    """Läuft als Windows-.exe (PyInstaller)?"""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    # In der .exe liegt __file__ in einem temporären Ordner – config.json gehört neben die .exe
    return Path(sys.executable).resolve().parent if is_frozen() else Path(__file__).resolve().parent


def example_config() -> dict:
    cfg = {
        "username": "dein_twitch_name",
        "client_id": "hier_deine_client_id",
        "access_token": "hier_dein_access_token",
        "channels": ["kanal1", "kanal2"],
        "ntfy_topic": "geheimer-name-nur-fuer-dich-8f3k2",
    }
    cfg.update({k: v for k, v in DEFAULTS.items() if k not in cfg and k not in ("channels",)})
    return cfg


def create_config_if_missing(path: Path) -> bool:
    """Beim ersten Start der .exe eine config.json zum Ausfüllen anlegen. True = neu angelegt."""
    if path.exists():
        return False
    path.write_text(json.dumps(example_config(), ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Neue Konfigurationsdatei angelegt: %s", path)
    log.info("Bitte mit einem Texteditor öffnen, deine Daten eintragen (siehe ANLEITUNG.md) und den Bot neu starten.")
    return True


def selftest() -> int:
    """Prüft ohne Netzwerk, ob die .exe vollständig ist (wird beim Bauen auf GitHub ausgeführt)."""
    import ssl
    import tempfile

    ssl.create_default_context()
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "config.json"
        assert create_config_if_missing(p)
        try:
            load_config(p)
            raise AssertionError("Beispielwerte wurden nicht erkannt")
        except FatalError:
            pass  # richtig: unausgefüllte Vorlage wird abgelehnt
        data = json.loads(p.read_text(encoding="utf-8"))
        data.update(username="testnutzer", client_id="cid", access_token="tok", channels=["testkanal"], ntfy_topic="t")
        p.write_text(json.dumps(data), encoding="utf-8")
        cfg = load_config(p)
    det = Detector(cfg)
    hit = det.check_giveaway("kanal", "streamelements", "GIVEAWAY! !discord dann !join", now=1.0)
    assert hit and hit[0] == "!join", hit
    assert det.is_win("nightbot", f"Glückwunsch @{cfg['username']}, du hast gewonnen")
    assert parse_irc(":a!a@a.tmi.twitch.tv PRIVMSG #k :hi")["text"] == "hi"
    print(f"OK websockets={websockets.__version__}")
    return 0


def main() -> None:
    setup_logging()
    if sys.version_info < (3, 9):
        log.error("Bitte Python 3.9 oder neuer verwenden.")
        sys.exit(1)
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    config_path = Path(args[0]) if args else app_dir() / "config.json"
    code = 0
    try:
        if is_frozen() and create_config_if_missing(config_path):
            code = 1
        else:
            code = asyncio.run(main_async(config_path))
    except KeyboardInterrupt:
        log.info("Bot beendet (Strg+C).")
        code = 0
    if is_frozen():
        # Bei Doppelklick schließt sich das Fenster sonst sofort – Meldungen sollen lesbar bleiben
        try:
            input("\nDrücke Enter zum Schließen …")
        except (EOFError, KeyboardInterrupt):
            pass
    sys.exit(code)


if __name__ == "__main__":
    main()
