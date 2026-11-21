"""Tests for Task 1: long-video options, priority queue, and per-platform gap floors."""
import atexit
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if shutil.disk_usage(tempfile.gettempdir()).free >= 4 * 1024**3:
    DATA_DIR = Path(tempfile.mkdtemp(prefix="pc-long-test-"))
else:
    DATA_DIR = Path(tempfile.mkdtemp(prefix=".pc-long-test-", dir=str(ROOT)))

TMP_DIR = DATA_DIR / "tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)
os.environ["PEAKCUT_DATA"] = str(DATA_DIR)
os.environ["TEMP"] = str(TMP_DIR)
os.environ["TMP"] = str(TMP_DIR)
os.environ["DATABASE_URL"] = ""
os.environ["PEAKCUT_ACCOUNTS"] = "0"
tempfile.tempdir = str(TMP_DIR)

from studio import accounts, brain, creators, ctx, db, jobs, live, media, posting, render  # noqa: E402

db.init()


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


def raises(fn, exc_type=ValueError, msg_sub=None):
    try:
        fn()
    except exc_type as e:
        if msg_sub and msg_sub not in str(e):
            return False
        return True
    except Exception:
        return False
    return False


# ---- 1. studio/creators.py ----
check("creators.DEFAULTS long_mode", creators.DEFAULTS.get("long_mode"), "off")
check("creators.DEFAULTS long_min_len", creators.DEFAULTS.get("long_min_len"), 600)
check("creators.DEFAULTS long_max_len", creators.DEFAULTS.get("long_max_len"), 1200)
check("creators.DEFAULTS derive_shorts", creators.DEFAULTS.get("derive_shorts"), True)
check("creators.DEFAULTS thumb_mode", creators.DEFAULTS.get("thumb_mode"), "local")
check("creators.NUMERIC long_min_len", creators.NUMERIC.get("long_min_len"), (300, 1200))
check("creators.NUMERIC long_max_len", creators.NUMERIC.get("long_max_len"), (600, 1800))

cleaned = creators._clean({
    "long_mode": "invalid_mode",
    "thumb_mode": "invalid_thumb",
    "derive_shorts": 0,
    "long_min_len": 100,
    "long_max_len": 5000,
})
check("creators._clean invalid long_mode falls back to off", cleaned.get("long_mode"), "off")
check("creators._clean invalid thumb_mode falls back to local", cleaned.get("thumb_mode"), "local")
check("creators._clean derive_shorts bool", cleaned.get("derive_shorts"), False)
check("creators._clean clamps long_min_len low", cleaned.get("long_min_len"), 300)
check("creators._clean clamps long_max_len high", cleaned.get("long_max_len"), 1800)

cleaned_valid = creators._clean({"long_mode": "both", "thumb_mode": "ai", "derive_shorts": True})
check("creators._clean valid long_mode", cleaned_valid.get("long_mode"), "both")
check("creators._clean valid thumb_mode", cleaned_valid.get("thumb_mode"), "ai")
check("creators._clean valid derive_shorts", cleaned_valid.get("derive_shorts"), True)

UID = "local_test_user"

with ctx.as_user(UID):
    c1 = creators.create({
        "name": "Long Creator",
        "long_mode": "compile",
        "long_min_len": 1000,
        "long_max_len": 800,
        "derive_shorts": False,
        "thumb_mode": "ai",
    })
    check("creators.create clamps long_min_len when >= long_max_len", c1["long_min_len"] < c1["long_max_len"], True)
    check("creators.create clamped value", c1["long_min_len"], 740)
    check("creators.create preserves long_mode", c1["long_mode"], "compile")
    check("creators.create preserves thumb_mode", c1["thumb_mode"], "ai")
    check("creators.create preserves derive_shorts", c1["derive_shorts"], False)

    check(
        "creators.update raises ValueError when long_min_len >= long_max_len",
        raises(
            lambda: creators.update(c1["id"], {"long_min_len": 900, "long_max_len": 900}),
            ValueError,
            "Shortest long video must be shorter than longest long video.",
        ),
    )

# ---- 2. studio/jobs.py ----
check("jobs.DEFAULTS long_mode", jobs.DEFAULTS.get("long_mode"), "off")
check("jobs.DEFAULTS long_min_len", jobs.DEFAULTS.get("long_min_len"), 600)
check("jobs.DEFAULTS long_max_len", jobs.DEFAULTS.get("long_max_len"), 1200)
check("jobs.DEFAULTS derive_shorts", jobs.DEFAULTS.get("derive_shorts"), True)
check("jobs.DEFAULTS thumb_mode", jobs.DEFAULTS.get("thumb_mode"), "local")
check("jobs.LIMITS long_min_len", jobs.LIMITS.get("long_min_len"), (300, 1200))
check("jobs.LIMITS long_max_len", jobs.LIMITS.get("long_max_len"), (600, 1800))

