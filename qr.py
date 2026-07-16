"""
Tiny QR encoder — just enough to put a scannable LAN URL on screen.

Byte mode, error-correction level L, versions 1-5 (single Reed-Solomon block,
up to 106 chars — a LAN URL is ~25), fixed mask 0. Pure stdlib, written to
ISO/IEC 18004. test_qr.py locks the pieces that are easy to get subtly wrong:
the RS math (known vector + the zero-syndrome property), the format bits
against the published L/mask-0 string, and a full matrix fixture that was
verified once with a real phone scan.

ponytail: fixed mask 0, no penalty scoring — every spec-compliant reader
decodes any mask; add the penalty pass only if some scanner ever balks.
"""

# version: (total codewords, EC codewords) — EC level L is a single block
# through version 5, which keeps the interleaving step away entirely.
_CAPACITY = {1: (26, 7), 2: (44, 10), 3: (70, 15), 4: (100, 20), 5: (134, 26)}
_ALIGN = {2: 18, 3: 22, 4: 26, 5: 30}   # centre of the one alignment pattern
_FMT_L_MASK0 = 0b111011111000100        # published format string for L / mask 0

# GF(256), QR's reduction polynomial 0x11D.
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _gf_mul(a, b):
    return _EXP[_LOG[a] + _LOG[b]] if a and b else 0


def _rs_ec(data, n):
    """n Reed-Solomon EC codewords for `data` (generator ∏(x − α^i), i<n)."""
    gen = [1]                              # built lowest-degree-first…
    for i in range(n):
        nxt = [0] * (len(gen) + 1)
        for j, c in enumerate(gen):
            nxt[j] ^= _gf_mul(c, _EXP[i])
            nxt[j + 1] ^= c
        gen = nxt
    gen.reverse()                          # …divided highest-first (monic)
    rem = list(data) + [0] * n
    for i in range(len(data)):
        f = rem[i]
        if f:
            for j in range(1, len(gen)):
                rem[i + j] ^= _gf_mul(gen[j], f)
    return rem[len(data):]


def _format_bits(mask):
    """15 format bits: 5 data bits (EC level L = 01, then the mask) + BCH(15,5),
    XOR-masked per spec. Reproduces the published table (L/0 → 111011111000100)."""
    data = (0b01 << 3) | mask
    rem = data << 10
    while rem.bit_length() > 10:
        rem ^= 0b10100110111 << (rem.bit_length() - 11)
    return ((data << 10) | rem) ^ 0b101010000010010


def _codewords(data, version):
    """Mode header + data + terminator + pad bytes + RS EC, as one codeword list."""
    total, ec = _CAPACITY[version]
    ncw = total - ec
    bits = []

    def put(val, n):
        for k in range(n - 1, -1, -1):
            bits.append((val >> k) & 1)

    put(0b0100, 4)               # byte mode
    put(len(data), 8)            # char count is 8 bits for versions 1-9
    for b in data:
        put(b, 8)
    bits.extend([0] * min(4, ncw * 8 - len(bits)))   # terminator
    while len(bits) % 8:
        bits.append(0)
    cw = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC                               # alternating pad bytes, 0xEC first
    while len(cw) < ncw:
        cw.append(pad)
        pad ^= 0xEC ^ 0x11
    return cw + _rs_ec(cw, ec)


def _function_patterns(size, version):
    """Matrix with every function module stamped; None marks a data cell."""
    m = [[None] * size for _ in range(size)]

    def finder(r0, c0):
        for r in range(-1, 8):               # -1/7 ring = the white separator
            for c in range(-1, 8):
                rr, cc = r0 + r, c0 + c
                if 0 <= rr < size and 0 <= cc < size:
                    inside = 0 <= r <= 6 and 0 <= c <= 6
                    dark = inside and (r in (0, 6) or c in (0, 6)
                                       or (2 <= r <= 4 and 2 <= c <= 4))
                    m[rr][cc] = 1 if dark else 0

    finder(0, 0)
    finder(0, size - 7)
    finder(size - 7, 0)

    for i in range(8, size - 8):             # timing patterns
        m[6][i] = m[i][6] = 1 - (i & 1)

    a = _ALIGN.get(version)
    if a:                                    # 5×5 alignment: dark ring, dark centre
        for r in range(-2, 3):
            for c in range(-2, 3):
                m[a + r][a + c] = 0 if max(abs(r), abs(c)) == 1 else 1

    for i in range(9):                       # reserve the format-info cells
        if m[8][i] is None:
            m[8][i] = 0
        if m[i][8] is None:
            m[i][8] = 0
    for i in range(8):
        if m[8][size - 1 - i] is None:
            m[8][size - 1 - i] = 0
        if m[size - 1 - i][8] is None:
            m[size - 1 - i][8] = 0
    m[size - 8][8] = 1                       # the always-dark module
    return m


def _place_format(m, size, fmt):
    bit = lambda i: (fmt >> i) & 1           # LSB first, per spec bit numbering
    for i in range(6):                       # copy 1: around the top-left finder
        m[i][8] = bit(i)
    m[7][8] = bit(6)
    m[8][8] = bit(7)
    m[8][7] = bit(8)
    for i in range(9, 15):
        m[8][14 - i] = bit(i)
    for i in range(8):                       # copy 2: split across the other two
        m[8][size - 1 - i] = bit(i)
    for i in range(8, 15):
        m[size - 15 + i][8] = bit(i)
    # (the always-dark module at (size-8, 8) is stamped in _function_patterns)


def _place_data(m, size, codewords):
    """Standard zigzag: 2-module columns from the right edge, snaking up/down,
    skipping the timing column; mask 0 ((r+c) even → flip) applied inline."""
    bits = [(cw >> k) & 1 for cw in codewords for k in range(7, -1, -1)]
    i = 0
    right = size - 1
    upward = True
    while right >= 1:
        if right == 6:
            right -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for r in rows:
            for c in (right, right - 1):
                if m[r][c] is None:
                    b = bits[i] if i < len(bits) else 0   # remainder bits = 0
                    m[r][c] = b ^ (1 if (r + c) % 2 == 0 else 0)
                    i += 1
        upward = not upward
        right -= 2


def matrix(text):
    """QR matrix (list of rows of 0/1) for `text`, smallest version that fits."""
    data = text.encode("utf-8")
    for version, (total, ec) in sorted(_CAPACITY.items()):
        if len(data) <= total - ec - 2:      # 2 bytes: mode + count header
            break
    else:
        raise ValueError(f"text too long for QR v1-5 ({len(data)} bytes)")
    size = 17 + 4 * version
    m = _function_patterns(size, version)
    _place_data(m, size, _codewords(data, version))
    _place_format(m, size, _FMT_L_MASK0)
    return m


def png_pixels(text, scale=8, border=4):
    """(top-down BGRA bytes, w, h) for ocr._encode_png — dark on white with the
    spec's 4-module quiet zone (scanners genuinely need it)."""
    m = matrix(text)
    n = len(m)
    w = (n + 2 * border) * scale
    px = bytearray()
    for py in range(w):
        my = py // scale - border
        row = m[my] if 0 <= my < n else None
        for qx in range(w):
            mx = qx // scale - border
            v = 0 if (row and 0 <= mx < n and row[mx]) else 255
            px += bytes((v, v, v, 255))
    return bytes(px), w, w


if __name__ == "__main__":
    for r in matrix("http://192.168.1.23:3939/"):
        print("".join("##" if v else "  " for v in r))
