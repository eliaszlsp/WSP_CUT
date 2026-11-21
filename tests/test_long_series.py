"""Tests for long-form 16:9 YouTube clips and sequential multi-part series posting."""
import os
import sys
import tempfile

os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-long-")
os.environ["DATABASE_URL"] = ""
sys.path.insert(0, ".")

from studio import accounts, auth, brain, creators, ctx, db, jobs, posting, render  # noqa: E402

db.init()
fails = []


def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  {extra}" if extra and not ok else ""))
    if not ok:
        fails.append(name)


# 1. Geometry & filter for horizontal 16:9
lay, fg_h, hy, cy = render.geometry(1280, 720, "horizontal")
check("horizontal 720p geometry", lay == "horizontal" and fg_h == 720, str((lay, fg_h)))
lay2, fg_h2, _, _ = render.geometry(1920, 1080, "horizontal")
check("horizontal 1080p geometry", lay2 == "horizontal" and fg_h2 == 1080, str((lay2, fg_h2)))
flt = render._filter("horizontal", 1080)
check("horizontal filter scales to 1920x1080", "scale=1920:1080" in flt, flt)

# 2. split_series_parts cuts at sentence boundaries with numbered hooks and titles
words = []
t = 0.0
for s_idx in range(90):
    for w_idx in range(12):
        w = "fim." if w_idx == 11 else f"palavra{w_idx}"
        words.append({"w": w, "s": round(t, 2), "e": round(t + 0.6, 2)})
        t += 0.65
    t += 0.2

parts = brain.split_series_parts(words, 0.0, 660.0, base_title="Missao Completa no GTA 6",
                                 base_hashtags=["gta6"], part_min=60, part_max=90, output_lang="pt")
check("series produces multiple sequential parts", len(parts) >= 7, f"len={len(parts)}")
check("parts cover entire span without gaps",
      parts[0]["start"] == 0.0 and abs(parts[-1]["end"] - 660.0) < 1.0
      and all(abs(parts[i]["start"] - parts[i - 1]["end"]) < 0.05 for i in range(1, len(parts))))
check("each part has sequential PARTE N hook and Parte N/Total title",
      parts[0]["hook"] == "PARTE 1" and f"(Parte 1/{len(parts)})" in parts[0]["title"]
      and parts[-1]["hook"] == f"PARTE {len(parts)}")

