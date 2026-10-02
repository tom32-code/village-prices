(() => {
const el = (t, c, x) => { const e = document.createElement(t); if (c) e.className = c; if (x != null) e.textContent = x; return e; };

const st = el("style", "", ".cur{padding:8px 10px;border:1.5px solid #8885;border-radius:10px;min-width:62px;text-align:center;font-size:15px}.line .pr{text-align:right}.line .pr small{display:block;font-weight:400;font-size:12px;color:var(--mut)}");
document.head.appendChild(st);

$("fileCam").setAttribute("capture", "environment");

// магазин с чека
const f0 = window.fetch.bind(window);
window.fetch = async (u, o) => {
  const r = await f0(u, o);
  if (String(u).includes("/api/ocr") && r.ok) {
    try {
      const j = await r.clone().json();
      if (j.store) {
        $("store").value = j.store;
        try { localStorage.setItem("store", j.store); } catch (e) {}
      }
      document.querySelector("#review h2").textContent = "3. Проверьте товары" + (j.store ? " · " + j.store : "");
    } catch (e) {}
  }
  return r;
};

// подпись ₽/шт или ₽/кг (по нажатию переключается)
const add0 = addRow;
addRow = function (it) {
  add0(it);
  const r = $("rows").lastChild, c = r.querySelector(".cur");
  r.dataset.unit = (it && it.unit) || "шт";
  const show = () => { c.textContent = "₽/" + r.dataset.unit; };
  show();
  c.onclick = () => { r.dataset.unit = r.dataset.unit === "кг" ? "шт" : "кг"; show(); };
};

$("save").onclick = async () => {
  const store = $("store").value.trim();
  if (!store) { say("Укажите название магазина вверху", true); window.scrollTo(0, 0); $("store").focus(); return; }
  const items = [...document.querySelectorAll("#rows .item")].map((r) => ({
    raw: r.dataset.raw, unit: r.dataset.unit,
    name: r.querySelector(".name").value, price: r.querySelector(".price").value }));
  if (!items.length) return;
  if (document.querySelector("#rows .item.bad") && !confirm("Остались непонятные названия. Сохранить всё равно?")) return;
  try {
    const j = await (await fetch("/api/items", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ store, items }) })).json();
    say("✅ Сохранено товаров: " + j.saved);
    $("review").classList.add("hidden"); $("rows").textContent = "";
    receiptTotal = null; sumCheck(); loadNames(); window.scrollTo(0, 0);
  } catch (e) { say("❌ " + e.message, true); }
};

loadList = async function () {
  const rows = await (await fetch("/api/items?q=" + encodeURIComponent($("q").value))).json();
  const g = {};
  rows.forEach((x) => { (g[norm(x.name)] ||= []).push(x); });
  const box = $("list"); box.textContent = "";
  const keys = Object.keys(g).sort();
  if (!keys.length) { box.appendChild(el("div", "empty", "Пока пусто. Отсканируйте первый чек 📷")); return; }
  keys.forEach((k) => {
    const latest = {};
    g[k].forEach((x) => { if (!latest[x.store]) latest[x.store] = x; });
    const L = Object.values(latest);
    const cmp = L.every((x) => x.ppu) && new Set(L.map((x) => x.pu)).size === 1;
    const v = (x) => (cmp ? x.ppu : x.price);
    L.sort((a, b) => v(a) - v(b));
    const multi = L.length > 1, card = el("div", "card grp");
    card.appendChild(el("h3", "", L[0].name));
    L.forEach((x, i) => {
      const best = multi && i === 0;
      const line = el("div", "line" + (best ? " best" : ""));
      const who = el("div", "who", x.store);
      who.appendChild(el("small", "", (x.created || "").slice(0, 10)));
      const pr = el("div", "pr", x.price.toFixed(2) + " ₽/" + x.unit + (best ? " ★" : ""));
      if (x.ppu && x.unit === "шт") pr.appendChild(el("small", "", "≈ " + x.ppu.toFixed(2) + " ₽/" + x.pu));
      const del = el("button", "del", "🗑"); del.type = "button";
      del.onclick = async () => {
        if (!confirm("Удалить эту запись?")) return;
        await fetch("/api/items/" + x.id, { method: "DELETE" }); loadList();
      };
      line.append(who, pr, del); card.appendChild(line);
    });
    if (multi) {
      const a = v(L[0]), b = v(L[L.length - 1]);
      card.appendChild(el("div", "small", "Разница: " + (b - a).toFixed(2) + " ₽" + (cmp ? "/" + L[0].pu : "") +
        " (" + Math.round((b - a) / a * 100) + "%)"));
    }
    box.appendChild(card);
  });
};
})();
