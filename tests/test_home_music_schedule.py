"""Tests for per-platform pause, 3-Shorts/Reels-per-period schedule, >5s music guard, and PT-BR Home UI."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import numpy as np

os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-hms-")
os.environ["DATABASE_URL"] = ""
sys.path.insert(0, ".")

from studio import accounts, brain, auth, creators, ctx, db, jobs, posting, quality  # noqa: E402

db.init()
ROOT = Path(__file__).resolve().parent.parent
fails = []


def check(name, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got {got!r}"))
    if not ok:
        fails.append(name)


# ------------------------------------------------------------------ 1. Quality: music >5s detection
sr = 16000

# 1a) Transcript cues
short_cue = [{"w": "[Música]", "s": 10.0, "e": 12.5}]
long_cue = [{"w": "[Música]", "s": 10.0, "e": 16.5}]
check("transcript music <=5s ignored", quality.detect_music_transcript(short_cue)[0], False)
check("transcript music >5s flagged", quality.detect_music_transcript(long_cue)[0], True)

# 1b) Audio spectrum: silence, broadband noise, low engine rumble, <=3s chord, >=7s chord
silence = np.zeros(sr * 10, dtype=np.float32)
check("audio silence not music", quality.detect_music_audio(silence)[0], False)

rng = np.random.default_rng(42)
noise = (rng.normal(0, 0.15, sr * 10)).astype(np.float32)
check("broadband white noise not music", quality.detect_music_audio(noise)[0], False)

t_ax = np.arange(sr * 10, dtype=np.float32) / sr
engine = 0.2 * np.sin(2 * np.pi * 90 * t_ax) + 0.1 * np.sin(2 * np.pi * 150 * t_ax)
check("low-frequency engine rumble not music", quality.detect_music_audio(engine.astype(np.float32))[0], False)

chord_3s = np.zeros(sr * 10, dtype=np.float32)
tc = np.arange(sr * 3, dtype=np.float32) / sr
chord_3s[sr:sr * 4] = 0.2 * (np.sin(2 * np.pi * 261.6 * tc) + np.sin(2 * np.pi * 329.6 * tc) + np.sin(2 * np.pi * 392.0 * tc))
check("tonal music <=3s not flagged", quality.detect_music_audio(chord_3s)[0], False)

chord_7s = np.zeros(sr * 10, dtype=np.float32)
tl = np.arange(sr * 7, dtype=np.float32) / sr
chord_7s[sr:sr * 8] = 0.2 * (np.sin(2 * np.pi * 261.6 * tl) + np.sin(2 * np.pi * 329.6 * tl) + np.sin(2 * np.pi * 392.0 * tl))
check("tonal music >5s flagged", quality.detect_music_audio(chord_7s)[0], True)


# ------------------------------------------------------------------ 2. Brain compilation skips has_music & Jobs converts long music blocks to Shorts
clean_clips = [
    {"start": 0.0, "end": 320.0, "score": 90, "title": "Clean 1"},
    {"start": 330.0, "end": 650.0, "score": 88, "title": "Clean 2"},
    {"start": 660.0, "end": 980.0, "score": 99, "title": "Music Part", "has_music": True},
]
comp = brain.build_compilation(clean_clips, min_len=600, max_len=1200)
check("compilation excludes has_music clip", comp is not None and len(comp["segments"]) == 2 and comp["end"] == 650.0)

u, _ = auth._create("hms@example.com", "HMS", password_hash="x")
with ctx.as_user(u.id):
    # Per-platform pause settings normalization
    st = accounts.save_settings({"paused_platforms": ["TikTok", "youtube", "unknown_plat", "youtube"]})
    check("paused_platforms normalized canonically", st["paused_platforms"], ["youtube", "tiktok"])

    c = creators.create({
        "name": "ViceTester",
        "autopost": True,
        "accounts": ["yt_h", "ig_h", "tt_h"],
        "long_enabled": True,
        "long_mode": "block",
        "long_min_len": 300,
        "long_max_len": 900,
        "daily_cap": 50,
    })
    check("creator default post_gap_min raised to 90", creators.DEFAULTS["post_gap_min"], 90)
    check("creator default daily_cap set to 12", creators.DEFAULTS["daily_cap"], 12)

    j = jobs.create({"type": "file", "name": "live_vod.mp4"}, {"long_mode": "block", "long_min_len": 300, "long_max_len": 900},
                    enqueue=False, creator=c["id"])
    words = []
    wt = 0.0
    for i in range(180):
        words.append({"w": "ação." if i % 10 == 9 else f"fala{i}", "s": round(wt, 2), "e": round(wt + 0.5, 2)})
        wt += 4.0
    db_arr = np.full(800, -25.0, dtype=np.float32)

    with patch.object(brain, "find_long_blocks", return_value=([
        {"start": 10.0, "end": 620.0, "score": 92, "title": "Missão com Música", "hashtags": ["gta6"],
         "thumb_text": "MÚSICA", "peak_t": 100.0, "has_music": True, "music_reason": "Rádio tocando >5s",
         "reason": "Missão boa"},
    ], {"model": "m", "tokens": 10, "note": ""})), \
         patch.object(brain, "find_clips", return_value=([
             {"start": 20.0, "end": 45.0, "score": 88, "category": "action", "hook": "OLHA ISSO",
              "thumb_text": "CHOCANTE", "title": "Cena rápida GTA 6", "description": "Boa cena.",
              "hashtags": ["gta6"], "highlight_words": ["cena"], "reason": "Top"},
         ], {"model": "m", "tokens": 5, "note": ""})):
        enq, _, _ = jobs._apply_long_and_derived(j, words, db_arr, 720.0, 0, "gta6", [])
        check("music long block NOT emitted as 16:9", not any(x.get("format") == "16:9" for x in enq))
        check("music long block converted to 9:16 Short", len(enq) == 1 and enq[0].get("format") == "9:16" and enq[0].get("has_music"))

    # ------------------------------------------------------------------ 3. Posting: music gate + 3/period cap + paused_platforms
    fake_accs = {
        "yt_h": {"id": "yt_h", "platform": "youtube", "name": "YT Main"},
        "ig_h": {"id": "ig_h", "platform": "instagram", "name": "IG Main"},
        "tt_h": {"id": "tt_h", "platform": "tiktok", "name": "TT Main"},
    }
    with patch.object(accounts, "get", side_effect=lambda aid: fake_accs.get(aid)):
        long_music = {"id": "lm1", "clip_kind": "long", "format": "16:9", "has_music": True,
                      "start": 0.0, "end": 700.0, "score": 95, "title": "Com Música", "status": "done"}
        j["clips"].append(long_music)
        check("enqueue refuses long 16:9 with has_music", posting.enqueue(j, long_music, ["yt_h"]), [])

        short_music = {"id": "sm1", "format": "9:16", "has_music": True,
                       "start": 0.0, "end": 40.0, "score": 90, "title": "Short com Música", "status": "done"}
        j["clips"].append(short_music)
        sm_made = posting.enqueue(j, short_music, ["yt_h", "ig_h"])
        check("enqueue allows Short/Reel 9:16 with has_music", len(sm_made), 2)

        # Period cap test: enqueue 7 Shorts onto Instagram -> at most 3 per period!
        posting._posts.clear()
        for k in range(7):
            sc = {"id": f"sc{k}", "format": "9:16", "start": 0.0, "end": 30.0, "score": 85, "title": f"S{k}", "status": "done"}
            j["clips"].append(sc)
            posting.enqueue(j, sc, ["ig_h"])
        ig_periods = {}
        for p in posting._posts:
            pk = posting._period_info(p["due_at"])[0]
            ig_periods[pk] = ig_periods.get(pk, 0) + 1
        check("shorts capped at max 3 per 6h period", all(cnt <= 3 for cnt in ig_periods.values()) and len(ig_periods) >= 3)

        # Long 16:9 does not consume Shorts/Reels period slots on YouTube
        posting._posts.clear()
        for k in range(4):
            sc = {"id": f"ys{k}", "format": "9:16", "start": 0.0, "end": 30.0, "score": 85, "title": f"YS{k}", "status": "done"}
            j["clips"].append(sc)
            posting.enqueue(j, sc, ["yt_h"])
        # Now enqueue a clean 16:9 long video on YouTube: should rebalance first without blocking shorts cap
        clean_long = {"id": "cl1", "clip_kind": "long", "format": "16:9", "has_music": False,
                      "start": 0.0, "end": 700.0, "score": 95, "title": "Limpo Longo", "status": "done"}
        j["clips"].append(clean_long)
        posting.enqueue(j, clean_long, ["yt_h"])
        yt_sorted = sorted(posting._posts, key=lambda x: x["due_at"])
        check("clean long video placed first on YouTube", yt_sorted[0]["clip"], "cl1")

        # Enqueue 3 more long videos (total 4 longs + 4 shorts): Day 1 must cap longs at 2 and still schedule shorts on Day 1
        for k in range(2, 5):
            cl_k = {"id": f"cl{k}", "clip_kind": "long", "format": "16:9", "has_music": False,
                    "start": 0.0, "end": 700.0, "score": 92, "title": f"Longo {k}", "status": "done"}
            j["clips"].append(cl_k)
            posting.enqueue(j, cl_k, ["yt_h"])
        yt_sorted2 = sorted(posting._posts, key=lambda x: x["due_at"])
        first_day = posting._day(yt_sorted2[0]["due_at"])
        day1_longs = [p for p in yt_sorted2 if posting._day(p["due_at"]) == first_day and p.get("format") == "16:9"]
        day1_shorts = [p for p in yt_sorted2 if posting._day(p["due_at"]) == first_day and p.get("format") != "16:9"]
        check("YouTube caps long 16:9 videos at max 2 per day and interleaves shorts on Day 1",
              len(day1_longs) <= 2 and len(day1_shorts) >= 1)

        # Per-platform pause runtime check
        accounts.save_settings({"paused": False, "paused_platforms": ["youtube"]})
        ov = posting.overview()
        check("overview returns paused_platforms", ov.get("paused_platforms"), ["youtube"])

        posting._posts.clear()
        now_t = time.time() - 10
        posting._posts.extend([
            {"id": "py", "user": u.id, "job": j["id"], "clip": "sm1", "creator": c["id"], "account": "yt_h",
             "account_name": "YT Main", "platform": "youtube", "status": "scheduled", "due_at": now_t, "manual_now": True},
            {"id": "pi", "user": u.id, "job": j["id"], "clip": "sm1", "creator": c["id"], "account": "ig_h",
             "account_name": "IG Main", "platform": "instagram", "status": "scheduled", "due_at": now_t, "manual_now": True},
        ])
        posted_ids = []

        class StopLoop(BaseException):
            pass

        calls = [0]

        def fake_sleep(_):
            calls[0] += 1
            if calls[0] > 1:
                raise StopLoop()

        with patch.object(posting.time, "sleep", side_effect=fake_sleep), \
             patch.object(posting, "_post", side_effect=lambda p: posted_ids.append(p["id"])):
            try:
                posting._run()
            except StopLoop:
                pass
        check("_run skips paused platform youtube and posts instagram", posted_ids, ["pi"])

        # Anti-burst check: 2 overdue non-manual posts on same account -> 1st runs, 2nd is deferred cleanly
        accounts.save_settings({"paused": False, "paused_platforms": []})
        posting._posts.clear()
        posting._posts.extend([
            {"id": "b1", "user": u.id, "job": j["id"], "clip": "sm1", "creator": c["id"], "account": "ig_h",
             "account_name": "IG Main", "platform": "instagram", "status": "scheduled", "due_at": now_t - 20, "manual_now": False},
            {"id": "b2", "user": u.id, "job": j["id"], "clip": "sc0", "creator": c["id"], "account": "ig_h",
             "account_name": "IG Main", "platform": "instagram", "status": "scheduled", "due_at": now_t - 10, "manual_now": False},
        ])
        burst_ids = []
        calls[0] = 0
        with patch.object(posting.time, "sleep", side_effect=fake_sleep), \
             patch.object(posting, "_post", side_effect=lambda p: burst_ids.append(p["id"])):
            try:
                posting._run()
            except StopLoop:
                pass
        b2 = next(p for p in posting._posts if p["id"] == "b2")
        check("_run anti-burst posts 1 and defers 2nd on same account", burst_ids == ["b1"] and b2["due_at"] > time.time())


# ------------------------------------------------------------------ 4. Home UI checks (PT-BR, filters, group, platform pause, JS syntax)
html_text = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
for needle in (
    "O que você quer clipar hoje?",
    "Fila de postagem",
    "data-act=\"pauseplat\"",
    "data-act=\"ovgroup\"",
    "data-ovf=\"creator\"",
    "data-ovf=\"platform\"",
    "data-ovf=\"status\"",
    "data-ovf=\"format\"",
    "Visão: Por clipe",
    "Pausar tudo",
):
    check(f"UI contains {needle}", needle in html_text, True)

check("UI has zero box-shadow", "box-shadow" not in html_text, True)
scripts = re.findall(r"<script[^>]*>(.*?)</script>", html_text, re.DOTALL)
js_tmp = Path(os.environ["PEAKCUT_DATA"]) / "ui_home_check.js"
js_tmp.write_text("\n".join(scripts), encoding="utf-8")
node_res = subprocess.run(["node", "--check", str(js_tmp)], capture_output=True, text=True)
check("UI JS passes node --check", node_res.returncode, 0)

print(f"\nSummary: {len(fails)} failed" if fails else "\nALL PASSED")
db.engine.dispose()
shutil.rmtree(os.environ["PEAKCUT_DATA"], ignore_errors=True)
sys.exit(1 if fails else 0)