# 3. Creator settings & platform routing + series dependency lock
u, _ = auth._create("long@example.com", "Long", password_hash="x")
with ctx.as_user(u.id):
    accounts._put({"id": "yt_1", "platform": "youtube", "name": "YT Main", "added": 1.0,
                   "refresh_token": "r", "access_token": "a", "expires": 9999999999})
    accounts._put({"id": "tt_1", "platform": "tiktok", "name": "@ttmain", "added": 1.0,
                   "open_id": "o", "access_token": "a", "refresh_token": "r", "expires": 9999999999})
    c = creators.create({
        "name": "Gamer",
        "channel": "https://www.twitch.tv/gamer",
        "autopost": True,
        "accounts": ["yt_1", "tt_1"],
        "long_enabled": True,
        "long_min_len": 600,
        "long_max_len": 900,
        "series_enabled": True,
        "series_part_min": 60,
        "series_part_max": 90,
    })
    check("creator long & series settings saved",
          c["long_enabled"] and c["long_min_len"] == 600 and c["series_enabled"] and c["series_part_max"] == 90)

    job = jobs.create({"type": "live", "url": c["channel"], "name": "Gamer live"}, {}, enqueue=False, creator=c["id"], kind="live")
    long_clip = {"id": "clip001", "clip_kind": "long", "start": 0.0, "end": 700.0, "score": 85,
                 "title": "Episodio Completo", "hook": "", "status": "done", "layout": "horizontal", "captions": False}
    job["clips"].append(long_clip)
    posting.on_clip_ready(job, long_clip)
    long_posts = [p for p in posting._posts if p["job"] == job["id"] and p["clip"] == "clip001"]
    check("long clip routed ONLY to YouTube (not TikTok)",
          len(long_posts) == 1 and long_posts[0]["platform"] == "youtube", str(long_posts))

    # Series clips queued in order 1..3
    s_clips = [
        {"id": "clip002", "clip_kind": "series", "series_id": "clip001", "part_index": 1, "start": 0, "end": 75,
         "score": 85, "title": "Ep (Parte 1/3)", "hook": "PARTE 1", "status": "done"},
        {"id": "clip003", "clip_kind": "series", "series_id": "clip001", "part_index": 2, "start": 75, "end": 150,
         "score": 85, "title": "Ep (Parte 2/3)", "hook": "PARTE 2", "status": "done"},
    ]
    job["clips"].extend(s_clips)
    posting.enqueue_series(job, s_clips, ["yt_1", "tt_1"])
    p1_yt = next(p for p in posting._posts if p["clip"] == "clip002" and p["account"] == "yt_1")
    p2_yt = next(p for p in posting._posts if p["clip"] == "clip003" and p["account"] == "yt_1")
    check("part 2 is blocked while part 1 is not posted yet",
          not posting._series_blocked(p1_yt) and posting._series_blocked(p2_yt))
    p1_yt["status"] = "posted"
    check("part 2 unblocks once part 1 is posted", not posting._series_blocked(p2_yt))

    # Interleaving + long-video priority via rebalance_account
    c2 = creators.create({"name": "Streamer2", "channel": "https://www.twitch.tv/s2", "autopost": True, "accounts": ["yt_1"], "daily_cap": 30})
    job2 = jobs.create({"type": "live", "url": c2["channel"], "name": "S2 live"}, {}, enqueue=False, creator=c2["id"], kind="live")
    s2_short = {"id": "clip001", "start": 0, "end": 60, "score": 85, "title": "S2 Short", "hook": "S2", "status": "done"}
    s2_long = {"id": "clip002", "clip_kind": "long", "start": 0, "end": 700, "score": 85, "title": "S2 Long", "hook": "", "status": "done"}
    job2["clips"].extend([s2_short, s2_long])
    posting.enqueue(job2, s2_short, ["yt_1"])
    posting.enqueue(job2, s2_long, ["yt_1"])
    yt_sched = sorted([p for p in posting._posts if p["account"] == "yt_1" and p["status"] == "scheduled"], key=lambda x: x["due_at"])
    check("rebalance puts all long videos before shorts on YouTube",
          [(p["creator"], p["clip"]) for p in yt_sched[:2]] == [(c["id"], "clip001"), (c2["id"], "clip002")],
          str([(p["creator"], p["clip"]) for p in yt_sched]))
    # Remaining shorts should interleave creators (Gamer, Streamer2)
    creators_order = [p["creator"] for p in yt_sched[2:]]
    check("rebalance interleaves creators", len(creators_order) == 2 and creators_order[0] != creators_order[1], str(creators_order))

    # YouTube quotaExceeded error should reschedule without burning tries to failed
    orig_upload = accounts.youtube_upload
    accounts.youtube_upload = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("YouTube failed (403): quotaExceeded"))
    q_clip = {"id": "clip099", "start": 0, "end": 60, "score": 85, "title": "Quota Test", "hook": "Q", "status": "done", "file": "clip099.mp4"}
    job2["clips"].append(q_clip)
    from pathlib import Path
    (Path(job2["dir"]) / "clips").mkdir(parents=True, exist_ok=True)
    (Path(job2["dir"]) / "clips" / "clip099.mp4").write_bytes(b"00")
    made_q = posting.enqueue(job2, q_clip, ["yt_1"], now=True)
    posting._post(made_q[0])
    accounts.youtube_upload = orig_upload
    check("quotaExceeded keeps status scheduled and tries=0",
          made_q[0]["status"] == "scheduled" and made_q[0]["tries"] == 0 and made_q[0]["due_at"] > 0,
          str((made_q[0]["status"], made_q[0]["tries"])))

print("ALL PASSED" if not fails else f"{len(fails)} failed")
import shutil
db.engine.dispose()
shutil.rmtree(os.environ["PEAKCUT_DATA"], ignore_errors=True)
sys.exit(1 if fails else 0)
