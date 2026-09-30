# 🐱 Shadow-Futterbot

Hält die Katzenfutter-Liste für Katjas Kater Shadow aktuell – per Telegram-Zuruf vom
Handy (iPhone + Android), ganz ohne dass ein PC laufen muss.

## Wie es funktioniert

```
Telegram (@mauderbot) ──alle 5 Min──► GitHub Action ──► DeepSeek (versteht „mag er / mag er nicht")
                                                     ──► CouchDB (LiveSync-Format, via obsidian-livesync-mcp)
                                                     ──► Obsidian: PC + Handys syncen automatisch
```

- **Phase 2 (aktiv):** Der Bot versteht Einzelmeldungen („SuperMono Ente mag er nicht")
  und Sammelaufträge („alles mit Zucchini auf vielleicht") und pflegt **Tabelle und
  🛒 Einkaufszettel direkt**. Unklare Fälle landen als ⚪-Eintrag im 📥 Spracheingang
  zur manuellen Prüfung.
- Alles läuft über **ausgehende HTTPS-Verbindungen** – nirgends offene Ports nötig.
- Antworten kommen per Telegram-Bestätigung zurück.

## Kosten: 0 €

- GitHub Actions (öffentliches Repo = unbegrenzte Minuten), Telegram gratis,
  DeepSeek aus vorhandenen API-Credits (Bruchteile von Cent pro Nachricht).
- Das Repo ist öffentlich, damit die Actions-Minuten unbegrenzt sind.
  **Geheimnisse stehen niemals im Code** – nur Adressen ohne Zugangsdaten.

## Benötigte Secrets (Repo → Settings → Secrets → Actions)

| Name | Inhalt |
|---|---|
| `TELEGRAM_TOKEN` | Bot-Token vom BotFather |
| `TELEGRAM_CHAT_ID` | Eigene Chat-ID (Allowlist – fremde Chats werden ignoriert) |
| `DEEPSEEK_API_KEY` | DeepSeek-API-Key |
| `OBSIDIAN_COUCH_URL` | CouchDB-Basis-URL, z. B. `https://couch.example.com` (ohne DB-Namen) |
| `OBSIDIAN_COUCH_USER` | CouchDB-Benutzer |
| `OBSIDIAN_COUCH_PASS` | CouchDB-Passwort |
| `OBSIDIAN_COUCH_DB` | Datenbankname des Vaults |

Zusätzlich merkt sich der Bot die Telegram-Update-ID in `offset.json`
(in Actions per Cache persistiert) – so wird keine Nachricht doppelt verarbeitet.
Doppelte Einträge erkennt er außerdem am Inhalt und antwortet dann nicht erneut.

## Dateien

| Datei | Zweck |
|---|---|
| `bot.py` | Der Bot (nur Python-Stdlib + `obsidian`-CLI) |
| `prompt.md` | System-Prompt für DeepSeek (Sortiment + JSON-Regeln) |
| `requirements.txt` | `obsidian-livesync-mcp` (LiveSync-kompatibles Lesen/Schreiben) |
| `.github/workflows/futterbot.yml` | Cron alle 5 Min + manueller Start |

## Manuell testen

Repo → Actions → „Shadow-Futterbot" → „Run workflow". Danach eine Nachricht an den
Bot schicken und beim nächsten Lauf landet sie im 📥 Spracheingang.