check(
    "jobs.clean_options rejects non-bool derive_shorts",
    raises(lambda: jobs.clean_options({"derive_shorts": "yes"}), ValueError),
)
j_clean = jobs.clean_options({
    "long_mode": "bogus",
    "thumb_mode": "bogus",
    "derive_shorts": False,
    "long_min_len": 100,
    "long_max_len": 3000,
})
check("jobs.clean_options invalid long_mode", j_clean.get("long_mode"), "off")
check("jobs.clean_options invalid thumb_mode", j_clean.get("thumb_mode"), "local")
check("jobs.clean_options derive_shorts", j_clean.get("derive_shorts"), False)
check("jobs.clean_options long_min_len clamp", j_clean.get("long_min_len"), 300)
check("jobs.clean_options long_max_len clamp", j_clean.get("long_max_len"), 1800)

UID = "local_test_user"

with ctx.as_user(UID):
    j1 = jobs.create(
        {"type": "file", "name": "stream.mp4"},
        {"long_min_len": 1100, "long_max_len": 900},
        enqueue=False,
        creator=c1["id"],
    )
    check("jobs.create inherits long_mode from creator", j1["options"]["long_mode"], "compile")
    check("jobs.create inherits thumb_mode from creator", j1["options"]["thumb_mode"], "ai")
    check("jobs.create inherits derive_shorts from creator", j1["options"]["derive_shorts"], False)
    check("jobs.create clamps long_min_len < long_max_len", j1["options"]["long_min_len"], 840)

    j1["clips"] = [
        {"id": "long1", "start": 0.0, "end": 1200.0, "format": "16:9", "status": "done", "style": "bounce", "layout": "blur"},
        {"id": "short1", "start": 0.0, "end": 45.0, "format": "9:16", "status": "done", "style": "bounce", "layout": "blur"},
    ]
    jobs.save(j1)

    with patch.object(jobs._q, "put"):
        jobs.request_render(j1["id"], "long1", {"start": 0, "end": 1800, "thumb_text": "X" * 60})
        updated_long = next(c for c in jobs.get(j1["id"])["clips"] if c["id"] == "long1")
        check("request_render allows 1800s for 16:9", updated_long["end"], 1800.0)
        check("request_render truncates thumb_text to 40", updated_long.get("thumb_text"), "X" * 40)

    check(
        "request_render rejects >1800s for 16:9",
        raises(lambda: jobs.request_render(j1["id"], "long1", {"start": 0, "end": 1801}), ValueError),
    )
    check(
        "request_render rejects >180s for 9:16",
        raises(lambda: jobs.request_render(j1["id"], "short1", {"start": 0, "end": 181}), ValueError),
    )

# ---- 3. studio/posting.py ----
check("platform_gap tiktok floor", posting.platform_gap("tiktok", 5), 120)
check("platform_gap instagram floor", posting.platform_gap("instagram", 10), 90)
check("platform_gap youtube floor", posting.platform_gap("youtube", 5), 90)
check("platform_gap youtube respects larger gap", posting.platform_gap("youtube", 120), 120)
check("platform_gap other floor", posting.platform_gap("twitch", 1), 5)

check("clip_priority 16:9 is 1", posting.clip_priority({"format": "16:9"}), 1)
check("clip_priority derived short is 2", posting.clip_priority({"format": "9:16", "parent_clip": "long1"}), 2)
check("clip_priority standalone short is 3", posting.clip_priority({"format": "9:16"}), 3)
check("clip_priority default is 3", posting.clip_priority({}), 3)

UID = "local_test_user"

