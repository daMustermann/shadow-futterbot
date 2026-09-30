#!/usr/bin/env python3
"""Shadow-Futterbot 🐱 – Telegram -> DeepSeek -> CouchDB (LiveSync) -> Obsidian.

Laeuft als GitHub-Action alle paar Minuten (oder ueberall sonst mit Python 3.10+).
Alle Geheimnisse kommen aus Umgebungsvariablen, niemals aus Dateien.
Der Telegram-Offset liegt in einer Datei (in Actions per Cache persistiert).

Phase 2: Der Bot pflegt Tabelle UND Einkaufszettel direkt.
- Einzelmeldung („SuperMono Ente mag er nicht") -> Zeile + Einkaufszettel werden aktualisiert.
- Sammelauftrag („alles mit Zucchini auf vielleicht") -> alle passenden Zeilen werden aktualisiert.
- Unklare/faellige Faelle -> ⚪-Eintrag in den 📥 Spracheingang zur manuellen Pruefung.
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
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
DEEPSEEK_API = "https://api.deepseek.com/chat/completions"
EMOJI = {"ja": "🟢", "nein": "🔴", "vielleicht": "🟡", "unbekannt": "⚪"}
MAX_AGE_SECONDS = 24 * 3600  # Nachrichten aelter als das werden still uebersprungen
MAX_EDITS = 15  # Sicherheitsdeckel pro Nachricht
SECTIONS = {
    "SuperMono": ("## 🖤 SuperMono", "## 🤍 Lifestage"),
    "Lifestage Adult": ("## 🤍 Lifestage", "## 🎁"),
}
LINIEN_EMOJI = {"SuperMono": "🖤", "Lifestage Adult": "🤍"}
EINKAUF_ABSCHNITTE = {
    "ja": "**Nachkaufen (🟢):**",
    "nein": "**Nicht kaufen (🔴):**",
    "vielleicht": "**Zum Testen mitbringen",
}


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


def deepseek_parse(api_key, model, prompt, text):
    today = datetime.now(BERLIN).strftime("%d.%m.%Y")
    payload = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": 1500,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"Heute ist der {today}. Nachricht: {text}"},
        ],
    }
    resp = http_json(DEEPSEEK_API, payload,
                     headers={"Authorization": f"Bearer {api_key}"}, timeout=90)
    choice = resp["choices"][0]
    raw = choice["message"]["content"]
    if choice.get("finish_reason") != "stop":
        print(f"DeepSeek finish_reason={choice.get('finish_reason')}, Rohantwort: {raw[:500]}")
    try:
        return extract_json(raw)
    except ValueError:
        print(f"DeepSeek-Rohantwort (Parse-Fehler): {raw[:500]}")
        raise


# ---------------------------------------------------------------- Tabellen-Logik

def norm(s):
    s = (s or "").lower().replace("&", "und").replace("🆕", "")
    return re.sub(r"\s+", " ", s).strip()


def split_row(line):
    """Zerlegt eine Markdown-Tabellenzeile in Zellen (ohne aeussere Pipes)."""
    s = line.strip()
    if not (s.startswith("|") and s.endswith("|")):
        return None
    return [c.strip() for c in s[1:-1].split("|")]


def is_data_row(cells):
    return (cells is not None and len(cells) >= 6
            and not all(set(c) <= set("-: ") for c in cells)
            and cells[0] != "Tüte" and cells[0] != "Zeichen")


def find_rows(lines):
    """Sammelt Tabellenzeilen der beiden Hauptsektionen mit Sektionszuordnung."""
    rows = []  # (zeilenindex, linie, zellen)
    current = None
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("## "):
            current = None
            for linie, (start, _end) in SECTIONS.items():
                if s.startswith(start):
                    current = linie
        if current and s.startswith("|"):
            cells = split_row(s)
            if is_data_row(cells):
                rows.append((i, current, cells))
    return rows


def match_row(rows, linie, sorte):
    """Findet genau eine passende Zeile oder gibt (None, grund) zurueck."""
    want_linie = (linie or "").strip()
    want = norm(sorte)
    if not want:
        return None, "leere Sorte"
    cands = [(i, l, c) for (i, l, c) in rows if norm(c[1]) == want]
    if not cands:
        cands = [(i, l, c) for (i, l, c) in rows
                 if want in norm(c[1]) or norm(c[1]) in want]
    if want_linie in SECTIONS:
        in_linie = [c for c in cands if c[1] == want_linie]
        if len(in_linie) == 1:
            return in_linie[0], ""
        if not in_linie:
            return None, f"'{sorte}' nicht in Linie {want_linie} gefunden"
        return None, f"'{sorte}' ist mehrdeutig ({len(in_linie)} Treffer)"
    if len(cands) == 1:
        return cands[0], ""
    if not cands:
        return None, f"keine Zeile für '{sorte}' gefunden"
    return None, f"'{sorte}' ist mehrdeutig ({len(cands)} Treffer)"


def rebuild_row(cells, status, note, today):
    """Baut eine Zeile neu: Bild/Sorte/Extra bleiben, Status/Anmerkung/Datum neu."""
    anm = note or ""
    if not anm and cells[4] != "noch nicht getestet":
        anm = cells[4]
    if not anm:
        anm = "–"
    return [cells[0], cells[1], cells[2], status, anm, today]


def einkauf_key(linie, sorte):
    return norm(f"{linie} – {sorte}")


def sync_einkaufszettel(lines, linie, sorte, extra, urteil, notiz):
    """Entfernt alte Zeilen dieser Sorte aus allen drei Abschnitten und
    haengt ggf. eine neue im passenden Abschnitt an. Gibt (lines, aktion) zurueck."""
    key = einkauf_key(linie, sorte)
    out, removed = [], False
    for line in lines:
        s = line.strip()
        if s.startswith("- ") and key in norm(s):
            removed = True
            continue
        out.append(line)
    lines = out
    if urteil == "unbekannt":
        return lines, ("entfernt" if removed else "nichts zu tun")
    le = LINIEN_EMOJI.get(linie, "")
    extra_txt = f" mit {extra}" if (extra or "").strip() else ""
    if urteil == "ja":
        neu = f"- {le} {linie} – {sorte}{extra_txt}"
        if linie == "SuperMono":
            neu += " (schwarze Tüte)"
        header = EINKAUF_ABSCHNITTE["ja"]
    elif urteil == "nein":
        neu = f"- {le} {linie} – {sorte}{extra_txt}"
        header = EINKAUF_ABSCHNITTE["nein"]
    else:
        neu = f"- 🟡 {le} {linie} – {sorte}{extra_txt}"
        if notiz:
            neu += f" ({notiz})"
        header = EINKAUF_ABSCHNITTE["vielleicht"]
    idx = next((i for i, l in enumerate(lines) if header in l), None)
    if idx is None:
        return lines, "Einkaufszettel-Abschnitt fehlt"
    j = idx + 1
    while j < len(lines) and lines[j].strip().startswith("- "):
        j += 1
    lines.insert(j, neu + "\n")
    return lines, "aktualisiert"


def apply_edits(content, edits, today):
    """Wendet Edits auf Notiztext an. Gibt (neuer_text, ergebnisse) zurueck.
    ergebnisse: Liste von {edit, ok, detail}."""
    lines = content.splitlines(keepends=True)
    rows = find_rows(lines)
    results = []
    for edit in edits[:MAX_EDITS]:
        linie = str(edit.get("linie", "")).strip()
        sorte = str(edit.get("sorte", "")).strip()
        urteil = str(edit.get("urteil", "unbekannt")).strip().lower()
        notiz = str(edit.get("notiz", "")).strip()
        if urteil not in EMOJI:
            urteil = "unbekannt"
        (match, grund) = match_row(rows, linie, sorte)
        if match is None:
            results.append({"edit": edit, "ok": False, "detail": grund})
            continue
        (ri, echte_linie, cells) = match
        status = EMOJI[urteil] + (" ⭐" if urteil == "ja" else "")
        neu = rebuild_row(cells, status, notiz, today)
        alt_zeile = "| " + " | ".join(cells) + " |"
        neu_zeile = "| " + " | ".join(neu) + " |"
        if alt_zeile == neu_zeile:
            results.append({"edit": edit, "ok": True, "detail": "bereits aktuell",
                            "linie": echte_linie, "sorte": cells[1]})
            continue
        lines[ri] = neu_zeile + "\n"
        extra = cells[2]
        lines, einkauf = sync_einkaufszettel(lines, echte_linie, cells[1], extra,
                                             urteil, notiz)
        # Zeilenindex-Verschiebungen nachfuehren
        rows = find_rows(lines)
        results.append({"edit": edit, "ok": True,
                        "detail": f"Zeile {EMOJI[urteil]}, Einkaufszettel {einkauf}",
                        "linie": echte_linie, "sorte": cells[1]})
    return "".join(lines), results


def fallback_inbox(content, ursprung, hinweis):
    """Haengt einen Sammel-/Problemfall in den 📥 Spracheingang an."""
    now = datetime.now(BERLIN).strftime("%d.%m.%Y %H:%M")
    line = f"\n- [{now} Telegram] ⚪ {hinweis} – Original: „{ursprung[:140]}“"
    if INBOX_MARKER not in content:
        return content.rstrip("\n") + f"\n\n## 📥 Spracheingang\n{line}\n"
    return content.replace(INBOX_MARKER, INBOX_MARKER + "\n" + line, 1)


INBOX_MARKER = "## 📥 Spracheingang"

HELP_TEXT = (
    "Miau! 🐱 Ich bin der Shadow-Futterbot – ich pflege Shadows Futterliste direkt!\n\n"
    "Schreib mir z. B.:\n"
    "• „SuperMono Wildschwein mag er“\n"
    "• „Lifestage Geflügel und Lachs frisst er nicht“\n"
    "• „Alles mit Zucchini auf vielleicht“\n\n"
    "Ich trage es in Tabelle + Einkaufszettel ein. ✅"
)


def handle_message(text):
    """Verarbeitet eine Nachricht. Gibt Antworttext zurueck (None = still)."""
    try:
        parsed = deepseek_parse(CFG["deepseek_key"], CFG["deepseek_model"],
                                CFG["prompt"], text)
    except Exception as e:
        print(f"DeepSeek-Fehler: {e}")
        return "Hoppala 😿 – das habe ich gerade nicht verstanden. Schreib es mir bitte noch einmal anders."

    edits = parsed.get("edits") or []
    rueckfrage = str(parsed.get("rueckfrage", "")).strip()
    if not edits:
        if rueckfrage:
            return rueckfrage
        return "Hm, damit kann ich nichts anfangen 😺 – schreib z. B. „SuperMono Ente mag er“."

    try:
        content = obsidian_cli("read", NOTE_PATH)
    except Exception as e:
        print(f"Lese-Fehler: {e}")
        return "Au weia 😿 – ich komme gerade nicht an die Futterliste. Ich versuche es beim nächsten Durchlauf erneut."

    today = datetime.now(BERLIN).strftime("%d.%m.%Y")
    try:
        neu, results = apply_edits(content, edits, today)
    except Exception as e:
        print(f"Anwendungs-Fehler: {e}")
        return "Au weia 😿 – beim Eintragen ist etwas schiefgelaufen. Es wurde nichts verändert."

    ok = [r for r in results if r["ok"]]
    fail = [r for r in results if not r["ok"]]
    if fail:
        hinweise = "; ".join(
            f"{(f['edit'].get('linie') or '')} {(f['edit'].get('sorte') or '')}".strip()
            + f" ({f['detail']})" for f in fail)
        neu = fallback_inbox(neu, text, f"Sammelauftrag/unklar: {hinweise}")
    try:
        obsidian_write(NOTE_PATH, neu)
    except Exception as e:
        print(f"Schreib-Fehler: {e}")
        return "Au weia 😿 – das Speichern hat nicht geklappt. Es wurde nichts verändert."

    zeilen = []
    for r in ok:
        e = r["edit"]
        u = str(e.get("urteil", "unbekannt")).lower()
        emoji = EMOJI.get(u, "⚪")
        extra = f" – {e['notiz']}" if str(e.get("notiz", "")).strip() else ""
        zeilen.append(f"• {r.get('linie', '')} {r.get('sorte', '')} → {emoji}{extra}")
    antwort = "Erledigt ✅"
    if len(ok) == 1:
        antwort = f"Eingetragen ✅: {zeilen[0][2:]}"
    elif ok:
        antwort = "Erledigt ✅:\n" + "\n".join(zeilen)
    if ok and all(r["detail"] == "bereits aktuell" for r in ok):
        return None  # Duplikat: still bleiben
    if fail:
        antwort += f"\n\n⚪ {len(fail)} Punkt(e) habe ich in den 📥 Spracheingang gelegt – bitte prüfen."
    if ok:
        antwort += "\nTabelle + 🛒 Einkaufszettel sind aktuell."
    return antwort


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
