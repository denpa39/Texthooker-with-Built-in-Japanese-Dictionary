/* VN Texthooker front-end ------------------------------------------------ *
 * - tokenizes incoming Japanese with kuromoji
 * - renders words as hoverable spans (with optional furigana)
 * - fetches offline JMdict definitions from the local server on hover
 * ------------------------------------------------------------------------ */

const linesEl = document.getElementById("lines");
const popup = document.getElementById("popup");
const statusEl = document.getElementById("status");
const dictStatus = document.getElementById("dictStatus");
const hint = document.getElementById("hint");

let tokenizer = null;
let showFurigana = false;
let lookupCache = new Map();

/* ---- POS abbreviation expansion (common JMdict tags) ------------------- */
const POS = {
  "n": "noun", "pn": "pronoun", "adj-i": "い-adjective", "adj-na": "な-adjective",
  "adj-no": "の-adjective", "adv": "adverb", "adv-to": "adverb (と)", "aux": "auxiliary",
  "aux-v": "auxiliary verb", "aux-adj": "auxiliary adjective", "conj": "conjunction",
  "cop": "copula", "ctr": "counter", "exp": "expression", "int": "interjection",
  "prt": "particle", "pref": "prefix", "suf": "suffix", "num": "numeric",
  "v1": "ichidan verb", "v5": "godan verb", "v5r": "godan verb (-る)",
  "v5u": "godan verb (-う)", "v5k": "godan verb (-く)", "v5g": "godan verb (-ぐ)",
  "v5s": "godan verb (-す)", "v5t": "godan verb (-つ)", "v5n": "godan verb (-ぬ)",
  "v5b": "godan verb (-ぶ)", "v5m": "godan verb (-む)", "vs": "する verb",
  "vs-i": "する verb (irregular)", "vs-s": "する verb (special)", "vk": "くる verb",
  "vi": "intransitive verb", "vt": "transitive verb", "vz": "ずる verb",
};
const expandPos = p => POS[p] || p;

const MISC = {
  "uk": "usu. kana", "col": "colloquial", "sl": "slang", "vulg": "vulgar",
  "fam": "familiar", "hon": "honorific", "hum": "humble", "pol": "polite",
  "arch": "archaic", "obs": "obsolete", "fem": "female term", "male": "male term",
  "abbr": "abbreviation", "on-mim": "onomatopoeia", "joc": "jocular", "derog": "derogatory",
};
const expandMisc = m => MISC[m] || m;

/* ---- katakana -> hiragana (for furigana) ------------------------------ */
function toHiragana(s) {
  let out = "";
  for (const ch of s) {
    const c = ch.codePointAt(0);
    out += (c >= 0x30a1 && c <= 0x30f6) ? String.fromCodePoint(c - 0x60) : ch;
  }
  return out;
}
const hasKanji = s => /[一-龯々]/.test(s);
const isJapanese = s => /[぀-ヿ一-龯々ｦ-ﾟ]/.test(s);

/* ---- tokenizer init ---------------------------------------------------- */
kuromoji.builder({ dicPath: "/static/kuromoji/dict" }).build((err, tk) => {
  if (err) {
    dictStatus.textContent = "tokenizer failed";
    console.error(err);
    return;
  }
  tokenizer = tk;
  dictStatus.textContent = "tokenizer ready";
  dictStatus.classList.add("ready");
  // Re-render any lines that arrived before the tokenizer was ready.
  rebuildSentences();
});

// Rebuild the tokenized text of every line in place.
function rebuildSentences() {
  document.querySelectorAll(".line[data-raw]").forEach(line => {
    const sentence = line.querySelector(".sentence");
    const rebuilt = buildSentence(line.dataset.raw);
    if (sentence) sentence.replaceWith(rebuilt);
    else line.insertBefore(rebuilt, line.firstChild);
  });
}

