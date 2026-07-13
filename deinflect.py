"""
Japanese de-inflector using Yomitan's transform table (see deinflect_data.py).

Given an inflected surface form, returns candidate dictionary forms together with
the chain of inflections that produced them. It deliberately *over-generates*:
the dictionary lookup that consumes these candidates filters out non-words, so a
missing rule only ever means a missed result, never a wrong one.

    deinflect("食べさせられた") -> {"食べる": ["causative", "-た"], ...}

Each rule carries grammatical pre/post conditions (bitmask over verb/adjective
classes), so chains stay grammatical: 〜ば can only peel off a verb form, and its
output is marked "godan/ichidan dictionary form" for the next rule to check.
"""

from collections import deque

from deinflect_data import RULES, COND_MASK

_M = COND_MASK
# Auxiliary-verb chains and suffixes Yomitan leaves to its multi-word scanner
# (it matches みる/いく as separate words); we deinflect single tokens, so peel
# them explicitly. Output of a て-rule is condition "-て" so the -て transform
# can finish the job; suffixes that expose the masu stem output 0 (unrestricted)
# so the "continuative" rules apply next.
_SUPPLEMENT = [
    ("s", "てみる", "て", _M["v1"], _M["-て"], "〜てみる (try)"),
    ("s", "でみる", "で", _M["v1"], _M["-て"], "〜てみる (try)"),
    ("s", "ていく", "て", _M["v5"], _M["-て"], "〜ていく (go on)"),
    ("s", "でいく", "で", _M["v5"], _M["-て"], "〜ていく (go on)"),
    ("s", "てくる", "て", _M["vk"], _M["-て"], "〜てくる (come to)"),
    ("s", "でくる", "で", _M["vk"], _M["-て"], "〜てくる (come to)"),
    ("s", "てくれる", "て", _M["v1"], _M["-て"], "〜てくれる (for me)"),
    ("s", "でくれる", "で", _M["v1"], _M["-て"], "〜てくれる (for me)"),
    ("s", "てあげる", "て", _M["v1"], _M["-て"], "〜てあげる (for someone)"),
    ("s", "であげる", "で", _M["v1"], _M["-て"], "〜てあげる (for someone)"),
    ("s", "てもらう", "て", _M["v5"], _M["-て"], "〜てもらう (have done)"),
    ("s", "でもらう", "で", _M["v5"], _M["-て"], "〜てもらう (have done)"),
    ("s", "てある", "て", _M["v5"], _M["-て"], "〜てある (has been done)"),
    ("s", "である", "で", _M["v5"], _M["-て"], "〜てある (has been done)"),
    ("s", "ておる", "て", _M["v5"], _M["-て"], "〜ておる (humble progressive)"),
    ("s", "でおる", "で", _M["v5"], _M["-て"], "〜ておる (humble progressive)"),
    # Spoken contractions Yomitan's table covers only in the て-variant (とる)
    # or not at all: どる (飲んどる), てく/でく (食べてく = ていく dropped い),
    # てらっしゃる (honorific ていらっしゃる, dropped い).
    ("s", "どる", "で", _M["v5"], _M["-て"], "〜とる (progressive)"),
    ("s", "てく", "て", _M["v5"], _M["-て"], "〜ていく (go on)"),
    ("s", "でく", "で", _M["v5"], _M["-て"], "〜ていく (go on)"),
    ("s", "てらっしゃる", "て", _M["v5"], _M["-て"], "〜ていらっしゃる (honorific)"),
    ("s", "でらっしゃる", "で", _M["v5"], _M["-て"], "〜ていらっしゃる (honorific)"),
    ("s", "にくい", "", _M["adj-i"], 0, "〜にくい (hard to)"),
    ("s", "やすい", "", _M["adj-i"], 0, "〜やすい (easy to)"),
    ("s", "づらい", "", _M["adj-i"], 0, "〜づらい (hard to)"),
    ("s", "がたい", "", _M["adj-i"], 0, "〜がたい (hard to)"),
    ("s", "ないで", "ない", 0, _M["adj-i"], "negative -te"),
    ("s", "なさそう", "ない", 0, _M["adj-i"], "〜なさそう (seems not)"),
    ("s", "なくなる", "ない", 0, _M["adj-i"], "〜なくなる (stop being)"),
    ("s", "さそう", "い", 0, _M["adj-i"], "〜そう (seems)"),
]

