# Twitch-Giveaway-Bot – Anleitung

Der Bot läuft mit **deinem** Twitch-Account in einer festen Liste von Kanälen. Er erkennt Giveaways im Chat,
trägt dich **einmal** ein und schickt dir eine **Push-Nachricht aufs Handy**, wenn

- du eine **Flüsternachricht** bekommst (Gewinner werden oft per Whisper angeschrieben) oder
- du im Chat als **Gewinner** genannt wirst.

> ⚠️ **Wichtig:** Der Access-Token ist **wie ein Passwort**. Wer ihn hat, kann in deinem Namen chatten und deine
> Flüsternachrichten lesen. Gib ihn nie weiter, poste ihn nirgends und lade `config.json` nirgends hoch.

---

## 0. Einfachster Weg: GiveawayBot.exe (Windows, ohne Python)

1. **[GiveawayBot.zip herunterladen](https://github.com/ReparaturServiceBredin/Publisher/releases/download/giveaway-bot/GiveawayBot.zip)**
   (oder nur die [GiveawayBot.exe](https://github.com/ReparaturServiceBredin/Publisher/releases/download/giveaway-bot/GiveawayBot.exe)).
2. Einen eigenen Ordner anlegen, z. B. `C:\GiveawayBot`, und den Inhalt dort hinein entpacken.
3. `GiveawayBot.exe` doppelklicken. Windows warnt evtl. („Der Computer wurde durch Windows geschützt“):
   *Weitere Informationen → Trotzdem ausführen*.
4. Beim ersten Start legt der Bot neben der exe eine **`config.json`** an und schließt sich nach Enter.
   Diese Datei mit dem Editor öffnen (Rechtsklick → Öffnen mit → Editor) und wie in **Schritt 2–5** beschrieben
   ausfüllen. Schritt 1 (Python) brauchst du mit der exe **nicht**.
5. `GiveawayBot.exe` erneut starten. Das schwarze Fenster zeigt alles, was passiert. Zum Beenden das Fenster
   schließen oder **Strg + C** drücken.

Die exe aktualisiert sich **nicht** selbst. Für eine neue Version lädst du sie über den Link oben neu herunter
und ersetzt die alte. Deine `config.json` bleibt dabei erhalten.

## 1. Python und websockets installieren (nur ohne exe nötig)

1. Python 3.9 oder neuer installieren: <https://www.python.org/downloads/>.
   Unter Windows beim Installieren **„Add Python to PATH“** anhaken.
2. Eine Eingabeaufforderung öffnen (Windows: Start → „cmd“) und eingeben:

   ```
   pip install websockets
   ```

## 2. Twitch-App anlegen (Client-ID)

1. <https://dev.twitch.tv/console> öffnen und mit deinem Twitch-Account anmelden
   (dafür muss bei Twitch die Zwei-Faktor-Anmeldung aktiv sein).
2. **„Register Your Application“** bzw. **„Anwendung registrieren“** klicken.
3. Ausfüllen:
   - **Name:** beliebig, z. B. `mein-giveaway-bot-123` (muss bei Twitch einmalig sein)
   - **OAuth Redirect URLs:** `http://localhost:3000`
   - **Kategorie:** „Chat Bot“
   - **Client-Typ:** „Vertraulich“ (Confidential) ist ok
4. Speichern, die App öffnen und die **Client-ID** kopieren. Ein Client-Secret brauchst du **nicht**.

## 3. Access-Token holen

1. Diese Adresse in den Browser kopieren und `DEINE_CLIENT_ID` durch deine Client-ID ersetzen:

   ```
   https://id.twitch.tv/oauth2/authorize?response_type=token&client_id=DEINE_CLIENT_ID&redirect_uri=http://localhost:3000&scope=chat:read+chat:edit+user:read:whispers
   ```

2. Mit dem Twitch-Account anmelden, mit dem der Bot laufen soll, und **„Autorisieren“** klicken.
3. Der Browser landet auf einer Seite, die nicht lädt („Diese Seite ist nicht erreichbar“). **Das ist richtig so.**
   In der **Adresszeile** steht jetzt etwas wie:

   ```
   http://localhost:3000/#access_token=abc123xyz...&scope=chat%3Aread+...&token_type=bearer
   ```

4. Kopiere **nur** den Teil zwischen `access_token=` und dem nächsten `&`. Das ist dein Token.

Der Token läuft nach einiger Zeit ab (meist nach ca. 60 Tagen oder wenn du dein Passwort änderst). Dann schickt
dir der Bot die Push-Nachricht **„Token abgelaufen“** und beendet sich. Hol dir dann mit Schritt 3 einen neuen Token.

## 4. Push-Nachrichten aufs Handy (ntfy)

1. Die App **ntfy** installieren ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy) /
   [iPhone](https://apps.apple.com/app/ntfy/id1625396347)).
2. In der App auf **„+“** tippen und einen **geheimen Topic-Namen** abonnieren, den niemand erraten kann,
   z. B. `giveaway-max-8f3k2q9`. (Wer den Namen kennt, kann deine Nachrichten mitlesen!)
3. Für dringende Nachrichten (Gewinn, Flüsternachricht) in den Topic-Einstellungen am besten einen lauten Ton wählen.

Optional zusätzlich **Discord**: In einem eigenen Discord-Server unter Kanal-Einstellungen → Integrationen →
Webhooks einen Webhook erstellen und die URL bei `discord_webhook` eintragen. Bei dringenden Nachrichten wird
`@everyone` erwähnt.

## 5. config.json ausfüllen

1. `config.example.json` kopieren und die Kopie **`config.json`** nennen (im selben Ordner wie `giveaway_bot.py`).
2. Eintragen:

| Feld | Bedeutung |
|---|---|
| `username` | dein Twitch-Name (klein geschrieben) |
| `client_id` | Client-ID aus Schritt 2 |
| `access_token` | Token aus Schritt 3 (ohne `oauth:`) |
| `channels` | Kanäle, in denen der Bot mitliest, z. B. `["kanal1", "kanal2"]` |
| `ntfy_topic` | dein geheimer ntfy-Topic aus Schritt 4 |
| `discord_webhook` | optional, sonst `""` lassen |
| `dry_run` | `true` = nur testen, nichts in den Chat schreiben (Standard) |
| `notify_on_entry` | Push bei jedem Eintrag (`true`/`false`) |
| `entry_delay_sec` | zufällige Wartezeit vor dem Eintrag, z. B. `[3, 12]` Sekunden |
| `entry_cooldown_sec` | höchstens ein Eintrag pro Kanal in diesem Zeitraum (600 = 10 Minuten) |
| `burst_min_users` / `burst_window_sec` | so viele **verschiedene** Leute müssen in so vielen Sekunden denselben `!befehl` schreiben, damit es als Giveaway gilt |
| `giveaway_bots` | Bots, deren Giveaway-Ansagen ausgewertet werden |
| `blocked_commands` | Befehle, die **nie** gesendet werden (`!discord`, `!uptime` …) |

## 6. Erst testen (dry_run), dann scharf schalten

1. Mit `"dry_run": true` starten: `GiveawayBot.exe` doppelklicken oder, mit Python,

   ```
   python giveaway_bot.py
   ```

   Du bekommst sofort die Push **„Bot gestartet“**. Wird ein Giveaway erkannt, steht in der Konsole
   `[DRY-RUN] Würde in #kanal senden: !join` und du bekommst eine Push, aber im Chat passiert nichts.
2. Ein paar Tage beobachten: Werden die richtigen Befehle erkannt? Wird nichts Falsches gesendet?
   Falls nötig, Befehle bei `blocked_commands` ergänzen.
3. Erst dann `"dry_run": false` setzen und den Bot neu starten.

Beenden mit **Strg + C**. Bei Verbindungsabbrüchen verbindet sich der Bot nach 10 Sekunden selbst neu.

## 7. Grenzen – bitte lesen

- Der Bot erkennt **nur Giveaways im Chat** (Bot-Ansagen wie „Giveaway gestartet, tippe !join“ oder viele Leute,
  die denselben Befehl schreiben). **Gesprochene Ansagen** im Stream, Giveaways über Kanalpunkte, Websites,
  Discord oder Twitter erkennt er **nicht**.
- Die Gewinn-Erkennung achtet auf deinen Namen plus Wörter wie „gewonnen“, „winner“, „congrats“. Sie kann
  Fehlalarme geben (z. B. „@du hast fast gewonnen“) und echte Gewinne übersehen, wenn sie anders formuliert sind.
  Schau also lieber einmal mehr in den Kanal.
- **Halte die Kanalliste klein.** Wer in sehr vielen Kanälen automatisch an Giveaways teilnimmt, fällt auf.
  Viele Streamer und Mods verbieten das ausdrücklich und **bannen** solche Accounts. Auch die Twitch-Regeln
  zu automatisiertem Verhalten gelten. Nutzung auf eigenes Risiko. Am besten nur in Kanälen, die du wirklich
  schaust und in denen automatische Teilnahme kein Problem ist.
- Der Bot trägt dich pro Kanal höchstens einmal pro `entry_cooldown_sec` ein und wartet vorher zufällig
  `entry_delay_sec`. So spammt er nicht, und es sieht weniger nach Bot aus.
- Der Bot muss laufen (PC an), damit er etwas mitbekommt.
