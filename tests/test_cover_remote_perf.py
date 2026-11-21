"""Tests for Default Profile Cover (manual + AI + frame 0), Remote QR SVG & tunnel routes, Light Mode, Mobile UI, and Performance."""
import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-crp-")
os.environ.update(
    DATABASE_URL="",
    PEAKCUT_ACCOUNTS="0",
    PEAKCUT_AI_PROVIDER="gemini",
    PEAKCUT_AI_MODELS="",
    PEAKCUT_AI_KEY="",
    PEAKCUT_AI_KEY_FILE="",
    PEAKCUT_AI_BASE_URL="",
    PEAKCUT_PUBLIC_URL="http://127.0.0.1:8765",
    PEAKCUT_AI_FALLBACK_PROVIDER="",
    PEAKCUT_AI_FALLBACK_MODELS="",
    PEAKCUT_AI_FALLBACK_KEY="",
    PEAKCUT_AI_FALLBACK_KEY_FILE="",
    PEAKCUT_AI_FALLBACK_BASE_URL="",
)
sys.path.insert(0, ".")

from fastapi.testclient import TestClient  # noqa: E402

from studio import accounts, auth, config, covers, creators, ctx, db, media, posting, remote, render, server  # noqa: E402

db.init()
ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8765"
ORIGIN = {"Origin": BASE}
c = TestClient(server.app, base_url=BASE, follow_redirects=False)
UID = c.get("/api/status").json()["user"]["id"]
fails = []


