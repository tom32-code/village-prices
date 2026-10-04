import csv
import io
import os
import re
import sqlite3
import ssl
from datetime import date
from urllib.parse import unquote, urlparse

import requests
from flask import Flask, Response, jsonify, request, send_file, send_from_directory
from PIL import Image, ImageDraw

import receipt

BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=None)

# Если задана переменная DATABASE_URL (постоянная база PostgreSQL), данные хранятся там.
# Иначе используется файл prices.db, который на бесплатном Render стирается при обновлении.
URL = os.environ.get("DATABASE_URL", "").strip()
PG = URL.startswith(("postgres://", "postgresql://"))
if PG:
    import pg8000.dbapi as pg
    U = urlparse(URL)
DB_FILE = os.path.join(BASE, "prices.db")


def connect():
    if PG:
        return pg.connect(user=unquote(U.username or ""), password=unquote(U.password or ""),
                          host=U.hostname, port=U.port or 5432,
                          database=(U.path or "/").lstrip("/"), ssl_context=ssl.create_default_context())
    return sqlite3.connect(DB_FILE)


def run(sql, args=(), fetch=False):
    c = connect()
    try:
        cur = c.cursor()
        cur.execute(sql.replace("?", "%s") if PG else sql, args)
        out = None
        if fetch:
            cols = [d[0] for d in cur.description]
            out = [dict(zip(cols, r)) for r in cur.fetchall()]
        c.commit()
        return out
    finally:
        c.close()


def run_many(sql, seq):
    seq = list(seq)
    if not seq:
        return
    c = connect()
    try:
        cur = c.cursor()
        cur.executemany(sql.replace("?", "%s") if PG else sql, seq)
        c.commit()
    finally:
        c.close()


def init_db():
    pk = "SERIAL PRIMARY KEY" if PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    real = "DOUBLE PRECISION" if PG else "REAL"
    run("CREATE TABLE IF NOT EXISTS prices(id %s, store TEXT NOT NULL, product TEXT NOT NULL, "
        "price %s NOT NULL, qty %s NOT NULL DEFAULT 1, bought TEXT NOT NULL)" % (pk, real, real))
    run("CREATE TABLE IF NOT EXISTS aliases(raw TEXT PRIMARY KEY, fixed TEXT NOT NULL)")


try:
    init_db()
except Exception as e:  # не роняем сервер: ошибку видно в /api/status
    print("DB INIT ERROR:", e)

ALIAS_SQL = "INSERT INTO aliases(raw,fixed) VALUES(?,?) ON CONFLICT(raw) DO UPDATE SET fixed=excluded.fixed"


def num(s):
    return float(str(s).replace(" ", "").replace("\xa0", "").replace(",", "."))


def iso(s):
    s = (s or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    m = re.fullmatch(r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})", s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y += 2000 if y < 100 else 0
        return "%04d-%02d-%02d" % (y, mo, d)
    return date.today().isoformat()


# ---------- страницы и файлы (все файлы лежат в корне репозитория) ----------
FILES = {"style.css", "app.js", "scan.js", "manifest.json", "service-worker.js"}


@app.get("/")
def index():
    with open(os.path.join(BASE, "index.html"), encoding="utf-8") as fh:
        return Response(fh.read(), mimetype="text/html", headers={"Cache-Control": "no-cache"})


@app.get("/static/<name>")
def static_file(name):
    if name not in FILES:
        return "", 404
    r = send_from_directory(BASE, name)
    r.headers["Cache-Control"] = "no-cache"
    return r


@app.get("/manifest.json")
def manifest():
    return send_from_directory(BASE, "manifest.json", mimetype="application/manifest+json")


@app.get("/service-worker.js")
def sw():
    r = send_from_directory(BASE, "service-worker.js", mimetype="application/javascript")
    r.headers["Service-Worker-Allowed"] = "/"
    r.headers["Cache-Control"] = "no-cache"
    return r


