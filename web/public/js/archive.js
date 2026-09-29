// Archive: one owner-only place that gathers transcripts from every source.
import { onUser, isOwner, signIn, logOut, loadArchive, saveToArchive, removeFromArchive } from "./cloud.js";
import { parseImport } from "./importers.js";

const $ = (id) => document.getElementById(id);
const esc = (s) => { const d = document.createElement("div"); d.textContent = s == null ? "" : String(s); return d.innerHTML; };
const status = (m) => { $("status").textContent = m || ""; };

const SOURCE_AR = { phone: "تسجيل موبايل", upload: "ملف مرفوع", share: "مشاركة واتساب", github: "بايب لاين GitHub", import: "مستورد", summary: "ملخص مجمّع" };

// Arabic-insensitive search: ignore hamza forms, taa marbuta, alef maqsura, diacritics and tatweel.
const norm = (s) => String(s || "").toLowerCase()
  .replace(/[\u064B-\u0652\u0670\u0640]/g, "").replace(/[إأآٱ]/g, "ا").replace(/ى/g, "ي").replace(/ة/g, "ه").replace(/ؤ/g, "و").replace(/ئ/g, "ي");

let all = [], srcFilter = "";

function fmtWhen(iso) {
  if (!iso) return "بدون تاريخ";
  const d = new Date(iso);
  return isNaN(d) ? String(iso) : d.toLocaleString("ar-EG", { timeZone: "Africa/Cairo", dateStyle: "medium", timeStyle: "short" });
}
function fmtDur(sec) {
  if (!sec) return "";
  const m = Math.round(sec / 60);
  return m >= 60 ? Math.floor(m / 60) + " س " + (m % 60) + " د" : m + " د";
}
function snippet(text, q) {
  if (!q) return "";
  const nt = norm(text), i = nt.indexOf(q);
  if (i < 0) return "";
  // norm() keeps the length except for removed marks; map back approximately by searching the raw text window
  const a = Math.max(0, i - 70), b = Math.min(text.length, i + q.length + 90);
  const raw = text.slice(a, b);
  const j = norm(raw).indexOf(q);
  const hit = j >= 0 ? esc(raw.slice(0, j)) + "<mark>" + esc(raw.slice(j, j + q.length)) + "</mark>" + esc(raw.slice(j + q.length)) : esc(raw);
  return '<div class="snip">…' + hit + "…</div>";
}

function renderStats(list) {
  const secs = list.reduce((s, e) => s + (e.duration_sec || 0), 0);
  const words = list.reduce((s, e) => s + (e.words || 0), 0);
  const folders = new Set(list.map((e) => e.folder).filter(Boolean)).size;
  $("stats").innerHTML = [
    [list.length, "تفريغ"], [Math.round(secs / 3600 * 10) / 10, "ساعة صوت"],
    [words.toLocaleString("ar-EG"), "كلمة"], [folders, "مجلد / جهة"],
  ].map(([n, l]) => '<div class="stat"><b>' + esc(n) + "</b><span>" + esc(l) + "</span></div>").join("");
}

function renderFilters() {
  const counts = {};
  all.forEach((e) => { counts[e.source] = (counts[e.source] || 0) + 1; });
  $("sources").innerHTML = ['<button class="chip" data-src="" aria-pressed="' + (!srcFilter) + '">الكل (' + all.length + ")</button>"]
    .concat(Object.keys(counts).map((s) => '<button class="chip" data-src="' + esc(s) + '" aria-pressed="' + (srcFilter === s) + '">' +
      esc(SOURCE_AR[s] || s) + " (" + counts[s] + ")</button>")).join("");
  const sel = $("folder"), cur = sel.value;
  const folders = [...new Set(all.map((e) => e.folder).filter(Boolean))].sort((a, b) => a.localeCompare(b, "ar"));
  sel.innerHTML = '<option value="">كل المجلدات</option>' + folders.map((f) => '<option value="' + esc(f) + '">' + esc(f) + "</option>").join("");
  sel.value = folders.includes(cur) ? cur : "";
}

function visible() {
  const q = norm($("q").value.trim()), f = $("folder").value;
  return all.filter((e) => (!srcFilter || e.source === srcFilter) && (!f || e.folder === f) &&
    (!q || norm(e.title).includes(q) || norm(e.summary).includes(q) || norm(e.text).includes(q) || norm((e.tags || []).join(" ")).includes(q)));
}

