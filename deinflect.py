"""
Compact Japanese de-inflector (Yomitan-style).

Given an inflected surface form, returns candidate dictionary forms together with
the chain of inflections that produced them. It deliberately *over-generates*:
the dictionary lookup that consumes these candidates filters out non-words, so a
missing rule only ever means a missed result, never a wrong one.

    deinflect("食べさせられた") -> {"食べる": ["causative","passive","past"], ...}
"""

from collections import deque

# godan stem-row -> dictionary-form ending
_A = {"か": "く", "が": "ぐ", "さ": "す", "た": "つ", "な": "ぬ", "ば": "ぶ", "ま": "む", "ら": "る", "わ": "う"}
_I = {"き": "く", "ぎ": "ぐ", "し": "す", "ち": "つ", "に": "ぬ", "び": "ぶ", "み": "む", "り": "る", "い": "う"}
_O = {"こ": "く", "ご": "ぐ", "そ": "す", "と": "つ", "の": "ぬ", "ぼ": "ぶ", "も": "む", "ろ": "る", "お": "う"}
_E = {"け": "く", "げ": "ぐ", "せ": "す", "て": "つ", "ね": "ぬ", "べ": "ぶ", "め": "む", "れ": "る", "え": "う"}


def _build_rules():
    R = []

    def add(a, b, reason):
        R.append((a, b, reason))

    # ---- progressive: strip the いる / てる contraction down to the て-form ----
    for a, b in (("ている", "て"), ("でいる", "で"), ("ていた", "て"), ("でいた", "で"),
                 ("てる", "て"), ("でる", "で"), ("てた", "て"), ("でた", "で"),
                 ("ています", "て"), ("でいます", "で")):
        add(a, b, "progressive")

    # ---- て-form and plain past ----
    for a in ("って", "った"):
        for u in ("う", "つ", "る"):
            add(a, u, "past/-te")
    for a, u in (("いて", "く"), ("いた", "く"), ("いで", "ぐ"), ("いだ", "ぐ"),
                 ("んで", "ぶ"), ("んだ", "ぶ"), ("んで", "む"), ("んだ", "む"),
                 ("んで", "ぬ"), ("んだ", "ぬ")):
        add(a, u, "past/-te")
    for a in ("して", "した"):
        add(a, "す", "past/-te")
        add(a, "する", "past/-te")
    add("て", "る", "-te")
    add("た", "る", "past")
    for a, b in (("行って", "行く"), ("行った", "行く"), ("いって", "いく"), ("いった", "いく"),
                 ("来て", "来る"), ("来た", "来る"), ("きて", "くる"), ("きた", "くる")):
        add(a, b, "past/-te")

    # ---- polite (reduce to ます, then ます -> dict) ----
    add("ませんでした", "ました", "negative")
    add("ません", "ます", "negative")
    add("ました", "ます", "past")
    add("ましょう", "ます", "volitional")
    add("まして", "ます", "-te")
    add("ます", "る", "polite")
    add("します", "する", "polite")
    add("します", "す", "polite")
    for i, u in _I.items():
        add(i + "ます", u, "polite")

    # ---- bare continuative / masu-stem (連用形): 振り乱し -> 振り乱す, 読み -> 読む ----
    for i, u in _I.items():
        add(i, u, "continuative")

    # ---- negative ----
    add("なかった", "ない", "past")
    add("なくて", "ない", "-te")
    add("なければ", "ない", "conditional")
    add("なきゃ", "ない", "conditional")
    add("なくちゃ", "ない", "conditional")
    add("なく", "ない", "adverbial")
    add("ないで", "ない", "-te")
    add("ない", "る", "negative")
    for a, u in _A.items():
        add(a + "ない", u, "negative")
    add("しない", "する", "negative")
    add("こない", "くる", "negative")

    # ---- passive / potential / causative ----
    add("られる", "る", "passive/potential")
    add("させる", "る", "causative")
    add("させる", "する", "causative")
    add("られる", "くる", "passive/potential")
    for a, u in _A.items():
        add(a + "れる", u, "passive")
        add(a + "せる", u, "causative")
    for e, u in _E.items():           # godan potential 行ける -> 行く
        add(e + "る", u, "potential")
    add("れる", "る", "potential")

    # ---- volitional / conditional ----
    add("よう", "る", "volitional")
    for o, u in _O.items():
        add(o + "う", u, "volitional")
    add("しよう", "する", "volitional")
    add("こよう", "くる", "volitional")
    add("れば", "る", "conditional")
    for e, u in _E.items():
        add(e + "ば", u, "conditional")
    add("たら", "た", "conditional")
    add("だら", "だ", "conditional")
    add("たり", "た", "-tari")
    add("だり", "だ", "-tari")
    add("ろ", "る", "imperative")          # ichidan: 食べろ -> 食べる

    # ---- desire / colloquial ----
    add("たい", "る", "desire")
    for i, u in _I.items():
        add(i + "たい", u, "desire")
    add("たがる", "る", "desire")
    for a, b in (("ちゃう", "て"), ("ちゃった", "て"), ("ちゃって", "て"),
                 ("じゃう", "で"), ("じゃった", "で"), ("じゃって", "で")):
        add(a, b, "colloquial")
    add("とく", "て", "colloquial")
    add("どく", "で", "colloquial")

    # ---- い-adjectives ----
    add("かった", "い", "past")
    add("くない", "い", "negative")
    add("くて", "い", "-te")
    add("ければ", "い", "conditional")
    add("く", "い", "adverbial")
    add("さ", "い", "nominal")
    return R


_RULES = _build_rules()


# i/e-row hiragana an ichidan verb stem can end in (食べ, 考え, 起き, 見...)
_ICHIDAN_STEM = set("いきしちにひみりゐぎじぢびぴえけせてねへめれゑげぜでべぺ")


def deinflect(word, max_depth=10):
    """Return {dictionary_form: [inflection reasons]} reachable from `word`."""
    results = {word: []}
    queue = deque([(word, [])])
    # ichidan bare continuative: 考え -> 考える, 食べ -> 食べる
    if word and word[-1] in _ICHIDAN_STEM:
        results[word + "る"] = ["continuative"]
        queue.append((word + "る", ["continuative"]))
    while queue:
        cur, reasons = queue.popleft()
        if len(reasons) >= max_depth:
            continue
        for sin, sout, label in _RULES:
            if len(cur) >= len(sin) and cur.endswith(sin):  # >= so whole-word irregulars fire
                nxt = cur[: len(cur) - len(sin)] + sout
                if nxt and nxt != cur and nxt not in results:
                    newr = reasons + [label]
                    results[nxt] = newr
                    queue.append((nxt, newr))
    return results


if __name__ == "__main__":
    import sqlite3
    import os
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
    }
    ok = 0
    for surface, expect in tests.items():
        cands = deinflect(surface)
        found = [c for c in cands if in_dict(c)]
        hit = expect in found
        ok += hit
        print(f"{'OK ' if hit else 'MISS'} {surface:10s} -> expect {expect:6s} | dict-hits: {found}")
    print(f"\n{ok}/{len(tests)} resolved")