with ctx.as_user(UID):
    c_post = creators.create({
        "name": "Post Creator",
        "autopost": True,
        "post_gap_min": 5,
        "post_min_score": 70,
        "accounts": ["yt1", "tt1", "ig1"],
    })
    j_post = jobs.create({"type": "file", "name": "vod.mp4"}, {}, enqueue=False, creator=c_post["id"])
    j_post["clips"] = [
        {"id": "c_long", "format": "16:9", "score": 90, "title": "Long Ep", "status": "done"},
        {"id": "c_derived", "format": "9:16", "parent_clip": "c_long", "score": 90, "title": "Derived", "status": "done"},
        {"id": "c_short", "format": "9:16", "score": 90, "title": "Standalone", "status": "done"},
        {"id": "c_low", "format": "16:9", "score": 40, "title": "Low Score", "status": "done"},
    ]
    jobs.save(j_post)

    fake_accounts = {
        "yt1": {"id": "yt1", "name": "YT Main", "platform": "youtube"},
        "tt1": {"id": "tt1", "name": "TT Main", "platform": "tiktok"},
        "ig1": {"id": "ig1", "name": "IG Main", "platform": "instagram"},
    }

    with patch.object(accounts, "get", side_effect=lambda aid: fake_accounts.get(aid)):
        made_long = posting.enqueue(j_post, j_post["clips"][0], ["yt1", "tt1", "ig1"])
        check("enqueue 16:9 skips non-youtube accounts", [p["account"] for p in made_long], ["yt1"])
        check("enqueue 16:9 sets priority 1", made_long[0].get("priority"), 1)
        check("enqueue 16:9 sets format 16:9", made_long[0].get("format"), "16:9")

        # Enqueue derived short after long video on yt1, tt1, ig1 to verify per-platform gap floors
        t_before = time.time()
        made_derived = posting.enqueue(j_post, j_post["clips"][1], ["yt1", "tt1", "ig1"])
        check("enqueue 9:16 includes all 3 platforms", len(made_derived), 3)
        by_acc = {p["account"]: p for p in made_derived}
        check("enqueue derived sets priority 2", by_acc["yt1"].get("priority"), 2)
        check("enqueue derived sets format 9:16", by_acc["yt1"].get("format"), "9:16")
        yt_gap = by_acc["yt1"]["due_at"] - made_long[0]["due_at"]
        check("youtube gap floor >= 20 min", yt_gap >= 20 * 60 - 1, True)

        # Enqueue standalone short on tt1 to check tiktok 30m floor
        made_short = posting.enqueue(j_post, j_post["clips"][2], ["tt1"])
        check("enqueue standalone sets priority 3", made_short[0].get("priority"), 3)
        tt_gap = made_short[0]["due_at"] - by_acc["tt1"]["due_at"]
        check("tiktok gap floor >= 30 min", tt_gap >= 30 * 60 - 1, True)

        # on_clip_ready score check for 16:9
        before_count = len(posting._posts)
        posting.on_clip_ready(j_post, j_post["clips"][3])  # score 40 < 70
        check("on_clip_ready skips low-score 16:9 clip", len(posting._posts), before_count)

        # overview includes priority and format
        ov = posting.overview()
        check("overview posts include priority", all("priority" in p for p in ov["posts"]), True)
        check("overview posts include format", all("format" in p for p in ov["posts"]), True)

    # _run() priority ordering test
    posting._posts.clear()
    now_t = time.time()
    posting._posts.extend([
        {"id": "p3", "user": UID, "job": j_post["id"], "clip": "c_short", "creator": c_post["id"],
         "account": "yt1", "account_name": "YT", "platform": "youtube", "status": "scheduled",
         "due_at": now_t - 30, "created": now_t - 30, "priority": 3, "format": "9:16", "manual_now": True},
        {"id": "p2", "user": UID, "job": j_post["id"], "clip": "c_derived", "creator": c_post["id"],
         "account": "yt1", "account_name": "YT", "platform": "youtube", "status": "scheduled",
         "due_at": now_t - 20, "created": now_t - 20, "priority": 2, "format": "9:16", "manual_now": True},
        {"id": "p1", "user": UID, "job": j_post["id"], "clip": "c_long", "creator": c_post["id"],
         "account": "yt1", "account_name": "YT", "platform": "youtube", "status": "scheduled",
         "due_at": now_t - 10, "created": now_t - 10, "priority": 1, "format": "16:9", "manual_now": True},
    ])
    order = []

    class StopLoop(BaseException):
        pass

    sleep_calls = [0]

    def fake_sleep(_):
        sleep_calls[0] += 1
        if sleep_calls[0] > 1:
            raise StopLoop()

    with patch.object(posting.time, "sleep", side_effect=fake_sleep), \
         patch.object(accounts, "settings", return_value={"paused": False}), \
         patch.object(posting, "_post", side_effect=lambda p: order.append(p["id"])):
        try:
            posting._run()
        except StopLoop:
            pass
    check("_run processes priority 1 -> 2 -> 3", order, ["p1", "p2", "p3"])

# ---- 4. Task 2: brain.find_long_blocks & brain.build_compilation ----
import numpy as np  # noqa: E402
import subprocess  # noqa: E402

long_words = []
t_cur = 0.0
for s_i in range(540):  # ~2700s (45 min)
    for w_i in range(10):
        tok = "fim." if w_i == 9 else f"palavra{w_i}"
        long_words.append({"w": tok, "s": round(t_cur, 2), "e": round(t_cur + 0.4, 2)})
        t_cur += 0.48
    t_cur += 0.2

long_db = np.full(2700, -30.0, dtype=np.float32)
long_db[700:715] = -5.0

ai_calls = []


