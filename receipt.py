"""Разбор текста чека. Поддерживает несколько типичных форматов:
1) "1.Название(код)....1.000X135.00=135.00" (название может переноситься на 2 строки);
2) "Название" и строка ниже "2 x 45.00  90.00" (Магнит, Пятёрочка и т.п.);
3) "Название ........ 123.45" в одну строку;
4) "Название" и строка ниже только с ценой.
Из всех вариантов выбирается тот, у которого сумма совпала с итогом чека."""
import difflib
import re
from datetime import date

PRICE = r"\d{1,6}[.,]\d{2}"
NUM = r"\d+(?:[.,]\d+)?"
TAIL = r"\s*(?:[рР₽]|руб\.?)?\s*[A-Za-zА-Яа-я*]?\s*$"
ITEM_START = re.compile(r"^\s*(\d{1,3})\s*[.)\-]\s*(?=[^\W\d_])")
QTY_X = re.compile(r"(\d*[.,]?\d{3})\s*[xXхХ×]\s*(\d+[.,]\d{1,2})(?:\s*=\s*(\d+[.,]\d{1,2}))?")
QX_LINE = re.compile(r"(?P<q>%s)\s*[xXхХ×*]\s*(?P<p>%s)(?:\s*[=\-–:]?\s*(?P<t>%s))?%s" % (NUM, PRICE, PRICE, TAIL))
END_PRICE = re.compile(r"^(?P<n>.*?)[\s.\-–—:=*_]*(?P<p>%s)%s" % (PRICE, TAIL))
LETTERS = re.compile(r"[A-Za-zА-Яа-яЁё]{2,}")
DATE_RE = re.compile(r"(?<!\d)(\d{2})[.\-/](\d{2})[.\-/](\d{4}|\d{2})(?!\d)")
STOP = ("итог", "электрон", "получено", "картой", "наличными", "к оплате")
SKIP_SUB = ("итог", "сдача", "налич", "ндс", "карта", "картой", "сумма", "скидк", "касс", "чек",
            "оплат", "всего", "электрон", "получен", "спасибо", "добро пожаловать", "место расч",
            "приход", "www", "http", "покупател", "бонус")
SKIP_WORD = re.compile(r"\b(тел|инн|фн|фд|фп|фпд|сно|смена|адрес|сайт|фнс|рнм|знаспа|рнаспа)\b")
VAGUE = {"продукты", "продукт", "товар", "товары", "бакалея", "разное", "прочее", "овощи",
         "фрукты", "напитки", "хозтовары"}


def f(s):
    return float(s.replace(",", "."))


def skip_line(low):
    return any(w in low for w in SKIP_SUB) or bool(SKIP_WORD.search(low))


def clean(s):
    s = s.replace("ТОВАР", " ").replace("\\", " ")
    s = re.sub(r"[-=_]{3,}|\.{2,}|\(\s*\d+\s*\)", " ", s)
    starts = list(re.finditer(r"(?<![\d.,])(\d{1,3})\s*[.)]\s*(?=[^\W\d_])", s))
    if starts:
        s = s[starts[-1].end():]
    s = re.sub(r"^\s*\d+[.,]\d{2}\s+", "", s)
    s = re.sub(r"\s+", " ", s).strip(" .,:;-–—=_*/|()")
    for _ in range(2):
        s = re.sub(r"\s+\d{1,3}\s*шт\.?$", "", s).strip(" .,:;-–—=_*/|()")
    return s


def suspicious(name):
    low = name.lower()
    words = re.findall(r"[a-zа-яёіїєґ0-9]+", low)
    if low.strip() in VAGUE or not any(len(w) >= 3 for w in words):
        return True
    if re.search(r"[іїєґ]", low):
        return True
    return any((re.search(r"[a-z]", w) and re.search(r"[а-яё]", w)) or re.search(r"[а-яё][0-9]$", w)
               for w in words)


