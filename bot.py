#!/usr/bin/env python3
"""Shadow-Futterbot 🐱 – Telegram -> DeepSeek -> CouchDB (LiveSync) -> Obsidian.

Laeuft als GitHub-Action alle paar Minuten (oder ueberall sonst mit Python 3.10+).
Alle Geheimnisse kommen aus Umgebungsvariablen, niemals aus Dateien.
Der Telegram-Offset liegt in einer Datei (in Actions per Cache persistiert).

Modus B („KI editiert direkt"):
- DeepSeek bekommt die KOMPLETTE Notiz + deine Nachricht und liefert die fertige,
  aktualisierte Notiz zurueck (als JSON: {"note": "...", "antwort": "..."}).
- Der Bot PRUEFT die Rueckgabe (Struktur-Heil-Test, siehe validate_note) und schreibt
  sie nur bei bestandener Pruefung. Davor sichert er die alte Version nach
  backup-note.md (wird als Action-Artifact aufbewahrt).
- Faellt Pruefung oder Schreiben durch: ⚪-Eintrag in den 📥 Spracheingang + Hinweis.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
NOTE_PATH = "40_Privat/Haustiere/Shadow-Katzenfutter.md"
BACKUP_FILE = "backup-note.md"
INBOX_MARKER = "## 📥 Spracheingang"
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
DEEPSEEK_API = "https://api.deepseek.com/chat/completions"
MAX_AGE_SECONDS = 24 * 3600  # Nachrichten aelter als das werden still uebersprungen

REQUIRED_HEADERS = [
    "## 🔎 Legende",
    "## 🖤 SuperMono 125 g",
    "## 🤍 Lifestage Adult 125 g",
    "## 🎁 Mix-Boxen",
    "## 🍬 Extra Food",
    "## 🛒 Einkaufszettel für den Laden",
    "## 📥 Spracheingang",
    "## 📝 Notizen / Tipps",
]
FRONTMATTER = "---\ntags: [privat/haustier]\ntyp: liste\n---"


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
    """Notizen im korrekten LiveSync-Format lesen/schreiben (via obsidian-livesync-mcp)."""
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


def obsidian_write(path, content):
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                     encoding="utf-8") as f:
        f.write(content)
        tmp = f.name
    try:
        return obsidian_cli("write", path, "-f", tmp)
    finally:
        os.unlink(tmp)


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


def deepseek_edit(api_key, model, prompt, note, text):
    today = datetime.now(BERLIN).strftime("%d.%m.%Y")
    payload = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": 8000,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user",
             "content": f"Heute ist der {today}.\n\n--- AKTUELLE NOTIZ ---\n{note}\n"
                        f"--- ENDE NOTIZ ---\n\nNachricht: {text}"},
        ],
    }
    resp = http_json(DEEPSEEK_API, payload,
                     headers={"Authorization": f"Bearer {api_key}"}, timeout=120)
    choice = resp["choices"][0]
    raw = choice["message"]["content"]
    if choice.get("finish_reason") != "stop":
        print(f"DeepSeek finish_reason={choice.get('finish_reason')}")
    try:
        return extract_json(strip_fences(raw))
    except ValueError:
        print(f"DeepSeek-Rohantwort (Parse-Fehler): {raw[:500]}")
        raise


def strip_fences(text):
    """Entfernt ```markdown-Codezaeune, falls das Modell sie doch gesetzt hat."""
    m = re.search(r"```(?:markdown|md)?\s*\n(.*?)```", text, re.DOTALL)
    if m and "{" in m.group(1):
        return m.group(1)
    return text


# ---------------------------------------------------------------- Pruefung

def norm(s):
    s = (s or "").lower().replace("&", "und").replace("🆕", "")
    return re.sub(r"\s+", " ", s).strip()


def split_row(line):
    s = line.strip()
    if not (s.startswith("|") and s.endswith("|")):
        return None
    return [c.strip() for c in s[1:-1].split("|")]


def is_data_row(cells):
    return (cells is not None and len(cells) >= 6
            and not all(set(c) <= set("-: ") for c in cells)
            and cells[0] not in ("Tüte", "Zeichen", "Box", "Sorte", "Linie"))


def section_sort_names(text, start_marker, end_marker):
    """Sortennamen (Spalte 2) aller Datenzeilen zwischen zwei Ueberschriften."""
    inside, names = False, []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("## "):
            inside = s.startswith(start_marker)
            continue
        if inside and s.startswith("|"):
            cells = split_row(s)
            if is_data_row(cells):
                names.append(norm(cells[1]))
    return names


def validate_note(old, new):
    """Struktur-Heil-Test. Gibt (ok, grund) zurueck."""
    if not new.startswith(FRONTMATTER):
        return False, "Frontmatter fehlt/beschaedigt"
    for h in REQUIRED_HEADERS:
        if h not in new:
            return False, f"Abschnitt fehlt: {h}"
    for start, end in [("## 🖤 SuperMono", "## 🤍 Lifestage"),
                       ("## 🤍 Lifestage", "## 🎁")]:
        a, b = section_sort_names(old, start, end), section_sort_names(new, start, end)
        if sorted(a) != sorted(b):
            return False, f"Sorten veraendert in {start} (Zeile geloescht/hinzugefuegt?)"
    old_imgs = set(re.findall(r"Assets/Shadow-Futter/[\w\-.]+\.png", old))
    new_imgs = set(re.findall(r"Assets/Shadow-Futter/[\w\-.]+\.png", new))
    if old_imgs - new_imgs:
        return False, f"Bilder verloren: {sorted(old_imgs - new_imgs)[:3]}"
    if not (0.7 * len(old) <= len(new) <= 1.3 * len(old)):
        return False, "Laenge unplausibel (mehr als ±30 %)"
    if new.count("|") < old.count("|") * 0.9:
        return False, "Tabellenstruktur beschaedigt"
    return True, "ok"


def fallback_inbox(content, ursprung, hinweis):
    now = datetime.now(BERLIN).strftime("%d.%m.%Y %H:%M")
    line = f"\n- [{now} Telegram] ⚪ {hinweis} – Original: „{ursprung[:140]}“"
    if INBOX_MARKER not in content:
        return content.rstrip("\n") + f"\n\n{INBOX_MARKER}\n{line}\n"
    return content.replace(INBOX_MARKER, INBOX_MARKER + "\n" + line, 1)


HELP_TEXT = (
    "Miau! 🐱 Ich bin der Shadow-Futterbot – ich pflege Shadows Futterliste direkt!\n\n"
    "Schreib mir z. B.:\n"
    "• „SuperMono Wildschwein mag er“\n"
    "• „Lifestage Geflügel und Lachs frisst er nicht“\n"
    "• „Alles mit Zucchini auf vielleicht“\n"
    "• Sogar Extrawünsche wie „nimm die Pastinaken-Notiz raus“\n\n"
    "Ich trage es in Tabelle + Einkaufszettel ein. ✅"
)


def handle_message(text):
    """Verarbeitet eine Nachricht. Gibt Antworttext zurueck (None = still)."""
    try:
        content = obsidian_cli("read", NOTE_PATH)
    except Exception as e:
        print(f"Lese-Fehler: {e}")
        return "Au weia 😿 – ich komme gerade nicht an die Futterliste. Ich versuche es beim nächsten Durchlauf erneut."

    try:
        parsed = deepseek_edit(CFG["deepseek_key"], CFG["deepseek_model"],
                               CFG["prompt"], content, text)
        neu = parsed.get("note", "")
        antwort = str(parsed.get("antwort", "")).strip()
    except Exception as e:
        print(f"DeepSeek-Fehler: {e}")
        try:
            obsidian_write(NOTE_PATH, fallback_inbox(
                content, text, "KI-Antwort unverstaendlich"))
        except Exception as e2:
            print(f"Inbox-Fallback fehlgeschlagen: {e2}")
        return "Hoppala 😿 – das habe ich gerade nicht verstanden. Es liegt im 📥 Spracheingang, ich schaue es mir beim nächsten Mal genauer an."

    if not neu or not neu.strip():
        return "Hm, da kam nichts Sinnvolles zurück 😺 – formuliere es bitte anders, z. B. „SuperMono Ente mag er“."

    if neu.strip() == content.strip():
        print("KI meldet keine Aenderung – still.")
        return None  # Duplikat: still bleiben

    ok, grund = validate_note(content, neu)
    if not ok:
        print(f"Validierung FEHLGESCHLAGEN: {grund}")
        try:
            obsidian_write(NOTE_PATH, fallback_inbox(
                content, text, f"Pruefung durchgefallen ({grund})"))
        except Exception as e2:
            print(f"Inbox-Fallback fehlgeschlagen: {e2}")
        return ("Das war mir zu heikel 😿 – meine Prüfung hat einen Fehler gefunden, "
                "deshalb habe ich nichts verändert. Der Wunsch liegt im 📥 Spracheingang.")

    try:
        with open(CFG["backup_file"], "w", encoding="utf-8") as f:
            f.write(content)
        obsidian_write(NOTE_PATH, neu if neu.endswith("\n") else neu + "\n")
    except Exception as e:
        print(f"Schreib-Fehler: {e}")
        return "Au weia 😿 – das Speichern hat nicht geklappt. Es wurde nichts verändert."

    return antwort or "Erledigt ✅ – Tabelle + 🛒 Einkaufszettel sind aktuell."


# ---------------------------------------------------------------- Ablauf

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
        print(f"Eingang: {text[:120]}")
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
        "backup_file": os.environ.get("BACKUP_FILE", "backup-note.md"),
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
