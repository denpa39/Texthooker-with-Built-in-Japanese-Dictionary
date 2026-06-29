/* Down the Rabbit Hole — front-end --------------------------------------- *
 * - tokenizes incoming Japanese with kuromoji
 * - renders words as hoverable spans (with optional furigana)
 * - fetches offline JMdict definitions from the local server on hover
 * ------------------------------------------------------------------------ */

const linesEl = document.getElementById("lines");
const popup = document.getElementById("popup");
const statusEl = document.getElementById("status");
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
  "arch": "archaic", "obs": "obsolete", "rare": "rare", "dated": "dated", "hist": "historical",
  "fem": "female term", "male": "male term", "form": "formal", "euph": "euphemistic",
  "abbr": "abbreviation", "on-mim": "onomatopoeia", "joc": "jocular", "derog": "derogatory",
  "poet": "poetic", "chn": "children's term", "yoji": "four-character idiom", "proverb": "proverb",
};
const expandMisc = m => MISC[m] || m;

/* ---- inflection-trail labels (raw de-inflection tags -> readable) ------- */
const REASON = { "passive/potential": "passive or potential", "past/-te": "past or -te" };
const expandReason = r => REASON[r] || r;
const isDatedSense = s => (s.misc || []).some(m => m === "arch" || m === "obs" || m === "rare");

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
    console.error(err);
    return;
  }
  tokenizer = tk;
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
      span.tabIndex = 0;                 // reachable by keyboard
      span.setAttribute("role", "button");
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

  // Only auto-scroll if the reader was already at the bottom — don't yank the user
  // away when they've scrolled up to re-read. (Measured before append.)
  const wasAtBottom = linesEl.scrollHeight - linesEl.scrollTop - linesEl.clientHeight < 80;
  linesEl.appendChild(line);
  if (wasAtBottom) {
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    linesEl.scrollTo({ top: linesEl.scrollHeight, behavior: reduce ? "auto" : "smooth" });
  }

  // keep DOM bounded
  while (linesEl.children.length > 300) linesEl.removeChild(linesEl.firstChild);
}

/* ---- dictionary lookup + popup (longest-match scan) -------------------- */
let pinned = false;

