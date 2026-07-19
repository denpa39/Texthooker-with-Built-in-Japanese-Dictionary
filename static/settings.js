/* Appearance settings: theme presets, custom colours, font, text size. ----- *
 * Loaded in <head> (blocking) so the saved theme is applied before first paint
 * — no flash. The panel UI is wired up on DOMContentLoaded. Everything persists
 * to localStorage; fonts are system/offline stacks so nothing is fetched.        */
(function () {
  "use strict";

  // A theme = these 6 core colours. Every other shade is derived in CSS via
  // color-mix(), so a preset only needs the palette below. Each is a Wonderland
  // character; listed alphabetically (which also puts the default, Alice, first).
  const PRESETS = {
    "Alice":           { "--bg": "#eef4fb", "--text": "#213243", "--accent": "#3f6fc0", "--accent-2": "#a9762a", "--pos": "#2f7d4a", "--danger": "#c0392b" }, // her blue dress, white pinafore (light)
    "Caterpillar":     { "--bg": "#0d1626", "--text": "#e4e9f3", "--accent": "#5f93de", "--accent-2": "#d98aa8", "--pos": "#82cf93", "--danger": "#e36673" }, // blue hookah smoke, mushroom pink
    "Cheshire":        { "--bg": "#181026", "--text": "#efe6f6", "--accent": "#e84d9c", "--accent-2": "#a982ef", "--pos": "#5fded0", "--danger": "#ff5d7a" }, // magenta stripes, teal grin in the dark
    "Mad Hatter":      { "--bg": "#0f211f", "--text": "#eef1e7", "--accent": "#34c1af", "--accent-2": "#ec9540", "--pos": "#c39be6", "--danger": "#e85f72" }, // teal hat, tea-orange, whimsy purple
    "Queen of Hearts": { "--bg": "#18090c", "--text": "#f4e9e8", "--accent": "#d8243c", "--accent-2": "#e6b84e", "--pos": "#5fae7a", "--danger": "#ff5a52" }, // crimson & royal gold on black
    "White Rabbit":    { "--bg": "#f7f3e8", "--text": "#38322a", "--accent": "#a87f24", "--accent-2": "#c1556f", "--pos": "#5f8742", "--danger": "#b8392b" }, // cream coat, watch gold, pink eyes (light)
  };
  const CORE_VARS = ["--bg", "--text", "--accent", "--accent-2", "--pos", "--danger"];
  // colours exposed as pickers (label -> css var). --furigana has no preset value
  // (it's derived from the theme in CSS); the picker just lets you override it.
  const PICKERS = [
    ["Background", "--bg"], ["Text", "--text"], ["Accent", "--accent"],
    ["Reading", "--accent-2"], ["Tag", "--pos"], ["Furigana", "--furigana"],
  ];
  const FONTS = {
    "Default (Sans)": '"Segoe UI", system-ui, sans-serif',
    "Gothic (JP)":    '"Yu Gothic", "YuGothic", "Meiryo", "Noto Sans JP", sans-serif',
    "Mincho (Serif)": '"Noto Serif JP", "Yu Mincho", "YuMincho", "Hiragino Mincho ProN", "MS Mincho", serif',
    "Rounded":        '"M PLUS Rounded 1c", "Yu Gothic UI", "BIZ UDPGothic", "Hiragino Maru Gothic ProN", sans-serif',
    "Maru":           '"Kosugi Maru", "Yu Gothic UI", "Hiragino Maru Gothic ProN", sans-serif',
    "Monospace":      '"Cascadia Code", "Consolas", "MS Gothic", monospace',
  };
  const DEFAULTS = { preset: "Alice", colors: {}, font: "Default (Sans)", fontSize: 30, align: "left", bold: false, italic: false, vertical: false, lineHeight: 1.9, furiSize: 0.45 };
  const ALIGNS = ["left", "center", "right", "justify"];
  // Per alignment: the block's [left, right] margins. "auto" pushes the block to the
  // opposite edge — so left → flush left, right → flush right, center/justify → centred.
  const ALIGN_MARGIN = { left: ["0", "auto"], center: ["auto", "auto"], right: ["auto", "0"], justify: ["auto", "auto"] };
  const LS_KEY = "vntex-appearance";

  function load() {
    let s;
    try { s = Object.assign({}, DEFAULTS, JSON.parse(localStorage.getItem(LS_KEY)) || {}); }
    catch (_) { s = Object.assign({}, DEFAULTS); }
    if (!PRESETS[s.preset]) s.preset = DEFAULTS.preset;   // retired preset name -> default
    if (!ALIGNS.includes(s.align)) s.align = DEFAULTS.align;
    return s;
  }
  function save(s) {
    try { localStorage.setItem(LS_KEY, JSON.stringify(s)); } catch (_) {}
  }

  // Resolve a CSS colour var (e.g. a color-mix derived value) to a #rrggbb hex,
  // so it can seed an <input type=color> (which only accepts hex).
  function resolveHex(varName) {
    const probe = document.createElement("span");
    probe.style.cssText = "display:none;color:var(" + varName + ")";
    document.body.appendChild(probe);
    const c = getComputedStyle(probe).color;     // "rgb(...)" or "color(srgb ...)"
    probe.remove();
    const hex = (a) => "#" + a.map(n => Math.max(0, Math.min(255, Math.round(n))).toString(16).padStart(2, "0")).join("");
    let m = c.match(/rgba?\(([\d.\s,]+)\)/);
    if (m) return hex(m[1].split(",").slice(0, 3).map(parseFloat));
    m = c.match(/color\(srgb\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)/);
    if (m) return hex([m[1], m[2], m[3]].map(x => parseFloat(x) * 255));
    return "#888888";
  }

  // Apply a settings object to <html> — runs before paint, and on every change.
  function apply(s) {
    const root = document.documentElement.style;
    const preset = PRESETS[s.preset] || PRESETS["Alice"];
    CORE_VARS.forEach(v => root.setProperty(v, preset[v]));
    root.removeProperty("--furigana");                 // default: derived from the theme in CSS
    if (s.colors) for (const v in s.colors) root.setProperty(v, s.colors[v]);  // custom overrides
    root.setProperty("--font-family", FONTS[s.font] || FONTS["Default (Sans)"]);
    root.setProperty("--font-size", (s.fontSize || 30) + "px");
    root.setProperty("--text-align", s.align || "left");
    const [ml, mr] = ALIGN_MARGIN[s.align] || ALIGN_MARGIN.left;
    root.setProperty("--line-ml", ml);
    root.setProperty("--line-mr", mr);
    root.setProperty("--font-weight", s.bold ? "700" : "400");
    root.setProperty("--font-style", s.italic ? "italic" : "normal");
    // Vertical text (tategaki): a class on <html>, the CSS flips the reader.
    document.documentElement.classList.toggle("vertical", !!s.vertical);
    root.setProperty("--line-height", String(s.lineHeight || 1.9));
    root.setProperty("--furi-size", (s.furiSize || 0.45) + "em");
  }

  let settings = load();
  apply(settings);   // <-- pre-paint, no flash

  // --- panel UI (needs the DOM) --------------------------------------------
  function init() {
    const panel = document.getElementById("settings");
    const openBtn = document.getElementById("setBtn");
    if (!panel || !openBtn) return;

    const presetRow = panel.querySelector("#presetRow");
    const colorsBox = panel.querySelector("#colorRow");
    const fontSel = panel.querySelector("#fontSelect");
    const sizeRange = document.getElementById("fontRange");   // existing toolbar slider

    // presets (each chip shows an accent swatch)
    Object.keys(PRESETS).forEach(name => {
      const chip = document.createElement("button");
      chip.className = "preset";
      chip.dataset.preset = name;
      const sw = document.createElement("span");
      sw.className = "swatch";
      sw.style.background = PRESETS[name]["--accent"];
      sw.style.borderColor = PRESETS[name]["--text"];
      chip.append(sw, document.createTextNode(name));
      chip.addEventListener("click", () => {
        settings.preset = name;
        settings.colors = {};          // a fresh preset clears custom colours
        commit();
      });
      presetRow.appendChild(chip);
    });

    // colour pickers
    const colorInputs = {};
    PICKERS.forEach(([label, varName]) => {
      const wrap = document.createElement("label");
      const span = document.createElement("span");
      span.textContent = label;
      const inp = document.createElement("input");
      inp.type = "color";
      inp.dataset.var = varName;
      inp.addEventListener("input", () => {
        settings.colors = Object.assign({}, settings.colors, { [varName]: inp.value });
        commit();
      });
      colorInputs[varName] = inp;
      wrap.append(span, inp);
      colorsBox.appendChild(wrap);
    });

    // fonts
    Object.keys(FONTS).forEach(name => {
      const opt = document.createElement("option");
      opt.value = name; opt.textContent = name;
      fontSel.appendChild(opt);
    });
    fontSel.addEventListener("change", () => { settings.font = fontSel.value; commit(); });

    // bold / italic reader-text toggles
    const boldToggle = document.getElementById("boldToggle");
    const italicToggle = document.getElementById("italicToggle");
    if (boldToggle) boldToggle.addEventListener("click", () => { settings.bold = !settings.bold; commit(); });
    if (italicToggle) italicToggle.addEventListener("click", () => { settings.italic = !settings.italic; commit(); });
    const vertToggle = document.getElementById("vertToggle");
    if (vertToggle) vertToggle.addEventListener("click", () => {
      settings.vertical = !settings.vertical;
      commit();
      // Jump to the newest line — the scroll axis just flipped, the old
      // scroll offset points somewhere meaningless.
      const lines = document.getElementById("lines");
      if (lines) lines.scrollTo({ top: lines.scrollHeight, left: -lines.scrollWidth });
    });

    // Text-size sliders: the toolbar's A slider AND the panel's "Text size" row
    // (the toolbar one hides on narrow windows, so the panel keeps it reachable).
    // Both drive the same setting and stay in sync.
    const sizeRange2 = document.getElementById("fontRange2");
    const fontVal = document.getElementById("fontVal");
    const fontVal2 = document.getElementById("fontVal2");
    const showSize = () => {
      if (fontVal) fontVal.textContent = settings.fontSize;
      if (fontVal2) fontVal2.textContent = settings.fontSize;
      if (sizeRange) sizeRange.value = settings.fontSize;
      if (sizeRange2) sizeRange2.value = settings.fontSize;
    };
    [sizeRange, sizeRange2].forEach(r => {
      if (!r) return;
      r.addEventListener("input", () => {
        settings.fontSize = +r.value;
        document.documentElement.style.setProperty("--font-size", r.value + "px");
        showSize();
        save(settings);
      });
    });

    // spacing sliders (line height / furigana size)
    const lhRange = document.getElementById("lineHeightRange");
    const lhVal = document.getElementById("lineHeightVal");
    const furiRange = document.getElementById("furiRange");
    const furiVal = document.getElementById("furiVal");
    if (lhRange) lhRange.addEventListener("input", () => {
      settings.lineHeight = +lhRange.value; commit();
    });
    if (furiRange) furiRange.addEventListener("input", () => {
      settings.furiSize = +furiRange.value; commit();
    });

    // text-alignment segmented control (lives in the toolbar, not the panel, but
    // persists through the same settings object).
    const alignSeg = document.getElementById("alignSeg");
    if (alignSeg) {
      alignSeg.querySelectorAll("button[data-align]").forEach(btn => {
        btn.addEventListener("click", () => { settings.align = btn.dataset.align; commit(); });
      });
    }

    // reset (with a brief confirmation)
    panel.querySelector("#setReset").addEventListener("click", (e) => {
      settings = Object.assign({}, DEFAULTS, { colors: {} });
      commit();
      const b = e.currentTarget;
      b.textContent = "Reset ✓";
      setTimeout(() => (b.textContent = "Reset to default"), 900);
    });

    // open / close
    const toggle = (show) => panel.classList.toggle("hidden", show === undefined ? !panel.classList.contains("hidden") : !show);
    openBtn.addEventListener("click", (e) => { e.stopPropagation(); toggle(); syncControls(); });
    panel.querySelector("#setClose").addEventListener("click", () => toggle(false));
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") toggle(false); });
    document.addEventListener("click", (e) => {
      if (!panel.classList.contains("hidden") && !panel.contains(e.target) && e.target !== openBtn)
        toggle(false);
    });

    // Reflect current settings back into the controls.
    function syncControls() {
      const cs = getComputedStyle(document.documentElement);
      presetRow.querySelectorAll(".preset").forEach(c =>
        c.classList.toggle("active", c.dataset.preset === settings.preset && !Object.keys(settings.colors).length));
      PICKERS.forEach(([, v]) => {
        let val = (cs.getPropertyValue(v) || "").trim();
        if (!/^#[0-9a-f]{6}$/i.test(val)) val = resolveHex(v);   // derived (furigana) -> resolve to hex
        colorInputs[v].value = val;
      });
      fontSel.value = settings.font;
      if (sizeRange) sizeRange.value = settings.fontSize;
      showSize();
      if (lhRange) { lhRange.value = settings.lineHeight; lhVal.textContent = Number(settings.lineHeight).toFixed(1); }
      if (furiRange) { furiRange.value = settings.furiSize; furiVal.textContent = Math.round(settings.furiSize * 100) + "%"; }
      if (alignSeg) alignSeg.querySelectorAll("button[data-align]").forEach(b =>
        b.classList.toggle("active", b.dataset.align === settings.align));
      if (boldToggle) boldToggle.setAttribute("aria-pressed", String(!!settings.bold));
      if (italicToggle) italicToggle.setAttribute("aria-pressed", String(!!settings.italic));
      if (vertToggle) vertToggle.setAttribute("aria-pressed", String(!!settings.vertical));
    }

    function commit() { apply(settings); save(settings); syncControls(); }

    syncControls();   // initialise control state from saved settings
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
