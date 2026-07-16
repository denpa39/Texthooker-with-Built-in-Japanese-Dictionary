/* Down the Rabbit Hole — front-end --------------------------------------- *
 * - tokenizes incoming Japanese with kuromoji
 * - renders words as hoverable spans (with optional furigana)
 * - fetches offline JMdict definitions from the local server on hover
 * ------------------------------------------------------------------------ */

const linesEl = document.getElementById("lines");
const popup = document.getElementById("popup");
const statusEl = document.getElementById("status");
const hint = document.getElementById("hint");

// Connection status is a plain coloured dot; the label lives only in title/aria-label.
function setStatus(state, label) {
  statusEl.className = "status " + state;
  statusEl.title = label;
  statusEl.setAttribute("aria-label", label);
}

let tokenizer = null;
// Hover-lookup cache, capped: an evening of reading used to grow this without
// bound (each entry holds ~12 full dictionary entries). Map iterates in insertion
// order, so evicting the first key is FIFO — good enough here.
const LOOKUP_CACHE_MAX = 500;
let lookupCache = new Map();
function cacheLookup(key, val) {
  if (lookupCache.size >= LOOKUP_CACHE_MAX)
    lookupCache.delete(lookupCache.keys().next().value);
  lookupCache.set(key, val);
}

/* ---- study preferences (persisted): Anki deck, hide-names, furigana mode -- */
const STUDY_KEY = "vntex-study";
const STUDY_DEFAULTS = { deck: "Down the Rabbit Hole", hideNames: false, furi: "off" };
let study;
try { study = Object.assign({}, STUDY_DEFAULTS, JSON.parse(localStorage.getItem(STUDY_KEY)) || {}); }
catch (_) { study = Object.assign({}, STUDY_DEFAULTS); }
study.furi = (study.furi === "all" || study.furi === "unknown") ? "all" : "off";
function saveStudy() {
  try { localStorage.setItem(STUDY_KEY, JSON.stringify(study)); } catch (_) {}
}

/* ---- session persistence + reading stats ------------------------------- */
const SESSION_KEY = "vntex-session";
let sessionChars = 0;    // characters read since this page loaded (drives 字/時)
let sessionStart = 0;    // first line's timestamp this page-load
let restoring = false;   // true while replaying saved lines (no save/stat churn)

function savedLines() {
  return [...linesEl.children].map(l => l.dataset.raw).filter(Boolean);
}
function saveSession() {
  if (restoring) return;
  try { localStorage.setItem(SESSION_KEY, JSON.stringify(savedLines())); } catch (_) {}
}
const statsEl = document.getElementById("stats");
function bumpStats(text) {
  if (restoring) return;
  const chars = text.replace(/\s/g, "").length;
  if (!sessionStart) sessionStart = Date.now();
  sessionChars += chars;
  const hours = (Date.now() - sessionStart) / 3600000;
  const rate = hours > 0.005 ? Math.round(sessionChars / hours) : 0;
  statsEl.textContent = sessionChars.toLocaleString() + " chars" +
                        (rate ? " · " + rate.toLocaleString() + " chars/hr" : "");
}

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
const REASON = {
  "-た": "past", "-て": "-te form", "-ば": "conditional", "-たら": "conditional (tara)",
  "-たり": "-tari", "-く": "adverbial", "-さ": "-sa nominal", "-ず": "without doing",
  "-ぬ": "negative (archaic)", "-ん": "negative (casual)", "-ゃ": "contraction",
  "-ちゃ": "contraction (-cha)", "continuative": "masu stem", "-まい": "won't/probably not",
  "potential or passive": "potential or passive",
};
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

