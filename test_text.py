"""Units for the text plumbing between a game and the reader — the string
functions that silently mangle lines when they regress:

  clean_hook_text   (server.py)  — Textractor artifact undoubling
  Hooker._split     (hook.py)    — CLI "[hook key] text" line parsing
  _ws_extract_text  (server.py)  — websocket JSON-envelope unwrapping
  request_allowed   (server.py)  — CSRF / DNS-rebinding origin guard
  romajiToKana      (app.js)     — lookup-box romaji, run via node like test_merge

No dict.sqlite, no Windows APIs — runs everywhere (CI included).
Run:  python test_text.py   (needs node on PATH for the romaji cases)
"""
import json
import re
import subprocess
import sys

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

import hook
import server

FAILURES = 0
TOTAL = 0


def check(label, got, want):
    global FAILURES, TOTAL
    TOTAL += 1
    if got == want:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}: got {got!r}, want {want!r}")
        FAILURES += 1


# ---- clean_hook_text: whole-line doubling + per-char doubling ------------- #
def test_clean_hook_text():
    c = server.clean_hook_text
    check("ABCABC halved", c("あいうえおかきくあいうえおかきく"), "あいうえおかきく")
    check("char-doubling", c("ああいいううええ"), "あいうえ")
    check("both stacked", c("ああいいううええああいいううええ"), "あいうえ")
    # short real reduplications must survive (the n >= 10 / n >= 8 floors)
    check("ばらばら kept", c("ばらばら"), "ばらばら")
    check("はいはい kept", c("はいはい"), "はいはい")
    check("8-char line dup kept", c("あいうえあいうえ"), "あいうえあいうえ")
    check("odd length untouched", c("ああいいううえ"), "ああいいううえ")
    check("plain line untouched", c("今日はいい天気だ"), "今日はいい天気だ")
    check("empty", c(""), "")


# ---- Hooker._split: "[key] text" ------------------------------------------ #
def test_split():
    s = hook.Hooker._split
    check("normal line", s("[19:1A2C:GetGlyphOutlineW] こんにちは"),
          ("19:1A2C:GetGlyphOutlineW", "こんにちは"))
    check("no space after ]", s("[k]text"), ("k", "text"))
    check("console line (no bracket)", s("Textractor: attached"), (None, None))
    check("unclosed bracket", s("[19:1A2C こんにちは"), (None, None))
    check("empty text", s("[k] "), ("k", ""))
    check("bracket in text", s("[k] 「はい」"), ("k", "「はい」"))


# ---- _ws_extract_text: plain or JSON envelope ------------------------------ #
def test_ws_extract():
    x = server._ws_extract_text
    check("plain text", x("こんにちは"), "こんにちは")
    check("sentence key", x('{"sentence": "はい"}'), "はい")
    check("text key", x('{"text": "はい"}'), "はい")
    check("data key", x('{"data": "はい"}'), "はい")
    check("message key", x('{"message": "はい"}'), "はい")
    check("no known key", x('{"foo": "はい"}'), "")
    check("non-string value skipped", x('{"text": 5, "data": "はい"}'), "はい")
    check("broken json = plain", x('{oops'), "{oops")
    check("empty", x(""), "")


# ---- request_allowed: CSRF / DNS-rebinding guard --------------------------- #
def test_request_allowed():
    ok = lambda **h: server.request_allowed(h)
    check("loopback host", ok(Host="127.0.0.1:3939"), True)
    check("localhost host", ok(Host="localhost:3939"), True)
    check("LAN host", ok(Host="192.168.1.5:3939"), True)
    check("mDNS host", ok(Host="mypc.local:3939"), True)
    check("ipv6 loopback", ok(Host="[::1]:3939"), True)
    check("rebinding domain", ok(Host="evil.example.com:3939"), False)
    check("public-ip host", ok(Host="8.8.8.8:3939"), False)
    check("tailscale CGNAT host", ok(Host="100.101.102.103:3939"), True)
    check("no host header", ok(), False)
    check("same-origin post", ok(Host="127.0.0.1:3939", Origin="http://127.0.0.1:3939"), True)
    check("LAN phone origin", ok(Host="192.168.1.5:3939", Origin="http://192.168.1.5:3939"), True)
    check("web-page CSRF", ok(Host="127.0.0.1:3939", Origin="https://evil.example.com"), False)
    check("public-ip origin", ok(Host="127.0.0.1:3939", Origin="http://8.8.8.8"), False)
    check("null origin", ok(Host="127.0.0.1:3939", Origin="null"), False)


# ---- romajiToKana: the REAL function pulled out of app.js ------------------ #
ROMAJI_CASES = [
    ("tabemasu", "たべます"),
    ("konnichiwa", "こんにちわ"),      # nn = ん + na-row, not っ
    ("shinbun", "しんぶん"),           # n before a consonant
    ("kippu", "きっぷ"),               # doubled consonant = っ
    ("matcha", "まっちゃ"),            # t before ch = っ
    ("ryokou", "りょこう"),            # digraph
    ("jya", "じゃ"),
    ("fune", "ふね"),
    ("n", "ん"),                       # bare final n
    ("ka-do", "かーど"),               # long-vowel dash
    ("xyz", "xyz"),                    # unconvertible passes through (x, y before z…)
]


def test_romaji():
    global FAILURES
    with open("static/app.js", encoding="utf-8") as f:
        src = f.read()
    m_tab = re.search(r"const ROMAJI = \{[\s\S]*?\n\};", src)
    m_fn = re.search(r"function romajiToKana\(s\) \{[\s\S]*?\n\}", src)
    if not (m_tab and m_fn):
        print("FAIL  romajiToKana/ROMAJI not found in static/app.js")
        FAILURES += 1
        return
    script = (m_tab.group(0) + "\n" + m_fn.group(0) +
              "\nconst cases = JSON.parse(require('fs').readFileSync(0, 'utf8'));" +
              "\nprocess.stdout.write(JSON.stringify(cases.map(c => romajiToKana(c))));")
    out = subprocess.run(["node", "-e", script],
                         input=json.dumps([c for c, _ in ROMAJI_CASES]),
                         capture_output=True, text=True, encoding="utf-8", check=True)
    got = json.loads(out.stdout)
    for (src_txt, want), g in zip(ROMAJI_CASES, got):
        check(f"romaji {src_txt}", g, want)


def main():
    test_clean_hook_text()
    test_split()
    test_ws_extract()
    test_request_allowed()
    test_romaji()
    print(f"\n{TOTAL - FAILURES}/{TOTAL} passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