def fake_ask_ai(prompt, schema=None, purpose="", timeout=None):
    ai_calls.append(purpose)
    if len(ai_calls) == 1:
        return ([{
            "start": 100.0, "end": 820.0, "score": 88,
            "title": "A Grande Virada no Final", "description": "Partida épica.",
            "hashtags": ["gaming", "clutch"], "thumb_text": "VIRADA ÉPICA",
            "peak_t": 9999.0,  # out of range -> should clamp inside [start, end]
            "reason": "Arco completo com virada.",
        }], "test-model", 150)
    return ([], "test-model", 50)


with patch.object(brain, "ask_ai", side_effect=fake_ask_ai):
    blocks, info_long = brain.find_long_blocks(
        long_words, long_db, 2700.0, title="Live Teste", uploader="Streamer",
        max_blocks=2, min_len=600, max_len=1200, output_lang="pt",
    )

check("find_long_blocks uses 30m overlapping windows (2 calls for 45m)", len(ai_calls), 2)
check("find_long_blocks returns 1 block", len(blocks), 1)
if blocks:
    b0 = blocks[0]
    check("long block format is 16:9", b0.get("format"), "16:9")
    check("long block clip_kind is long", b0.get("clip_kind"), "long")
    check("long block long_type is block", b0.get("long_type"), "block")
    check("long block duration in [600, 1200]", 600 <= (b0["end"] - b0["start"]) <= 1200, True)
    check("long block has thumb_text", bool(b0.get("thumb_text")), True)
    check("long block clamps peak_t inside [start, end]", b0["start"] <= b0["peak_t"] <= b0["end"], True)

# Fallback by loudness when AI fails
with patch.object(brain, "ask_ai", side_effect=RuntimeError("AI down")):
    fb_blocks, fb_info = brain.find_long_blocks(
        long_words, long_db, 2700.0, title="Live Teste", uploader="Streamer",
        max_blocks=2, min_len=600, max_len=1200, output_lang="pt",
    )
check("find_long_blocks falls back to loudness when AI unavailable", len(fb_blocks) >= 1, True)
if fb_blocks:
    check("fallback block format is 16:9", fb_blocks[0].get("format"), "16:9")
    check("fallback block duration >= 600", fb_blocks[0]["end"] - fb_blocks[0]["start"] >= 599, True)

# build_compilation: returns None if < min_len
short_only = [{"start": 10.0, "end": 70.0, "score": 90, "hashtags": ["a"]}]
check("build_compilation returns None when total < min_len",
      brain.build_compilation(short_only, title="Live", min_len=600, max_len=1200), None)

many_shorts = [
    {"start": float(i * 100), "end": float(i * 100 + 80), "score": 70 + (i % 20), "hashtags": ["gta"]}
    for i in range(12)
]
comp = brain.build_compilation(many_shorts, title="Live GTA", min_len=600, max_len=1200, output_lang="pt")
check("build_compilation returns dict when >= min_len", isinstance(comp, dict), True)
if comp:
    seg_dur = sum(s["end"] - s["start"] for s in comp["segments"])
    check("compilation total segment duration in [600, 1200]", 600 <= seg_dur <= 1200, True)
    check("compilation segments sorted chronologically",
          all(comp["segments"][i]["start"] <= comp["segments"][i + 1]["start"] for i in range(len(comp["segments"]) - 1)), True)
    check("compilation format is 16:9", comp.get("format"), "16:9")
    check("compilation long_type is compile", comp.get("long_type"), "compile")
    check("compilation has thumb_text", bool(comp.get("thumb_text")), True)

# ---- 5. Task 2: render.render_clip 16:9 & compilation segments ----
syn_src = TMP_DIR / "syn_16_9.mp4"
subprocess.run(
    [
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", "testsrc=duration=3:size=1280x720:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "48000", str(syn_src),
    ],
    check=True,
)
render_out = TMP_DIR / "render_out"
res_16_9 = render.render_clip(
    syn_src, render_out, "long_test",
    {"start": 0.0, "end": 1.5, "format": "16:9", "layout": "blur", "captions": False, "peak_t": 0.8},
    [], 1280, 720, aspect="16:9",
)
check("render_clip 16:9 returns horizontal layout", res_16_9.get("layout"), "horizontal")
mp4_16_9 = render_out / "long_test.mp4"
jpg_16_9 = render_out / "long_test.jpg"
check("render_clip 16:9 wrote mp4", mp4_16_9.is_file(), True)
check("render_clip 16:9 wrote jpg", jpg_16_9.is_file(), True)
if mp4_16_9.is_file():
    p_info = media.probe(mp4_16_9)
    check("render_clip 16:9 width is 1920", p_info["width"], 1920)
    check("render_clip 16:9 height is 1080", p_info["height"], 1080)