/* ---- romaji -> hiragana (for the manual lookup box) --------------------- */
const ROMAJI = {
  // digraphs first (longest-match wins)
  kya:"きゃ",kyu:"きゅ",kyo:"きょ",gya:"ぎゃ",gyu:"ぎゅ",gyo:"ぎょ",
  sha:"しゃ",shu:"しゅ",sho:"しょ",sya:"しゃ",syu:"しゅ",syo:"しょ",
  cha:"ちゃ",chu:"ちゅ",cho:"ちょ",tya:"ちゃ",tyu:"ちゅ",tyo:"ちょ",
  ja:"じゃ",ju:"じゅ",jo:"じょ",jya:"じゃ",jyu:"じゅ",jyo:"じょ",
  nya:"にゃ",nyu:"にゅ",nyo:"にょ",hya:"ひゃ",hyu:"ひゅ",hyo:"ひょ",
  bya:"びゃ",byu:"びゅ",byo:"びょ",pya:"ぴゃ",pyu:"ぴゅ",pyo:"ぴょ",
  mya:"みゃ",myu:"みゅ",myo:"みょ",rya:"りゃ",ryu:"りゅ",ryo:"りょ",
  shi:"し",chi:"ち",tsu:"つ",
  ka:"か",ki:"き",ku:"く",ke:"け",ko:"こ",ga:"が",gi:"ぎ",gu:"ぐ",ge:"げ",go:"ご",
  sa:"さ",si:"し",su:"す",se:"せ",so:"そ",za:"ざ",zi:"じ",zu:"ず",ze:"ぜ",zo:"ぞ",
  ta:"た",ti:"ち",tu:"つ",te:"て",to:"と",da:"だ",de:"で",do:"ど",ji:"じ",
  na:"な",ni:"に",nu:"ぬ",ne:"ね",no:"の",
  ha:"は",hi:"ひ",hu:"ふ",fu:"ふ",he:"へ",ho:"ほ",
  ba:"ば",bi:"び",bu:"ぶ",be:"べ",bo:"ぼ",pa:"ぱ",pi:"ぴ",pu:"ぷ",pe:"ぺ",po:"ぽ",
  ma:"ま",mi:"み",mu:"む",me:"め",mo:"も",ya:"や",yu:"ゆ",yo:"よ",
  ra:"ら",ri:"り",ru:"る",re:"れ",ro:"ろ",wa:"わ",wo:"を",vu:"ゔ",
  a:"あ",i:"い",u:"う",e:"え",o:"お",n:"ん","-":"ー","'":"",
};
function romajiToKana(s) {
  s = s.toLowerCase();
  let out = "", i = 0;
  while (i < s.length) {
    const c = s[i];
    // sokuon: doubled consonant (kk, tt, pp…) or the t in "tch" (matcha)
    if (c === s[i + 1] && "bcdfghjklmpqrstvwz".includes(c)) { out += "っ"; i++; continue; }
    if (c === "t" && s.slice(i + 1, i + 3) === "ch") { out += "っ"; i++; continue; }
    let matched = false;
    for (const len of [3, 2, 1]) {
      const chunk = s.slice(i, i + len);
      // bare "n" only when NOT starting a syllable (na/ni/nya… match longer first,
      // but "n" before a vowel/y must not swallow the syllable's consonant)
      if (chunk === "n" && i + 1 < s.length && "aiueoy".includes(s[i + 1])) continue;
      if (ROMAJI[chunk] !== undefined) { out += ROMAJI[chunk]; i += len; matched = true; break; }
    }
    if (!matched) { out += c; i++; }   // pass anything unconvertible through
  }
  return out;
}

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

      if (study.furi !== "off" && hasKanji(surface) && t.reading && t.reading !== "*") {
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

// The best single line obtainable from two reads of the same on-screen text
// (mirror of ocr.py _merge_reads): containment picks the fuller read — covers
// end-growth した→した。, front-growth 油断…→……く、油断…, shorter re-reads
// and exact dups — and a substantial head/tail overlap (≥ half the shorter
// read) splices reads that each missed a different end. null = different lines.
function mergeReads(a, b) {
  if (a.includes(b)) return a;
  if (b.includes(a)) return b;
  const lo = Math.max(4, Math.ceil(Math.min(a.length, b.length) / 2));
  for (let k = Math.min(a.length, b.length); k >= lo; k--) {
    if (a.endsWith(b.slice(0, k))) return a + b.slice(k);
    if (b.endsWith(a.slice(0, k))) return b + a.slice(k);
  }
  return null;
}

function addLine(text) {
  text = (text || "").replace(/\r/g, "").trim();
  if (!text) return;
  // Reconcile with the previous line: OCR re-reads the same on-screen text as
  // it stabilises (typewriter, late maru, Windows finding the leading ……く、
  // a frame late), the SSE stream replays the last line on reconnect, and NVL
  // games append to the same screen. If the new text merges with the last
  // line, swap the merged version in place instead of stacking a duplicate.
  const last = linesEl.lastElementChild;
  let statsText = text;
  if (last) {
    const merged = mergeReads(last.dataset.raw, text);
    if (merged === last.dataset.raw) return;   // nothing new (dup / shorter re-read)
    if (merged) {
      // Stats count only the newly added chars (any slice of the right length).
      statsText = merged.slice(last.dataset.raw.length);
      text = merged;
      last.remove();
    }
  }
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

  // Keep the DOM bounded. Known cosmetic quirk: a popup pinned to a word in the
  // evicted line keeps showing until closed — harmless, not worth tracking.
  while (linesEl.children.length > 300) linesEl.removeChild(linesEl.firstChild);

  bumpStats(statsText);
  saveSession();
}

// Restore the previous session's lines (tokenizer-independent: rebuildSentences
// re-renders them once kuromoji is ready).
(function restoreSession() {
  let lines;
  try { lines = JSON.parse(localStorage.getItem(SESSION_KEY)) || []; } catch (_) { lines = []; }
  if (!lines.length) return;
  restoring = true;
  lines.forEach(addLine);
  restoring = false;
})();

/* ---- dictionary lookup + popup (longest-match scan) -------------------- */
let pinned = false;

async function fetchScan(text, pos, reading, base, surface) {
  // /scan only ever looks at the first 32 chars — key and send exactly that,
  // so the same word hit in different lines shares one cache entry (client
  // FIFO and the server's lru_cache both).
  text = text.slice(0, 32);
  const key = [pos || "", reading || "", base || "", surface || "", text].join("|");
  if (lookupCache.has(key)) return lookupCache.get(key);
  try {
    const r = await fetch("/scan?text=" + encodeURIComponent(text) +
                          "&pos=" + encodeURIComponent(pos || "") +
                          "&reading=" + encodeURIComponent(reading || "") +
                          "&base=" + encodeURIComponent(base || "") +
                          "&surface=" + encodeURIComponent(surface || ""));
    const res = (await r.json()).candidates || [];
    cacheLookup(key, res);
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

/* ---- kanji info card (KANJIDIC2, /kanji route) --------------------------- */
const kanjiCache = new Map();
async function toggleKanjiCard(entryDiv, ch) {
  if (entryDiv.dataset.kjBusy) return;   // a fetch is in flight — ignore rapid re-clicks
  const old = entryDiv.querySelector(".kanji-card");
  const sameChar = old && old.dataset.ch === ch;
  if (old) old.remove();
  if (sameChar) return;                       // second click on the same kanji closes it
  let info = kanjiCache.get(ch);
  let fetchFailed = false;
  if (info === undefined) {
    entryDiv.dataset.kjBusy = "1";
    try {
      const r = await fetch("/kanji?c=" + encodeURIComponent(ch));
      if (!r.ok) throw new Error(String(r.status));   // 404 = server running old code
      info = (await r.json()).info;
      kanjiCache.set(ch, info);                       // only cache real answers
    } catch (_) {
      info = null;
      fetchFailed = true;
    } finally {
      delete entryDiv.dataset.kjBusy;
    }
  }
  const card = document.createElement("div");
  card.className = "kanji-card";
  card.dataset.ch = ch;
  if (!info) {
    card.textContent = fetchFailed
      ? "kanji lookup failed — quit every app window/terminal and start it again "
        + "(an old server instance may still be running)"
      : "no kanji data — run `python setup.py` to add KANJIDIC2";
  } else {
    const big = document.createElement("span");
    big.className = "kj-big";
    big.textContent = ch;
    const body = document.createElement("div");
    body.className = "kj-body";
    const mean = document.createElement("div");
    mean.className = "kj-mean";
    mean.textContent = (info.meanings || []).join(", ");
    body.appendChild(mean);
    const addReadings = (label, list) => {
      if (!list || !list.length) return;
      const d = document.createElement("div");
      d.className = "kj-read";
      const b = document.createElement("b");
      b.textContent = label + " ";
      d.append(b, document.createTextNode(list.join("、")));
      body.appendChild(d);
    };
    addReadings("音", info.on);
    addReadings("訓", info.kun);
    const chips = document.createElement("div");
    chips.className = "kj-chips";
    const chip = (txt, title) => {
      const s = document.createElement("span");
      s.textContent = txt;
      if (title) s.title = title;
      chips.appendChild(s);
    };
    if (info.strokes) chip(info.strokes + " strokes");
    if (info.grade) chip("grade " + info.grade, "school grade in which the kanji is taught");
    if (info.jlpt) chip("JLPT " + info.jlpt, "old 4-level JLPT scale (1 = hardest)");
    if (info.freq) chip("№" + info.freq.toLocaleString(), "newspaper frequency rank");
    body.appendChild(chips);
    card.append(big, body);
  }
  entryDiv.querySelector(".head").after(card);
}

/* ---- Anki export (via the server's /anki proxy to AnkiConnect) ---------- */
const ANKI_MODEL = "Down the Rabbit Hole";
const ankiDecksReady = new Set();
let ankiModelReady = false;

function ankiDeck() { return (study.deck || "").trim() || STUDY_DEFAULTS.deck; }

async function anki(action, params) {
  const r = await fetch("/anki", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, version: 6, params: params || {} }),
  });
  const j = await r.json();
  if (j.error) throw new Error(j.error);
  return j.result;
}