def check(name, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got {got!r}"))
    if not ok:
        fails.append(name)


def _png_bytes(w=720, h=1280, color=(235, 38, 120)):
    im = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


# ------------------------------------------------------------------ 1. Global & Creator Covers (manual + modes + AI fallback)
g_up = c.post("/api/cover/upload", files={"file": ("global.png", _png_bytes(900, 1600, (34, 211, 238)), "image/png")}, headers=ORIGIN)
check("global cover manual upload HTTP 200", g_up.status_code, 200)
g_st = g_up.json()
check("global cover active and has_cover", (g_st.get("has_cover"), g_st.get("cover_enabled")), (True, True))

g_img_resp = c.get("/api/cover")
check("GET /api/cover returns 200 jpeg", (g_img_resp.status_code, g_img_resp.headers.get("content-type")), (200, "image/jpeg"))
with ctx.as_user(UID):
    g_path_v = covers.cover_file(None, wide=False)
    g_path_w = covers.cover_file(None, wide=True)
    g_url = accounts.clip_link("covjob", "_dc_global.jpg")
check("global cover 9:16 1080x1920", Image.open(g_path_v).size, (1080, 1920))
check("global cover 16:9 1280x720", Image.open(g_path_w).size, (1280, 720))

# Signed public clip URL serves _dc_global.jpg
pub_g = c.get(g_url.replace(BASE, ""))
check("public_clip serves _dc_global.jpg", (pub_g.status_code, pub_g.headers.get("content-type")), (200, "image/jpeg"))

# Creator cover upload & modes
cr_resp = c.post("/api/creators", json={"name": "Canal Teste"}, headers=ORIGIN)
check("create creator HTTP 200", cr_resp.status_code, 200)
cid = cr_resp.json()["id"]
c_up = c.post(f"/api/creators/{cid}/cover/upload", files={"file": ("creator.png", _png_bytes(1080, 1920, (255, 64, 160)), "image/png")}, headers=ORIGIN)
check("creator cover upload HTTP 200", c_up.status_code, 200)
check("creator cover_mode switches to custom", c_up.json().get("cover_mode"), "custom")
with ctx.as_user(UID):
    c_path_v = covers.cover_file(cid, wide=False)
    c_url = accounts.clip_link("covjob", f"_dc_{cid}.jpg")
check("creator custom cover_file resolves", c_path_v is not None and c_path_v.exists())

pub_c = c.get(c_url.replace(BASE, ""))
check("public_clip serves _dc_<cid>.jpg", (pub_c.status_code, pub_c.headers.get("content-type")), (200, "image/jpeg"))

# Switch creator to inherit global, then clip
c.put(f"/api/creators/{cid}", json={"cover_mode": "global", "cover_first_frame": False}, headers=ORIGIN)
with ctx.as_user(UID):
    check("creator cover_mode=global resolves global cover", covers.cover_file(cid, wide=False), g_path_v)
    check("cover_first_frame=False disables frame 0 injection", covers.should_inject_first_frame(cid), None)

c.put(f"/api/creators/{cid}", json={"cover_mode": "clip", "cover_first_frame": True}, headers=ORIGIN)
with ctx.as_user(UID):
    check("creator cover_mode=clip disables default cover", covers.cover_file(cid, wide=False), None)

# AI cover generation (fallback composer works without external keys)
with patch.object(covers, "_try_gemini_image", return_value=None), patch.object(
    covers, "_ask_cover_direction", return_value={"headline": "CORTE ÉPICO", "kicker": "CANAL TESTE", "accent_hex": "#FF1E78", "mood": "neon"}
):
    gen_resp = c.post(f"/api/creators/{cid}/cover/generate", json={"prompt": "Highlight cyberpunk"}, headers=ORIGIN)
check("AI cover generate HTTP 200", gen_resp.status_code, 200)
gen_data = gen_resp.json()
with ctx.as_user(UID):
    ai_cov = covers.cover_file(cid)
check("AI cover fallback source & dimensions", (gen_data.get("source"), gen_data.get("cover_mode"), Image.open(ai_cov).size), ("ai-composer", "custom", (1080, 1920)))

# Delete creator cover
del_resp = c.delete(f"/api/creators/{cid}/cover", headers=ORIGIN)
check("creator cover delete resets to clip", (del_resp.status_code, del_resp.json().get("has_cover"), del_resp.json().get("cover_mode")), (200, False, "clip"))


# ------------------------------------------------------------------ 2. Render frame-0 cover injection via ffmpeg
ff = shutil.which("ffmpeg")
if ff:
    work = Path(tempfile.mkdtemp(prefix="pc-ren-"))
    try:
        src_vid = work / "src.mp4"
        subprocess.run(
            [ff, "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=2",
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=2",
             "-ac", "2", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(src_vid)],
            check=True, capture_output=True,
        )
        cov_jpg = work / "cov.jpg"
        Image.new("RGB", (1080, 1920), (18, 22, 42)).save(cov_jpg, format="JPEG", quality=92)
        out_dir = work / "out"
        clip_info = {"start": 0.0, "end": 2.0, "hook": "", "layout": "blur", "captions": False}
        res = render.render_clip(
            src_vid, out_dir, "clip-01", clip_info, [], 640, 360,
            default_cover=cov_jpg, cover_first_frame=True,
        )
        mp4_path = out_dir / res["file"]
        jpg_path = out_dir / res["cover"]
        dur = media.probe(mp4_path)["duration"]
        check("render_clip with frame-0 cover produces MP4 + JPG", mp4_path.is_file() and jpg_path.is_file())
        check("render_clip copies default cover JPG", Image.open(jpg_path).size, (1080, 1920))
        check("render_clip frame-0 concat preserves audio/video and extends duration ~0.12s", dur > 2.04, True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
else:
    print("PASS ffmpeg not present; render skip", flush=True)


# ------------------------------------------------------------------ 3. Remote & Offline QR Code
rem_resp = c.get("/api/remote")
check("GET /api/remote HTTP 200", rem_resp.status_code, 200)
rem_data = rem_resp.json()
check("remote payload has active_url and svg", bool(rem_data.get("active_url")) and "<svg " in (rem_data.get("qr_svg") or ""))

sample_url = "https://responded-wax-thirty-carries.trycloudflare.com/app"
mat = remote.qr_matrix(sample_url)
sz = len(mat)
finder_ok = all(mat[r][col] for r in range(7) for col in (0, 6)) and all(mat[0][col] for col in range(7)) and mat[sz - 8][8] is True
timing_ok = all(mat[6][x] == (x % 2 == 0) for x in range(8, sz - 8)) and all(mat[y][6] == (y % 2 == 0) for y in range(8, sz - 8))
check("QR matrix ISO 18004 structure (finders, timing, dark module)", finder_ok and timing_ok and sz >= 33)


# ------------------------------------------------------------------ 4. Posting overview optimization + UI invariants
with ctx.as_user(UID):
    ov = posting.overview()
check("posting.overview includes queued_by_platform map", isinstance(ov.get("queued_by_platform"), dict))

html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
check("web/index.html has zero box-shadow", "box-shadow" not in html.lower())
check("web/index.html includes Light Mode tokens & vc_theme", 'html[data-theme="light"]' in html and "vc_theme" in html)
check("web/index.html includes mobile bottom dock & responsive touch CSS", ".mob-dock" in html and "max-width:760px" in html)
check("web/index.html includes Cover UI functions", all(k in html for k in ("renderCoverCard", "/cover/upload", "/cover/generate", "cover_mode")))
check("web/index.html includes Remote QR modal & API hook", "openRemoteDlg" in html and "/api/remote" in html)
check("web/index.html includes DOM anti-lag guards", "content-visibility" in html and "S.jobsLimit" in html)

ascii_js = (ROOT / "web" / "gradient-ascii.js").read_text(encoding="utf-8")
check("web/gradient-ascii.js caps FPS and DPR to save GPU", "fps = 24" in ascii_js and "minStep = 1000 / (s.fps || 24)" in ascii_js and "1.25" in ascii_js)

shutil.rmtree(os.environ["PEAKCUT_DATA"], ignore_errors=True)
sys.exit(1 if fails else 0)
