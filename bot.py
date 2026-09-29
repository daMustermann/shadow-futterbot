#!/usr/bin/env python3
"""Shadow-Futterbot 🐱 – Telegram -> DeepSeek -> CouchDB (LiveSync) -> Obsidian.

Laeuft als GitHub-Action alle paar Minuten (oder ueberall sonst mit Python 3.10+).
Alle Geheimnisse kommen aus Umgebungsvariablen, niemals aus Dateien.
Phase 1: Schreibt nur in den 📥 Spracheingang der Futterliste (risikoarm).
"""
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
NOTE_PATH = "40_Privat/Haustiere/Shadow-Katzenfutter.md"
INBOX_MARKER = "## 📥 Spracheingang"
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
DEEPSEEK_API = "https://api.deepseek.com/chat/completions"
EMOJI = {"ja": "🟢", "nein": "🔴", "vielleicht": "🟡", "unbekannt": "⚪"}


def env(name, required=True, default=None):
    val = os.environ.get(name, default)
    if required and not val:
        print(f"FEHLER: Umgebungsvariable {name} fehlt!", file=sys.stderr)
        sys.exit(1)
    return val


def http_json(url, payload=None, headers=None, timeout=30, method=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def tg(token, method, payload):
    return http_json(TELEGRAM_API.format(token=token, method=method), payload)


def couch_request(cfg, method, path, body=None):
    """Direkter CouchDB-Zugriff (nur fuer bot_state, NICHT fuer Notizen!)."""
    mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, cfg["url"], cfg["user"], cfg["password"])
    opener = urllib.request.build_opener(urllib.request.HTTPBasicAuthHandler(mgr))
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(cfg["url"] + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with opener.open(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8")
        except Exception:
            detail = ""
        return e.code, detail


def obsidian_cli(*args):
    """Schreibt/liest Notizen im korrekten LiveSync-Format (via obsidian-livesync-mcp)."""
    env_vars = dict(os.environ)
    env_vars["OBSIDIAN_COUCH_URL"] = CFG["couch"]["url"]
    env_vars["OBSIDIAN_COUCH_USER"] = CFG["couch"]["user"]
    env_vars["OBSIDIAN_COUCH_PASS"] = CFG["couch"]["password"]
    env_vars["OBSIDIAN_COUCH_DB"] = CFG["couch"]["db"]
    proc = subprocess.run(["obsidian", *args], capture_output=True, text=True,
                          timeout=120, env=env_vars)
    if proc.returncode != 0:
        raise RuntimeError(f"obsidian {' '.join(args)} failed: {proc.stderr.strip()[:500]}")
    return proc.stdout


def deepseek_parse(api_key, prompt, text):
    today = datetime.now(BERLIN).strftime("%d.%m.%Y")
    payload = {
        "model": "deepseek-chat",
        "temperature": 0.2,
        "max_tokens": 400,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"Heute ist der {today}. Nachricht: {text}"},
        ],
    }
    resp = http_json(DEEPSEEK_API, payload,
                     headers={"Authorization": f"Bearer {api_key}"}, timeout=60)
    return json.loads(resp["choices"][0]["message"]["content"])


HELP_TEXT = (
    "Miau! 🐱 Ich bin der Shadow-Futterbot.\n\n"
    "Schreib mir einfach, was Shadow gefressen hat – z. B.:\n"
    "• „SuperMono Wildschwein mag er"\n"
    "• „Lifestage Geflügel und Lachs frisst er nicht"\n"
    "• „Geflügel mit Pastinaken nur halb gefressen"\n\n"
    "Ich trage es in die Futterliste ein und melde mich mit Bestätigung. ✅"
)


