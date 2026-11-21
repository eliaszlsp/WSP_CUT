"""Remote access helper: detects LAN/Cloudflare tunnel URLs, manages quick tunnels, and renders crisp offline QR SVGs."""
import html
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

from . import config

_lock = threading.RLock()
_tunnel_proc = None
_tunnel_url = ""
_URL_FILE = config.DATA_DIR / "tunnel_url.txt"
_TRYCF_RE = re.compile(r"https://[a-z0-9\-]+\.trycloudflare\.com", re.I)

# ===================================================================== Pure-Python QR Code SVG Encoder (Versions 1-8, EC Level M)
_GF_EXP = [0] * 512
_GF_LOG = [0] * 256
_x = 1
for _i in range(255):
    _GF_EXP[_i] = _x
    _GF_LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _GF_EXP[_i] = _GF_EXP[_i - 255]


def _gf_mul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_gen(deg: int) -> list[int]:
    g = [1]
    for i in range(deg):
        nxt = [0] * (len(g) + 1)
        f = _GF_EXP[i]
        for j, v in enumerate(g):
            nxt[j] ^= v
            nxt[j + 1] ^= _gf_mul(v, f)
        g = nxt
    return g


def _rs_encode(data: list[int], ec_len: int) -> list[int]:
    gen = _rs_gen(ec_len)
    res = list(data) + [0] * ec_len
    for i in range(len(data)):
        coef = res[i]
        if coef != 0:
            for j in range(1, len(gen)):
                res[i + j] ^= _gf_mul(gen[j], coef)
    return res[len(data) :]


# Version -> (total_data_cw, ec_cw_per_block, (blocks_g1, cw_g1), (blocks_g2, cw_g2), align_centers)
# EC Level M (recovers ~15% damage)
_QR_SPECS = {
    1: (16, 10, (1, 16), (0, 0), ()),
    2: (28, 16, (1, 28), (0, 0), (6, 18)),
    3: (44, 26, (1, 44), (0, 0), (6, 22)),
    4: (64, 18, (2, 32), (0, 0), (6, 26)),
    5: (86, 24, (2, 43), (0, 0), (6, 30)),
    6: (108, 16, (4, 27), (0, 0), (6, 34)),
    7: (124, 18, (4, 31), (0, 0), (6, 22, 38)),
    8: (154, 22, (2, 38), (2, 39), (6, 24, 42)),
}
_QR_REMINDER = {1: 0, 2: 7, 3: 7, 4: 7, 5: 7, 6: 7, 7: 0, 8: 0}


def _bch_format(mask_id: int) -> int:
    # EC Level M = 0b00
    data = (0b00 << 3) | mask_id
    d = data << 10
    g = 0x537
    for shift in range(4, -1, -1):
        if d & (1 << (shift + 10)):
            d ^= g << shift
    return ((data << 10) | d) ^ 0x5412


def _bch_version(ver: int) -> int:
    d = ver << 12
    g = 0x1F25
    for shift in range(5, -1, -1):
        if d & (1 << (shift + 12)):
            d ^= g << shift
    return (ver << 12) | d


