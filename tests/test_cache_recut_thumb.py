"""Tests for thumbnail testing, transcript/segment caching, and Re-cut with editable options."""
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx
import numpy as np
from fastapi.testclient import TestClient
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if shutil.disk_usage(tempfile.gettempdir()).free >= 4 * 1024**3:
    DATA_DIR = Path(tempfile.mkdtemp(prefix="pc-recut-test-"))
else:
    DATA_DIR = Path(tempfile.mkdtemp(prefix=".pc-recut-test-", dir=str(ROOT)))

TMP_DIR = DATA_DIR / "tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)
os.environ["PEAKCUT_DATA"] = str(DATA_DIR)
os.environ["TEMP"] = str(TMP_DIR)
os.environ["TMP"] = str(TMP_DIR)
os.environ["DATABASE_URL"] = ""
os.environ["PEAKCUT_ACCOUNTS"] = "0"
os.environ["PEAKCUT_SKIP_STATIC"] = "0"
tempfile.tempdir = str(TMP_DIR)

from studio import brain, ctx, db, jobs, media, render, server, thumbs  # noqa: E402

db.init()
render.install_fonts()


def _cleanup():
    try:
        db.engine.dispose()
    except Exception:
        pass
    shutil.rmtree(DATA_DIR, ignore_errors=True)


atexit.register(_cleanup)

fails = []


def check(name, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got {got!r}, want {want!r}"))
    if not ok:
        fails.append(name)


def _make_sample_mp4(path: Path, seconds: int = 8):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc=size=1280x720:rate=15:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(path),
        ],
        check=True,
    )


# 1. clean_options supports 2 min / 4 min / 5 min cuts while preserving long_min_len/long_max_len
check("jobs.LIMITS long_min_len untouched", jobs.LIMITS["long_min_len"], (300, 1200))
check("jobs.LIMITS long_max_len untouched", jobs.LIMITS["long_max_len"], (600, 1800))
c2 = jobs.clean_options({"min_len": 120, "max_len": 300, "layout": "horizontal"})
check("clean_options accepts min_len=120", c2["min_len"], 120)
check("clean_options accepts max_len=300", c2["max_len"], 300)
check("clean_options accepts horizontal layout", c2["layout"], "horizontal")

# 2. thumbs.generate creates 1280x720 JPEG locally with PT-BR accents
sample_mp4 = DATA_DIR / "sample.mp4"
_make_sample_mp4(sample_mp4, seconds=8)
thumb_out = DATA_DIR / "t1.jpg"
thumbs.generate(sample_mp4, thumb_out, peak_t=3.0, text="ATÉ NÃO DAR MAIS?!", mode="local", collage=True)
with Image.open(thumb_out) as im:
    check("thumbs.generate outputs 1280x720", im.size, (1280, 720))

# 3. brain.find_clips excludes existing intervals and adapts prompt for >=120s
words = []
for i in range(420):
    t0 = round(i * 1.0, 2)
    words.append({"w": f"palavra{i}", "s": t0, "e": round(t0 + 0.5, 2)})
db_arr = np.zeros(430, dtype=np.float32)

seen_prompts = []


def _fake_ask(prompt, schema=None, purpose="AI call", timeout=None):
    seen_prompts.append(prompt)
    return [
        {"start": 10.0, "end": 40.0, "score": 95, "hook": "OLD", "title": "Old", "description": "D", "hashtags": ["gta"], "reason": "r"},
        {"start": 100.0, "end": 340.0, "score": 90, "hook": "NOVO CORTE", "title": "Novo 4 min", "description": "D2", "hashtags": ["gta"], "thumb_text": "ABSURDO?!", "reason": "r2"},
    ], "mock-model", 100


with patch.object(brain, "ask_ai", side_effect=_fake_ask):
    picked, info = brain.find_clips(
        words, db_arr, 420.0, title="Live GTA", uploader="Tester",
        max_clips=4, min_len=210, max_len=270, output_lang="pt",
        exclude=[{"start": 8.0, "end": 45.0}],
    )
check("brain.find_clips skipped overlapping clip", len(picked) == 1 and picked[0]["start"] >= 95.0, True)
check("brain.find_clips prompt mentions excluded range", bool(seen_prompts and "8-45s" in seen_prompts[0]), True)
check("brain.find_clips adapted long-segment wording when max_len>=120", bool(seen_prompts and "self-contained narrative segments" in seen_prompts[0]), True)