/* ---- rendering --------------------------------------------------------- */
function buildSentence(text) {
  const div = document.createElement("div");
  div.className = "sentence";

  if (!tokenizer) {
    div.textContent = text;            // plain until tokenizer is ready
    return div;
  }

  const tokens = tokenizer.tokenize(text);
  for (const t of tokens) {
    const surface = t.surface_form;
    const span = document.createElement("span");
    span.className = "token";

    if (isJapanese(surface) && t.pos !== "記号") {
      span.classList.add("word");
      // dictionary form: prefer basic_form (handles inflection), else surface
      const base = (t.basic_form && t.basic_form !== "*") ? t.basic_form : surface;
      span.dataset.term = base;
      span.dataset.surface = surface;
      span.dataset.pos = t.pos || "";
      span.dataset.jreading = (t.reading && t.reading !== "*") ? t.reading : "";
      span.dataset.off = String((t.word_position || 1) - 1);  // start index in the line

      if (showFurigana && hasKanji(surface) && t.reading && t.reading !== "*") {
        const ruby = document.createElement("ruby");
        ruby.textContent = surface;
        const rt = document.createElement("rt");
        rt.textContent = toHiragana(t.reading);
        ruby.appendChild(rt);
        span.appendChild(ruby);
      } else {
        span.textContent = surface;
      }
    } else {
      span.textContent = surface;
    }
    div.appendChild(span);
  }
  return div;
}

function addLine(text) {
  text = (text || "").replace(/\r/g, "").trim();
  if (!text) return;
  hint.classList.add("gone");

  document.querySelectorAll(".line.latest").forEach(e => e.classList.remove("latest"));

  const line = document.createElement("div");
  line.className = "line latest";
  line.dataset.raw = text;
  line.appendChild(buildSentence(text));

  linesEl.appendChild(line);
  linesEl.scrollTo({ top: linesEl.scrollHeight, behavior: "smooth" });

  // keep DOM bounded
  while (linesEl.children.length > 300) linesEl.removeChild(linesEl.firstChild);
}

/* ---- dictionary lookup + popup (longest-match scan) -------------------- */
let pinned = false;

async function fetchScan(text, pos, reading, base) {
  const key = [pos || "", reading || "", base || "", text].join("|");
  if (lookupCache.has(key)) return lookupCache.get(key);
  try {
    const r = await fetch("/scan?text=" + encodeURIComponent(text) +
                          "&pos=" + encodeURIComponent(pos || "") +
                          "&reading=" + encodeURIComponent(reading || "") +
                          "&base=" + encodeURIComponent(base || ""));
    const res = (await r.json()).candidates || [];
    lookupCache.set(key, res);
    return res;
  } catch (e) {
    return [];
  }
}

function plainReading(reading) {
  const rd = document.createElement("span");
  rd.className = "reading";
  rd.textContent = reading;
  return rd;
}

// Fortnite-style word rarity (rarer word = higher tier).
// Prefers the VN frequency rank (jiten.moe) when present, else general frequency.
function rarity(e) {
  const rk = e.vrd;                          // VN rank: lower = more common
  if (rk != null) {
    if (rk <= 1500)  return ["Common", "r-common", rk];
    if (rk <= 5000)  return ["Uncommon", "r-uncommon", rk];
    if (rk <= 15000) return ["Rare", "r-rare", rk];
    if (rk <= 35000) return ["Epic", "r-epic", rk];
    return ["Legendary", "r-legendary", rk];
  }
  const df = (e.df != null) ? e.df : (e.f || 0);
  if (df >= 30000) return ["Common", "r-common", null];
  if (df >= 3000)  return ["Uncommon", "r-uncommon", null];
  if (df >= 250)   return ["Rare", "r-rare", null];
  if (df >= 15)    return ["Epic", "r-epic", null];
  if (df >= 1)     return ["Legendary", "r-legendary", null];
  return ["Mythic", "r-mythic", null];
}

