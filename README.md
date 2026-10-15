# Publisher

## Schnellstart: Publisher.exe (Windows)

1. **Herunterladen:** Unter [Releases](https://github.com/ReparaturServiceBredin/Publisher/releases/latest)
   die Datei `Publisher.exe` laden.
2. **Ablegen:** Ordner `C:\Publisher` anlegen und die exe dort hineinlegen (nicht direkt nach `C:\`
   und nicht in „Programme“, sonst darf sie sich nicht selbst aktualisieren).
3. **Starten:** Doppelklick. Beim ersten Mal warnt Windows evtl. („Der Computer wurde durch Windows
   geschützt“): *Weitere Informationen → Trotzdem ausführen*.
4. **Benutzen:** Unter **Erstellen** einen oder mehrere VOD-Links (Twitch/YouTube) oder Dateien eintragen,
   dann auf **Highlights erstellen** klicken.

**Updates:** Jeder neue Stand in diesem Repo baut automatisch eine neue `Publisher.exe` (GitHub Actions →
Release). Die exe prüft beim Start, ob es eine neuere Version gibt, lädt sie herunter und startet sich neu.
Kein Passwort und kein Login nötig.

### Die Bereiche der App

| Bereich | Wofür |
|---|---|
| **Erstellen** | Quellen eintragen, Länge (5–15 Min), Fokus („Lachen“ oder „Alles Laute“), Layout bei mehreren VODs, Übergänge, Hochformat-Clips, Empfehlungen aus dem Erfolgsprofil |
| **Videobox** | Alle fertigen Videos mit Vorschaubild; abspielen, Ordner öffnen, **direkt zu YouTube oder TikTok hochladen** |
| **Referenzvideos** | Fertige Videos hochladen, Ergebnisse eintragen, **Erfolgsprofil** ansehen |
| **Verbindungen** | YouTube- und TikTok-Konto einmalig verbinden |
| **Freigaben** | Pro Creator „erlaubt / angefragt / nicht erlaubt“ – nur freigegebene Clips (oder eigene VODs) lassen sich hochladen |

Ergebnis in `C:\Users\<Name>\Publisher\Fertig\<Datum Titel>\`:
- `<Titel>.mp4`: Highlight-Video mit weichen Überblendungen
- `Einzelclips\…`: jeder Moment einzeln (16:9)
- `Hochformat\…`: dieselben Clips in 9:16 mit unscharfem Hintergrund für TikTok/Shorts
- `Beschreibung.txt`: Titel, Kapitelmarken und Credits

Oben rechts: **⟳ Update** mit der aktuellen Versionsnummer daneben.

### Referenzvideos: aus fertigen Videos lernen
1. **Referenzvideos → Videos:** fertige Videos ins Fenster ziehen oder „Dateien auswählen“ (mehrere gleichzeitig) –
   **oder einen Link einfügen** (YouTube, Shorts, TikTok, Instagram, Twitch …; mehrere mit Leerzeichen, Links
   lassen sich auch aus dem Browser ins Fenster ziehen). Link-Videos werden in 720p geladen, Views/Likes/Kommentare
   automatisch übernommen („Zahlen aktualisieren“ holt später die aktuellen Werte). Alles landet in
   `C:\Users\<Name>\Publisher\Referenzvideos` und wird automatisch analysiert.
2. **Bearbeiten:** Plattform, „Wie lief es?“ (gut / mittel / schlecht) und optional Views, Ø Wiedergabedauer,
   Retention, Likes, Kommentare und Shares eintragen (Zahlen auch als „12.500“, „1,2k“, „3 Mio“).
   Falsch erkannte Merkmale (Untertitel, Musik, Text im Hook, CTA, Hook-Zeit) lassen sich dort korrigieren.
3. **Erfolgsprofil:** Ab je 2 gut und 2 schwach laufenden Videos zeigt das Tool, welche Merkmale die guten
   Videos häufiger haben (Karten mit Balken: grün = gute, grau = schwache Videos) und was es empfiehlt.
   Das Profil aktualisiert sich bei jeder Änderung.
4. **Im Editor (Erstellen):** Die wichtigsten Empfehlungen stehen über dem Start-Button. Mit
   „Erfolgsprofil beim Schnitt anwenden“ übernimmt der Generator sie, zum Beispiel mit Clips, die näher am Höhepunkt
   starten, mit kürzeren Clips (mehr Schnitte) und mit der empfohlenen Länge. Nach dem Erstellen prüft das Tool das neue Video
   gegen dein Profil („Check deines neuen Videos“).
5. **Verwalten:** Ansehen, Bearbeiten, Neu analysieren, Löschen; „Für Lernen nutzen“ schließt ein Video aus.

Analysiert werden Länge, Schnittfrequenz, Ø Einstellungslänge, Rhythmus, Bewegung/Tempo, Hook (erster starker
Moment in den ersten 3 s), Hook-Energie, Text-Overlays, Untertitel, Musik- und Sprachanteil, Lautstärke,
Intro-Länge, Position des Höhepunkts, Call-to-Action am Ende und das Format. Alles läuft lokal (ffmpeg + numpy),
ohne Spracherkennung oder Texterkennung. Die Werte sind deshalb gute Schätzungen und keine exakten Messungen.

### Update-Button
- **⟳ Update** (oben rechts) prüft auf eine neue Version: „Du bist auf dem neuesten Stand“ oder
  „Update verfügbar“ mit der Option, es direkt zu installieren (mit Fortschrittsanzeige).
- Vorher wird gesichert: offene Bearbeitungsfenster werden gespeichert, die Eingaben auf „Erstellen“ werden
  nach dem Neustart wiederhergestellt. Läuft gerade eine Erstellung, wird das Update erst danach installiert.
  Bei einem laufenden Upload wartet das Update.
- Nach dem Neustart kommt „Update installiert ✓“ oder eine verständliche Fehlermeldung.

### Wie die Highlights gefunden werden
- **Lachen zuerst:** Lachen pulsiert rhythmisch („ha-ha-ha“, 3,5–7,5 Silben pro Sekunde im Sprachbereich).
  Genau dieser Takt wird gemessen. Gleichmäßiges Schreien, Musik oder Explosionen zählen weniger.
  Mit „Alles Laute“ zählt wieder jede laute Stelle.
- **Saubere Schnitte:** Start und Ende jedes Clips werden auf die nächste Sprechpause geschoben. Zwischen den
  Clips gibt es eine 0,5-Sekunden-Überblendung (Bild und Ton), am Anfang und Ende wird ein- bzw. ausgeblendet.
- **Mehrere VODs (gemeinsamer Stream):** Die VODs werden über die Startzeit und den Ton synchronisiert (Streamer im
  selben Call hören sich gegenseitig). Momente, in denen mehrere gleichzeitig reagieren, zählen mehr.
  - *POV-Wechsel:* erst die Situation aus dem stärksten POV, danach die Reaktionen der anderen
  - *Splitscreen:* 2 Streamer nebeneinander, 3–4 im Raster, jeweils mit Namen
- Bei Links wird zuerst nur die Tonspur geladen, danach nur die Highlight-Abschnitte als Video.

### YouTube verbinden (einmalig)
1. In der [Google Cloud Console](https://console.cloud.google.com/) ein Projekt anlegen, *YouTube Data API v3*
   aktivieren, OAuth-Zustimmungsbildschirm einrichten (dich selbst als Testnutzer eintragen) und einen
   OAuth-Client vom Typ **Desktop-App** erstellen. Die JSON-Datei herunterladen.
2. In der App unter **Verbindungen → YouTube → Verbinden** die JSON-Datei wählen und im Browser anmelden.
3. Wichtig: Solange Google das API-Projekt nicht geprüft hat, setzt YouTube per API hochgeladene Videos
   automatisch auf **privat**. Das kannst du danach in YouTube Studio ändern. Das Tageskontingent reicht für etwa 6 Uploads.

### TikTok verbinden (einmalig)
1. Auf [developers.tiktok.com](https://developers.tiktok.com/) eine App anlegen, **Login Kit** und
   **Content Posting API** (Direct Post) hinzufügen, Scopes `user.info.basic` und `video.publish`.
2. Bei Login Kit → Desktop die Redirect-URI `http://127.0.0.1:3455/callback/` eintragen
   (Button „Redirect-URI kopieren“ in der App).
3. Client Key und Client Secret in der App eintragen → **Verbinden** → im Browser bestätigen.
4. Wichtig: Bis TikTok die App geprüft hat, erlaubt TikTok nur private Posts („Nur ich“).

---

## Für Entwickler

Macht Highlight-Videos und Shorts aus eigenen und fremden Twitch-/YouTube-VODs:
mit vollständigen Quellenangaben, automatischen Credits und Upload über die YouTube Data API v3.

## Installation

```bash
pip install -e ".[all,dev]"     # ffmpeg/ffprobe müssen im PATH liegen
cp config.example.yaml config.yaml
```

## Ablauf

```bash
# 1. VOD importieren – fragt Creator (aus creators.yaml oder „eigenes VOD“),
#    Datum + Startuhrzeit der Live-Aufnahme, optional Original-VOD-Link und Stream-Titel
publisher import mein-video ~/vods/stream.mp4

# 2. Clip-Kandidaten anlegen (oder in der Review-UI); Erkennungs-Code nutzt Project.add_candidate()
publisher add-clip mein-video --vod vod-1-xxxx --start 1:02:03 --end 1:03:00 --title "Der große Fail"

# 3. Review-UI: Clips auswählen/trimmen, Metadaten + Intro bearbeiten, exportieren, hochladen, veröffentlichen
publisher ui                     # http://127.0.0.1:5000

# Alternativ per CLI
publisher export mein-video      # rendert Video + description.txt + manifest.json
publisher upload mein-video      # immer privat; optional --publish-at 2026-10-20T18:00
publisher log                    # Upload-Log
```

## 1. Creator-Register (`creators.yaml`)

Pro Creator: `name`, `youtube_channel_url`, `twitch_channel_url`, `permission_status`
(`erlaubt` | `angefragt` | `nicht erlaubt`), `permission_proof` (Link oder Notiz) und optional
`credit` mit eigenen Vorgaben:

| Feld | Wirkung |
|---|---|
| `credit.first_line` | Steht als erster Satz der YouTube-Beschreibung |
| `credit.overlay_text` | Ersetzt die Einblendung „Clip von [Name] – twitch.tv/[name]“ |
| `credit.extra_line` | Zusätzliche Zeile im Credit-Block jedes Clips |
| `credit.link` | Zusätzlicher Link in der Creator-Liste am Ende |

Das Register wird bei jedem Upload und jedem Seitenaufruf frisch gelesen.

## 2. Quellen-Tracking

Jeder Clip speichert beim Anlegen eine unveränderliche Quelle (`ClipSource`): Creator, VOD,
Original-VOD-Link, Stream-Titel, Beginn der Livesendung, Zeitstempel im VOD und die echte
Uhrzeit der Live-Aufnahme. In der Review-UI lassen sich nur Titel, Trim, Status und Reihenfolge
ändern (`Candidate.EDITABLE_FIELDS`). Jede Änderung landet in `history`. Verworfene Clips werden
nicht gelöscht. Beim Export kommt die Position im fertigen Video dazu. Export-Manifest und Upload-Log
enthalten die vollständige Herkunft inklusive der ursprünglichen Erkennung.

## 3. Beschreibung und Credits

Beim Export entstehen:

- Credit-Vorgaben der Creator als erster Satz, danach der Intro-Text. Den schreibt Claude
  (`claude-opus-5-5`) im Stil der Beispiele in `style_references/`, ohne API-Zugang greift ein
  Vorlagentext. Das Intro lässt sich in der UI bearbeiten.
- Kapitelmarken (`00:00 …`; bei über 1 Stunde Länge `H:MM:SS`). Die Prüfung warnt, wenn YouTube
  keine Kapitel erzeugen würde: weniger als 3 Marken oder ein Kapitel unter 10 Sekunden.
- Pro Clip ein Credit-Block mit Creator, Live-Zeitpunkt, Original-VOD-Link inklusive Zeitsprung
  (`?t=1h2m3s` bei Twitch, `?t=123s` bei YouTube) und Kanal-Links.
- Am Ende eine Liste aller Creator mit allen Links.
- Im Video wird in den ersten 3 Sekunden jedes fremden Clips „Clip von [Name] – twitch.tv/[name]“
  eingeblendet.
- Wird das YouTube-Limit von 5000 Zeichen überschritten, fallen zuerst die Kanal-Links pro Clip weg
  (sie stehen weiter in der Creator-Liste), dann das Intro.

Shorts (Haken „YouTube Short“, genau ein Clip): Das Video wird ins Hochformat 9:16 geschnitten und
der Credit ist durchgehend zu sehen. Die Beschreibung ist kurz und enthält Creator und Links.
Titel und Tags bekommen `#Shorts`.

## 4. YouTube-Upload

1. In der Google Cloud Console die *YouTube Data API v3* aktivieren und einen OAuth-Client vom Typ
   „Desktop-App“ anlegen. Die JSON-Datei als `client_secret.json` speichern.
2. Beim ersten Upload öffnet sich der Browser für den Login. Das Token liegt danach in `data/token.json`.
3. Hochgeladen wird über `videos.insert` (Titel, Beschreibung, Tags, Kategorie), das Thumbnail
   über `thumbnails.set`. Neue Videos sind immer **privat**.
4. Veröffentlicht wird nur in der Review-UI („Jetzt veröffentlichen“ mit Bestätigung) oder als
   geplanter Zeitpunkt (`publishAt`).
5. Jeder Upload, jede Planung und jede Veröffentlichung wird in `data/upload_log.jsonl`
   protokolliert, mit Video, Quellen, Creatorn und Zeitpunkt.

## 5. Sicherheitsregeln

- `gate.find_blockers()` prüft alle ausgewählten Clips **und** alle Clips im zuletzt exportierten
  Video gegen den aktuellen Stand von `creators.yaml`. Gesperrt wird jeder Status außer `erlaubt`.
  Creator, die im Register fehlen, sperren ebenfalls. Eigene VODs brauchen keine Freigabe.
- `upload_project`, `publish_video` und `schedule_video` rufen die Prüfung vor jedem API-Aufruf
  auf. Die Sperre gilt also auch über die CLI und direkte POST-Requests, nicht nur über den
  ausgegrauten Button.
- Die UI zeigt, welcher Clip von welchem Creator mit welchem Status blockiert.
- Wird eine Freigabe nach dem Planen entzogen, hebt `publisher check-scheduled` die geplante
  Veröffentlichung wieder auf (z. B. per Cron).
- Verarbeitung und lokaler Export sind für alle VODs möglich.

## Tests

```bash
pytest
```