// Create our deck + note type once, so export works with zero Anki-side setup.
async function ensureAnki() {
  if (!ankiModelReady) {
    const models = await anki("modelNames");
    if (!models.includes(ANKI_MODEL)) {
      await anki("createModel", {
        modelName: ANKI_MODEL,
        inOrderFields: ["Word", "Reading", "Meaning", "Sentence"],
        cardTemplates: [{
          Name: "Card 1",
          Front: '<div style="font-size:40px">{{Word}}</div><div>{{Sentence}}</div>',
          Back: '<div style="font-size:40px">{{Word}}</div>{{Reading}}<hr>{{Meaning}}<hr>{{Sentence}}',
        }],
      });
    }
    ankiModelReady = true;
  }
  const deck = ankiDeck();
  if (!ankiDecksReady.has(deck)) {
    await anki("createDeck", { deck });   // no-op if it exists
    ankiDecksReady.add(deck);
  }
}

// One-time toolbar indicator: is Anki (with AnkiConnect) reachable right now?
(async () => {
  const el = document.getElementById("ankiInd");
  if (!el) return;
  try {
    const v = await anki("version");
    el.textContent = "Anki ✓";
    el.classList.add("ok");
    el.title = "AnkiConnect connected (v" + v + ") — ★ in the popup adds a card";
  } catch (_) {
    el.textContent = "Anki –";
    el.title = "Anki not found — start Anki with the AnkiConnect add-on to export cards";
  }
  setTimeout(() => el.classList.add("fade"), 8000);
})();