def numbered(text):
    lines, started = [], False
    for line in text.replace("\r", "").split("\n"):
        line = line.strip()
        if not line or re.fullmatch(r"[\s=\-_.]+", line):
            continue
        if started and any(w in line.lower() for w in STOP):
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
        price = f(m.group(2))
        qty = round(f(total) / price, 3) if total and price else f(m.group(1))
        name = clean(head)
        if name and price > 0:
            out.append((name, price, qty or 1.0))
    return out


def generic(text):
    out, pending = [], ""
    for line in text.replace("\r", "").split("\n"):
        s = line.strip()
        if not s or re.fullmatch(r"[\s=\-_.*#]+", s):
            continue
        low = s.lower()
        if skip_line(low):
            if any(w in low for w in STOP) and out:
                break
            pending = ""
            continue
        s = s.replace("ТОВАР", " ").strip()
        m = QX_LINE.search(s)
        if m:
            pre = s[:m.start()].strip()
            price, q = f(m.group("p")), f(m.group("q"))
            if m.group("t") and price:
                q = round(f(m.group("t")) / price, 3) or q
            if LETTERS.search(pre):
                name = pre if (ITEM_START.match(pre) or pre[:1].isupper() or not pending) else pending + " " + pre
            else:
                name = pending
            name = clean(name)
            if name and price > 0:
                out.append((name, price, q or 1.0))
            pending = ""
            continue
        m = END_PRICE.match(s)
        if m:
            pre = m.group("n").strip()
            if LETTERS.search(pre):
                name = pre if (ITEM_START.match(pre) or pre[:1].isupper() or not pending) else pending + " " + pre
            else:
                name = pending
            name = clean(name)
            if name and LETTERS.search(name):
                out.append((name, f(m.group("p")), 1.0))
            pending = ""
            continue
        if LETTERS.search(s) and len(re.sub(r"\D", "", s)) < 8:
            if pending and (s[:1].islower() or s[:1].isdigit()):
                pending += " " + s
            else:
                pending = s
    return out


def diff(rows, total):
    if total is None:
        return None
    return abs(total - sum(q * p for _, p, q in rows))


def parse(text, known, aliases):
    total = find_total(text)
    cands = [numbered(text), generic(text)]
    cands = [c for c in cands if c]
    rows = []
    if cands:
        if total is not None:
            cands.sort(key=lambda c: (diff(c, total) >= 0.05, diff(c, total) / (total or 1), -len(c)))
        else:
            cands.sort(key=lambda c: -len(c))
        rows = cands[0]
    low = {k.lower(): k for k in known}
    out = []
    for name, price, qty in rows:
        key, shown, sure = name.lower(), name, False
        if key in aliases:
            shown, sure = aliases[key], True
        else:
            near = difflib.get_close_matches(key, list(low), 1, 0.85)
            if near:
                shown, sure = low[near[0]], True
        out.append({"raw": name, "name": shown, "price": price, "qty": qty,
                    "bad": (not sure) and suspicious(name)})
    return out


def find_total(text):
    m = re.search(r"(?:итог|к оплате|всего)[^\d\n]{0,15}(\d{2,}[.,]\d{2})", text, re.I)
    return f(m.group(1)) if m else None


def find_date(text):
    for m in DATE_RE.finditer(text):
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        if 1 <= mo <= 12 and 1 <= d <= 31 and 2000 <= y <= date.today().year + 1:
            return "%04d-%02d-%02d" % (y, mo, d)
    return None


def find_store(text, stores):
    lines = [l.strip() for l in text.replace("\r", "").split("\n") if l.strip()][:8]
    name = None
    for l in lines:
        m = re.search(r'["«“„]([^"«»“”„]{3,40})["»”“]', l)
        if m:
            name = m.group(1).strip()
            break
    if not name:
        bad = ("чек", "добро", "пожаловать", "продаж", "приход", "кассир", "ккт", "ооо", "ип ")
        for l in lines[:5]:
            if (not re.search(r"\d", l) and LETTERS.search(l) and 3 <= len(l) <= 30
                    and not any(b in l.lower() for b in bad)):
                name = l.title()
                break
    if not name:
        return None
    near = difflib.get_close_matches(name, stores, 1, 0.75)
    return near[0] if near else name