function renderCandidate(c) {
  const entry = c.entry;
  const div = document.createElement("div");
  div.className = "entry" + (c.kind === "name" ? " name" : "");

  // inflection trail: 食べさせられた · causative › passive › past
  if (c.reasons && c.reasons.length) {
    const inf = document.createElement("div");
    inf.className = "inflect";
    inf.textContent = c.matched + "  ·  " + c.reasons.join(" › ");
    div.appendChild(inf);
  }

  const head = document.createElement("div");
  head.className = "head";
  const primary = (entry.k && entry.k[0]) || (entry.r && entry.r[0]) || c.matched;
  const hw = document.createElement("span");
  hw.className = "hw";
  hw.textContent = primary;
  head.appendChild(hw);
  const reading = entry.r && entry.r[0];
  if (reading && entry.k && entry.k.length) {
    head.appendChild(plainReading(reading));
  }
  if (c.kind === "name") {
    const tag = document.createElement("span");
    tag.className = "name-tag";
    tag.textContent = "name";
    head.appendChild(tag);
  } else {
    const [label, cls, rk] = rarity(entry);
    const tag = document.createElement("span");
    tag.className = "rarity " + cls;
    tag.textContent = label;
    tag.title = rk != null ? `VN frequency rank #${rk.toLocaleString()}`
                           : "word rarity (by general frequency)";
    head.appendChild(tag);
  }

  const copy = document.createElement("button");
  copy.className = "mini";
  copy.textContent = "⧉";
  copy.title = "copy word";
  copy.addEventListener("click", ev => {
    ev.stopPropagation();
    if (navigator.clipboard) navigator.clipboard.writeText(primary);
    copy.textContent = "✓";
    setTimeout(() => (copy.textContent = "⧉"), 900);
  });
  head.appendChild(copy);

  const jisho = document.createElement("a");
  jisho.className = "mini";
  jisho.textContent = "↗";
  jisho.title = "look up on Jisho.org";
  jisho.href = "https://jisho.org/search/" + encodeURIComponent(primary);
  jisho.target = "_blank";
  jisho.rel = "noopener";
  jisho.addEventListener("click", ev => ev.stopPropagation());
  head.appendChild(jisho);
  div.appendChild(head);

  const alts = [...(entry.k || []).slice(1),
                ...((entry.k && entry.k.length) ? [] : (entry.r || []).slice(1))];
  if (alts.length) {
    const alt = document.createElement("div");
    alt.className = "alt";
    alt.textContent = "also: " + alts.join("、");
    div.appendChild(alt);
  }

  (entry.s || []).forEach((s, i) => {
    const sense = document.createElement("div");
    sense.className = "sense";
    if (s.pos && s.pos.length) {
      const pos = document.createElement("span");
      pos.className = "pos";
      pos.textContent = s.pos.map(expandPos).join(", ");
      sense.appendChild(pos);
    }
    const g = document.createElement("span");
    g.className = "glosses";
    const num = document.createElement("span");
    num.className = "num";
    num.textContent = (i + 1) + ".";
    g.appendChild(num);
    let txt = s.gloss.join("; ");
    if (s.misc && s.misc.length) txt = "(" + s.misc.map(expandMisc).join(", ") + ") " + txt;
    g.appendChild(document.createTextNode(txt));
    sense.appendChild(g);
    div.appendChild(sense);
  });
  return div;
}

async function showScanPopup(target) {
  const line = target.closest(".line");
  if (!line) return;
  const off = parseInt(target.dataset.off || "0", 10);
  const text = line.dataset.raw.slice(off);
  const cands = await fetchScan(text, target.dataset.pos,
                                target.dataset.jreading, target.dataset.term);

  popup.innerHTML = "";
  if (pinned) {
    const close = document.createElement("button");
    close.className = "pin-close";
    close.textContent = "×";
    close.title = "close";
    close.addEventListener("click", unpin);
    popup.appendChild(close);
  }
  if (!cands.length) {
    const e = document.createElement("div");
    e.className = "empty";
    e.textContent = `No entry for 「${target.dataset.surface || target.dataset.term}」`;
    popup.appendChild(e);
  } else {
    cands.forEach(c => popup.appendChild(renderCandidate(c)));
  }
  positionPopup(target);
  popup.classList.remove("hidden");
}

