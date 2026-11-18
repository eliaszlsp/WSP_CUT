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

from studio import accounts, creators, ctx, db, jobs, posting  # noqa: E402

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
check("platform_gap tiktok floor", posting.platform_gap("tiktok", 5), 30)
check("platform_gap instagram floor", posting.platform_gap("instagram", 10), 30)
check("platform_gap youtube floor", posting.platform_gap("youtube", 5), 20)
check("platform_gap youtube respects larger gap", posting.platform_gap("youtube", 45), 45)
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
         "due_at": now_t - 30, "created": now_t - 30, "priority": 3, "format": "9:16"},
        {"id": "p2", "user": UID, "job": j_post["id"], "clip": "c_derived", "creator": c_post["id"],
         "account": "yt1", "account_name": "YT", "platform": "youtube", "status": "scheduled",
         "due_at": now_t - 20, "created": now_t - 20, "priority": 2, "format": "9:16"},
        {"id": "p1", "user": UID, "job": j_post["id"], "clip": "c_long", "creator": c_post["id"],
         "account": "yt1", "account_name": "YT", "platform": "youtube", "status": "scheduled",
         "due_at": now_t - 10, "created": now_t - 10, "priority": 1, "format": "16:9"},
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

print(f"\nSummary: {len(fails)} failed" if fails else "\nALL PASSED")
sys.exit(1 if fails else 0)