async function addToAnki(c, sentence, btn) {
  const entry = c.entry;
  const word = (entry.k && entry.k[0]) || (entry.r && entry.r[0]) || c.matched;
  const reading = c.mr || (entry.r && entry.r[0]) || "";
  const meaning = (entry.s || []).slice(0, 3)
    .map((s, i) => (i + 1) + ". " + s.gloss.join("; ")).join("<br>");
  try {
    await ensureAnki();
    // Screenshot of the whole game window (OCR region's window, or the
    // hooked game) rides along on the Sentence field. Best effort — a null
    // /snap (clipboard-only session) just means a text-only card.
    let picture;
    try {
      const j = await (await fetch("/snap")).json();
      if (j.data) picture = [{ data: j.data, filename: "rabbit-hole-" + Date.now() + ".png",
                               fields: ["Sentence"] }];
    } catch (_) {}
    await anki("addNote", {
      note: {
        deckName: ankiDeck(), modelName: ANKI_MODEL,
        fields: { Word: word, Reading: reading, Meaning: meaning, Sentence: sentence || "" },
        options: { allowDuplicate: false },
        ...(picture ? { picture } : {}),
      },
    });
    btn.textContent = "✓";
    btn.title = "added to Anki";
  } catch (e) {
    if (/duplicate/i.test(e.message)) {
      btn.textContent = "dup";
      btn.title = "already in your Anki collection";
    } else {
      btn.textContent = "✗";
      btn.title = "Anki: " + e.message;   // hover the button to see why
    }
  }
  setTimeout(() => { btn.textContent = "★"; btn.title = "add to Anki"; }, 1800);
}