# 4. jobs.request_recut + _run_recut uses cached words.json, preserves existing clips, creates 16:9 clip02 reusing cached src
UID = "u_local"
with ctx.as_user(UID):
    j = jobs.create({"type": "url", "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "name": "vid"}, {"max_clips": 1}, enqueue=False)
    jdir = Path(j["dir"])
    (jdir / "clips").mkdir(parents=True, exist_ok=True)
    cached_src = jdir / "clips" / "clip01.src.mp4"
    shutil.copyfile(sample_mp4, cached_src)
    (jdir / "words.json").write_text(json.dumps(words), encoding="utf-8")
    jobs._save_loudness(jdir, 0.0, db_arr)
    j.update(
        status="done", duration=420.0, width=1280, height=720,
        clips=[{
            "id": "clip01", "start": 0.0, "end": 4.0, "score": 80, "hook": "FIRST", "title": "First",
            "description": "d", "hashtags": [], "status": "done", "version": 1,
            "file": "clip01.mp4", "cover": "clip01.jpg", "src": str(cached_src), "src_offset": 0.0, "src_end": 8.0,
        }],
    )
    jobs.save(j)

    # regenerate_thumb using cached clip01.src.mp4
    updated = jobs.regenerate_thumb(j["id"], "clip01", text="TESTE THUMB!", mode="local")
    c1 = updated["clips"][0]
    check("regenerate_thumb updated thumb_text", c1["thumb_text"], "TESTE THUMB!")
    check("regenerate_thumb incremented version", c1["version"], 2)
    check("regenerate_thumb wrote cover jpg", (jdir / "clips" / "clip01.jpg").is_file(), True)

    # render_compilation consumes segments with cached src/src_offset
    comp_out = render.render_compilation(
        cached_src, jdir / "clips", "comp_test",
        {"segments": [{"start": 0.5, "end": 2.5, "src": str(cached_src), "src_offset": 0.0},
                      {"start": 4.0, "end": 6.0, "src": str(cached_src), "src_offset": 0.0}],
         "thumb_text": "COMPILAÇÃO!", "peak_t": 1.5},
        1280, 720,
    )
    check("render_compilation with cached segment srcs succeeded", (jdir / "clips" / comp_out["file"]).is_file(), True)

    # _run_recut without calling download_audio or transcribe
    jobs.request_recut(j["id"], {"max_clips": 1, "min_len": 3, "max_len": 15, "layout": "horizontal", "thumb_mode": "local", "range_start": 0, "range_end": 120})
    check("request_recut queued pending_recut", bool(j.get("pending_recut")), True)
    # Remove from queue before running directly in-process
    with jobs._q.cv:
        while jobs._q.lines.get(UID):
            jobs._q.lines[UID].popleft()

    def _recut_ai(prompt, schema=None, purpose="AI call", timeout=None):
        return [{"start": 4.0, "end": 7.5, "score": 88, "hook": "NOVO", "title": "Novo 16:9", "description": "d", "hashtags": ["gta"], "thumb_text": "EPICO!", "reason": "ok"}], "mock-ai", 50

    with patch.object(brain, "ask_ai", side_effect=_recut_ai), \
         patch.object(media, "download_audio", side_effect=AssertionError("must not download audio on recut")), \
         patch.object(media, "download_section", side_effect=AssertionError("must reuse cached clip01.src.mp4")):
        # Allow short min_len=3 cleanly in recut test by directly adjusting cleaned min_len (clean_options clamps to 5; test span is 5..8s)
        j["pending_recut"]["min_len"] = 3
        j["pending_recut"]["max_len"] = 8
        with patch.object(brain, "_snap", return_value=(4.0, 7.5)):
            jobs._run_recut(j)

    check("recut preserved clip01", j["clips"][0]["id"], "clip01")
    check("recut added clip02", len(j["clips"]) >= 2 and j["clips"][1]["id"] == "clip02", True)
    check("recut clip02 is 16:9 horizontal", j["clips"][1].get("format") == "16:9" and j["clips"][1].get("layout") == "horizontal", True)
    check("recut clip02 rendered mp4 and jpg", (jdir / "clips" / "clip02.mp4").is_file() and (jdir / "clips" / "clip02.jpg").is_file(), True)

# 5. FastAPI routes (/api/jobs, /api/jobs/{jid}/clips/{cid}/thumb, /api/jobs/{jid}/recut)
client = TestClient(server.app, base_url="http://127.0.0.1:8765")
r_list = client.get("/api/jobs")
check("GET /api/jobs exposes has_cache=True", r_list.status_code == 200 and any(x["id"] == j["id"] and x.get("has_cache") is True for x in r_list.json()), True)

r_thumb = client.post(f"/api/jobs/{j['id']}/clips/clip01/thumb", json={"thumb_text": "API THUMB!", "thumb_mode": "local"})
check("POST /thumb returns 200 and updates thumb_text", r_thumb.status_code == 200 and r_thumb.json()["clips"][0]["thumb_text"] == "API THUMB!", True)

# 6. Thumbnail error classification + unique variations per click
check("classify billing_cap", thumbs.classify_api_error(429, "Your project has exceeded its monthly spending cap")["category"], "billing_cap")
check("classify rate_limit", thumbs.classify_api_error(429, "Too many requests rate limit")["category"], "rate_limit")
check("classify auth", thumbs.classify_api_error(403, "API key not valid")["category"], "auth")
check("classify model_unavailable", thumbs.classify_api_error(404, "model not found")["category"], "model_unavailable")
check("classify server_error", thumbs.classify_api_error(503, "unavailable")["category"], "server_error")
check("classify safety_blocked", thumbs.classify_api_error(200, "SAFETY blocked")["category"], "safety_blocked")
check("classify timeout", thumbs.classify_api_error(exc=httpx.ReadTimeout("timed out"))["category"], "timeout")
check("classify connection", thumbs.classify_api_error(exc=httpx.ConnectError("dns fail"))["category"], "connection")
check("classify api_error", thumbs.classify_api_error(400, "bad input")["category"], "api_error")
with patch("studio.ai.current", return_value={"provider": "gemini", "models": [], "base_url": "", "key": "SECRET_TEST_KEY_123"}):
    e_red = thumbs.classify_api_error(403, "bad key SECRET_TEST_KEY_123")
    check("classify never leaks api keys", "SECRET_TEST_KEY_123" not in str(e_red), True)

with ctx.as_user(UID):
    t_prev = jobs.regenerate_thumb(j["id"], "clip01", text="VAR TEST", mode="local", new_variant=True)
    v1 = t_prev["clips"][0]["thumb_variant"]
    pt1 = t_prev["clips"][0]["peak_t"]
    b1 = (jdir / "clips" / "clip01.jpg").read_bytes()
    t_next = jobs.regenerate_thumb(j["id"], "clip01", text="VAR TEST", mode="local", new_variant=True)
    v2 = t_next["clips"][0]["thumb_variant"]
    pt2 = t_next["clips"][0]["peak_t"]
    b2 = (jdir / "clips" / "clip01.jpg").read_bytes()
check("two regenerate_thumb clicks advance variant", v2 > v1, True)
check("two regenerate_thumb clicks pick different frame times", pt1 != pt2, True)
check("two regenerate_thumb clicks produce distinct image bytes", b1 != b2, True)


class _MockHTTPResp:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text

    def json(self):
        return {}


with patch.object(thumbs, "_gemini_thumb_cfg", return_value=("https://generativelanguage.googleapis.com/v1beta/models", "AIzaFakeKeyForTest")), \
     patch.object(thumbs, "_pick_best_frames_ai", side_effect=lambda fs: (fs[0], fs[-1])):
    with patch("studio.thumbs.httpx.post", return_value=_MockHTTPResp(429, "Your project has exceeded its monthly spending cap.")):
        r_cap = client.post(f"/api/jobs/{j['id']}/clips/clip01/thumb", json={"thumb_text": "IA TEST", "thumb_mode": "ai", "new_variant": True})
    rep_cap = r_cap.json().get("thumb_report") or {}
    check("AI 429 spending cap reports billing_cap + fallback", r_cap.status_code == 200 and rep_cap.get("fallback") is True and (rep_cap.get("error") or {}).get("category") == "billing_cap", True)

    with patch("studio.thumbs.httpx.post", side_effect=httpx.ConnectError("connection refused")):
        r_conn = client.post(f"/api/jobs/{j['id']}/clips/clip01/thumb", json={"thumb_text": "IA TEST", "thumb_mode": "ai", "new_variant": True})
    rep_conn = r_conn.json().get("thumb_report") or {}
    check("AI ConnectError reports connection + fallback", r_conn.status_code == 200 and rep_conn.get("fallback") is True and (rep_conn.get("error") or {}).get("category") == "connection", True)

    with patch("studio.thumbs.httpx.post", side_effect=httpx.ReadTimeout("timed out")):
        r_to = client.post(f"/api/jobs/{j['id']}/clips/clip01/thumb", json={"thumb_text": "IA TEST", "thumb_mode": "ai", "new_variant": True})
    rep_to = r_to.json().get("thumb_report") or {}
    check("AI ReadTimeout reports timeout + fallback", r_to.status_code == 200 and rep_to.get("fallback") is True and (rep_to.get("error") or {}).get("category") == "timeout", True)

    # Restore local thumb cleanly on clip01 before recut route test
    client.post(f"/api/jobs/{j['id']}/clips/clip01/thumb", json={"thumb_text": "API THUMB!", "thumb_mode": "local"})

with patch.object(jobs._q, "put") as mock_put:
    r_recut = client.post(f"/api/jobs/{j['id']}/recut", json={"max_clips": 2, "min_len": 210, "max_len": 270, "layout": "horizontal", "thumb_mode": "local"})
check("POST /recut returns 200 and enqueues recut", r_recut.status_code == 200 and mock_put.called, True)

print("ALL PASSED" if not fails else f"{len(fails)} failed: {fails}")
sys.exit(1 if fails else 0)