res_comp = render.render_clip(
    syn_src, render_out, "comp_test",
    {
        "start": 0.0, "end": 2.5, "format": "16:9", "layout": "horizontal", "captions": False,
        "segments": [{"start": 0.0, "end": 1.0}, {"start": 1.5, "end": 2.5}],
        "peak_t": 1.8,
    },
    [], 1280, 720, aspect="16:9",
)
check("render_clip compilation returns horizontal", res_comp.get("layout"), "horizontal")
mp4_comp = render_out / "comp_test.mp4"
jpg_comp = render_out / "comp_test.jpg"
check("render_clip compilation wrote mp4", mp4_comp.is_file(), True)
check("render_clip compilation wrote jpg", jpg_comp.is_file(), True)
if mp4_comp.is_file():
    c_info = media.probe(mp4_comp)
    check("render_clip compilation width is 1920", c_info["width"], 1920)
    check("render_clip compilation height is 1080", c_info["height"], 1080)
    check("render_clip compilation duration ~2.0s", 1.5 <= c_info["duration"] <= 2.5, True)

# ---- 6. Task 2: jobs derived Shorts & render_one 16:9 ----
with ctx.as_user(UID):
    j_drv = jobs.create({"type": "file", "name": "vod_long.mp4"}, {"long_mode": "both", "derive_shorts": True, "max_clips": 4}, enqueue=False)
    j_drv["source_path"] = str(syn_src)
    j_drv["width"] = 1280
    j_drv["height"] = 720
    j_drv["duration"] = 2100.0
    j_drv["clips"] = [
        {"id": "clip01", "start": 150.0, "end": 195.0, "score": 85, "format": "9:16", "status": "done"},
        {"id": "clip02", "start": 1900.0, "end": 1945.0, "score": 80, "format": "9:16", "status": "done"},
    ]
    jobs.save(j_drv)

    with patch.object(brain, "find_long_blocks", return_value=([
        {"start": 100.0, "end": 800.0, "score": 90, "title": "Bloco 1", "description": "", "hashtags": [], "thumb_text": "BLOCO 1", "peak_t": 200.0, "format": "16:9", "clip_kind": "long", "long_type": "block"},
        {"start": 1000.0, "end": 1700.0, "score": 85, "title": "Bloco 2", "description": "", "hashtags": [], "thumb_text": "BLOCO 2", "peak_t": 1200.0, "format": "16:9", "clip_kind": "long", "long_type": "block"},
    ], {"model": "test", "tokens": 10, "note": ""})), \
         patch.object(brain, "find_clips", return_value=([
             {"start": 50.0, "end": 95.0, "score": 82, "hook": "CHEGOU", "title": "Sub", "description": "", "hashtags": [], "highlight_words": [], "reason": "peak"}
         ], {"model": "test", "tokens": 5, "note": ""})), \
         patch.object(brain, "build_compilation", return_value=None):
        enq = jobs._apply_long_and_derived(j_drv, long_words, long_db, 2100.0, 0, "", [])

    long_ids = [c["id"] for c in j_drv["clips"] if c.get("format") == "16:9"]
    check("jobs._apply_long_and_derived adds 2 long blocks", len(long_ids), 2)
    check("clip01 tagged with parent_clip long01", j_drv["clips"][0].get("parent_clip"), "long01")
    check("clip02 outside blocks has no parent_clip", j_drv["clips"][1].get("parent_clip"), None)
    derived_for_b2 = [c for c in j_drv["clips"] if c.get("parent_clip") == "long02"]
    check("derived short extracted for block 2 when none fell inside", len(derived_for_b2) >= 1, True)

    # render_one skips _caption_words for 16:9 and passes aspect="16:9"
    long_clip_obj = {"id": "long_r1", "start": 0.0, "end": 1.5, "score": 90, "title": "L", "description": "", "hashtags": [], "hook": "", "format": "16:9", "layout": "horizontal", "captions": False, "status": "queued", "version": 0}
    j_drv["clips"].append(long_clip_obj)
    with patch.object(jobs, "_caption_words", side_effect=AssertionError("should not caption 16:9")), \
         patch.object(render, "render_clip", return_value={"file": "long_r1.mp4", "cover": "long_r1.jpg", "layout": "horizontal"}) as m_rc:
        jobs.render_one(j_drv, long_clip_obj, [])
        check("render_one passes format='16:9' for 16:9 clip", m_rc.call_args.args[3].get("format"), "16:9")

    # live.Session._finalize_long safety with empty/pruned segments
    c_live = creators.create({"name": "Live Comp", "long_mode": "compile", "long_min_len": 600, "long_max_len": 1200})
    j_live = jobs.create({"type": "live", "url": "https://twitch.tv/x", "name": "Live Comp"}, {}, enqueue=False, creator=c_live["id"], kind="live")
    sess = live.Session(j_live, c_live)
    sess.segments = []  # pruned / empty
    sess._finalize_long()
    check("live.Session._finalize_long handles pruned segments without error", True, True)