function renderCandidate(c, sentence) {
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
  // each kanji in the headword is clickable -> KANJIDIC2 mini card (pin to click)
  for (const ch of primary) {
    if (/[一-龯々]/.test(ch)) {
      const k = document.createElement("span");
      k.className = "kj";
      k.textContent = ch;
      k.title = "kanji info";
      k.addEventListener("click", ev => {
        ev.stopPropagation();
        toggleKanjiCard(div, ch);
      });
      hw.appendChild(k);
    } else {
      hw.appendChild(document.createTextNode(ch));
    }
  }
  head.appendChild(hw);
  const readingShown = c.mr || (entry.r && entry.r[0]);
  if (!allUk && hasKanji && readingShown) {
    head.appendChild(plainReading(readingShown));
  }
  // VN-frequency chip: how common this word is in visual novels
  if (typeof entry.vr === "number") {
    const f = document.createElement("span");
    f.className = "freq" + (entry.vr <= 6600 ? " hot" : "");
    f.textContent = "№" + entry.vr.toLocaleString();
    f.title = "visual-novel frequency rank" + (entry.vr <= 6600 ? " (common — worth learning)" : "");
    head.appendChild(f);
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

  if (c.kind !== "name" && entry.r && entry.r.length) {
    const audio = document.createElement("button");
    audio.className = "mini";
    audio.textContent = "♪";   // basic-plane char — WebView2 renders emoji as tofu
    audio.title = "play pronunciation (JapanesePod101 — needs internet)";
    audio.setAttribute("aria-label", "play pronunciation of " + primary);
    audio.addEventListener("click", ev => {
      ev.stopPropagation();
      const k = (entry.k && entry.k[0]) || entry.r[0];
      const a = new Audio("/audio?" + new URLSearchParams({ k, r: c.mr || entry.r[0] }));
      audio.textContent = "…";
      const done = ok => {
        audio.textContent = ok ? "♪" : "✗";
        if (!ok) setTimeout(() => (audio.textContent = "♪"), 1500);
      };
      a.onended = () => done(true);          // reset once it finishes playing
      a.onerror = () => done(false);         // 404 = JPod101 has no recording
      a.play().then(() => (audio.textContent = "♪")).catch(() => done(false));
    });
    head.appendChild(audio);
  }

  if (c.kind !== "name") {
    const star = document.createElement("button");
    star.className = "mini";
    star.textContent = "★";
    star.title = "add to Anki";
    star.setAttribute("aria-label", "add " + primary + " to Anki");
    star.addEventListener("click", ev => {
      ev.stopPropagation();
      addToAnki(c, sentence, star);
    });
    head.appendChild(star);
  }
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

// Shared popup body used by the hover/pin path and the manual lookup box.
// opts: {sentence, emptyLabel, rerender} — rerender is called when the
// hide-names toggle flips, so the same lookup re-renders with the new filter.
function renderPopupBody(cands, opts) {
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

  // Hide-names filter: name clusters can be noisy. Only filter when a real word
  // remains — a pure-name token still shows its names.
  const nameCount = cands.filter(c => c.kind === "name").length;
  const filterable = nameCount > 0 && nameCount < cands.length;
  const shown = (study.hideNames && filterable)
    ? cands.filter(c => c.kind !== "name") : cands;

  if (!shown.length) {
    const e = document.createElement("div");
    e.className = "empty";
    e.textContent = opts.emptyText || `No entry for 「${opts.emptyLabel}」`;
    popup.appendChild(e);
  } else {
    shown.forEach(c => popup.appendChild(renderCandidate(c, opts.sentence)));
  }
  if (filterable) {
    const nt = document.createElement("div");
    nt.className = "name-toggle";
    nt.textContent = study.hideNames
      ? `${nameCount} name${nameCount > 1 ? "s" : ""} hidden — show`
      : "hide names";
    nt.addEventListener("click", ev => {
      ev.stopPropagation();
      study.hideNames = !study.hideNames;
      saveStudy();
      opts.rerender();
    });
    popup.appendChild(nt);
  }
  return shown.length;
}

function finishPopup(anchor, showFoot) {
  // Peek footer: teach the two least-discoverable features (and signal scrollability).
  let foot = null;
  if (showFoot) {
    foot = document.createElement("div");
    foot.className = "popup-foot";
    popup.appendChild(foot);
  }
  popup.scrollTop = 0;   // new word -> start at the top, don't inherit the last scroll
  positionPopup(anchor);
  if (foot) {
    const more = popup.scrollHeight > popup.clientHeight + 1;
    foot.textContent = more ? "click to pin · scroll for more ↓" : "click to pin";
  }
  popup.classList.remove("hidden");
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
  // A newer call superseded this one while the lookup was in flight — drop it.
  // Guarding on seq alone (not `!pinned`) also stops a stale hover response from
  // silently overwriting a popup the user just pinned; pin()'s own call is always
  // the newest seq, so pins still render.
  if (seq !== lookupSeq) return;

  const n = renderPopupBody(cands, {
    sentence: line.dataset.raw,
    emptyLabel: target.dataset.surface || target.dataset.term,
    rerender: () => showScanPopup(target),
  });
  finishPopup(target, !pinned && n > 0);
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
  if (!t || pinned) return;
  // Cancel any pending hide BEFORE the same-word check: re-entering the word you
  // just left (within the 180ms hide delay) used to early-return with the timer
  // still armed, so the popup vanished under a hovering cursor.
  cancelHide();
  if (t === activeWord) return;   // skip re-render of the same word
  activeWord = t;
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

/* ---- embedded Textractor: attach panel ---------------------------------- */
const attachBtn = document.getElementById("attachBtn");
const hookPanel = document.getElementById("hookPanel");
const hookMsg = document.getElementById("hookMsg");
const procList = document.getElementById("procList");
const hookList = document.getElementById("hookList");
const detachBtn = document.getElementById("detachBtn");
let hookPoll = null;

async function jpost(url, body) {
  return (await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                             body: JSON.stringify(body || {}) })).json();
}

function renderHookState(st) {
  detachBtn.classList.toggle("hidden", !st.attached);
  attachBtn.classList.toggle("active", !!(st.attached && st.picked));
  if (!st.attached) {
    hookMsg.textContent = "Pick the game to hook (it must already be running):";
    hookList.innerHTML = "";
    // A poll answered while we were still attached can land late and wipe the
    // process list a detach just refilled — refill whenever it's missing.
    if (!procList.children.length) refreshProcesses();
    return;
  }
  procList.innerHTML = "";
  attachBtn.title = "Attached: " + (st.exe || st.pid);
  if (!st.hooks.length) {
    hookMsg.textContent = `Attached to ${st.exe}. Advance the game a line or two — ` +
                          "text channels appear here as they speak.";
    hookList.innerHTML = "";
    return;
  }
  hookMsg.textContent = st.picked
    ? `Attached to ${st.exe} — streaming. Pick another channel if the text looks wrong:`
    : `Attached to ${st.exe}. Click the channel showing the game's dialogue:`;
  hookList.innerHTML = "";
  st.hooks.forEach(h => {
    const b = document.createElement("button");
    b.className = "hook-item" + (h.key === st.picked ? " active" : "");
    const name = document.createElement("span");
    name.className = "hook-name";
    name.textContent = (h.key.split(":").pop() || h.key) + " ×" + h.count;
    const prev = document.createElement("span");
    prev.className = "hook-prev";
    prev.textContent = h.last || "(no text yet)";
    b.append(name, prev);
    b.addEventListener("click", async () => renderHookState(await jpost("/hookpick", { key: h.key })));
    hookList.appendChild(b);
  });
}

let procFetching = false;
async function refreshProcesses() {
  if (procFetching) return;
  procFetching = true;
  let j;
  try { j = await (await fetch("/processes")).json(); }
  catch (_) { j = null; }
  finally { procFetching = false; }
  if (!j) {   // server unreachable — leave the panel as-is, the 1s poll retries
    hookMsg.textContent = "Can't reach the server — is it still running?";
    return;
  }
  procList.innerHTML = "";
  if (!j.available) {
    hookMsg.textContent = "Textractor isn't downloaded yet. Run:  python setup.py --textractor";
    return;
  }
  j.processes.forEach(p => {
    const b = document.createElement("button");
    b.className = "proc-item";
    const name = document.createElement("b");
    name.textContent = p.exe || ("pid " + p.pid);
    const title = document.createElement("span");
    title.textContent = p.title;
    b.append(name, title);
    b.addEventListener("click", async () => {
      hookMsg.textContent = "Attaching…";
      const st = await jpost("/attach", { pid: p.pid });
      if (st.error) hookMsg.textContent = "Could not attach: " + st.error;
      else renderHookState(st);
    });
    procList.appendChild(b);
  });
}

/* OCR fallback controls (bottom of the Attach panel) */
const ocrMsg = document.getElementById("ocrMsg");
const ocrArea = document.getElementById("ocrArea");
const ocrToggle = document.getElementById("ocrToggle");
const OCR_HINT = "For games that won't hook — reads text straight off the screen. " +
                 "Pick just the text box, not the whole window.";

function renderOcrState(st) {
  ocrToggle.textContent = (st.running || st.starting) ? "Stop OCR" : "Start OCR";
  ocrToggle.classList.toggle("active", !!(st.running || st.starting));
  if (st.error) {
    ocrMsg.textContent = "OCR: " + st.error;
  } else if (st.starting) {
    ocrMsg.textContent = "OCR starting… (first start loads the engine, give it a moment)";
  } else if (st.running) {
    const r = st.region;
    ocrMsg.textContent = `OCR watching ${r.w}×${r.h} px via ${st.engine} — new text ` +
                         "appears here as the game shows it.";
  } else if (st.region) {
    const r = st.region;
    ocrMsg.textContent = `Area saved (${r.w}×${r.h} px). Start OCR when the game is visible.`;
  } else {
    ocrMsg.textContent = OCR_HINT;
  }
}

async function refreshOcr() {
  try { renderOcrState(await (await fetch("/ocr")).json()); } catch (_) {}
}

ocrArea.addEventListener("click", async () => {
  ocrMsg.textContent = "Drag a box over the game's text on the dimmed screen (Esc cancels)…";
  try { renderOcrState(await jpost("/ocr/region")); } catch (_) { refreshOcr(); }
});
ocrToggle.addEventListener("click", async () => {
  const stopping = ocrToggle.classList.contains("active");
  try {
    const st = await jpost(stopping ? "/ocr/stop" : "/ocr/start");
    renderOcrState(st);
    if (st.error && !stopping) ocrMsg.textContent = "OCR: " + st.error;
  } catch (_) { refreshOcr(); }
});

/* Emulator hooking via Agent (bottom of the Attach panel) */
const agentMsg = document.getElementById("agentMsg");
const agentLaunch = document.getElementById("agentLaunch");

function renderAgentState(st) {
  agentLaunch.classList.toggle("hidden", !st.installed);
  agentLaunch.classList.toggle("active", !!st.connected);
  if (!st.installed) {
    agentMsg.textContent = "Emulated games (PPSSPP, PCSX2, Vita3K…) hook through Agent. " +
                           "Not downloaded yet — run:  python setup.py --agent";
  } else if (st.connected) {
    agentMsg.textContent = "Agent connected — attach to your emulator in Agent's window " +
                           "and its text appears here.";
  } else if (st.running) {
    agentMsg.textContent = "Agent is open. In its window: update scripts, pick your game's " +
                           "script, drag the crosshair onto the emulator, Attach.";
  } else {
    agentMsg.textContent = "Emulated games (PPSSPP, PCSX2, Vita3K, yuzu…) hook through " +
                           "Agent — launch it, pick your game's script, attach.";
  }
}

async function refreshAgent() {
  try { renderAgentState(await (await fetch("/agent")).json()); } catch (_) {}
}

agentLaunch.addEventListener("click", async () => {
  try {
    const st = await jpost("/agent/start");
    renderAgentState(st);
    if (st.error) agentMsg.textContent = st.error;
  } catch (_) { refreshAgent(); }
});

function toggleHookPanel(show) {
  const hidden = show === undefined ? !hookPanel.classList.contains("hidden") : !show;
  hookPanel.classList.toggle("hidden", hidden);
  clearInterval(hookPoll);
  if (!hidden) {
    (async () => {
      const st = await (await fetch("/hooks")).json();
      renderHookState(st);
      if (!st.attached) refreshProcesses();
    })();
    refreshOcr();
    refreshAgent();
    hookPoll = setInterval(async () => {
      renderHookState(await (await fetch("/hooks")).json());
      refreshOcr();   // engine startup / errors surface without reopening the panel
      refreshAgent(); // "connected" flips live once Agent's websocket answers
    }, 1000);
  }
}
attachBtn.addEventListener("click", e => { e.stopPropagation(); toggleHookPanel(); });
document.getElementById("hookClose").addEventListener("click", () => toggleHookPanel(false));
detachBtn.addEventListener("click", async () => {
  renderHookState(await jpost("/detach"));
  refreshProcesses();
});
document.addEventListener("click", e => {
  if (!hookPanel.classList.contains("hidden") && !hookPanel.contains(e.target) && e.target !== attachBtn)
    toggleHookPanel(false);
});
// reflect an existing attachment on load (e.g. after a reload mid-session)
(async () => {
  try {
    const st = await (await fetch("/hooks")).json();
    if (st.attached) renderHookState(st);
  } catch (_) {}
})();

/* ---- clipboard stream (SSE) ------------------------------------------- */
function connectStream() {
  const es = new EventSource("/events");
  es.onopen = () => {
    // don't flash "ready" over a paused session
    if (!pauseBtn.classList.contains("active")) setStatus("ready", "Ready");
  };
  es.onmessage = ev => {
    try { addLine(JSON.parse(ev.data).text); } catch (_) {}
  };
  es.onerror = () => {
    if (!pauseBtn.classList.contains("active")) setStatus("connecting", "Disconnected — reconnecting…");
    // EventSource auto-reconnects.
  };
}
connectStream();

/* ---- toolbar ----------------------------------------------------------- */
const pauseBtn = document.getElementById("pauseBtn");
async function refreshPause() {
  const j = await (await fetch("/state")).json();
  applyPause(j.paused);
  if (j.lan_url) {   // --lan: show the phone QR in Settings
    document.getElementById("lanRow").classList.remove("hidden");
    document.getElementById("lanQr").src = "/qr";
    document.getElementById("lanUrl").textContent = j.lan_url;
  }
}
function applyPause(paused) {
  pauseBtn.classList.toggle("active", paused);
  pauseBtn.textContent = paused ? "Resume" : "Pause";
  setStatus(paused ? "paused" : "ready", paused ? "Paused" : "Ready");
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
  study.furi = study.furi === "off" ? "all" : "off";
  saveStudy();
  furiBtn.classList.toggle("active", study.furi !== "off");
  rebuildSentences();
});
furiBtn.classList.toggle("active", study.furi !== "off");
if (study.furi !== "off") rebuildSentences();