function positionPopup(target) {
  popup.classList.remove("hidden");
  const r = target.getBoundingClientRect();
  const pw = Math.min(popup.offsetWidth || 440, window.innerWidth - 20);
  let left = r.left;
  if (left + pw > window.innerWidth - 10) left = window.innerWidth - pw - 10;
  if (left < 10) left = 10;

  const ph = popup.offsetHeight;
  let top = r.bottom + 8;
  if (top + ph > window.innerHeight - 10) top = r.top - ph - 8; // flip above
  if (top < 10) top = 10;
  popup.style.left = left + "px";
  popup.style.top = top + "px";
}

let hideTimer = null;
function scheduleHide() {
  if (pinned) return;
  clearTimeout(hideTimer);
  hideTimer = setTimeout(() => popup.classList.add("hidden"), 180);
}
function cancelHide() { clearTimeout(hideTimer); }
function unpin() {
  pinned = false;
  popup.classList.remove("pinned");
  popup.classList.add("hidden");
}

linesEl.addEventListener("mouseover", e => {
  const t = e.target.closest(".token.word");
  if (!t || pinned) return;
  cancelHide();
  showScanPopup(t);
});
linesEl.addEventListener("mouseout", e => {
  if (e.target.closest(".token.word")) scheduleHide();
});
linesEl.addEventListener("click", e => {
  const t = e.target.closest(".token.word");
  if (!t) return;
  pinned = true;
  popup.classList.add("pinned");
  cancelHide();
  showScanPopup(t);          // click a word to pin the popup open
});
popup.addEventListener("mouseenter", cancelHide);
popup.addEventListener("mouseleave", scheduleHide);
document.addEventListener("keydown", e => { if (e.key === "Escape") unpin(); });
document.addEventListener("click", e => {
  if (pinned && !e.target.closest(".token.word") && !e.target.closest("#popup")) unpin();
});

/* ---- clipboard stream (SSE) ------------------------------------------- */
function connectStream() {
  const es = new EventSource("/events");
  es.onopen = () => { statusEl.textContent = "● live"; statusEl.className = "status live"; };
  es.onmessage = ev => {
    try { addLine(JSON.parse(ev.data).text); } catch (_) {}
  };
  es.onerror = () => {
    statusEl.textContent = "reconnecting…"; statusEl.className = "status error";
    // EventSource auto-reconnects.
  };
}
connectStream();

/* ---- toolbar ----------------------------------------------------------- */
const pauseBtn = document.getElementById("pauseBtn");
async function refreshPause() {
  const j = await (await fetch("/state")).json();
  applyPause(j.paused);
}
function applyPause(paused) {
  pauseBtn.classList.toggle("active", paused);
  pauseBtn.textContent = paused ? "▶ Resume" : "⏸ Pause";
  if (paused) { statusEl.textContent = "paused"; statusEl.className = "status paused"; }
  else { statusEl.textContent = "● live"; statusEl.className = "status live"; }
}
pauseBtn.addEventListener("click", async () => {
  const want = !pauseBtn.classList.contains("active");
  const j = await (await fetch("/pause", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ paused: want }),
  })).json();
  applyPause(j.paused);
});
refreshPause();

const furiBtn = document.getElementById("furiBtn");
furiBtn.addEventListener("click", () => {
  showFurigana = !showFurigana;
  furiBtn.classList.toggle("active", showFurigana);
  rebuildSentences();
});

// Remove only the most recent line (undo), and move the "latest" highlight back.
document.getElementById("clearBtn").addEventListener("click", () => {
  const last = linesEl.lastElementChild;
  if (!last) return;
  if (!popup.classList.contains("hidden")) unpin();  // close any popup tied to it
  last.remove();
  const prev = linesEl.lastElementChild;
  if (prev) {
    prev.classList.add("latest");
  } else {
    hint.classList.remove("gone");  // back to empty state
  }
});

// Clear all lines.
document.getElementById("clearAllBtn").addEventListener("click", () => {
  if (!popup.classList.contains("hidden")) unpin();
  linesEl.innerHTML = "";
  hint.classList.remove("gone");
});

const fontRange = document.getElementById("fontRange");
fontRange.addEventListener("input", () => {
  document.documentElement.style.setProperty("--font-size", fontRange.value + "px");
});
