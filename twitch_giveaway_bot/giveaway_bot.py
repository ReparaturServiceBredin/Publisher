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
NTFY_URL = "https://ntfy.sh/"

RECONNECT_DELAY_SEC = 10
JOIN_BATCH_SIZE = 20
JOIN_BATCH_INTERVAL_SEC = 10
USER_AGENT = "TwitchGiveawayBot/1.0 (+python-urllib)"

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
    cfg = {**DEFAULTS, **{k: v for k, v in raw.items() if not k.startswith("_")}}

    cfg["username"] = str(cfg["username"]).strip().lstrip("@").lower()
    token = str(cfg["access_token"]).strip()
    cfg["access_token"] = token[6:] if token.lower().startswith("oauth:") else token
    cfg["client_id"] = str(cfg["client_id"]).strip()
    cfg["channels"] = list(dict.fromkeys(str(c).strip().lstrip("#").lower() for c in cfg["channels"] if str(c).strip()))
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
        self.client_id = cfg["client_id"]
        self.token = cfg["access_token"]

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

IRC_LINE = re.compile(r"^(?:@(?P<tags>\S+) )?(?::(?P<prefix>\S+) )?(?P<command>\S+)(?P<params>.*)$")
COMMAND_RE = re.compile(r"(?<!\S)(![A-Za-z0-9_]+)")
SINGLE_COMMAND_RE = re.compile(r"^![A-Za-z0-9_]+$")


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

    def first_command(self, text: str) -> Optional[str]:
        for cmd in COMMAND_RE.findall(text):
            cmd = cmd.lower()
            if cmd not in self.cfg["blocked_commands"]:
                return cmd
        return None

    def check_giveaway(self, channel: str, sender: str, text: str, now: Optional[float] = None) -> Optional[Tuple[str, str]]:
        """Gibt (Befehl, Grund) zurück, wenn ein Eintrag fällig ist – sonst None."""
        now = time.monotonic() if now is None else now
        if sender == self.me:
            return None
        lower = text.lower()
        found: Optional[Tuple[str, str]] = None
        # 1) Giveaway-Bot kündigt an
        if sender in self.cfg["giveaway_bots"] and any(w in lower for w in GIVEAWAY_WORDS):
            cmd = self.first_command(text)
            if cmd:
                found = (cmd, f"Ansage von {sender}")
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
        if found is None:
            return None
        if not self.cooldown_ok(channel, now):
            log.info("#%s: Giveaway erkannt (%s), aber Cooldown aktiv – kein Eintrag.", channel, found[1])
            return None
        self.last_entry[channel] = now  # sofort sperren, damit keine doppelten Einträge geplant werden
        return found

    def cooldown_ok(self, channel: str, now: float) -> bool:
        last = self.last_entry.get(channel)
        return last is None or now - last >= self.cfg["entry_cooldown_sec"]

    def is_win(self, sender: str, text: str) -> bool:
        if sender == self.me:
            return False
        lower = text.lower()
        return bool(self.me_re.search(text)) and any(w in lower for w in WIN_WORDS)


# ====================================================================== Chat (IRC über WebSocket)