# Bucket rules by the last character of their inflected suffix, so each step only
# tries the small slice of the 850+ rules that could possibly match.
_BY_LAST = {}
for _r in RULES + _SUPPLEMENT:
    _BY_LAST.setdefault(_r[1][-1], []).append(_r)


def deinflect(word, max_depth=6):
    """Return {dictionary_form: [inflection reasons]} reachable from `word`.

    Reasons are listed outermost-first (the order the rules were peeled off);
    the caller reverses them for display. Conditions start unrestricted (0) and
    each applied rule constrains what may apply next, per Yomitan's semantics.
    """
    results = {word: []}
    visited = {(word, 0)}
    queue = deque([(word, 0, [])])
    while queue:
        cur, conds, reasons = queue.popleft()
        if len(reasons) >= max_depth:
            continue
        for kind, sin, sout, m_in, m_out, label in _BY_LAST.get(cur[-1], ()):
            if conds != 0 and not (conds & m_in):
                continue
            if kind == "w":
                if cur != sin:
                    continue
                nxt = sout
            else:
                if len(cur) < len(sin) or not cur.endswith(sin):
                    continue
                nxt = cur[: len(cur) - len(sin)] + sout
            if not nxt:
                continue
            state = (nxt, m_out)
            if state in visited:
                continue
            visited.add(state)
            newr = reasons + [label]
            if nxt not in results:          # BFS -> first hit is the shortest chain
                results[nxt] = newr
            queue.append((nxt, m_out, newr))
    return results


if __name__ == "__main__":
    import sqlite3
    import os
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    db = sqlite3.connect(os.path.join(os.path.dirname(__file__), "dict.sqlite"))

    def in_dict(term):
        return db.execute("SELECT 1 FROM terms WHERE term=? LIMIT 1", (term,)).fetchone() is not None

    tests = {
        "食べた": "食べる", "食べない": "食べる", "食べさせられた": "食べる",
        "行った": "行く", "飲んでいる": "飲む", "書きます": "書く", "話して": "話す",
        "泳いだ": "泳ぐ", "死んだ": "死ぬ", "呼んだ": "呼ぶ", "買って": "買う",
        "会った": "会う", "した": "する", "来た": "来る", "行きたい": "行く",
        "食べてる": "食べる", "食べちゃった": "食べる", "高かった": "高い",
        "美味しくない": "美味しい", "早く": "早い", "行こう": "行く",
        "読ませる": "読む", "見られる": "見る", "帰れば": "帰る", "泣かないで": "泣く",
        # coverage added with the Yomitan rule table:
        "行け": "行く", "来い": "来る", "しろ": "する", "食べてみる": "食べる",
        "読んでしまった": "読む", "書いておく": "書く", "食べていく": "食べる",
        "分かりにくい": "分かる", "読みやすい": "読む", "食べたがっている": "食べる",
        "高くなさそう": "高い", "行かなきゃ": "行く", "見せてくれ": "見せる",
        "振り乱し": "振り乱す", "考え": "考える", "わからん": "わかる",
    }
    ok = 0
    for surface, expect in tests.items():
        cands = deinflect(surface)
        found = [c for c in cands if in_dict(c)]
        hit = expect in found
        ok += hit
        print(f"{'OK ' if hit else 'MISS'} {surface:10s} -> expect {expect:6s} | dict-hits: {found[:8]}")
    print(f"\n{ok}/{len(tests)} resolved")
