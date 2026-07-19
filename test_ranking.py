"""Regression tests for dictionary lookup ranking (server.scan).

Guards two classes of bug that made the *intended* word rank below junk:

  1. over-match  — a common word run into the next particle landing on a rare
                   homograph (hover そこ -> the rare 底荷「そこに」"ballast").
  2. name-domination — a real word buried under same-length name readings
                   (hover 村 -> the surnames むらさき/たくみ above 村 "village").

...while preserving the behaviour we *want*: genuinely longer matches still win,
whether a compound (一日中, という) or a real name (田中 -> Tanaka).

Run after `python setup.py`:   python test_ranking.py
"""
import os
import sys

try:
    sys.stdout.reconfigure(errors="replace")   # cp932 consoles can't print '›'
except Exception:
    pass

import server

# (label, text, pos, reading, base, surface, want_kind, want_substr)
#   the tuple mirrors what static/app.js sends to /scan for the hovered token;
#   `surface` is the tokenizer's surface form of that token (anchors the cap).
CASES = [
    # -- over-match: the short common word must win, not the rare homograph ----
    ("そこ -> there",      "そこには海水のように", "代名詞", "ソコ", "そこ", "そこ", "word", "there"),
    ("これ -> this",       "これは何ですか",       "代名詞", "コレ", "これ", "これ", "word", "this"),
    ("それ -> that",       "それを取って",         "代名詞", "ソレ", "それ", "それ", "word", "that"),
    # -- name-domination: the real word must beat its same-length name readings -
    ("村 -> village",      "村を去った日と同じ",   "名詞",   "ムラ", "村",   "村",   "word", "village"),
    ("空 -> sky",          "空を見上げると",       "名詞",   "ソラ", "空",   "空",   "word", "sky"),
    ("林 -> woods (word)", "林の中を歩く",         "名詞",   "ハヤシ", "林",  "林",   "word", "wood"),
    # -- keep: a genuinely longer name still wins -----------------------------
    ("田中 (split) name",  "田中さんはいますか",   "名詞",   "タ",   "田",   "田",   "name", "Tanaka"),
    ("田中 (1 token) name","田中さんはいますか",   "名詞",   "タナカ", "田中", "田中", "name", "Tanaka"),
    # -- keep: legitimate longest-match compounds/expressions -----------------
    ("という (long)",      "というのは大事だ",     "助詞",   "ト",   "と",   "と",   "word", "because"),
    ("一日中 (long)",      "一日中ずっと寝てた",   "名詞",   "イチ", "一日", "一日", "word", "all day"),
    ("について (long)",    "について話す",         "助詞",   "ニ",   "に",   "に",   "word", "about"),
    # -- degenerate 51-homograph cluster (こう) must still find a real word ----
    ("こう (cluster)",     "こうやって笑った",     "副詞",   "コウ", "こう", "こう", "word", "thus"),
    # -- kana verb resolves to its dominant kanji (居る, not 射る/没る) ---------
    ("いる -> 居る",       "いるよ",               "動詞",   "イル", "いる", "いる", "word", "to be"),
    # -- B2: a JMdict-common word beats an obscure same-reading homograph -------
    ("みさき -> 岬 (cape)", "みさきに立つ",         "名詞",   "ミサキ", "みさき", "みさき", "word", "cape"),
    # -- B3: secondary-reading homograph must not bury the primary-reading word -
    ("おう -> 追う (chase)","おう",                 "動詞",   "オウ",  "おう",  "おう",  "word", "chase"),
    # -- exp POS map: 様に earns the POS tiebreak over the rare 陽に -------------
    ("ように -> 様に",      "ように見える",         "助詞",   "ヨウニ", "ように", "ように", "word", "like"),
    # -- uk boost: kana hover prefers the usually-kana homograph ----------------
    ("ずるい (uk)",         "ずるいなあ",           "形容詞", "ズルイ", "ずるい", "ずるい", "word", "sly"),
    ("かける (uk)",         "かけてみた",           "動詞",   "カケ",  "かける", "かけ",  "word", "hang"),
    # -- uk guard: a JMdict-priority reading still beats a rare uk homograph ----
    ("まめ -> 豆 (guard)",  "まめを食べた",         "名詞",   "マメ",  "まめ",   "まめ",  "word", "legume"),
    # -- boundary rescue: a rare-but-real compound the tokenizer split keeps its
    #    length when it extends with kanji AND ends at a natural boundary.
    #    (The over-match guard stays: そこには above — 底荷's extension is kana.)
    ("生返事 (rescue)",     "生返事だった",         "名詞",   "ナマ",  "生",    "生",    "word", "half-hearted"),
    ("棒読み (rescue)",     "棒読みで言った",       "名詞",   "ボウ",  "棒",    "棒",    "word", "monotone"),
    # -- katakana tokens: a rare loanword homograph must not bury the name -----
    #    (hover レオン showed "leone (currency of Sierra Leone)" above Leon)
    ("レオン -> name Leon", "レオン・ウェルトに捧げます", "名詞", "レオン", "レオン", "レオン", "name", "Leon"),
    #    ...but an established katakana word still beats its name homograph
    ("カメラ stays a word", "カメラを買った",       "名詞",   "カメラ", "カメラ", "カメラ", "word", "camera"),
]