# ---- 7. Task 3: studio/thumbs.py, accounts.youtube_upload, posting._post ----
from PIL import Image  # noqa: E402
from studio import ai, thumbs  # noqa: E402

thumb_dir = TMP_DIR / "thumbs_test"
thumb_dir.mkdir(parents=True, exist_ok=True)

th_local = thumb_dir / "local.jpg"
out_th1 = thumbs.generate(syn_src, th_local, peak_t=1.0, text="VIRADA ÉPICA NO FINAL", mode="local")
check("thumbs.generate returns output path", out_th1, th_local)
check("thumbs.generate creates local jpeg", th_local.is_file(), True)
if th_local.is_file():
    with Image.open(th_local) as im:
        check("thumbs.generate local size is 1280x720", im.size, (1280, 720))
        check("thumbs.generate local format is JPEG", im.format, "JPEG")

th_face = thumb_dir / "facecam.jpg"
thumbs.generate(syn_src, th_face, peak_t=0.8, text="CLUTCH INSANO", facecam=[20, 20, 200, 150], mode="local")
check("thumbs.generate with facecam creates jpeg", th_face.is_file(), True)
if th_face.is_file():
    with Image.open(th_face) as im:
        check("thumbs.generate facecam size is 1280x720", im.size, (1280, 720))

th_ai_fb = thumb_dir / "ai_fallback.jpg"
with patch.object(ai, "ready", return_value=True), \
     patch.object(ai, "provider", return_value="gemini"), \
     patch.object(ai, "current", return_value={"provider": "gemini", "models": ["gemini-2.5-flash"], "base_url": "", "key": "test-key"}), \
     patch("httpx.post", side_effect=RuntimeError("network down")):
    thumbs.generate(syn_src, th_ai_fb, peak_t=0.5, text="FALLBACK TESTE", mode="ai")
check("thumbs.generate mode='ai' falls back gracefully on error", th_ai_fb.is_file(), True)
if th_ai_fb.is_file():
    with Image.open(th_ai_fb) as im:
        check("thumbs.generate ai fallback size is 1280x720", im.size, (1280, 720))

# render.render_clip 16:9 produces 1280x720 thumbnail cover
if jpg_16_9.is_file():
    with Image.open(jpg_16_9) as im:
        check("render_clip 16:9 cover is 1280x720", im.size, (1280, 720))

# accounts.youtube_upload: is_short=False + thumbnails/set + 403 tolerance
class _FakeResp:
    def __init__(self, status_code=200, data=None, headers=None):
        self.status_code = status_code
        self._data = data or {}
        self.headers = headers or {}
        self.text = str(self._data)

    def json(self):
        return self._data


yt_dummy_mp4 = thumb_dir / "vid.mp4"
yt_dummy_mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42")
yt_acc = {"id": "yt_test", "platform": "youtube", "name": "YT Test", "access_token": "tok", "expires": time.time() + 3600}

post_urls = []


def _fake_yt_post(url, **kwargs):
    post_urls.append(url)
    if "thumbnails/set" in url:
        return _FakeResp(200, {"items": [{"default": {"url": "ok"}}]})
    return _FakeResp(200, {}, headers={"Location": "https://upload.example/1"})


with patch.object(accounts.httpx, "post", side_effect=_fake_yt_post), \
     patch.object(accounts.httpx, "put", return_value=_FakeResp(200, {"id": "VID123"})):
    vid, vurl = accounts.youtube_upload(yt_acc, yt_dummy_mp4, "Long Title", "Desc", ["tag"], "public", is_short=False, thumb_path=th_local)
    check("youtube_upload is_short=False returns watch URL", vurl, "https://www.youtube.com/watch?v=VID123")
    check("youtube_upload calls thumbnails/set", any("thumbnails/set" in u for u in post_urls), True)

# 403 on thumbnails/set must not fail upload
def _fake_yt_post_403(url, **kwargs):
    if "thumbnails/set" in url:
        return _FakeResp(403, {"error": {"message": "forbidden"}})
    return _FakeResp(200, {}, headers={"Location": "https://upload.example/2"})


with patch.object(accounts.httpx, "post", side_effect=_fake_yt_post_403), \
     patch.object(accounts.httpx, "put", return_value=_FakeResp(200, {"id": "VID403"})):
    vid403, vurl403 = accounts.youtube_upload(yt_acc, yt_dummy_mp4, "Long Title", "Desc", ["tag"], "public", is_short=False, thumb_path=th_local)
    check("youtube_upload ignores 403 on thumbnails/set", (vid403, vurl403), ("VID403", "https://www.youtube.com/watch?v=VID403"))