async function fetchScan(text, pos, reading, base, surface) {
  const key = [pos || "", reading || "", base || "", surface || "", text].join("|");
  if (lookupCache.has(key)) return lookupCache.get(key);
  try {
    const r = await fetch("/scan?text=" + encodeURIComponent(text) +
                          "&pos=" + encodeURIComponent(pos || "") +
                          "&reading=" + encodeURIComponent(reading || "") +
                          "&base=" + encodeURIComponent(base || "") +
                          "&surface=" + encodeURIComponent(surface || ""));
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

function renderSense(s, n) {
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
  num.textContent = n + ".";
  g.appendChild(num);
  let txt = s.gloss.join("; ");
  if (s.misc && s.misc.length) txt = "(" + s.misc.map(expandMisc).join(", ") + ") " + txt;
  g.appendChild(document.createTextNode(txt));
  sense.appendChild(g);
  return sense;
}

function renderCandidate(c) {
  const entry = c.entry;
  const div = document.createElement("div");
  div.className = "entry" + (c.kind === "name" ? " name" : "");

  // inflection trail: 食べさせられた · causative › passive › past
  if (c.reasons && c.reasons.length) {
    const inf = document.createElement("div");
    inf.className = "inflect";
    inf.textContent = c.matched + "  ·  " + c.reasons.map(expandReason).join(" › ");
    div.appendChild(inf);
  }

  const head = document.createElement("div");
  head.className = "head";
  // Headword + reading. When *every* sense is "usually kana", show the kana as the
  // headword (やはり, not 矢張り). Otherwise show the kanji, with the reading that
  // actually matched the hover (口【こう】 when hovered こう, not the primary 口【くち】).
  const senses = entry.s || [];
  const hasKanji = !!(entry.k && entry.k.length);
  const allUk = c.kind !== "name" && hasKanji && senses.length &&
                senses.every(s => (s.misc || []).includes("uk"));
  const primary = allUk ? entry.r[0]
                        : ((entry.k && entry.k[0]) || (entry.r && entry.r[0]) || c.matched);
  const hw = document.createElement("span");
  hw.className = "hw";
  hw.textContent = primary;
  head.appendChild(hw);
  const readingShown = c.mr || (entry.r && entry.r[0]);
  if (!allUk && hasKanji && readingShown) {
    head.appendChild(plainReading(readingShown));
  }
  if (c.kind === "name") {
    const tag = document.createElement("span");
    tag.className = "name-tag";   // pushed right via margin-left:auto
    tag.textContent = "name";
    head.appendChild(tag);
  }

  const copy = document.createElement("button");
  // for words there's no badge, so the copy button carries the right-align push
  copy.className = "mini" + (c.kind === "name" ? "" : " push");
  copy.textContent = "⧉";
  copy.title = "copy word";
  copy.setAttribute("aria-label", "copy word");
  copy.addEventListener("click", ev => {
    ev.stopPropagation();
    if (!navigator.clipboard) return;
    // only show ✓ once the write actually succeeds
    navigator.clipboard.writeText(primary).then(() => {
      copy.textContent = "✓";
      setTimeout(() => (copy.textContent = "⧉"), 900);
    }).catch(() => {});
  });
  head.appendChild(copy);

  const jisho = document.createElement("a");
  jisho.className = "mini";
  jisho.textContent = "↗";
  jisho.title = "look up on Jisho.org";
  jisho.setAttribute("aria-label", "look up " + primary + " on Jisho.org");
  jisho.href = "https://jisho.org/search/" + encodeURIComponent(primary);
  jisho.target = "_blank";
  jisho.rel = "noopener";
  jisho.addEventListener("click", ev => ev.stopPropagation());
  head.appendChild(jisho);
  div.appendChild(head);

  // "also written": for a kana-headword (all-uk) entry surface the kanji form(s).
  const alts = allUk
    ? [...(entry.k || []), ...(entry.r || []).slice(1)]
    : [...(entry.k || []).slice(1), ...(hasKanji ? [] : (entry.r || []).slice(1))];
  if (alts.length) {
    const alt = document.createElement("div");
    alt.className = "alt";
    alt.textContent = "also: " + alts.join("、");
    div.appendChild(alt);
  }

  // Senses. Fold archaic/obsolete/rare senses behind a toggle — but only when a
  // modern sense remains (a wholly-archaic entry still renders all of its senses).
  const modern = senses.filter(s => !isDatedSense(s));
  const dated = senses.filter(isDatedSense);
  const fold = modern.length > 0 && dated.length > 0;
  const visible = fold ? modern : senses;
  visible.forEach((s, i) => div.appendChild(renderSense(s, i + 1)));
  if (fold) {
    const more = document.createElement("div");
    more.className = "fold";
    more.textContent = `+ ${dated.length} rare / archaic sense${dated.length > 1 ? "s" : ""}`;
    more.addEventListener("click", ev => {
      ev.stopPropagation();
      dated.forEach((s, i) => div.insertBefore(renderSense(s, visible.length + i + 1), more));
      more.remove();
    });
    div.appendChild(more);
  }
  return div;
}

let lookupSeq = 0;
async function showScanPopup(target) {
  const line = target.closest(".line");
  if (!line) return;
  const off = parseInt(target.dataset.off || "0", 10);
  const text = line.dataset.raw.slice(off);
  const seq = ++lookupSeq;
  const cands = await fetchScan(text, target.dataset.pos, target.dataset.jreading,
                                target.dataset.term, target.dataset.surface);
  // A newer peek superseded this one while the lookup was in flight — drop it so a
  // slow response can't overwrite the word the user is now on. (Pins always render.)
  if (!pinned && seq !== lookupSeq) return;

  popup.innerHTML = "";
  if (pinned) {
    const close = document.createElement("button");
    close.className = "pin-close";
    close.textContent = "×";
    close.title = "close";
    close.setAttribute("aria-label", "close");
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
  // Peek footer: teach the two least-discoverable features (and signal scrollability).
  let foot = null;
  if (!pinned && cands.length) {
    foot = document.createElement("div");
    foot.className = "popup-foot";
    popup.appendChild(foot);
  }
  popup.scrollTop = 0;   // new word -> start at the top, don't inherit the last scroll
  positionPopup(target);
  if (foot) {
    const more = popup.scrollHeight > popup.clientHeight + 1;
    foot.textContent = more ? "click to pin · scroll for more ↓" : "click to pin";
  }
  popup.classList.remove("hidden");
}

function positionPopup(target) {
  popup.classList.remove("hidden");
  popup.style.maxHeight = "";                 // reset before measuring natural height
  const r = target.getBoundingClientRect();
  const gap = 8, margin = 10;
  const vw = window.innerWidth, vh = window.innerHeight;

  const pw = Math.min(popup.offsetWidth || 440, vw - 2 * margin);
  const left = Math.min(Math.max(margin, r.left), vw - pw - margin);

  // Place the popup fully below or fully above the word — never overlapping it —
  // and cap its height to the room on that side (it scrolls if taller). This stops
  // a tall popup from blanketing the screen and covering the word you're reading.
  const below = vh - r.bottom - gap - margin;
  const above = r.top - gap - margin;
  const ph = popup.offsetHeight;
  let top, maxH;
  if (ph <= below || below >= above) {        // below the word
    top = r.bottom + gap;
    maxH = below;
  } else {                                    // above the word (bottom edge above r.top)
    maxH = above;
    top = r.top - gap - Math.min(ph, maxH);
  }
  popup.style.maxHeight = Math.max(120, maxH) + "px";
  popup.style.left = left + "px";
  popup.style.top = Math.max(margin, top) + "px";
}

let hideTimer = null;
function scheduleHide() {
  if (pinned) return;
  clearTimeout(hideTimer);
  hideTimer = setTimeout(() => { popup.classList.add("hidden"); activeWord = null; }, 180);
}
function cancelHide() { clearTimeout(hideTimer); }
function unpin() {
  pinned = false;
  activeWord = null;
  popup.classList.remove("pinned");
  popup.classList.add("hidden");
}

let activeWord = null;   // the word the popup is currently showing (flicker guard)
function peek(t) {
  if (!t || pinned || t === activeWord) return;   // skip re-render of the same word
  activeWord = t;
  cancelHide();
  showScanPopup(t);
}
function pin(t) {
  pinned = true;
  popup.classList.add("pinned");
  activeWord = t;
  cancelHide();
  showScanPopup(t);
}
linesEl.addEventListener("mouseover", e => peek(e.target.closest(".token.word")));
linesEl.addEventListener("mouseout", e => { if (e.target.closest(".token.word")) scheduleHide(); });
linesEl.addEventListener("click", e => { const t = e.target.closest(".token.word"); if (t) pin(t); });
// keyboard: Tab to a word (focus peeks it), Enter/Space pins it open.
linesEl.addEventListener("focusin", e => peek(e.target.closest(".token.word")));
linesEl.addEventListener("focusout", e => { if (e.target.closest(".token.word")) scheduleHide(); });
linesEl.addEventListener("keydown", e => {
  const t = e.target.closest(".token.word");
  if (t && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); pin(t); }
});
popup.addEventListener("mouseenter", cancelHide);
popup.addEventListener("mouseleave", scheduleHide);

// The hover popup is pointer-events:none so it never blocks hovering the words
// beneath it — which also means the wheel can't scroll it natively. So while a
// peek popup is open, route the wheel to it manually AND keep the page still: the
// wheel only scrolls the meaning, so the word never slides out from under the
// cursor mid-read. Move off the word to scroll the page again. (A pinned popup is
// interactive and scrolls natively, so leave it alone.)
window.addEventListener("wheel", (e) => {
  if (pinned || popup.classList.contains("hidden")) return;
  popup.scrollTop += e.deltaY;
  e.preventDefault();
}, { passive: false });
document.addEventListener("keydown", e => { if (e.key === "Escape") unpin(); });
document.addEventListener("click", e => {
  if (pinned && !e.target.closest(".token.word") && !e.target.closest("#popup")) unpin();
});

/* ---- clipboard stream (SSE) ------------------------------------------- */
function connectStream() {
  const es = new EventSource("/events");
  es.onopen = () => {
    // don't flash "live" over a paused session
    if (!pauseBtn.classList.contains("active")) {
      statusEl.textContent = "● live"; statusEl.className = "status live";
    }
  };
  es.onmessage = ev => {
    try { addLine(JSON.parse(ev.data).text); } catch (_) {}
  };
  es.onerror = () => {
    // a routine reconnect, not an error — keep the calm "connecting" styling.
    if (!pauseBtn.classList.contains("active")) {
      statusEl.textContent = "connecting…"; statusEl.className = "status connecting";
    }
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

// Clear all lines — asks once (inline) before wiping everything.
const clearAllBtn = document.getElementById("clearAllBtn");
let clearArmed = false, clearTimer = null;
function disarmClear() {
  clearArmed = false; clearTimeout(clearTimer);
  clearAllBtn.classList.remove("confirm");
  clearAllBtn.textContent = "🗑";
  clearAllBtn.title = "Clear all lines";
}
clearAllBtn.addEventListener("click", () => {
  if (!clearArmed) {                       // first click: arm + ask
    clearArmed = true;
    clearAllBtn.classList.add("confirm");
    clearAllBtn.textContent = "🗑?";
    clearAllBtn.title = "Click again to clear everything";
    clearTimer = setTimeout(disarmClear, 2500);
    return;
  }
  disarmClear();                           // second click: do it
  if (!popup.classList.contains("hidden")) unpin();
  linesEl.innerHTML = "";
  hint.classList.remove("gone");
});

// Appearance (theme / colours / font / text size) lives in settings.js, which
// also wires up the toolbar's #fontRange size slider.