# English reverse lookup (/search): (query, acceptable top-1 headwords, and a
# word that must appear in the top 3). Encodes the bm25-preselection bug (刀 cut
# from "sword" entirely) and the sense-tier bug (鋼's 3rd sense "sword" beating
# 刀's 1st).
EN_CASES = [
    ("sword",              ("剣", "刀"),        "刀"),
    ("eat",                ("食べる", "食う"),   "食べる"),
    ("to eat",             ("食べる", "食う"),   "食べる"),   # "to " prefix must not bury verbs
    ("half-hearted reply", ("生返事",),          "生返事"),
    ("cherry blossom",     ("桜",),              "桜"),
    ("beautiful",          ("綺麗", "美しい"),   "美しい"),
]

# de-inflection coverage: each surface form must reach its dictionary base.
DEINFL = [
    ("行かん", "行く"), ("知らず", "知る"), ("せず", "する"),
    ("読まされる", "読む"), ("高すぎる", "高い"), ("高くありません", "高い"),
    ("食べなさい", "食べる"), ("食べそう", "食べる"), ("食べなくなる", "食べる"),
    ("高かろう", "高い"),
]


def top(text, pos, reading, base, surface):
    results = server.scan(text, pos, reading, base, surface)
    return results[0] if results else None


def main():
    if not os.path.isfile(server.DB_PATH):
        print("SKIP: dict.sqlite not found — run `python setup.py` first.")
        return 0

    failures = 0
    total = 0
    for label, text, pos, reading, base, surface, want_kind, want_substr in CASES:
        total += 1
        c = top(text, pos, reading, base, surface)
        if c is None:
            print(f"FAIL  {label}: no results")
            failures += 1
            continue
        e = c["entry"]
        head = (e.get("k") or e.get("r") or ["?"])[0]
        gloss = "; ".join(e["s"][0]["gloss"]) if e.get("s") else ""
        kind_ok = c["kind"] == want_kind
        text_ok = want_substr.lower() in (head + " " + gloss).lower()
        if kind_ok and text_ok:
            print(f"PASS  {label}: {head}【{e['r'][0]}】 [{c['kind']}]")
        else:
            why = []
            if not kind_ok:
                why.append(f"kind={c['kind']} want {want_kind}")
            if not text_ok:
                why.append(f"'{want_substr}' not in top result")
            print(f"FAIL  {label}: {head}【{e['r'][0]}】 [{c['kind']}] — {', '.join(why)}")
            failures += 1

    # English reverse lookup (needs the gloss_fts table — setup.py adds it)
    import sqlite3
    has_fts = sqlite3.connect(server.DB_PATH).execute(
        "SELECT 1 FROM sqlite_master WHERE name='gloss_fts'").fetchone()
    if not has_fts:
        print("SKIP  /search cases: no gloss_fts — run `python setup.py` to add it")
    else:
        for q, top1_ok, in_top3 in EN_CASES:
            total += 1
            r = server.search_english(q)
            heads = [(c["entry"].get("k") or c["entry"].get("r") or ["?"])[0] for c in r]
            if heads and heads[0] in top1_ok and in_top3 in heads[:3]:
                print(f"PASS  /search {q}: {heads[:3]}")
            else:
                print(f"FAIL  /search {q}: got {heads[:3]}, want top1 in {top1_ok}, "
                      f"{in_top3} in top 3")
                failures += 1

    # de-inflection coverage
    import deinflect
    for surface, base in DEINFL:
        total += 1
        forms = deinflect.deinflect(surface)
        if base in forms:
            print(f"PASS  deinflect {surface} -> {base}")
        else:
            print(f"FAIL  deinflect {surface} -> {base}: got {list(forms)[:4]}")
            failures += 1

    # B1: the displayed inflection trail reads stem-outward (causative › … › past)
    total += 1
    sc = server.scan("食べさせられた", "動詞", "タベサセラレタ", "食べる", "食べさせられた")
    trail = sc[0]["reasons"] if sc else []
    if trail == ["causative", "potential or passive", "-た"]:
        print(f"PASS  trail order: {' › '.join(trail)}")
    else:
        print(f"FAIL  trail order: got {trail}")
        failures += 1

    # Katakana prefix garbage: a match covering only a prefix of a pure-katakana
    # token is dropped (hover テグジュペリ used to show 大邱「テグ」"Daegu").
    total += 1
    sc = server.scan("テグジュペリ", "名詞", "テグジュペリ", "テグジュペリ", "テグジュペリ")
    partial = [c for c in sc if c["len"] < 6]
    if not partial:
        print("PASS  katakana prefix-match drop (テグジュペリ shows no 大邱)")
    else:
        heads = [(c["entry"].get("k") or c["entry"].get("r") or ["?"])[0] for c in partial]
        print(f"FAIL  katakana prefix-match drop: got sub-token matches {heads[:3]}")
        failures += 1

    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
