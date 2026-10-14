# Publisher

## Schnellstart: Publisher.exe (Windows)

1. **Herunterladen:** Unter [Releases](https://github.com/ReparaturServiceBredin/Publisher/releases/latest)
   die Datei `Publisher.exe` laden.
2. **Ablegen:** Ordner `C:\Publisher` anlegen und die exe dort hineinlegen (nicht direkt nach `C:\`
   und nicht in „Programme“, sonst darf sie sich nicht selbst aktualisieren).
3. **Starten:** Doppelklick. Beim ersten Mal warnt Windows evtl. („Der Computer wurde durch Windows
   geschützt“): *Weitere Informationen → Trotzdem ausführen*.
4. **Benutzen:** VOD-Link (Twitch/YouTube) einfügen oder eine Datei wählen → **Highlights erstellen**.

Ergebnis in `C:\Users\<Name>\Publisher\Fertig\…`:
- `<Titel>.mp4`: Highlight-Video (Länge wählbar, Standard 12 Minuten)
- `Einzelclips\01 Highlight 1.mp4`, …: jeder Moment einzeln
- `Beschreibung.txt`: Titel, Kapitelmarken und Credits zum Kopieren

**Updates:** Jeder neue Stand in diesem Repo baut automatisch eine neue `Publisher.exe` (GitHub Actions →
Release). Die exe prüft beim Start, ob es eine neuere Version gibt, lädt sie und startet sich neu.
Kein Passwort und kein Login nötig.

**Wie werden Highlights gefunden?** Über die Tonspur: Momente, die deutlich lauter sind als der normale
Pegel des Streams (Schreien, Lachen, Jubel, Action). Jeder Treffer bekommt etwa 20 s Vorlauf und 8 s
Nachlauf. Bei Links wird zuerst nur die Tonspur geladen, danach nur die Highlight-Abschnitte als Video.
Das ganze VOD muss also nicht heruntergeladen werden.

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