@app.get("/icon-<int:size>.png")
def icon(size):
    size = max(48, min(size, 1024))
    im = Image.new("RGB", (size, size), "#2e7d32")
    d = ImageDraw.Draw(im)
    a = size // 5
    d.rounded_rectangle([a, a // 1.3, size - a, size - a // 1.3], radius=size // 12, fill="white")
    for i in range(4):
        y = int(size * (0.3 + i * 0.13))
        d.rectangle([a + size // 9, y, size - a - size // 9, y + max(2, size // 40)], fill="#2e7d32")
    buf = io.BytesIO()
    im.save(buf, "PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@app.get("/api/status")
def status():
    try:
        n = run("SELECT COUNT(*) AS n FROM prices", fetch=True)[0]["n"]
        return jsonify(ok=True, persistent=PG, count=n)
    except Exception as e:
        return jsonify(ok=False, persistent=PG, error=str(e)), 500


# ---------- записи ----------
@app.get("/api/prices")
def list_prices():
    return jsonify(run("SELECT id,store,product,price,qty,bought FROM prices ORDER BY bought DESC, id DESC",
                       fetch=True))


def read_entry(d):
    store = (d.get("store") or "").strip()
    product = (d.get("product") or "").strip()
    try:
        price, qty = num(d.get("price")), num(d.get("qty") or 1)
    except ValueError:
        return None
    if not store or not product or price <= 0 or qty <= 0:
        return None
    return store, product, price, qty, iso(d.get("date"))


@app.post("/api/prices")
def add_price():
    e = read_entry(request.get_json(force=True))
    if not e:
        return jsonify(error="Заполните магазин, товар и цену"), 400
    run("INSERT INTO prices(store,product,price,qty,bought) VALUES(?,?,?,?,?)", e)
    return jsonify(ok=True)


@app.put("/api/prices/<int:pid>")
def edit_price(pid):
    d = request.get_json(force=True)
    e = read_entry(d)
    if not e:
        return jsonify(error="Заполните магазин, товар и цену"), 400
    old = run("SELECT product FROM prices WHERE id=?", (pid,), fetch=True)
    run("UPDATE prices SET store=?, product=?, price=?, qty=?, bought=? WHERE id=?", e + (pid,))
    if d.get("rename_all") and old and old[0]["product"] != e[1]:
        run("UPDATE prices SET product=? WHERE product=?", (e[1], old[0]["product"]))
    return jsonify(ok=True)


@app.delete("/api/prices/<int:pid>")
def del_price(pid):
    run("DELETE FROM prices WHERE id=?", (pid,))
    return jsonify(ok=True)


@app.post("/api/prices/bulk")
def bulk():
    d = request.get_json(force=True)
    store = (d.get("store") or "").strip()
    if not store:
        return jsonify(error="Укажите магазин"), 400
    bought, rows, aliases = iso(d.get("date")), [], []
    for it in d.get("items", []):
        product = (it.get("product") or "").strip()
        try:
            price = num(it.get("price"))
        except ValueError:
            continue
        if product and price > 0:
            rows.append((store, product, price, 1, bought))
            raw = (it.get("raw") or "").strip().lower()
            if raw and raw != product.lower():
                aliases.append((raw, product))
    run_many("INSERT INTO prices(store,product,price,qty,bought) VALUES(?,?,?,?,?)", rows)
    run_many(ALIAS_SQL, aliases)
    return jsonify(saved=len(rows))


# ---------- распознавание чека ----------
@app.post("/api/ocr")
def ocr():
    key = os.environ.get("OCR_API_KEY", "")
    if not key:
        return jsonify(error="На сервере не задан OCR_API_KEY"), 500
    f = request.files.get("image")
    if not f:
        return jsonify(error="Нет файла"), 400
    try:
        r = requests.post(
            "https://api.ocr.space/parse/image",
            files={"file": (f.filename or "receipt.jpg", f.stream, f.mimetype or "image/jpeg")},
            data={"apikey": key, "language": "rus", "isTable": "true", "OCREngine": "1",
                  "detectOrientation": "true", "scale": "true"},
            timeout=60)
        j = r.json()
    except Exception as e:
        return jsonify(error="OCR недоступен: %s" % e), 502
    if j.get("IsErroredOnProcessing"):
        msg = j.get("ErrorMessage")
        msg = msg[0] if isinstance(msg, list) and msg else (msg or "Ошибка OCR")
        return jsonify(error=str(msg)), 502
    text = "\n".join(p.get("ParsedText", "") for p in j.get("ParsedResults", []))
    known = [r["product"] for r in run("SELECT DISTINCT product FROM prices", fetch=True)]
    stores = [r["store"] for r in run("SELECT DISTINCT store FROM prices", fetch=True)]
    aliases = {r["raw"]: r["fixed"] for r in run("SELECT raw,fixed FROM aliases", fetch=True)}
    return jsonify(lines=receipt.parse(text, known, aliases), total=receipt.find_total(text),
                   store=receipt.find_store(text, stores), date=receipt.find_date(text))


# ---------- резервная копия ----------
@app.get("/api/export.csv")
def export_csv():
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow(["Дата", "Магазин", "Товар", "Цена", "Штук в упаковке"])
    for r in run("SELECT bought,store,product,price,qty FROM prices ORDER BY bought,id", fetch=True):
        w.writerow([r["bought"], r["store"], r["product"],
                    str(r["price"]).replace(".", ","), str(r["qty"]).replace(".", ",")])
    return Response("\ufeff" + out.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=prices.csv"})


@app.post("/api/import")
def import_csv():
    f = request.files.get("file")
    if not f:
        return jsonify(error="Нет файла"), 400
    text = f.read().decode("utf-8-sig", errors="replace")
    delim = ";" if text.count(";") >= text.count(",") else ","
    have = {(r["store"], r["product"], r["price"], r["qty"], r["bought"])
            for r in run("SELECT store,product,price,qty,bought FROM prices", fetch=True)}
    new = []
    for row in csv.reader(io.StringIO(text), delimiter=delim):
        if len(row) < 4:
            continue
        try:
            bought, store, product = iso(row[0]), row[1].strip(), row[2].strip()
            price = num(row[3])
            qty = num(row[4]) if len(row) > 4 and row[4].strip() else 1.0
        except ValueError:
            continue
        if not store or not product or price <= 0 or qty <= 0:
            continue
        key = (store, product, price, qty, bought)
        if key not in have:
            have.add(key)
            new.append((store, product, price, qty, bought))
    run_many("INSERT INTO prices(store,product,price,qty,bought) VALUES(?,?,?,?,?)", new)
    return jsonify(added=len(new))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
