"""Parity test: ocr._merge_reads (Python) and mergeReads (static/app.js).

The OCR loop publishes a merged superstring and the reader swaps it in place
of the partial line — that swap is only correct because BOTH ends compute the
same merge. The two implementations are hand-mirrored in different languages,
so this locks them together: every case (plus an exhaustive slice sweep) runs
through both, and any drift fails loudly.

Run:  python test_merge.py   (needs node on PATH; no dict.sqlite needed)
"""
import json
import re
import subprocess
import sys

import ocr

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

# The documented merge behaviours, verbatim from the comments in both files.
HAND_CASES = [
    ("した", "した。"),                      # end growth (containment)
    ("油断していた", "……く、油断していた"),   # front growth (containment)
    ("まもなく電車がまい", "電車がまいります"),  # head/tail splice
    ("電車がまいります", "まもなく電車がまい"),  # splice, arguments swapped
    ("あいうえお", "あいうえお"),             # identical
    ("あいうえおか", "えおかきくけ"),          # overlap below the ≥half floor -> no merge
    ("だから", "たから"),                    # kana-flip jitter -> no merge
    ("", "abc"),                            # empty read
]


def all_cases():
    cases = list(HAND_CASES)
    # Exhaustive sweep: every pair of substrings of a fixed string exercises
    # every containment/overlap/disjoint geometry the algorithms can meet.
    s = "あいうえおかきくけこさし"
    subs = [s[i:j] for i in range(len(s)) for j in range(i + 1, len(s) + 1)]
    cases.extend((a, b) for a in subs for b in subs)
    return cases


def js_merge(cases):
    """Run the REAL mergeReads pulled out of app.js — not a copy — over cases."""
    with open("static/app.js", encoding="utf-8") as f:
        src = f.read()
    m = re.search(r"function mergeReads\(a, b\) \{[\s\S]*?\n\}", src)
    if not m:
        raise SystemExit("FAIL: mergeReads not found in static/app.js")
    script = (m.group(0) +
              "\nconst cases = JSON.parse(require('fs').readFileSync(0, 'utf8'));" +
              "\nprocess.stdout.write(JSON.stringify(cases.map(([a, b]) => mergeReads(a, b))));")
    out = subprocess.run(["node", "-e", script], input=json.dumps(all_cases()),
                         capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(out.stdout)


def main():
    cases = all_cases()
    py = [ocr._merge_reads(a, b) for a, b in cases]
    js = js_merge(cases)
    failures = 0
    for (a, b), p, j in zip(cases, py, js):
        if p != j:
            failures += 1
            print(f"FAIL  merge({a!r}, {b!r}): python={p!r} js={j!r}")
    # Spot-check the semantics themselves, not just parity.
    assert ocr._merge_reads("した", "した。") == "した。"
    assert ocr._merge_reads("まもなく電車がまい", "電車がまいります") == "まもなく電車がまいります"
    assert ocr._merge_reads("だから", "たから") is None
    print(f"{len(cases) - failures}/{len(cases)} cases agree" +
          ("" if failures else " — python/js merge in sync"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