# posting._post routing for 16:9 long vs 9:16 Shorts
with ctx.as_user(UID):
    c_route = creators.create({"name": "Route Creator", "privacy": "public", "accounts": ["yt_r", "tt_r", "ig_r"]})
    j_route = jobs.create({"type": "file", "name": "vod_route.mp4"}, {}, enqueue=False, creator=c_route["id"])
    clips_dir = Path(j_route["dir"]) / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    (clips_dir / "long_r.mp4").write_bytes(b"mp4")
    (clips_dir / "long_r.jpg").write_bytes(b"jpg")
    (clips_dir / "short_r.mp4").write_bytes(b"mp4")
    (clips_dir / "short_r.jpg").write_bytes(b"jpg")
    j_route["clips"] = [
        {"id": "long_r", "start": 0.0, "end": 900.0, "format": "16:9", "clip_kind": "long", "file": "long_r.mp4", "cover": "long_r.jpg", "title": "Long Ep", "hashtags": ["gg"], "status": "done"},
        {"id": "short_r", "start": 0.0, "end": 45.0, "format": "9:16", "file": "short_r.mp4", "cover": "short_r.jpg", "title": "Short Ep", "hashtags": ["gg"], "status": "done"},
    ]
    jobs.save(j_route)

    route_accs = {
        "yt_r": {"id": "yt_r", "platform": "youtube", "name": "YT R"},
        "tt_r": {"id": "tt_r", "platform": "tiktok", "name": "TT R"},
        "ig_r": {"id": "ig_r", "platform": "instagram", "name": "IG R"},
    }
    yt_calls, tt_calls, ig_calls = [], [], []
    with patch.object(accounts, "get", side_effect=lambda aid: route_accs.get(aid)), \
         patch.object(accounts, "youtube_upload", side_effect=lambda *a, **kw: (yt_calls.append((a, kw)), ("yt1", "https://youtu.be/yt1"))[1]), \
         patch.object(accounts, "tiktok_upload", side_effect=lambda *a, **kw: (tt_calls.append((a, kw)), ("tt1", "https://tiktok.com/1"))[1]), \
         patch.object(accounts, "instagram_upload", side_effect=lambda *a, **kw: (ig_calls.append((a, kw)), ("ig1", "https://instagram.com/1"))[1]):
        p_long = {"id": "plong", "user": UID, "job": j_route["id"], "clip": "long_r", "creator": c_route["id"], "account": "yt_r", "account_name": "YT R", "platform": "youtube", "status": "scheduled", "tries": 0, "title": "Long Ep"}
        posting._post(p_long)
        check("posting._post long 16:9 status posted", p_long["status"], "posted")
        check("posting._post long 16:9 passes is_short=False", yt_calls[-1][1].get("is_short"), False)
        check("posting._post long 16:9 passes thumb_path", yt_calls[-1][1].get("thumb_path"), clips_dir / "long_r.jpg")

        p_sy = {"id": "psy", "user": UID, "job": j_route["id"], "clip": "short_r", "creator": c_route["id"], "account": "yt_r", "account_name": "YT R", "platform": "youtube", "status": "scheduled", "tries": 0, "title": "Short Ep"}
        p_st = {"id": "pst", "user": UID, "job": j_route["id"], "clip": "short_r", "creator": c_route["id"], "account": "tt_r", "account_name": "TT R", "platform": "tiktok", "status": "scheduled", "tries": 0, "title": "Short Ep"}
        p_si = {"id": "psi", "user": UID, "job": j_route["id"], "clip": "short_r", "creator": c_route["id"], "account": "ig_r", "account_name": "IG R", "platform": "instagram", "status": "scheduled", "tries": 0, "title": "Short Ep"}
        posting._post(p_sy)
        posting._post(p_st)
        posting._post(p_si)
        check("posting._post short 9:16 passes is_short=True", yt_calls[-1][1].get("is_short"), True)
        check("posting._post short 9:16 passes thumb_path=None", yt_calls[-1][1].get("thumb_path"), None)
        check("posting._post short 9:16 posts to TikTok", len(tt_calls), 1)
        check("posting._post short 9:16 posts to Instagram", len(ig_calls), 1)

# ---- 8. Task 4: UI controls, priority badges & JS syntax ----
import re  # noqa: E402

html_path = ROOT / "web" / "index.html"
html_text = html_path.read_text(encoding="utf-8")

for needle in (
    'data-k="long_mode"',
    'data-k="long_min_len"',
    'data-k="long_max_len"',
    'data-k="derive_shorts"',
    'data-k="thumb_mode"',
    'data-k="long_enabled"',
    'data-k="series_enabled"',
    'data-f="thumb_text"',
    'Derived Short',
    '16:9 videos post to YouTube only',
    'P1 · 16:9',
    'P2 · Derivado',
):
    check(f"UI contains {needle}", needle in html_text, True)

check("UI has zero box-shadow", "box-shadow" not in html_text, True)