// Manual lookup box: type/paste a word (romaji works — romajiToKana above),
// Enter -> pinned popup. English works too: ASCII that isn't valid romaji
// (spaces, letters romajiToKana can't place) searches the JMdict glosses via
// /search, and valid romaji that finds nothing in Japanese ("sake" typo'd,
// "ninja gaiden") falls back to the same English search.
async function scanJapanese(text) {
  // borrow the tokenizer's analysis of the first token for better ranking
  let pos = "", reading = "", base = "", surface = "";
  if (tokenizer) {
    const t = tokenizer.tokenize(text)[0];
    if (t) {
      pos = t.pos || "";
      reading = (t.reading && t.reading !== "*") ? t.reading : "";
      base = (t.basic_form && t.basic_form !== "*") ? t.basic_form : "";
      surface = t.surface_form;
    }
  }
  return fetchScan(text, pos, reading, base, surface);
}

async function searchEnglish(q) {
  const key = "en|" + q.toLowerCase();
  if (lookupCache.has(key)) return lookupCache.get(key);
  let out;
  try {
    const j = await (await fetch("/search?q=" + encodeURIComponent(q))).json();
    out = { cands: j.candidates || [], error: j.error || null };
  } catch (_) {
    out = { cands: [], error: null };
  }
  cacheLookup(key, out);
  return out;
}