def qr_matrix(text: str) -> list[list[bool]]:
    """Encode UTF-8 string into an exact QR Code boolean matrix (EC Level M)."""
    raw = text.encode("utf-8")
    nbytes = len(raw)
    ver = next((v for v, s in _QR_SPECS.items() if (s[0] * 8) >= 4 + 8 + nbytes * 8), None)
    if ver is None:
        raw = raw[:150]
        nbytes = len(raw)
        ver = 8
    total_data, ec_per, (b1, d1), (b2, d2), aligns = _QR_SPECS[ver]

    # Bit stream: mode 0100 (Byte) + 8-bit count + payload + terminator + padding
    bits: list[int] = [0, 1, 0, 0]
    for s in range(7, -1, -1):
        bits.append((nbytes >> s) & 1)
    for byte in raw:
        for s in range(7, -1, -1):
            bits.append((byte >> s) & 1)
    max_bits = total_data * 8
    for _ in range(min(4, max_bits - len(bits))):
        bits.append(0)
    while len(bits) % 8:
        bits.append(0)
    pad = (0xEC, 0x11)
    pi = 0
    while len(bits) < max_bits:
        b = pad[pi & 1]
        for s in range(7, -1, -1):
            bits.append((b >> s) & 1)
        pi += 1

    cws = [sum(bits[i + k] << (7 - k) for k in range(8)) for i in range(0, len(bits), 8)]
    blocks, ec_blocks = [], []
    pos = 0
    for count, length in ((b1, d1), (b2, d2)):
        for _ in range(count):
            blk = cws[pos : pos + length]
            pos += length
            blocks.append(blk)
            ec_blocks.append(_rs_encode(blk, ec_per))

    interleaved: list[int] = []
    for i in range(max(len(b) for b in blocks)):
        for b in blocks:
            if i < len(b):
                interleaved.append(b[i])
    for i in range(ec_per):
        for eb in ec_blocks:
            interleaved.append(eb[i])
    all_bits = [(cw >> s) & 1 for cw in interleaved for s in range(7, -1, -1)] + [0] * _QR_REMINDER[ver]

    size = ver * 4 + 17
    mod = [[False] * size for _ in range(size)]
    func = [[False] * size for _ in range(size)]

    def set_f(r: int, c: int, val: bool):
        mod[r][c] = val
        func[r][c] = True

    def place_finder(r0: int, c0: int):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                rr, cc = r0 + dr, c0 + dc
                if 0 <= rr < size and 0 <= cc < size:
                    in_outer = 0 <= dr <= 6 and 0 <= dc <= 6
                    on_ring = in_outer and (dr in (0, 6) or dc in (0, 6))
                    in_core = 2 <= dr <= 4 and 2 <= dc <= 4
                    set_f(rr, cc, on_ring or in_core)

    place_finder(0, 0)
    place_finder(0, size - 7)
    place_finder(size - 7, 0)

    # Alignment patterns (skip only the 3 finder corners; allow overlapping timing strip cleanly)
    for r in aligns:
        for c in aligns:
            if (r < 9 and c < 9) or (r < 9 and c > size - 9) or (r > size - 9 and c < 9):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    set_f(r + dr, c + dc, max(abs(dr), abs(dc)) != 1)

    # Timing & dark module
    for i in range(8, size - 8):
        if not func[6][i]:
            set_f(6, i, i % 2 == 0)
        if not func[i][6]:
            set_f(i, 6, i % 2 == 0)
    set_f(size - 8, 8, True)

    # Reserve format info & version info
    for i in range(9):
        if not func[8][i]:
            set_f(8, i, False)
        if not func[i][8]:
            set_f(i, 8, False)
    for i in range(size - 8, size):
        set_f(8, i, False)
    for i in range(size - 7, size):
        set_f(i, 8, False)
    if ver >= 7:
        vbits = _bch_version(ver)
        for i in range(18):
            bit = bool((vbits >> i) & 1)
            set_f(size - 11 + (i % 3), i // 3, bit)
            set_f(i // 3, size - 11 + (i % 3), bit)

    # Zig-zag data placement
    bi = 0
    up = True
    col = size - 1
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if up else range(size)
        for row in rows:
            for dc in (0, -1):
                c = col + dc
                if not func[row][c] and bi < len(all_bits):
                    mod[row][c] = bool(all_bits[bi])
                    bi += 1
        up = not up
        col -= 2

    masks = (
        lambda r, c: (r + c) % 2 == 0,
        lambda r, c: r % 2 == 0,
        lambda r, c: c % 3 == 0,
        lambda r, c: (r + c) % 3 == 0,
        lambda r, c: (r // 2 + c // 3) % 2 == 0,
        lambda r, c: ((r * c) % 2 + (r * c) % 3) == 0,
        lambda r, c: (((r * c) % 2 + (r * c) % 3) % 2) == 0,
        lambda r, c: (((r + c) % 2 + (r * c) % 3) % 2) == 0,
    )

    def apply_mask_and_format(mask_id: int) -> list[list[bool]]:
        g = [list(row) for row in mod]
        fn = masks[mask_id]
        for r in range(size):
            for c in range(size):
                if not func[r][c] and fn(r, c):
                    g[r][c] = not g[r][c]
        fmt = _bch_format(mask_id)
        # Format bits: 14 (MSB) down to 0 (LSB) per ISO 18004 Figure 19
        tl_rows = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8),
                   (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)]
        other = [(size - 1, 8), (size - 2, 8), (size - 3, 8), (size - 4, 8),
                 (size - 5, 8), (size - 6, 8), (size - 7, 8),
                 (8, size - 8), (8, size - 7), (8, size - 6), (8, size - 5),
                 (8, size - 4), (8, size - 3), (8, size - 2), (8, size - 1)]
        for idx in range(15):
            bit = bool((fmt >> (14 - idx)) & 1)
            r1, c1 = tl_rows[idx]
            r2, c2 = other[idx]
            g[r1][c1] = bit
            g[r2][c2] = bit
        g[size - 8][8] = True
        return g

    def penalty(g: list[list[bool]]) -> int:
        pen = 0
        for row in g:
            run, prev = 0, None
            for cell in row:
                run = run + 1 if cell == prev else 1
                prev = cell
                if run == 5:
                    pen += 3
                elif run > 5:
                    pen += 1
        for c in range(size):
            run, prev = 0, None
            for r in range(size):
                cell = g[r][c]
                run = run + 1 if cell == prev else 1
                prev = cell
                if run == 5:
                    pen += 3
                elif run > 5:
                    pen += 1
        for r in range(size - 1):
            for c in range(size - 1):
                v = g[r][c]
                if v == g[r][c + 1] == g[r + 1][c] == g[r + 1][c + 1]:
                    pen += 3
        dark = sum(1 for row in g for x in row if x)
        pen += (abs(dark * 100 // (size * size) - 50) // 5) * 10
        return pen

    best = min((apply_mask_and_format(m) for m in range(8)), key=penalty)
    return best


def qr_svg(text: str, box_px: int = 240) -> str:
    """Return a high-contrast pure SVG QR code with white quiet zone so mobile cameras scan reliably."""
    mat = qr_matrix(text)
    n = len(mat)
    quiet = 3
    total = n + quiet * 2
    parts = []
    for r, row in enumerate(mat):
        c = 0
        while c < n:
            if row[c]:
                start = c
                while c < n and row[c]:
                    c += 1
                parts.append(f"M{start + quiet},{r + quiet}h{c - start}v1h-{c - start}z")
            else:
                c += 1
    d = "".join(parts)
    label = html.escape(text, quote=True)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total} {total}" '
        f'width="{box_px}" height="{box_px}" shape-rendering="crispEdges" role="img" '
        f'aria-label="QR Code para {label}">'
        f'<rect width="{total}" height="{total}" rx="2" fill="#FFFFFF"/>'
        f'<path d="{d}" fill="#090614"/></svg>'
    )


# ===================================================================== Remote & Tunnel Discovery
def _lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.2)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
    except OSError:
        pass
    try:
        host_ip = socket.gethostbyname(socket.gethostname())
        if host_ip and not host_ip.startswith("127."):
            return host_ip
    except OSError:
        pass
    return ""