function render() {
  const list = visible(), q = norm($("q").value.trim());
  renderStats(list);
  if (!list.length) { $("list").innerHTML = '<p class="empty">' + (all.length ? "مفيش نتايج للبحث ده." : "الأرشيف فاضي لسه.") + "</p>"; return; }
  const shown = list.slice(0, 200);
  $("list").innerHTML = shown.map((e) =>
    '<article class="entry" data-id="' + esc(e.id) + '">' +
    "<h2>" + esc(e.title) + "</h2>" +
    '<div class="meta">' + esc(fmtWhen(e.recorded_at)) + (e.duration_sec ? " · " + esc(fmtDur(e.duration_sec)) : "") +
    " · " + esc(SOURCE_AR[e.source] || e.source) + (e.folder ? " · 📁 " + esc(e.folder) : "") +
    ((e.tags || []).length ? " " + e.tags.map((t) => '<span class="tag">' + esc(t) + "</span>").join("") : "") + "</div>" +
    (e.summary ? '<p class="summary">' + esc(e.summary) + "</p>" : "") +
    snippet(e.text, q) +
    "<details><summary>التفريغ الكامل (" + esc((e.words || 0).toLocaleString("ar-EG")) + " كلمة)</summary><pre data-lazy></pre></details>" +
    '<div class="row" style="margin-top:8px"><button class="btn" data-act="copy">📋 انسخ</button>' +
    '<button class="btn" data-act="md">⬇ .md</button><button class="btn" data-act="del">🗑 احذف من الأرشيف</button></div>' +
    "</article>").join("") +
    (list.length > shown.length ? '<p class="empty">معروض أول ' + shown.length + " من " + list.length + " — ضيّق البحث لرؤية الباقي.</p>" : "");
}

const toMd = (e) => "# " + e.title + "\n\n_" + fmtWhen(e.recorded_at) + " · " + (SOURCE_AR[e.source] || e.source) +
  (e.folder ? " · " + e.folder : "") + "_\n\n" + (e.summary ? "> " + e.summary + "\n\n" : "") + e.text + "\n";
function download(name, text) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: "text/markdown;charset=utf-8" }));
  a.download = name.replace(/[\\/:*?"<>|]/g, "").slice(0, 80) + ".md"; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

$("list").addEventListener("toggle", (ev) => {
  const pre = ev.target.querySelector && ev.target.querySelector("pre[data-lazy]");
  if (!pre || !ev.target.open) return;
  const e = all.find((x) => x.id === ev.target.closest(".entry").dataset.id);
  pre.textContent = e ? e.text : ""; pre.removeAttribute("data-lazy");
}, true);
$("list").addEventListener("click", async (ev) => {
  const b = ev.target.closest("button[data-act]"); if (!b) return;
  const e = all.find((x) => x.id === b.closest(".entry").dataset.id); if (!e) return;
  if (b.dataset.act === "copy") { await navigator.clipboard.writeText(e.text); status("اتنسخ ✔"); }
  else if (b.dataset.act === "md") download((e.recorded_at || "").slice(0, 10) + "_" + e.title, toMd(e));
  else if (b.dataset.act === "del" && confirm("تحذف «" + e.title + "» من الأرشيف المجمّع؟ (النسخة على الموبايل مش هتتمسح)")) {
    await removeFromArchive(e.id); all = all.filter((x) => x.id !== e.id); renderFilters(); render(); status("اتحذف من الأرشيف.");
  }
});
$("sources").addEventListener("click", (ev) => { const c = ev.target.closest(".chip"); if (!c) return; srcFilter = c.dataset.src; renderFilters(); render(); });
let t; $("q").addEventListener("input", () => { clearTimeout(t); t = setTimeout(render, 200); });
$("folder").addEventListener("change", render);
$("dlAll").addEventListener("click", () => {
  const list = visible(); if (!list.length) return;
  download("أرشيف_التفريغات_" + new Date().toISOString().slice(0, 10), list.map(toMd).join("\n\n---\n\n"));
});
$("reload").addEventListener("click", load);
$("importIn").addEventListener("change", async (ev) => {
  const files = [...ev.target.files]; ev.target.value = "";
  let ok = 0, bad = 0;
  for (const f of files) {
    status("بيستورد " + (ok + bad + 1) + " من " + files.length + "…");
    try { const item = await parseImport(f); await saveToArchive(item.id, item); ok++; }
    catch (err) { bad++; console.warn(f.name, err); }
  }
  status("اتستورد " + ok + " ملف" + (bad ? " · فشل " + bad + " (صيغة غير معروفة)" : "") + ".");
  load();
});

async function load() {
  status("بيحمّل الأرشيف…");
  try { all = await loadArchive(); renderFilters(); render(); status(""); }
  catch (e) { status("تعذّر تحميل الأرشيف: " + (e.code || e.message)); }
}

$("inBtn").addEventListener("click", () => signIn().catch((e) => { $("gateMsg").textContent = "تسجيل الدخول فشل: " + (e.code || e.message); }));
$("outBtn").addEventListener("click", () => logOut());
onUser((u) => {
  const ok = isOwner(u);
  $("gate").hidden = ok; $("app").hidden = !ok; $("outBtn").hidden = !u;
  $("who").textContent = u ? u.email : "";
  if (u && !ok) $("gateMsg").textContent = "الحساب " + u.email + " مش صاحب الأرشيف. ادخل بحساب صاحب الأرشيف.";
  if (ok) load();
});