scripts = re.findall(r"<script[^>]*>(.*?)</script>", html_text, re.DOTALL)
check("UI has inline script block", len(scripts) >= 1, True)
if scripts:
    js_tmp = TMP_DIR / "ui_check.js"
    js_tmp.write_text("\n".join(scripts), encoding="utf-8")
    node_res = subprocess.run(["node", "--check", str(js_tmp)], capture_output=True, text=True)
    check("UI JS passes node --check", node_res.returncode, 0)

# ---- 9. OpenRouter free task routing, collage thumbnails & 5-10s hook / clickbait rules ----
pool_models = [
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3.5-super-vl:free",
    "baidu/ernie-5.5-flash-preview:free",
    "openrouter/free",
]
with patch.object(ai, "current", return_value={"provider": "openrouter", "models": pool_models, "base_url": "", "key": "or-test-key-123456"}), \
     patch.object(ai, "fallback", return_value={"provider": "", "models": [], "base_url": "", "key": "", "ready": False, "label": ""}):
    t_editorial = [m for _, m in ai.targets("Live: words judge")]
    t_caption = [m for _, m in ai.targets("Caption wording fix")]
    t_vision = [m for _, m in ai.targets("vision")]
    check("editorial purpose puts 550b ultra first", t_editorial[0], "nvidia/nemotron-3-ultra-550b-a55b:free")
    check("caption purpose puts fast VL first", t_caption[0], "nvidia/nemotron-3.5-super-vl:free")
    check("vision purpose skips text-only 550b and puts VL first", t_vision[0], "nvidia/nemotron-3.5-super-vl:free")
    check("vision purpose excludes text-only 550b", "nvidia/nemotron-3-ultra-550b-a55b:free" not in t_vision, True)

ass_20s = render.build_ass([{"w": "olá", "s": 0.5, "e": 1.0}], 0.0, 20.0, "GANCHO EXPLOSIVO", "bounce", [], 300, 1380)
check("hook banner stays for 7s (5-10s hook window) on 20s clip", "0:00:00.00,0:00:07.00,Hook" in ass_20s, True)
check("RULES includes 5-10 second hook rule", "5-10 SECOND HOOK RULE" in brain.RULES, True)
check("LONG_BLOCK_RULES includes 5-10 second hook rule", "5-10 SECOND HOOK RULE" in brain.LONG_BLOCK_RULES, True)

collage_jpg = TMP_DIR / "thumb_collage_ai.jpg"
with patch.object(ai, "ready", return_value=True), \
     patch.object(ai, "provider", return_value="openrouter"), \
     patch.object(ai, "see", return_value=("2", 120, 5)):
    out_col = thumbs.generate(syn_src, collage_jpg, peak_t=1.2, text="SEGREDO PROIBIDO!", facecam=[20, 20, 120, 90], mode="ai", collage=True)
    check("collage AI vision selector generates valid jpg", out_col.is_file(), True)
    with Image.open(out_col) as im_col:
        check("collage thumb is 1280x720", im_col.size, (1280, 720))

# ---- 10. Dedicated platform tabs (TikTok, YouTube, Instagram), per-platform prefs & copy-ready captions ----
with ctx.as_user(UID):
    accounts.save_settings({"platform_prefs": {"tiktok": {"gap_min": 150, "hashtags": "ViceLoop,#TikTokBR", "cta": "Comenta o que achou!"}}})
    accounts.save_settings({"platform_prefs": {"youtube": {"gap_min": 95}}})
    prefs = accounts.platform_prefs()
    check("tiktok platform_prefs saved independently", (prefs["tiktok"]["gap_min"], prefs["tiktok"]["hashtags"]), (150, "ViceLoop,TikTokBR"))
    check("youtube save keeps tiktok platform_prefs intact", (prefs["youtube"]["gap_min"], prefs["tiktok"]["gap_min"]), (95, 150))
    cap_tt = posting._caption({"description": "Cena insana", "hashtags": ["GTA6"], "end": 30, "start": 0}, creators.DEFAULTS, "tiktok")
    check("tiktok caption includes platform CTA and custom hashtag", ("Comenta o que achou!" in cap_tt, "#ViceLoop" in cap_tt), (True, True))
    ov_full = posting.overview()
    check("overview exposes platform_prefs and accounts", ("tiktok" in ov_full.get("platform_prefs", {}), isinstance(ov_full.get("accounts"), list)), (True, True))
    if ov_full.get("posts"):
        sample_p = ov_full["posts"][0]
        check("overview post has caption_ready and hook fields", ("caption_ready" in sample_p, "hook" in sample_p), (True, True))

for ui_marker in ("drawPlatformView", "platcfgsave", "copycap", "copytags", "PLAT_VIEWS"):
    check(f"UI includes {ui_marker}", ui_marker in html_text, True)

print(f"\nSummary: {len(fails)} failed" if fails else "\nALL PASSED")
sys.exit(1 if fails else 0)