const lookupBox = document.getElementById("lookupBox");
lookupBox.addEventListener("keydown", async e => {
  if (e.key !== "Enter") return;
  const text = lookupBox.value.trim();
  if (!text) return;
  let cands = null, label = text, emptyText = null;
  if (/^[a-zA-Z'-]+$/.test(text)) {
    const kana = romajiToKana(text);
    if (!/[a-zA-Z]/.test(kana)) {           // valid romaji -> Japanese first
      cands = await scanJapanese(kana);
      label = kana;
    }
  }
  if (!cands || !cands.length) {
    if (/^[\x20-\x7E]+$/.test(text) && /[a-zA-Z]/.test(text)) {   // English
      const r = await searchEnglish(text);
      cands = r.cands;
      label = text;
      if (r.error) emptyText = r.error;
    } else if (!cands) {                     // Japanese input, as before
      cands = await scanJapanese(text);
    }
  }
  pinned = true;
  popup.classList.add("pinned");
  activeWord = null;
  const rerender = () => {
    renderPopupBody(cands, { sentence: label, emptyLabel: label, emptyText, rerender });
    finishPopup(lookupBox, false);
  };
  rerender();
});

/* ---- find in lines (Ctrl+F) --------------------------------------------- */
// Searches the raw text of every kept line (300 cap + whatever the session
// restored). Kana-insensitive: query and lines both normalize katakana ->
// hiragana, so さくら finds サクラ. Line-level highlight, Enter/Shift+Enter
// cycle matches (starting from the newest).
const findBar = document.getElementById("findBar");
const findInput = document.getElementById("findInput");
const findCountEl = document.getElementById("findCount");
let findHits = [], findIdx = -1;

function clearFind() {
  document.querySelectorAll(".line.find-hit, .line.find-cur")
    .forEach(l => l.classList.remove("find-hit", "find-cur"));
  findHits = []; findIdx = -1;
  findCountEl.textContent = "";
}
function jumpFind(dir, startIdx) {
  if (!findHits.length) { findCountEl.textContent = "0"; return; }
  if (findHits[findIdx]) findHits[findIdx].classList.remove("find-cur");
  findIdx = startIdx !== undefined ? startIdx
          : (findIdx + dir + findHits.length) % findHits.length;
  const el = findHits[findIdx];
  el.classList.add("find-cur");
  el.scrollIntoView({ block: "center" });
  findCountEl.textContent = (findIdx + 1) + "/" + findHits.length;
}
function runFind() {
  clearFind();
  const q = toHiragana(findInput.value.trim().toLowerCase());
  if (!q) return;
  findHits = [...linesEl.children]
    .filter(l => toHiragana((l.dataset.raw || "").toLowerCase()).includes(q));
  findHits.forEach(l => l.classList.add("find-hit"));
  jumpFind(0, findHits.length - 1);   // newest match first — "an hour ago" is scrolled up from there
}
function openFind() {
  findBar.classList.remove("hidden");
  findInput.focus();
  findInput.select();
  if (findInput.value) runFind();
}
function closeFind() {
  findBar.classList.add("hidden");
  clearFind();
}
findInput.addEventListener("input", runFind);
findInput.addEventListener("keydown", e => {
  if (e.key === "Enter") { e.preventDefault(); jumpFind(e.shiftKey ? -1 : 1); }
  else if (e.key === "Escape") { e.stopPropagation(); closeFind(); }
});
document.getElementById("findPrev").addEventListener("click", () => jumpFind(-1));
document.getElementById("findNext").addEventListener("click", () => jumpFind(1));
document.getElementById("findClose").addEventListener("click", closeFind);

// "logs": the same query against every past session on disk (/logsearch over
// logs/*.txt) — Ctrl+F only sees the 300 lines the DOM keeps. Results open in
// a pinned popup; clicking one loads the line into the reader, hoverable again.
document.getElementById("findLogs").addEventListener("click", async () => {
  const q = findInput.value.trim();
  if (!q) { findInput.focus(); return; }
  let j;
  try { j = await (await fetch("/logsearch?q=" + encodeURIComponent(q))).json(); }
  catch (_) { return; }
  pinned = true;
  popup.classList.add("pinned");
  activeWord = null;
  popup.innerHTML = "";
  const close = document.createElement("button");
  close.className = "pin-close";
  close.textContent = "×";
  close.title = "close";
  close.setAttribute("aria-label", "close");
  close.addEventListener("click", unpin);
  popup.appendChild(close);
  const hits = j.hits || [];
  const head = document.createElement("div");
  head.className = "log-head";
  head.textContent = hits.length
    ? `「${q}」 in past sessions — ${hits.length}${j.truncated ? "+" : ""} line${hits.length > 1 ? "s" : ""}, click one to load it:`
    : `「${q}」 isn't in any past session (logs/)`;
  popup.appendChild(head);
  let lastFile = null;
  hits.forEach(h => {
    if (h.file !== lastFile) {
      lastFile = h.file;
      const f = document.createElement("div");
      f.className = "log-file";
      f.textContent = h.file;
      popup.appendChild(f);
    }
    const d = document.createElement("div");
    d.className = "log-hit";
    d.textContent = h.line;
    d.title = "load this line into the reader";
    d.addEventListener("click", ev => { ev.stopPropagation(); addLine(h.line); });
    popup.appendChild(d);
  });
  finishPopup(findBar, false);
});
document.addEventListener("keydown", e => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "f") {
    e.preventDefault();   // replace the browser's find — it can't see dataset.raw across ruby/furigana
    openFind();
  }
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
  saveSession();
});

