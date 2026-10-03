import difflib
import io
import os
import re
import sqlite3
from datetime import date

import requests
from flask import Flask, jsonify, render_template, request, send_file, send_from_directory
from PIL import Image, ImageDraw

app = Flask(__name__)
BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "prices.db")
OCR_KEY = os.environ.get("OCR_API_KEY", "")

VAGUE = {"продукты", "продукт", "товар", "товары", "бакалея", "разное", "прочее",
         "овощи", "фрукты", "напитки", "хозтовары", "item", "food", "goods"}
SKIP_WORDS = ("итог", "сдача", "налич", "ндс", "карта", "картой", "сумма", "скидк",
              "касс", "чек", "оплата", "всего", "электрон", "получен")
STOP_WORDS = ("итог", "электрон", "получено", "картой", "наличными")
ITEM_START = re.compile(r"^\s*(\d{1,3})\s*([.)\-])\s*(?=[^\W\d_])")
QTY_X = re.compile(r"(\d*[.,]?\d{3})\s*[xXхХ×]\s*(\d+[.,]\d{1,2})(?:\s*=\s*(\d+[.,]\d{1,2}))?")
SIMPLE_RE = re.compile(r"^(.*?)[\s.\-–—:=*]*(\d{1,5}[.,]\d{2})\s*(?:[рР₽]|руб\.?)?\s*$")
UNIT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(кг|гр|г|мл|л)(?![а-яё])", re.I)
DATE_RE = re.compile(r"(?<!\d)(\d{2})[.\-/](\d{2})[.\-/](\d{4}|\d{2})(?!\d)")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store TEXT NOT NULL, name TEXT NOT NULL, price REAL NOT NULL,
            unit TEXT DEFAULT 'шт', bought TEXT,
            created TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS aliases(raw TEXT PRIMARY KEY, fixed TEXT NOT NULL);
        """)
        cols = [r[1] for r in c.execute("PRAGMA table_info(items)")]
        if "unit" not in cols:
            c.execute("ALTER TABLE items ADD COLUMN unit TEXT DEFAULT 'шт'")
        if "bought" not in cols:
            c.execute("ALTER TABLE items ADD COLUMN bought TEXT")


init_db()


def to_float(s):
    return float(s.replace(",", "."))


def is_suspicious(name):
    n = name.strip().lower()
    words = re.findall(r"[a-zа-яёіїєґ0-9]+", n)
    if n in VAGUE or not any(len(w) >= 3 for w in words):
        return True
    if re.search(r"[іїєґ]", n):
        return True
    return any((re.search(r"[a-z]", w) and re.search(r"[а-яё]", w))
               or re.search(r"[а-яё][0-9]$", w) for w in words)


def clean_name(s):
    s = s.replace("ТОВАР", " ")
    s = re.sub(r"[-=_]{3,}|\.{2,}|\(\s*\d+\s*\)", " ", s)
    starts = list(re.finditer(r"(?<![\d.,])(\d{1,3})\s*[.)]\s*(?=[^\W\d_])", s))
    if starts:
        s = s[starts[-1].end():]
    s = re.sub(r"^\s*\d+[.,]\d{2}\s+", "", s)
    s = re.sub(r"\s+", " ", s).strip(" .,:;-–—=_*\\/|()")
    for _ in range(2):  # оптовая фасовка вида "\25шт"
        s = re.sub(r"[\s\\/|:]+\d{1,3}\s*шт\.?$", "", s).strip(" .,:;-–—=_*\\/|()")
    return s


def known_names():
    with db() as c:
        a = [r[0] for r in c.execute("SELECT DISTINCT fixed FROM aliases")]
        b = [r[0] for r in c.execute("SELECT DISTINCT name FROM items")]
    return sorted(set(a + b))


def find_total(text):
    m = re.search(r"итог[^\d\n]{0,15}(\d{2,}[.,]\d{2})", text, re.I)
    return to_float(m.group(1)) if m else None


def find_date(text):
    for m in DATE_RE.finditer(text):
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        if 1 <= mo <= 12 and 1 <= d <= 31 and 2000 <= y <= date.today().year + 1:
            return "%04d-%02d-%02d" % (y, mo, d)
    return None


def find_store(text):
    lines = [l.strip() for l in text.replace("\r", "").split("\n") if l.strip()][:8]
    for l in lines:
        m = re.search(r'["«“„]([^"«»“”„]{3,40})["»”“]', l)
        if m:
            name = m.group(1).strip()
            break
    else:
        return None
    with db() as c:
        stores = [r[0] for r in c.execute("SELECT DISTINCT store FROM items")]
    near = difflib.get_close_matches(name, stores, 1, 0.75)
    return near[0] if near else name


def per_unit(name, price, unit):
    if unit == "кг":
        return price, "кг"
    m = UNIT_RE.search(name)
    if not m:
        return None, None
    n, u = to_float(m.group(1)), m.group(2).lower()
    if n <= 0:
        return None, None
    if u in ("гр", "г"):
        return round(price / (n / 1000), 2), "кг"
    if u == "кг":
        return round(price / n, 2), "кг"
    if u == "мл":
        return round(price / (n / 1000), 2), "л"
    return round(price / n, 2), "л"


def parse_numbered(text):
    lines, started = [], False
    for line in text.replace("\r", "").split("\n"):
        line = line.strip()
        if not line or re.fullmatch(r"[\s=\-_.]+", line):
            continue
        if started and any(w in line.lower() for w in STOP_WORDS):
            break
        if not started and not ITEM_START.match(line):
            continue
        started = True
        lines.append(line)
    body, out, pos = " ".join(lines), [], 0
    for m in QTY_X.finditer(body):
        head = body[pos:m.start()]
        pos = m.end()
        total = m.group(3)
        if not total:
            t = re.match(r"\s*[=\-–—:~]?\s*(\d+[.,]\d{2})(?!\d)", body[pos:])
            if t:
                total, pos = t.group(1), pos + t.end()
        price = to_float(m.group(2))
        qty = round(to_float(total) / price, 3) if total and price else (to_float(m.group(1)) or 1.0)
        name = clean_name(head)
        if name and price > 0:
            out.append((name, price, bool(re.search(r"ТОВАР(?=[а-яё])", head)), qty))
    return out


def parse_simple(text):
    out = []
    for line in text.replace("\r", "").split("\n"):
        line = line.strip()
        if not line or any(w in line.lower() for w in SKIP_WORDS):
            continue
        m = SIMPLE_RE.match(line)
        if m and m.group(1).strip(" .-–—:*"):
            out.append((m.group(1).strip(" .-–—:*"), to_float(m.group(2))))
    return out


def parse_receipt(text):
    with db() as c:
        aliases = {r["raw"]: r["fixed"] for r in c.execute("SELECT * FROM aliases")}
    low = {k.lower(): k for k in known_names()}
    rows = parse_numbered(text) or [(n, p, False, 1.0) for n, p in parse_simple(text)]
    out = []
    for raw, price, bad, qty in rows:
        key, name, sure = raw.lower(), raw, False
        if key in aliases:
            name, sure = aliases[key], True
        else:
            near = difflib.get_close_matches(key, list(low), 1, 0.82)
            if near:
                name, sure = low[near[0]], True
        out.append({"raw": raw, "name": name, "price": price, "qty": qty, "known": sure,
                    "unit": "шт" if abs(qty - round(qty)) < 0.01 else "кг",
                    "suspicious": (not sure) and (bad or is_suspicious(raw))})
    return out


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/manifest.json")
def manifest():
    return send_from_directory(os.path.join(BASE, "static"), "manifest.json",
                               mimetype="application/manifest+json")


@app.get("/service-worker.js")
def sw():
    resp = send_from_directory(os.path.join(BASE, "static"), "service-worker.js",
                               mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/icon-<int:size>.png")
def icon(size):
    size = max(48, min(size, 1024))
    im = Image.new("RGB", (size, size), "#2e7d32")
    d = ImageDraw.Draw(im)
    m = size // 4
    d.ellipse([m, m, size - m, size - m], fill="white")
    k = size // 2
    d.rectangle([k - size // 14, k - size // 6, k + size // 14, k + size // 6], fill="#2e7d32")
    buf = io.BytesIO()
    im.save(buf, "PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@app.post("/api/ocr")
def ocr():
    if not OCR_KEY:
        return jsonify(error="На сервере не задан OCR_API_KEY"), 500
    f = request.files.get("image")
    if not f:
        return jsonify(error="Нет файла"), 400
    try:
        r = requests.post(
            "https://api.ocr.space/parse/image",
            files={"file": (f.filename or "receipt.jpg", f.stream, f.mimetype or "image/jpeg")},
            data={"apikey": OCR_KEY, "language": "rus", "isTable": "true",
                  "OCREngine": "1", "detectOrientation": "true", "scale": "true"},
            timeout=60,
        )
        j = r.json()
    except Exception as e:
        return jsonify(error=f"OCR недоступен: {e}"), 502
    if j.get("IsErroredOnProcessing"):
        msg = j.get("ErrorMessage")
        msg = msg[0] if isinstance(msg, list) and msg else (msg or "Ошибка OCR")
        return jsonify(error=str(msg)), 502
    text = "\n".join(p.get("ParsedText", "") for p in j.get("ParsedResults", []))
    return jsonify(lines=parse_receipt(text), total=find_total(text),
                   store=find_store(text), date=find_date(text))


@app.get("/api/names")
def api_names():
    with db() as c:
        stores = [r[0] for r in c.execute("SELECT DISTINCT store FROM items ORDER BY store")]
    return jsonify(names=known_names(), stores=stores)


@app.post("/api/items")
def save_items():
    d = request.get_json(force=True)
    store = (d.get("store") or "").strip() or "Без названия"
    bought = d.get("date") or ""
    if not ISO_RE.match(bought):
        bought = date.today().isoformat()
    n = 0
    with db() as c:
        for it in d.get("items", []):
            name = (it.get("name") or "").strip()
            raw = (it.get("raw") or "").strip().lower()
            try:
                price = float(it.get("price"))
            except (TypeError, ValueError):
                continue
            if not name:
                continue
            unit = "кг" if it.get("unit") == "кг" else "шт"
            c.execute("INSERT INTO items(store,name,price,unit,bought) VALUES(?,?,?,?,?)",
                      (store, name, price, unit, bought))
            if raw and raw != name.lower():
                c.execute("INSERT OR REPLACE INTO aliases(raw,fixed) VALUES(?,?)", (raw, name))
            n += 1
    return jsonify(saved=n)


@app.get("/api/items")
def list_items():
    q = (request.args.get("q") or "").strip().lower()
    with db() as c:
        rows = c.execute(
            "SELECT id,store,name,price,unit,COALESCE(bought,substr(created,1,10)) AS bought "
            "FROM items ORDER BY bought DESC, id DESC LIMIT 1500").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if q in d["name"].lower():
            d["ppu"], d["pu"] = per_unit(d["name"], d["price"], d["unit"])
            out.append(d)
    return jsonify(out)


@app.delete("/api/items/<int:item_id>")
def delete_item(item_id):
    with db() as c:
        c.execute("DELETE FROM items WHERE id=?", (item_id,))
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
