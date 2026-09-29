#!/usr/bin/env python3
"""Shadow-Futterbot 🐱 – Telegram -> DeepSeek -> CouchDB (LiveSync) -> Obsidian.

Laeuft als GitHub-Action alle paar Minuten (oder ueberall sonst mit Python 3.10+).
Alle Geheimnisse kommen aus Umgebungsvariablen, niemals aus Dateien.
Der Telegram-Offset liegt in einer Datei (in Actions per Cache persistiert).
Doppelte Einträge werden am Inhalt erkannt (gleiche Sorte, gleicher Tag) – der Bot
antwortet dann NICHT erneut, damit kein Spam entsteht.
Phase 1: Schreibt nur in den 📥 Spracheingang der Futterliste (risikoarm).
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
NOTE_PATH = "40_Privat/Haustiere/Shadow-Katzenfutter.md"
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
DEEPSEEK_API = "https://api.deepseek.com/chat/completions"
EMOJI = {"ja": "🟢", "nein": "🔴", "vielleicht": "🟡", "unbekannt": "⚪"}
MAX_AGE_SECONDS = 24 * 3600  # Nachrichten aelter als das werden still uebersprungen


def env(name, required=True, default=None):
    val = os.environ.get(name, default)
    if required and not val:
        print(f"FEHLER: Umgebungsvariable {name} fehlt!", file=sys.stderr)
        sys.exit(1)
    return val


def http_json(url, payload=None, headers=None, timeout=30):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {})
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def tg(token, method, payload):
    return http_json(TELEGRAM_API.format(token=token, method=method), payload)


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


def extract_json(text):
    """Tolerantes JSON-Lesen: notfalls ersten {...}-Block aus Begleittext fischen."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start < 0:
        raise ValueError("kein JSON gefunden")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(text[start:i + 1])
    raise ValueError("kein vollständiges JSON gefunden")


def deepseek_parse(api_key, model, prompt, text):
    today = datetime.now(BERLIN).strftime("%d.%m.%Y")
    payload = {
        "model": model,
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
    raw = resp["choices"][0]["message"]["content"]
    try:
        return extract_json(raw)
    except ValueError:
        print(f"DeepSeek-Rohantwort (Parse-Fehler): {raw[:300]}")
        raise


HELP_TEXT = (
    "Miau! 🐱 Ich bin der Shadow-Futterbot.\n\n"
    "Schreib mir einfach, was Shadow gefressen hat – z. B.:\n"
    "• „SuperMono Wildschwein mag er“\n"
    "• „Lifestage Geflügel und Lachs frisst er nicht“\n"
    "• „Geflügel mit Pastinaken nur halb gefressen“\n\n"
    "Ich trage es in die Futterliste ein und melde mich mit Bestätigung. ✅"
)


def already_entered_today(content, kern, today):
    for line in content.splitlines():
        s = line.strip()
        if s.startswith("- [") and today in s and kern.lower() in s.lower():
            return True
    return False


def handle_message(text):
    """Parst eine Nachricht und traegt sie ein. Gibt Antworttext zurueck (None = still)."""
    try:
        parsed = deepseek_parse(CFG["deepseek_key"], CFG["deepseek_model"], CFG["prompt"], text)
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
        return "Hm, welche Sorte meinst du genau? 😺 Schreib z. B. „SuperMono Ente mag er“."

    now = datetime.now(BERLIN)
    today = now.strftime("%d.%m.%Y")
    kern = f"{linie} {sorte}".strip()

    try:
        content = obsidian_cli("read", NOTE_PATH)
    except Exception as e:
        print(f"Lese-Fehler: {e}")
        return "Au weia 😿 – ich komme gerade nicht an die Futterliste. Ich versuche es beim nächsten Durchlauf erneut."

    if already_entered_today(content, kern, today):
        print(f"Duplikat erkannt ({kern}) – kein erneuter Eintrag, keine Antwort.")
        return None

    line = f"- [{now.strftime('%d.%m.%Y %H:%M')} Telegram] {kern} → {EMOJI[urteil]}"
    if notiz:
        line += f" – {notiz}"

    try:
        obsidian_cli("append", NOTE_PATH, "\n" + line)
    except Exception as e:
        print(f"Schreib-Fehler: {e}")
        return "Au weia 😿 – das Eintragen hat nicht geklappt. Ich versuche es beim nächsten Durchlauf erneut."

    if urteil == "ja":
        return f"Eingetragen ✅: {kern} {EMOJI[urteil]} – Shadow mag es! Wird beim nächsten Aufräumen in die Liste übernommen."
    if urteil == "nein":
        return f"Eingetragen ✅: {kern} {EMOJI[urteil]} – landet auf der Nicht-kaufen-Liste."
    if urteil == "vielleicht":
        return f"Eingetragen ✅: {kern} {EMOJI[urteil]} – zum Nochmal-Testen vorgemerkt."
    return f"Eingetragen ✅: {kern} {EMOJI[urteil]} – bitte beim Aufräumen prüfen."


def load_offset(path):
    try:
        with open(path, encoding="utf-8") as f:
            return int(json.load(f).get("offset", 0))
    except (OSError, ValueError, AttributeError):
        return 0


def save_offset(path, offset):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"offset": offset}, f)


def main():
    offset = load_offset(CFG["offset_file"])
    params = {"timeout": 0, "limit": 50}
    if offset:
        params["offset"] = offset + 1
    try:
        updates = tg(CFG["tg_token"], "getUpdates", params).get("result", [])
    except Exception as e:
        print(f"FEHLER bei getUpdates: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"{len(updates)} Update(s) ab Offset {offset}.")
    now_ts = int(time.time())
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
        if now_ts - int(msg.get("date", 0)) > MAX_AGE_SECONDS:
            print(f"Update {uid} aelter als 24h – still uebersprungen.")
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
        if reply is None:
            continue
        try:
            tg(CFG["tg_token"], "sendMessage",
               {"chat_id": CFG["tg_chat"], "text": reply})
        except Exception as e:
            print(f"Antwort konnte nicht gesendet werden: {e}")

    if max_id != offset:
        save_offset(CFG["offset_file"], max_id)
        print(f"Offset {max_id} gespeichert.")
    print("Fertig. ✅")


if __name__ == "__main__":
    CFG = {
        "tg_token": env("TELEGRAM_TOKEN"),
        "tg_chat": str(env("TELEGRAM_CHAT_ID")),
        "deepseek_key": env("DEEPSEEK_API_KEY"),
        "deepseek_model": os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"),
        "offset_file": os.environ.get("OFFSET_FILE", "offset.json"),
        "couch": {
            "url": env("OBSIDIAN_COUCH_URL"),
            "user": env("OBSIDIAN_COUCH_USER"),
            "password": env("OBSIDIAN_COUCH_PASS"),
            "db": env("OBSIDIAN_COUCH_DB"),
        },
    }
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompt.md"),
              encoding="utf-8") as f:
        CFG["prompt"] = f.read()
    main()