// Clear all lines — asks once (inline) before wiping everything.
const clearAllBtn = document.getElementById("clearAllBtn");
let clearArmed = false, clearTimer = null;
function disarmClear() {
  clearArmed = false; clearTimeout(clearTimer);
  clearAllBtn.classList.remove("confirm");
  clearAllBtn.textContent = "Clear";
  clearAllBtn.title = "Clear all lines";
}
clearAllBtn.addEventListener("click", () => {
  if (!clearArmed) {                       // first click: arm + ask
    clearArmed = true;
    clearAllBtn.classList.add("confirm");
    clearAllBtn.textContent = "Clear?";
    clearAllBtn.title = "Click again to clear everything";
    clearTimer = setTimeout(disarmClear, 2500);
    return;
  }
  disarmClear();                           // second click: do it
  if (!popup.classList.contains("hidden")) unpin();
  linesEl.innerHTML = "";
  hint.classList.remove("gone");
  // The stats measure THIS session's reading — clearing it resets the count.
  sessionChars = 0;
  sessionStart = 0;
  statsEl.textContent = "";
  saveSession();
});

// Export the session: the server writes exports/rabbit-hole-….txt and reveals it
// in Explorer. (A blob-download <a> doesn't work inside the app window — WebView2
// silently drops it — so the server does the saving for both window and browser.)
const exportBtn = document.getElementById("exportBtn");
exportBtn.addEventListener("click", async () => {
  const lines = savedLines();
  if (!lines.length) return;
  try {
    const j = await jpost("/export", { lines });
    if (j.error) throw new Error(j.error);
    exportBtn.textContent = "Saved ✓";
    exportBtn.title = "saved to " + j.path;
  } catch (e) {
    exportBtn.textContent = "Export ✗";
    exportBtn.title = "export failed: " + e.message;
  }
  setTimeout(() => { exportBtn.textContent = "Export"; }, 2000);
});

/* ---- Study section of the settings panel (Anki deck) --------------------- */
(function initStudyPanel() {
  const deckInput = document.getElementById("deckInput");
  deckInput.value = study.deck;
  deckInput.placeholder = STUDY_DEFAULTS.deck;
  deckInput.addEventListener("change", () => {
    study.deck = deckInput.value.trim() || STUDY_DEFAULTS.deck;
    deckInput.value = study.deck;
    saveStudy();
  });
})();
