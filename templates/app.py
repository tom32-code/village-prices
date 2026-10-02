import io
import os
import re
import sqlite3

import requests
from flask import Flask, jsonify, render_template, request, send_file, send_from_directory
from PIL import Image, ImageDraw

app = Flask(__name__)
BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "prices.db")
OCR_KEY = os.environ.get("OCR_API_KEY", "")

# Строка чека: "Название ..... 123.45"
PRICE_RE = re.compile(r"^(.*?)[\s.\-–—:=*]*(\d{1,5}[.,]\d{2})\s*(?:[рР₽]|руб\.?)?\s*$")
SKIP_WORDS = ("итог", "сдача", "налич", "ндс", "карта", "сумма", "скидк", "касс", "чек", "оплата", "всего")
VAGUE = {"продукты", "продукт", "товар", "товары", "бакалея", "разное", "прочее",
         "овощи", "фрукты", "напитки", "хозтовары", "item", "food", "goods"}


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store TEXT NOT NULL,
            name TEXT NOT NULL,
            price REAL NOT NULL,
            created TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS aliases(
            raw TEXT PRIMARY KEY,
            fixed TEXT NOT NULL);
        """)


init_db()


def is_suspicious(name):
    n = name.strip().lower()
    letters = re.findall(r"[a-zа-яё]", n)
    return len(letters) < 3 or n in VAGUE or len(letters) / max(len(n), 1) < 0.5


def parse_receipt(text):
    out = []
    with db() as c:
        aliases = {r["raw"]: r["fixed"] for r in c.execute("SELECT * FROM aliases")}
    for line in text.replace("\r", "").split("\n"):
        line = line.strip()
        if not line or any(w in line.lower() for w in SKIP_WORDS):
            continue
        m = PRICE_RE.match(line)
        if not m:
            continue
        raw = m.group(1).strip(" .-–—:*")
        price = float(m.group(2).replace(",", "."))
        if not raw:
            continue
        name = aliases.get(raw.lower(), raw)
        out.append({"raw": raw, "name": name, "price": price,
                    "suspicious": raw.lower() not in aliases and is_suspicious(raw)})
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
                  "OCREngine": "1", "scale": "true"},
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
    return jsonify(lines=parse_receipt(text), text=text)


@app.post("/api/items")
def save_items():
    data = request.get_json(force=True)
    store = (data.get("store") or "").strip() or "Без названия"
    n = 0
    with db() as c:
        for it in data.get("items", []):
            name = (it.get("name") or "").strip()
            raw = (it.get("raw") or "").strip().lower()
            try:
                price = float(it.get("price"))
            except (TypeError, ValueError):
                continue
            if not name:
                continue
            c.execute("INSERT INTO items(store,name,price) VALUES(?,?,?)", (store, name, price))
            if raw and raw != name.lower():
                c.execute("INSERT OR REPLACE INTO aliases(raw,fixed) VALUES(?,?)", (raw, name))
            n += 1
    return jsonify(saved=n)


@app.get("/api/items")
def list_items():
    q = (request.args.get("q") or "").strip().lower()
    with db() as c:
        rows = c.execute("SELECT id,store,name,price,created FROM items ORDER BY id DESC LIMIT 1000").fetchall()
    rows = [dict(r) for r in rows if q in r["name"].lower()]
    return jsonify(rows)


@app.delete("/api/items/<int:item_id>")
def delete_item(item_id):
    with db() as c:
        c.execute("DELETE FROM items WHERE id=?", (item_id,))
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