class ChatBot:
    def __init__(self, cfg: dict, notifier: Notifier, stop: asyncio.Event):
        self.cfg = cfg
        self.notifier = notifier
        self.stop = stop
        self.detector = Detector(cfg)
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
                log.warning("Chat-Verbindung getrennt: %s", exc or type(exc).__name__)
            except Exception as exc:  # noqa: BLE001
                log.exception("Unerwarteter Fehler in der Chat-Verbindung: %s", exc)
            finally:
                self.ws = None
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
        async with websockets.connect(IRC_URL, ping_interval=None, max_size=2 ** 20) as ws:
            self.ws = ws
            await ws.send("CAP REQ :twitch.tv/tags twitch.tv/commands")
            await ws.send(f"PASS oauth:{self.cfg['access_token']}")
            await ws.send(f"NICK {self.cfg['username']}")
            while True:
                # Twitch schickt etwa alle 5 Minuten ein PING – nach 6 Minuten Stille gilt die Verbindung als tot
                raw = await asyncio.wait_for(ws.recv(), timeout=360)
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
            self.join_task = self._spawn(self._join_channels())
        elif cmd == "NOTICE" and msg["text"] and ("authentication failed" in msg["text"].lower()
                                                   or "improperly formatted auth" in msg["text"].lower()):
            log.error("Chat-Anmeldung fehlgeschlagen: %s", msg["text"])
            await self.notifier.send("Token abgelaufen",
                                     "Die Anmeldung im Twitch-Chat ist fehlgeschlagen. Bitte einen neuen Token holen "
                                     "und in config.json eintragen. Der Bot wurde beendet.", urgent=True)
            raise FatalError("Chat-Anmeldung fehlgeschlagen (Token abgelaufen oder falsch)")
        elif cmd == "NOTICE":
            channel = msg["params"][0].lstrip("#") if msg["params"] else "?"
            log.info("Hinweis von Twitch (#%s): %s", channel, msg["text"])
        elif cmd == "RECONNECT":
            log.info("Twitch bittet um Neuverbindung.")
            raise ConnectionError("RECONNECT von Twitch")
        elif cmd == "JOIN" and msg["nick"] == self.cfg["username"]:
            log.info("Kanal #%s betreten.", msg["params"][0].lstrip("#") if msg["params"] else "?")
        elif cmd == "PRIVMSG" and msg["params"] and msg["text"] is not None:
            channel = msg["params"][0].lstrip("#").lower()
            sender = (msg["tags"].get("login") or msg["nick"] or "").lower()
            await self._on_message(channel, sender, msg["tags"].get("display-name") or sender, msg["text"])

    async def _join_channels(self) -> None:
        """Höchstens 20 Kanäle pro 10 Sekunden (Twitch-Limit) – läuft im Hintergrund."""
        channels = self.cfg["channels"]
        for i in range(0, len(channels), JOIN_BATCH_SIZE):
            batch = channels[i:i + JOIN_BATCH_SIZE]
            await self.send_raw("JOIN " + ",".join(f"#{c}" for c in batch))
            log.info("Betrete %d Kanal/Kanäle: %s", len(batch), ", ".join(batch))
            if i + JOIN_BATCH_SIZE < len(channels):
                await asyncio.sleep(JOIN_BATCH_INTERVAL_SEC)

    async def _on_message(self, channel: str, sender: str, display: str, text: str) -> None:
        if self.detector.is_win(sender, text):
            log.warning("🎉 MÖGLICHER GEWINN in #%s von %s: %s", channel, display, text)
            self._spawn(self.notifier.send(
                f"🎉 Gewinn in #{channel}?",
                f"Kanal: {channel}\nVon: {display}\nNachricht: {text}\nhttps://twitch.tv/{channel}",
                urgent=True, click=f"https://twitch.tv/{channel}",
            ))
        hit = self.detector.check_giveaway(channel, sender, text)
        if hit:
            command, reason = hit
            log.info("Giveaway in #%s erkannt (%s) → Eintrag mit %s geplant.", channel, reason, command)
            self._spawn(self._enter(channel, command, reason))

    async def _enter(self, channel: str, command: str, reason: str) -> None:
        low, high = self.cfg["entry_delay_sec"]
        delay = random.uniform(low, high)
        log.info("#%s: warte %.1f s vor dem Eintrag …", channel, delay)
        await asyncio.sleep(delay)
        if self.cfg["dry_run"]:
            log.info("[DRY-RUN] Würde in #%s senden: %s", channel, command)
            await self.notifier.send(f"Dry-Run: Giveaway in #{channel}",
                                     f"Würde „{command}“ senden ({reason}).", click=f"https://twitch.tv/{channel}")
            return
        try:
            await self.send_raw(f"PRIVMSG #{channel} :{command}")
        except Exception as exc:  # noqa: BLE001
            log.error("Eintrag in #%s fehlgeschlagen (%s) – Verbindung weg?", channel, exc)
            self.detector.last_entry.pop(channel, None)  # später erneut erlauben
            return
        log.info("✅ In #%s eingetragen mit %s.", channel, command)
        if self.cfg["notify_on_entry"]:
            await self.notifier.send(f"Eingetragen in #{channel}", f"Gesendet: {command} ({reason})",
                                     click=f"https://twitch.tv/{channel}")


# ====================================================================== Flüsternachrichten (EventSub WebSocket)

class WhisperListener:
    def __init__(self, cfg: dict, api: TwitchApi, notifier: Notifier, stop: asyncio.Event):
        self.cfg = cfg
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
                log.warning("EventSub getrennt (%s) – neuer Versuch in %d Sekunden.", exc or type(exc).__name__,
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
        elif status == 409:
            log.info("Flüsternachrichten sind bereits abonniert.")
        elif status == 401:
            await self.notifier.send("Token abgelaufen", "Flüsternachrichten konnten nicht abonniert werden – "
                                                         "bitte neuen Token holen. Der Bot wurde beendet.", urgent=True)
            raise FatalError("Token abgelaufen (EventSub)")
        elif status == 403:
            self.disabled = True
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
        await self.notifier.send(f"Flüsternachricht von {name}", text or "(leer)", urgent=True,
                                 click="https://www.twitch.tv/popout/moderator/whispers")


# ====================================================================== Start

async def main_async(config_path: Path) -> int:
    try:
        cfg = load_config(config_path)
    except FatalError as exc:
        log.error("%s", exc)
        return 1
    notifier = Notifier(cfg)
    api = TwitchApi(cfg)
    stop = asyncio.Event()

    try:
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
        await notifier.send("Token abgelaufen", "Der Twitch-Token ist abgelaufen oder ungültig. Bitte neuen Token "
                                                "holen und in config.json eintragen. Der Bot wurde beendet.", urgent=True)
        return 1
    except Exception as exc:  # noqa: BLE001 – ohne Prüfung trotzdem weitermachen
        log.warning("Token konnte nicht geprüft werden (%s) – versuche es trotzdem.", exc)

    mode = "DRY-RUN (es wird nichts in den Chat geschrieben)" if cfg["dry_run"] else "SCHARF (trägt sich wirklich ein)"
    log.info("Bot startet für %s in %d Kanal/Kanälen – Modus: %s", cfg["username"], len(cfg["channels"]), mode)
    await notifier.send("Bot gestartet", f"Überwache {len(cfg['channels'])} Kanäle als {cfg['username']}. Modus: {mode}")

    chat = ChatBot(cfg, notifier, stop)
    whispers = WhisperListener(cfg, api, notifier, stop)
    tasks = [asyncio.ensure_future(chat.run()), asyncio.ensure_future(whispers.run())]
    exit_code = 0
    try:
        pending = set(tasks)
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                exc = t.exception()
                if isinstance(exc, FatalError):
                    log.error("Bot wird beendet: %s", exc)
                    exit_code = 1
                    stop.set()
                elif exc is not None:
                    log.error("Unerwarteter Fehler: %s", exc)
            if exit_code:
                break
    finally:
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