def _read_saved_tunnel() -> str:
    if _tunnel_url:
        return _tunnel_url
    try:
        if _URL_FILE.is_file():
            u = _URL_FILE.read_text(encoding="utf-8").strip()
            if u.startswith("https://"):
                return u
    except OSError:
        pass
    return ""


def _find_cloudflared() -> str:
    exe = shutil.which("cloudflared")
    if exe:
        return exe
    cand = Path.home() / ".gemini" / "antigravity" / "bin" / ("cloudflared.exe" if os.name == "nt" else "cloudflared")
    if cand.is_file():
        return str(cand)
    return ""


def start_tunnel(timeout_sec: float = 12.0) -> str:
    """Launch a Cloudflare Quick Tunnel to local port if none active, save URL, and update config.PUBLIC_URL."""
    global _tunnel_proc, _tunnel_url
    with _lock:
        if _tunnel_proc and _tunnel_proc.poll() is None and _tunnel_url:
            return _tunnel_url
        bin_path = _find_cloudflared()
        if not bin_path:
            raise RuntimeError("cloudflared não encontrado neste computador.")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            [bin_path, "tunnel", "--url", f"http://127.0.0.1:{config.PORT}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=flags,
        )
        _tunnel_proc = proc
    found = []

    def _reader():
        global _tunnel_url
        if not proc.stdout:
            return
        for line in proc.stdout:
            m = _TRYCF_RE.search(line)
            if m and not _tunnel_url:
                u = m.group(0).rstrip("/")
                _tunnel_url = u
                found.append(u)
                try:
                    _URL_FILE.write_text(u, encoding="utf-8")
                except OSError:
                    pass
                config.PUBLIC_URL = u
                config.HTTPS = True

    threading.Thread(target=_reader, daemon=True).start()
    deadline = time.time() + timeout_sec
    while time.time() < deadline and not found:
        if proc.poll() is not None:
            break
        time.sleep(0.25)
    if not found:
        raise RuntimeError("Não foi possível obter a URL do túnel Cloudflare no momento.")
    return found[0]


def info(mode: str = "auto") -> dict:
    pub = config.PUBLIC_URL.rstrip("/")
    is_local_pub = any(h in pub.lower() for h in ("127.0.0.1", "localhost", "::1"))
    tunnel = _read_saved_tunnel() or ("" if is_local_pub else pub)
    if not tunnel and not is_local_pub:
        tunnel = pub
    lan_ip = _lan_ip()
    lan_url = f"http://{lan_ip}:{config.PORT}/app" if lan_ip else ""
    local_url = f"http://127.0.0.1:{config.PORT}/app"
    tunnel_app = f"{tunnel}/app" if tunnel else ""
    if mode == "lan" and lan_url:
        active_url = lan_url
    elif mode == "local":
        active_url = local_url
    else:
        active_url = tunnel_app or lan_url or local_url
    return {
        "tunnel_url": tunnel_app,
        "lan_url": lan_url,
        "local_url": local_url,
        "active_url": active_url,
        "tunnel_active": bool(tunnel),
        "cloudflared_installed": bool(_find_cloudflared()),
        "accounts_on": bool(config.ACCOUNTS),
        "qr_svg": qr_svg(active_url, box_px=228),
    }
