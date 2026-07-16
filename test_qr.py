"""Units for qr.py (the --lan phone QR). No dependencies, runs in CI.

What locks what:
  - _format_bits(0) against the PUBLISHED format string for EC L / mask 0 —
    the BCH code is the easiest place to be subtly wrong.
  - Reed-Solomon via the zero-syndrome property: a correct codeword is
    divisible by the generator, so it evaluates to 0 at α^0..α^(n-1). This
    catches any GF/table/division slip without needing a known vector.
  - Structure: finders, timing, dark module, every cell filled, version
    selection at the exact capacity boundaries.
  - A full matrix fixture (sha256) for one URL. The fixture was verified
    bit-for-bit against the `qrcode` reference library AND decoded with jsQR
    when it was frozen — if an edit changes any module, this trips.
"""
import hashlib
import json
import sys

import qr

FAILURES = 0


def check(label, cond):
    global FAILURES
    if cond:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}")
        FAILURES += 1


def test_format_bits():
    # ISO 18004 Annex C table: EC L, mask 0 -> 111011111000100
    check("format bits L/mask0", qr._format_bits(0) == 0b111011111000100)
    # all 8 masks distinct and 15-bit
    vals = {qr._format_bits(m) for m in range(8)}
    check("format bits distinct per mask", len(vals) == 8)
    check("format bits fit 15 bits", all(v < (1 << 15) for v in vals))


def _syndromes_zero(data, n):
    cw = data + qr._rs_ec(data, n)
    N = len(cw)
    for i in range(n):                      # codeword(α^i) must be 0
        acc = 0
        for k, c in enumerate(cw):
            if c:
                acc ^= qr._EXP[(qr._LOG[c] + i * (N - 1 - k)) % 255]
        if acc:
            return False
    return True


def test_reed_solomon():
    check("RS syndromes zero (short)", _syndromes_zero([32, 91, 11, 120, 209], 7))
    check("RS syndromes zero (v2 shape)", _syndromes_zero(list(range(1, 35)), 10))
    check("RS syndromes zero (all zeros)", qr._rs_ec([0] * 19, 7) == [0] * 7)
    check("RS length", len(qr._rs_ec(list(range(55)), 15)) == 15)


def test_structure():
    for text, want_v in [("x" * 17, 1), ("x" * 18, 2), ("x" * 32, 2),
                         ("x" * 53, 3), ("x" * 78, 4), ("x" * 106, 5)]:
        m = qr.matrix(text)
        n = len(m)
        v = (n - 17) // 4
        check(f"version for {len(text)} bytes = v{want_v}", v == want_v)
        check(f"v{v} all cells filled", all(x in (0, 1) for row in m for x in row))
        # the three finders' outer rings + centres
        for r0, c0 in ((0, 0), (0, n - 7), (n - 7, 0)):
            ok = (all(m[r0][c0 + i] and m[r0 + 6][c0 + i] for i in range(7)) and
                  all(m[r0 + i][c0] and m[r0 + i][c0 + 6] for i in range(7)) and
                  m[r0 + 3][c0 + 3] == 1 and m[r0 + 1][c0 + 1] == 0)
            check(f"v{v} finder at ({r0},{c0})", ok)
        check(f"v{v} timing row", all(m[6][i] == 1 - (i & 1) for i in range(8, n - 8)))
        check(f"v{v} timing col", all(m[i][6] == 1 - (i & 1) for i in range(8, n - 8)))
        check(f"v{v} dark module", m[n - 8][8] == 1)
    try:
        qr.matrix("x" * 107)
        check("overlong raises", False)
    except ValueError:
        check("overlong raises", True)


def test_fixture():
    # Frozen 2026-07-16: matched the `qrcode` reference library module-for-module
    # and decoded with jsQR as exactly this URL. Regenerate ONLY after re-verifying
    # with a real decoder:  python -c "import test_qr; test_qr.print_fixture()"
    m = qr.matrix("http://192.168.1.23:3939/")
    digest = hashlib.sha256(json.dumps(m).encode()).hexdigest()
    check("v2 matrix fixture", digest == FIXTURE_SHA256)
    px, w, h = qr.png_pixels("http://192.168.1.23:3939/")
    check("png pixel buffer", w == h == (25 + 8) * 8 and len(px) == w * h * 4)


FIXTURE_SHA256 = "bdac916b22b1f431238c66f9ccd8a7660f957a5144a6d754aeea94d654ea23cd"


def print_fixture():
    m = qr.matrix("http://192.168.1.23:3939/")
    print(hashlib.sha256(json.dumps(m).encode()).hexdigest())


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    test_format_bits()
    test_reed_solomon()
    test_structure()
    test_fixture()
    print(f"\n{'FAILED' if FAILURES else 'OK'} ({FAILURES} failures)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