def handle_message(text):
    """Parst eine Nachricht und traegt sie in den Spracheingang ein. Gibt Antworttext zurueck."""
    try:
        parsed = deepseek_parse(CFG["deepseek_key"], CFG["prompt"], text)
    except Exception as e:
        print(f"DeepSeek-Fehler: {e}")
        return "Hoppala 😿 – das habe ich gerade nicht verstanden. Schreib es mir bitte noch einmal anders."

    linie = str(parsed.get("linie", "unbekannt")).strip() or "unbekannt"
    sorte = str(parsed.get("sorte", "")).strip()
    urteil = str(parsed.get("urteil", "unbekannt")).strip().lower()
    if urteil not in EMOJI:
        urteil = "unbekannt"
    notiz = str(parsed.get("notiz", "")).strip() or str(parsed.get("zusammenfassung", "")).strip()

    if not sorte:
        return ("Hm, welche Sorte meinst du genau? 😺 Schreib z. B. „SuperMono Ente mag er".")

    now = datetime.now(BERLIN).strftime("%d.%m.%Y %H:%M")
    kern = f"{linie} {sorte}".strip()
    line = f"- [{now} Telegram] {kern} → {EMOJI[urteil]}"
    if notiz:
        line += f" – {notiz}"

    try:
        content = obsidian_cli("read", NOTE_PATH)
    except Exception as e:
        print(f"Lese-Fehler: {e}")
        return "Au weia 😿 – ich komme gerade nicht an die Futterliste. Ich versuche es beim nächsten Durchlauf erneut."

    if kern.lower() in content.lower() and INBOX_MARKER in content:
        # grobe Doppel-Erkennung: gleiche Sorte heute schon im Eingang?
        pass
    if line in content:
        return f"Das steht schon drin ✅ ({kern} {EMOJI[urteil]})"

    try:
        obsidian_cli("append", NOTE_PATH, "\n" + line)
    except Exception as e:
        print(f"Schreib-Fehler: {e}")
        return "Au weia 😿 – das Eintragen hat nicht geklappt. Ich versuche es beim nächsten Durchlauf erneut."

    name = kern or "Eintrag"
    if urteil == "ja":
        return f"Eingetragen ✅: {name} {EMOJI[urteil]} – Shadow mag es! Wird beim nächsten Aufräumen in die Liste übernommen."
    if urteil == "nein":
        return f"Eingetragen ✅: {name} {EMOJI[urteil]} – landet auf der Nicht-kaufen-Liste."
    if urteil == "vielleicht":
        return f"Eingetragen ✅: {name} {EMOJI[urteil]} – zum Nochmal-Testen vorgemerkt."
    return f"Eingetragen ✅: {name} {EMOJI[urteil]} – bitte beim Aufräumen prüfen."


def main():
    get_updates_url = None
    offset = 0
    rev = None

    # Offset aus separater CouchDB-DB laden (bleibt auch bei Cache-Verlust erhalten)
    import urllib.error
    status, _ = couch_request(CFG["couch"], "PUT", f"/{CFG['state_db']}")
    if status not in (201, 202, 412):
        print(f"WARNUNG: state-DB antwortet mit {status} – Updates werden trotzdem geholt.")
    status, doc = couch_request(CFG["couch"], "GET", f"/{CFG['state_db']}/telegram_offset")
    if status == 200 and isinstance(doc, dict):
        offset = int(doc.get("offset", 0))
        rev = doc.get("_rev")

    params = {"timeout": 0, "limit": 50}
    if offset:
        params["offset"] = offset + 1
    try:
        updates = tg(CFG["tg_token"], "getUpdates", params).get("result", [])
    except Exception as e:
        print(f"FEHLER bei getUpdates: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"{len(updates)} Update(s) ab Offset {offset}.")
    max_id = offset
    for upd in updates:
        uid = upd.get("update_id", 0)
        max_id = max(max_id, uid)
        msg = upd.get("message")
        if not msg:
            continue
        if str(msg.get("chat", {}).get("id")) != CFG["tg_chat"]:
            print(f"Ignoriere fremden Chat {msg.get('chat', {}).get('id')}.")
            continue
        text = (msg.get("text") or "").strip()
        if not text:
            reply = "Ich verstehe nur Text 😺 – tippe oder nutze das Mikrofon deiner Tastatur (Diktat). Sprachnachrichten kann ich leider nicht abhören."
        elif text.startswith("/start") or text.startswith("/help"):
            reply = HELP_TEXT
        elif text.startswith("/"):
            continue
        else:
            try:
                reply = handle_message(text)
            except Exception as e:
                print(f"Unerwarteter Fehler bei Nachricht: {e}")
                reply = "Hoppala 😿 – da ist etwas schiefgelaufen. Versuch es bitte noch einmal."
        try:
            tg(CFG["tg_token"], "sendMessage",
               {"chat_id": CFG["tg_chat"], "text": reply})
        except Exception as e:
            print(f"Antwort konnte nicht gesendet werden: {e}")

    if max_id != offset:
        body = {"_id": "telegram_offset", "offset": max_id}
        if rev:
            body["_rev"] = rev
        status, result = couch_request(CFG["couch"], "PUT",
                                       f"/{CFG['state_db']}/telegram_offset", body)
        if status in (201, 202):
            print(f"Offset {max_id} gespeichert.")
        elif status == 409:
            print("Offset-Konflikt (409) – paralleler Lauf? Wird beim nächsten Mal korrigiert.")
        else:
            print(f"WARNUNG: Offset nicht gespeichert ({status}): {result}")

    print("Fertig. ✅")


if __name__ == "__main__":
    CFG = {
        "tg_token": env("TELEGRAM_TOKEN"),
        "tg_chat": str(env("TELEGRAM_CHAT_ID")),
        "deepseek_key": env("DEEPSEEK_API_KEY"),
        "couch": {
            "url": env("OBSIDIAN_COUCH_URL"),
            "user": env("OBSIDIAN_COUCH_USER"),
            "password": env("OBSIDIAN_COUCH_PASS"),
            "db": env("OBSIDIAN_COUCH_DB"),
        },
        "state_db": os.environ.get("BOT_STATE_DB", "bot_state"),
    }
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompt.md"),
              encoding="utf-8") as f:
        CFG["prompt"] = f.read()
    main()
