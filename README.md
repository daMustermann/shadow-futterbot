# 🐱 Shadow-Futterbot

Hält die Katzenfutter-Liste für Katjas Kater Shadow aktuell – per Telegram-Zuruf vom
Handy (iPhone + Android), ganz ohne dass ein PC laufen muss.

## Wie es funktioniert

```
Telegram (@mauderbot) ──alle 5 Min──► GitHub Action ──► DeepSeek (versteht „mag er / mag er nicht")
                                                     ──► CouchDB (LiveSync-Format, via obsidian-livesync-mcp)
                                                     ──► Obsidian: PC + Handys syncen automatisch
```

- **Phase 1 (aktiv):** Der Bot schreibt nur in den 📥 Spracheingang von
  `40_Privat/Haustiere/Shadow-Katzenfutter.md`. Die KI-Kuration (Tabelle, ✅/❌, 🛒)
  übernimmt der Mensch bzw. Claudian.
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

Zusätzlich legt der Bot einmalig die CouchDB-Datenbank `bot_state` an
(merkt sich die Telegram-Update-ID – braucht Admin-Rechte des CouchDB-Users).

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
