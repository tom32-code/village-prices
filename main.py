import difflib
import re
from datetime import date

from flask import jsonify, render_template, request

from app import app, db

with db() as c:
    cols = [r[1] for r in c.execute("PRAGMA table_info(items)")]
    if "unit" not in cols:
        c.execute("ALTER TABLE items ADD COLUMN unit TEXT DEFAULT 'шт'")
    if "bought" not in cols:
        c.execute("ALTER TABLE items ADD COLUMN bought TEXT")

UNIT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(кг|гр|г|мл|л)(?![а-яё])", re.I)
DATE_RE = re.compile(r"(?<!\d)(\d{2})[.\-/](\d{2})[.\-/](\d{4}|\d{2})(?!\d)")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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
    name = None
    for l in lines:
        m = re.search(r'["«“„]([^"«»“”„]{3,40})["»”“]', l)
        if m:
            name = m.group(1).strip()
            break
    if not name:
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
    n = float(m.group(1).replace(",", "."))
    u = m.group(2).lower()
    if n <= 0:
        return None, None
    if u in ("гр", "г"):
        return round(price / (n / 1000), 2), "кг"
    if u == "кг":
        return round(price / n, 2), "кг"
    if u == "мл":
        return round(price / (n / 1000), 2), "л"
    return round(price / n, 2), "л"


def index2():
    html = render_template("index.html").split("</html>")[0].rsplit("</body>", 1)[0]
    return html + '<script src="/static/extra.js?v=5"></script></body></html>'


orig_ocr = app.view_functions["ocr"]


def ocr2():
    resp = orig_ocr()
    if isinstance(resp, tuple):
        return resp
    j = resp.get_json()
    j["store"] = find_store(j.get("text", ""))
    j["date"] = find_date(j.get("text", ""))
    for l in j.get("lines", []):
        q = l.get("qty") or 1
        l["unit"] = "шт" if abs(q - round(q)) < 0.01 else "кг"
    return jsonify(j)


def save2():
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


def list2():
    q = (request.args.get("q") or "").strip().lower()
    with db() as c:
        rows = c.execute(
            "SELECT id,store,name,price,unit,"
            "COALESCE(bought,substr(created,1,10)) AS bought FROM items "
            "ORDER BY bought DESC, id DESC LIMIT 1500").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if q in d["name"].lower():
            d["ppu"], d["pu"] = per_unit(d["name"], d["price"], d["unit"])
            out.append(d)
    return jsonify(out)


app.view_functions["index"] = index2
app.view_functions["ocr"] = ocr2
app.view_functions["save_items"] = save2
app.view_functions["list_items"] = list2
