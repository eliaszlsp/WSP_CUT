"""Post queue: decides when each finished clip goes to which account, posts it, and tracks views."""
import secrets
import threading
import time
from pathlib import Path

import json

from . import accounts, alerts, config, creators, ctx, jobs, live

_lock = threading.RLock()
_FILE = config.DATA_DIR / "posts.json"
try:
    _posts: list = json.loads(_FILE.read_text(encoding="utf-8")) if _FILE.exists() else []
except ValueError:
    _posts = []


def _mine() -> list:
    u = ctx.uid()
    return [p for p in _posts if p.get("user") == u]
OPEN = ("scheduled", "posting")


def has_open_posts(jid: str, cid: str) -> bool:
    u = ctx.uid()
    with _lock:
        return any(
            p.get("user") == u and p.get("job") == jid and p.get("clip") == cid and p.get("status") in OPEN
            for p in _posts
        )


def _save():
    with _lock:
        tmp = _FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_posts, indent=1), encoding="utf-8")
        tmp.replace(_FILE)


def forget(uid: str):
    """Drop everything queued or recorded for a deleted account, so nothing is posted or retried for it any more."""
    with _lock:
        for p in _posts:
            if p.get("user") == uid:
                p["status"] = "deleted"          # the posting loop may be holding this one already: it then skips it
        _posts[:] = [p for p in _posts if p.get("user") != uid]
        _save()


def _day(t):
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _mirror(p):
    """Copy the post's state onto its clip so the clip card can show it."""
    job = jobs.get(p["job"])
    clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
    if clip is not None:
        clip.setdefault("posts", {})[p["id"]] = {k: p.get(k) for k in ("account_name", "platform", "status", "url", "due_at", "error")}
        jobs.save(job)


PLATFORM_CAPS = {"tiktok": 14}               # TikTok ~15/24h OpenAPI limit
PLATFORM_GAPS = {"tiktok": 120, "instagram": 90, "youtube": 90}  # increased post-hype spacing across platforms
SHORT_PERIOD_CAP = 2                         # Shorts/Reels: max 2 per active 6h period (1 at night + 2*3 daytime = 7/day)
_PERIOD_SHORT_CAPS = (1, 2, 2, 2)            # Madrugada (00-06): 1, Manhã (06-12): 2, Tarde (12-18): 2, Noite (18-24): 2
LONG_DAILY_CAP = 2                           # Long 16:9 YouTube videos: max 2 per day so Longs never starve Shorts

_PERIOD_START_HOURS = ((0, 30), (8, 30), (14, 0), (19, 0))  # Madrugada 00:30, Manhã 08:30 (pico 08-11h), Tarde 14:00, Noite 19:00


def _period_cap(p_idx: int) -> int:
    if SHORT_PERIOD_CAP <= 0:
        return 0
    if 0 <= p_idx < len(_PERIOD_SHORT_CAPS):
        return min(SHORT_PERIOD_CAP, _PERIOD_SHORT_CAPS[p_idx])
    return SHORT_PERIOD_CAP


def _period_info(t: float) -> tuple[str, float]:
    """Return (YYYY-MM-DD:period_idx, next_period_start_ts) for 6h local windows (00-06, 06-12, 12-18, 18-24)."""
    lt = time.localtime(t)
    p_idx = lt.tm_hour // 6
    day = time.strftime("%Y-%m-%d", lt)
    if p_idx < 3:
        nh, nm = _PERIOD_START_HOURS[p_idx + 1]
        nxt = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, nh, nm, 0, 0, 0, -1))
    else:
        nh, nm = _PERIOD_START_HOURS[0]
        lt2 = time.localtime(t + 86400)
        nxt = time.mktime((lt2.tm_year, lt2.tm_mon, lt2.tm_mday, nh, nm, 0, 0, 0, -1))
    return f"{day}:{p_idx}", nxt


def _is_short_post(p: dict) -> bool:
    if p.get("format") == "16:9" or p.get("priority") == 1:
        return False
    return not _is_long_post(p)


def _is_music_blocked_long(clip: dict) -> bool:
    is_long = clip.get("clip_kind") == "long" or clip.get("format") == "16:9" or (clip.get("end", 0) - clip.get("start", 0)) > 180
    return bool(is_long and clip.get("has_music"))


def _next_slot(account_id, gap_min, cap, platform="", is_short=True):
    """Earliest time respecting account gap, daily/24h cap, and 3 Shorts/Reels per 6h period."""
    mine = [p for p in _mine() if p["account"] == account_id and p["status"] in OPEN + ("posted",)]
    eff_gap = max(gap_min, PLATFORM_GAPS.get(platform, 0))
    t = max(time.time(), max((p.get("posted_at") or p["due_at"] for p in mine), default=0) + eff_gap * 60)
    plat_cap = PLATFORM_CAPS.get(platform, 0)
    eff_cap = min(cap, plat_cap) if (cap and cap > 0 and plat_cap) else (plat_cap or cap)
    short_mine = [p for p in mine if _is_short_post(p)] if is_short else []
    for _ in range(48):
        moved = False
        if eff_cap and eff_cap > 0:
            if platform == "tiktok":
                win = [p.get("posted_at") or p["due_at"] for p in mine if t - (p.get("posted_at") or p["due_at"]) < 86400]
                if len(win) >= eff_cap:
                    t = max(t, min(win) + 86400 + 120)
                    moved = True
            else:
                if sum(1 for p in mine if _day(p.get("posted_at") or p["due_at"]) == _day(t)) >= eff_cap:
                    lt = time.localtime(t + 86400)
                    t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 6, 0, 0, 0, 0, -1))
                    moved = True
        if not moved and is_short and SHORT_PERIOD_CAP > 0:
            pk, nxt_p = _period_info(t)
            p_cap = _period_cap(int(pk.rsplit(":", 1)[-1]))
            if p_cap > 0 and sum(1 for p in short_mine if _period_info(p.get("posted_at") or p["due_at"])[0] == pk) >= p_cap:
                t = max(t, nxt_p)
                moved = True
        if not moved and not is_short and platform == "youtube" and LONG_DAILY_CAP > 0:
            long_mine = [p for p in mine if not _is_short_post(p)]
            if sum(1 for p in long_mine if _day(p.get("posted_at") or p["due_at"]) == _day(t)) >= LONG_DAILY_CAP:
                lt = time.localtime(t + 86400)
                t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 12, 0, 0, 0, 0, -1))
                moved = True
        if not moved:
            break
    return t


def platform_gap(platform: str, gap_min: int) -> int:
    """Minimum safe gap in minutes between posts on a given platform."""
    if platform == "tiktok":
        return max(120, int(gap_min))
    if platform in ("instagram", "youtube"):
        return max(90, int(gap_min))
    return max(5, int(gap_min))


def clip_priority(clip: dict) -> int:
    """Queue priority: 1 for 16:9 long video, 2 for derived short, 3 for standalone clip."""
    if clip.get("format") == "16:9" or clip.get("clip_kind") == "long":
        return 1
    if clip.get("parent_clip"):
        return 2
    return 3


def _is_long_post(p) -> bool:
    with ctx.as_user(p["user"]):
        job = jobs.get(p["job"])
        clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
    if not clip:
        return False
    return clip.get("clip_kind") == "long" or (clip.get("end", 0) - clip.get("start", 0)) >= 180


def _interleave_by_creator(items: list[dict]) -> list[dict]:
    """Order items round-robin across creators while preserving each creator's internal/series order."""
    buckets: dict[str, list[dict]] = {}
    for p in items:
        buckets.setdefault(p.get("creator") or "", []).append(p)
    for q in buckets.values():
        q.sort(key=lambda x: (x.get("series_id") or "", x.get("part_index") or 0, x.get("created", 0)))
    out = []
    order = list(buckets.keys())
    while order:
        nxt_order = []
        for cid in order:
            q = buckets[cid]
            if q:
                out.append(q.pop(0))
            if q:
                nxt_order.append(cid)
        order = nxt_order
    return out


def rebalance_account(account_id: str, gap_min: int = 90, cap: int = 12):
    """Re-slot scheduled posts: interleave up to LONG_DAILY_CAP (2) Long videos/day with Shorts/Reels (7/day across periods)."""
    with _lock:
        mine = _mine()
        sched = [p for p in mine if p["account"] == account_id and p["status"] == "scheduled"]
        if len(sched) <= 1:
            return
        posted = [p for p in mine if p["account"] == account_id and p["status"] in ("posting", "posted")]
        platform = (accounts.get(account_id) or {}).get("platform") or (sched[0].get("platform") or "")
        eff_gap = max(gap_min, PLATFORM_GAPS.get(platform, 0))
        plat_cap = PLATFORM_CAPS.get(platform, 0)
        eff_cap = min(cap, plat_cap) if (cap and cap > 0 and plat_cap) else (plat_cap or cap)
        longs = _interleave_by_creator([p for p in sched if _is_long_post(p)])
        shorts = _interleave_by_creator([p for p in sched if not _is_long_post(p)])
        ordered = []
        li, si = 0, 0
        long_batch = max(1, LONG_DAILY_CAP)
        short_batch = max(1, sum(_PERIOD_SHORT_CAPS))
        while li < len(longs) or si < len(shorts):
            ordered.extend(longs[li:li + long_batch])
            li += long_batch
            ordered.extend(shorts[si:si + short_batch])
            si += short_batch
        last_t = max((p.get("posted_at") or p["due_at"] for p in posted), default=0)
        base_t = max(time.time(), last_t + eff_gap * 60) if last_t else time.time()
        t_short = base_t
        t_long = base_t
        day_counts: dict[str, int] = {}
        long_day_counts: dict[str, int] = {}
        period_counts: dict[str, int] = {}
        win_times: list[float] = []
        used_slots: list[float] = []
        for p in posted:
            pt = p.get("posted_at") or p["due_at"]
            d = _day(pt)
            day_counts[d] = day_counts.get(d, 0) + 1
            if _is_short_post(p):
                pk = _period_info(pt)[0]
                period_counts[pk] = period_counts.get(pk, 0) + 1
            else:
                long_day_counts[d] = long_day_counts.get(d, 0) + 1
            win_times.append(pt)
            used_slots.append(pt)
        for p in ordered:
            is_short = _is_short_post(p)
            t = t_short if is_short else t_long
            for _ in range(90):
                moved = False
                for u in sorted(used_slots):
                    if abs(t - u) < eff_gap * 60:
                        t = u + eff_gap * 60
                        moved = True
                if eff_cap and eff_cap > 0:
                    if platform == "tiktok":
                        recent = [x for x in win_times if 0 <= t - x < 86400]
                        if len(recent) >= eff_cap:
                            t = max(t, min(recent) + 86400 + 120)
                            moved = True
                    else:
                        d = _day(t)
                        if day_counts.get(d, 0) >= eff_cap:
                            lt = time.localtime(t + 86400)
                            t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 8, 30, 0, 0, 0, -1))
                            moved = True
                if not moved and is_short and SHORT_PERIOD_CAP > 0:
                    pk, nxt_p = _period_info(t)
                    p_cap = _period_cap(int(pk.rsplit(":", 1)[-1]))
                    if p_cap > 0 and period_counts.get(pk, 0) >= p_cap:
                        t = max(t, nxt_p)
                        moved = True
                if not moved and not is_short and platform == "youtube" and LONG_DAILY_CAP > 0:
                    d = _day(t)
                    if long_day_counts.get(d, 0) >= LONG_DAILY_CAP:
                        lt = time.localtime(t + 86400)
                        t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 12, 0, 0, 0, 0, -1))
                        moved = True
                if not moved:
                    break
            p["due_at"] = t
            d = _day(t)
            day_counts[d] = day_counts.get(d, 0) + 1
            if is_short:
                pk = _period_info(t)[0]
                period_counts[pk] = period_counts.get(pk, 0) + 1
                t_short = t + eff_gap * 60
            else:
                long_day_counts[d] = long_day_counts.get(d, 0) + 1
                step_gap = max(eff_gap, 240) if platform == "youtube" else eff_gap
                t_long = t + step_gap * 60
                if d == _day(t_short):
                    t_short = max(t_short, t + eff_gap * 60)
            win_times.append(t)
            used_slots.append(t)
        _save()
    for p in ordered:
        _mirror(p)


def rebalance_platform(platform: str):
    """Reschedule scheduled posts on every connected account for one platform using its current effective gap and cap."""
    accs = [a for a in accounts._all() if a.get("platform") == platform]
    if not accs:
        return
    base_gap = 90
    base_cap = 12
    for acc in accs:
        rebalance_account(acc["id"], gap_min=_eff_gap(platform, base_gap), cap=_eff_cap(platform, base_cap))


def enqueue(job, clip, account_ids, now=False) -> list:
    if _is_music_blocked_long(clip):
        return []
    c = creators.get(job["creator"]) or creators.DEFAULTS
    made = []
    prio = clip_priority(clip)
    fmt = clip.get("format", "9:16")
    is_short = (fmt != "16:9") and (clip.get("clip_kind") != "long") and ((clip.get("end", 0) - clip.get("start", 0)) <= 180)
    with _lock:
        for aid in account_ids:
            acc = accounts.get(aid)
            if not acc:
                continue
            if fmt == "16:9" and acc["platform"] != "youtube":
                continue
            dur = (clip.get("end") or 0) - (clip.get("start") or 0)
            if acc["platform"] == "instagram" and dur > 90:
                continue  # Instagram Reels API rejects >90s (error 2207077)
            if any(p["job"] == job["id"] and p["clip"] == clip["id"] and p["account"] == aid
                   and p["status"] in OPEN + ("posted",) for p in _mine()):
                continue  # never post the same clip to the same account twice
            eff_gap = _eff_gap(acc["platform"], c["post_gap_min"])
            eff_cap = _eff_cap(acc["platform"], c["daily_cap"])
            due = time.time() if now else _next_slot(aid, eff_gap, eff_cap, acc["platform"], is_short=is_short)
            p = {"id": secrets.token_hex(8), "user": ctx.uid(), "job": job["id"], "clip": clip["id"], "creator": job["creator"],
                 "account": aid, "account_name": acc["name"], "platform": acc["platform"], "status": "scheduled",
                 "due_at": due, "created": time.time(), "made_at": clip.get("rendered_at") or clip.get("made_at") or time.time(),
                 "title": clip.get("title") or clip.get("hook") or "Clip",
                 "score": clip.get("score", 0), "tries": 0, "url": "", "remote_id": "", "error": None, "views": None,
                 "series_id": clip.get("series_id"), "part_index": clip.get("part_index"),
                 "priority": prio, "format": fmt, "manual_now": bool(now)}
            _posts.append(p)
            made.append(p)
        _save()
    for p in made:
        _mirror(p)
    if made and not now:
        c = creators.get(job["creator"]) or creators.DEFAULTS
        for aid in {p["account"] for p in made}:
            plat = (accounts.get(aid) or {}).get("platform") or ""
            rebalance_account(aid, gap_min=_eff_gap(plat, c["post_gap_min"]), cap=_eff_cap(plat, c["daily_cap"]))
    return made


def enqueue_series(job, clips: list[dict], account_ids: list[str]) -> list:
    """Enqueue multi-part series clips in strict part_index order (Part 1 -> Part 2 -> ...)."""
    ordered = sorted(clips, key=lambda k: k.get("part_index") or 0)
    made = []
    for clip in ordered:
        made.extend(enqueue(job, clip, account_ids, now=False))
    return made


def on_clip_ready(job, clip):
    """Called once when a clip first finishes rendering."""
    c = creators.get(job["creator"])
    if not c or not c["autopost"] or not c["accounts"]:
        return
    if clip.get("clip_kind") == "series":
        return  # series parts are enqueued in strict 1..N order by enqueue_series
    if clip.get("clip_kind") == "long":
        if _is_music_blocked_long(clip):
            creators.log(c["id"], f"Vídeo longo bloqueado por música >5s: {clip.get('title') or clip.get('hook')}")
            return
        if clip.get("score", 0) < c.get("long_min_score", 70):
            return
        yt_accs = [aid for aid in c["accounts"] if (accounts.get(aid) or {}).get("platform") == "youtube"]
        if not yt_accs:
            return
        made = enqueue(job, clip, yt_accs)
        if made:
            creators.log(c["id"], f"Queued long YouTube video for {len(made)} account(s): {clip.get('title') or clip.get('hook')}")
        return
    if clip.get("score", 0) < c["post_min_score"]:
        return  # includes hand-made clips (score 0): those wait for you to press Post
    made = enqueue(job, clip, c["accounts"])
    if made:
        creators.log(c["id"], f"Queued for {len(made)} account(s): {clip.get('title') or clip.get('hook')}")


def _plat_override(platform: str) -> dict:
    if not platform or ctx.uid_or_none() is None:
        return {}
    raw = accounts.settings().get("platform_prefs") or {}
    return dict(raw.get(platform) or {}) if isinstance(raw.get(platform), dict) else {}


def _plat_merged(platform: str) -> dict:
    base = accounts.PLATFORM_PREF_DEFAULTS.get(platform, {})
    return {**base, **_plat_override(platform)}


def _eff_gap(platform: str, creator_gap: int) -> int:
    ov = _plat_override(platform)
    g = ov["gap_min"] if "gap_min" in ov and ov["gap_min"] is not None else creator_gap
    return platform_gap(platform, g)


def _eff_cap(platform: str, creator_cap: int) -> int:
    ov = _plat_override(platform)
    return int(ov["daily_cap"]) if "daily_cap" in ov and ov["daily_cap"] is not None else creator_cap


def _eff_min_score(platform: str, creator: dict, is_long: bool = False) -> int:
    ov = _plat_override(platform)
    if "min_score" in ov and ov["min_score"] is not None:
        return int(ov["min_score"])
    return int(creator.get("long_min_score", 70) if is_long else creator.get("post_min_score", 80))


def _caption_tags(clip: dict, platform: str = "") -> list[str]:
    raw_tags = list(clip.get("hashtags") or [])
    for core_tag in ("GTA6", "GTAVI", "GTA6Brasil", "ViceCity"):
        if any(str(t).lower() in ("gta6", "gtavi", "vicecity", "gta6game") for t in raw_tags):
            if core_tag.lower() not in {str(t).lower() for t in raw_tags}:
                raw_tags.append(core_tag)
    ov = _plat_override(platform) if platform else {}
    if ov.get("hashtags"):
        for extra in str(ov["hashtags"]).split(","):
            et = extra.strip().lstrip("#")
            if et and et.lower() not in {str(t).lower() for t in raw_tags}:
                raw_tags.append(et)
    clean_tags = []
    for h in raw_tags:
        s = str(h).strip().lstrip("#")
        if s and s.lower() not in {x.lower() for x in clean_tags}:
            clean_tags.append(s)
        if len(clean_tags) >= 10:
            break
    return clean_tags


def _caption(clip, creator, platform: str = ""):
    desc = (clip.get("description") or "").strip()
    is_long = clip.get("clip_kind") == "long" or clip.get("format") == "16:9" or (clip.get("end", 0) - clip.get("start", 0)) > 180
    ov = _plat_override(platform) if platform else {}
    cta = str(ov.get("cta") or "").strip() or (
        "Qual detalhe mais chamou sua atenção? Deixe nos comentários!" if is_long else "O que você achou dessa cena? Comenta aí!"
    )
    if desc and "?" not in desc and "comenta" not in desc.lower():
        desc = f"{desc}\n{cta}"
    elif not desc:
        desc = cta
    tags = " ".join("#" + h for h in _caption_tags(clip, platform))
    footer = str(ov.get("footer") or "").strip() or (creator.get("post_footer", "") if creator else "")
    return "\n\n".join(x for x in (desc, tags, footer) if x)


def _post(p):
    job = jobs.get(p["job"])
    clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
    acc = accounts.get(p["account"])
    try:
        if not clip or clip.get("status") != "done":
            raise RuntimeError("The clip no longer exists or is not finished.")
        if not acc:
            raise RuntimeError("That account was disconnected.")
        fname = Path(str(clip.get("file") or "")).name
        path = (Path(job["dir"]) / "clips" / fname) if fname else Path(job["dir"]) / "clips"
        if not path.is_file() and clip.get("drive"):
            from . import gdrive
            path = gdrive.ensure_local(job, clip)
        if not path or not path.is_file():
            raise RuntimeError("The clip file is no longer available.")
        creator = creators.get(job["creator"]) or dict(creators.DEFAULTS, name="clips")
        plat = acc["platform"]
        title, cap = clip.get("title") or clip.get("hook") or "Clip", _caption(clip, creator, plat)
        privacy = _plat_override(plat).get("privacy") or creator["privacy"]
        is_short = (clip.get("clip_kind") != "long") and (clip.get("format") != "16:9") and ((clip.get("end", 0) - clip.get("start", 0)) <= 180)
        if not is_short and clip.get("has_music"):
            raise RuntimeError("Vídeo longo bloqueado: contém música (>5s); permitido apenas em Shorts/Reels.")
        if plat == "youtube":
            yt_desc = (cap + "\n\n#Shorts") if is_short else cap
            thumb_path = (Path(job["dir"]) / "clips" / Path(str(clip.get("cover") or "")).name) if (not is_short and clip.get("cover")) else None
            rid, url = accounts.youtube_upload(acc, path, title, yt_desc, _caption_tags(clip, plat)[:15], privacy,
                                               is_short=is_short, thumb_path=thumb_path)
        elif plat == "instagram":
            cov_name = Path(str(clip.get("cover") or "")).name if clip.get("cover") else ""
            rid, url = accounts.instagram_upload(acc, path, f"{title}\n\n{cap}", job["id"] if fname else "",
                                                 clip_name=fname, cover_name=cov_name)
        elif plat == "tiktok":
            rid, url = accounts.tiktok_upload(acc, path, f"{title}\n\n{cap}", privacy)
        else:
            raise RuntimeError("That kind of account is no longer supported.")
        p.update(status="posted", posted_at=time.time(), remote_id=rid, url=url, error=None)
        creators.log(p["creator"], f"Posted to {acc['name']}: {title}")
    except Exception as e:  # noqa: BLE001
        err_msg = str(e)[-300:]
        p["error"] = err_msg
        if "spam_risk" in err_msg or "too_many_posts" in err_msg or "rate_limit" in err_msg.lower():
            # Platform 24h quota hit: push this account's queue out without burning retry tries
            with _lock:
                oldest = min((x.get("posted_at") or x["due_at"] for x in _mine()
                              if x["account"] == p["account"] and x["status"] == "posted"
                              and time.time() - (x.get("posted_at") or x["due_at"]) < 86400), default=0)
                wake = max(time.time() + 7200, (oldest + 86400 + 180) if oldest else 0)
                p.update(status="scheduled", due_at=wake)
                for other in _mine():
                    if other["account"] == p["account"] and other["status"] == "scheduled" and other["due_at"] < wake:
                        other["due_at"] = wake + 60
        elif "quotaexceeded" in err_msg.lower() or "uploadlimitexceeded" in err_msg.lower():
            # YouTube Data API / channel daily quota hit: reschedule for next day 05:05 local (PST midnight reset)
            with _lock:
                now_t = time.time()
                lt = time.localtime(now_t)
                wake = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 5, 5, 0, 0, 0, -1))
                if wake <= now_t + 300:
                    lt2 = time.localtime(now_t + 86400)
                    wake = time.mktime((lt2.tm_year, lt2.tm_mon, lt2.tm_mday, 5, 5, 0, 0, 0, -1))
                p.update(status="scheduled", due_at=wake)
                for other in _mine():
                    if other["account"] == p["account"] and other["status"] == "scheduled" and other["due_at"] < wake:
                        other["due_at"] = wake + 60
        else:
            p["tries"] += 1
            if p["tries"] < 3 and clip and acc:
                p.update(status="scheduled", due_at=time.time() + 600)  # try again in 10 minutes
            else:
                p["status"] = "failed"
                creators.log(p["creator"], f"Post failed on {p['account_name']}: {p['error']}")
                alerts.notify(f"post failed on {p['account_name']} ({p['title']}): {p['error'][:200]}", kind="post")
    _save()
    _mirror(p)
    if p.get("status") == "posted":
        with ctx.as_user(p["user"]):
            still_open = has_open_posts(p["job"], p["clip"])
        if not still_open:
            from . import gdrive
            gdrive.on_clip_posted(p["user"], p["job"], p["clip"])


def _series_blocked(p) -> bool:
    """True if an earlier part in the same series for the same account hasn't been posted yet,
    or if this clip is still rendering."""
    with ctx.as_user(p["user"]):
        job = jobs.get(p["job"])
        clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
    if clip and clip.get("status") in ("queued", "rendering"):
        return True
    sid, idx = p.get("series_id"), p.get("part_index")
    if not sid or not idx or idx <= 1:
        return False
    with _lock:
        for other in _posts:
            if (other.get("user") == p.get("user")
                    and other.get("account") == p.get("account")
                    and other.get("series_id") == sid
                    and (other.get("part_index") or 0) < idx
                    and other.get("status") in ("scheduled", "posting", "failed")):
                return True
    return False


def _run():
    while True:
        time.sleep(15)
        due = sorted(
            (p for p in list(_posts) if p["status"] == "scheduled" and p["due_at"] <= time.time() and p.get("user")),
            key=lambda p: (p.get("priority", 3), p.get("due_at", 0), p.get("created", 0)),
        )
        for p in due:
            try:
                if _series_blocked(p):
                    continue
                with ctx.as_user(p["user"]):
                    st = accounts.settings()
                    if st.get("paused") or p.get("platform") in (st.get("paused_platforms") or ()):
                        continue
                    if not p.get("manual_now"):
                        c = creators.get(p.get("creator")) or creators.DEFAULTS
                        plat_k = p.get("platform", "")
                        eff_gap = _eff_gap(plat_k, c["post_gap_min"])
                        eff_cap = _eff_cap(plat_k, c["daily_cap"])
                        mine_acc = [x for x in _mine() if x["account"] == p["account"] and x["status"] in ("posting", "posted")]
                        last_t = max((x.get("posted_at") or x["due_at"] for x in mine_acc), default=0)
                        now_t = time.time()
                        cur_pk = _period_info(now_t)[0]
                        is_short = _is_short_post(p)
                        period_hits = sum(
                            1 for x in mine_acc
                            if _is_short_post(x) and _period_info(x.get("posted_at") or x["due_at"])[0] == cur_pk
                        ) if is_short else 0
                        if (last_t and now_t - last_t < eff_gap * 60 - 5) or (is_short and period_hits >= SHORT_PERIOD_CAP):
                            nxt = _next_slot(p["account"], eff_gap, eff_cap, plat_k, is_short=is_short)
                            with _lock:
                                if p["status"] == "scheduled":
                                    p["due_at"] = max(now_t + 60, nxt)
                                    _save()
                            continue
                    with _lock:
                        if p["status"] != "scheduled":
                            continue
                        p["status"] = "posting"
                    _post(p)
            except Exception:  # noqa: BLE001
                pass


def _stats_for(recent):
    for aid in {p["account"] for p in recent if p["platform"] == "youtube"}:
        acc = accounts.get(aid)
        if acc:
            views = accounts.youtube_views(acc, [p["remote_id"] for p in recent if p["account"] == aid])
            for p in recent:
                if p["account"] == aid and p["remote_id"] in views:
                    p["views"] = views[p["remote_id"]]
    for p in recent:
        acc = accounts.get(p["account"]) if p["platform"] == "instagram" else None
        if acc:
            v = accounts.instagram_views(acc, p["remote_id"])
            if v is not None:
                p["views"] = v


def _stats():
    while True:
        for uid in {p.get("user") for p in _posts if p.get("user")}:
            try:
                with ctx.as_user(uid):
                    _stats_for([p for p in _mine() if p["status"] == "posted" and time.time() - p["posted_at"] < 14 * 86400])
            except Exception:  # noqa: BLE001
                pass
        _save()
        time.sleep(1800)


def cancel(pid):
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] != "scheduled":
            raise ValueError("Only posts that are still waiting can be cancelled.")
        p["status"] = "cancelled"
        _save()
    _mirror(p)


def retry(pid):
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] not in ("failed", "cancelled"):
            raise ValueError("Only failed or cancelled posts can be retried.")
        p.update(status="scheduled", due_at=time.time(), tries=0, error=None)
        _save()
    _mirror(p)


def overview() -> dict:
    today = _day(time.time())
    _posts = _mine()  # everything below is about the signed-in user only
    rows = []
    all_cs, all_js = creators.all_creators(), jobs.list_jobs()
    for c in all_cs:
        js = [j for j in all_js if j["creator"] == c["id"]]
        clips = [k for j in js for k in j["clips"] if k.get("status") == "done"]
        ps = [p for p in _posts if p.get("creator") == c["id"]]
        views = sum(p.get("views") or 0 for p in ps)
        rows.append({
            "id": c["id"], "name": c["name"],
            "state": "recording" if live.active_for(c["id"]) else "waiting" if c["auto_watch"] and c["channel"] else "idle",
            "autopost": c["autopost"] and bool(c["accounts"]),
            "clips_today": sum(1 for k in clips if _day(k.get("made_at", 0)) == today), "clips_total": len(clips),
            "posted_today": sum(1 for p in ps if p.get("status") == "posted" and _day(p.get("posted_at") or p.get("due_at") or 0) == today),
            "posted_total": sum(1 for p in ps if p.get("status") == "posted"),
            "queued": sum(1 for p in ps if p.get("status") in OPEN), "failed": sum(1 for p in ps if p.get("status") == "failed"),
            "views": views, "earnings": round(views / 1000 * c["rate_per_1k"], 2)})
    names = {c["id"]: c["name"] for c in all_cs}
    creator_map = {c["id"]: c for c in all_cs}
    clip_meta = {(j["id"], k["id"]): (j, k) for j in all_js for k in j.get("clips", [])}
    queue = sorted((p for p in _posts if p.get("status") in OPEN), key=lambda p: p.get("due_at", 0))
    done = sorted((p for p in _posts if p.get("status") in ("posted", "failed")), key=lambda p: -(p.get("posted_at") or p.get("due_at") or 0))[:40]
    show = []
    for p in queue + done:
        j_obj, k_obj = clip_meta.get((p.get("job"), p.get("clip")), ({}, {}))
        made_ts = k_obj.get("rendered_at") or k_obj.get("made_at") or p.get("made_at") or j_obj.get("created") or p.get("created") or 0
        queued_ts = p.get("created") or made_ts
        dur = round(max(0.0, float((k_obj.get("end") or 0) - (k_obj.get("start") or 0))), 1)
        tags_lower = {str(t).lower() for t in (k_obj.get("hashtags") or [])}
        is_opt = bool((p.get("score") or k_obj.get("score") or 0) >= 82 and ("gta6brasil" in tags_lower or k_obj.get("thumb_text")))
        plat = p.get("platform", "")
        cr_obj = creator_map.get(p.get("creator")) or creators.DEFAULTS
        title_txt = p.get("title") or k_obj.get("title") or k_obj.get("hook") or "Clip"
        cap_body = _caption(k_obj, cr_obj, plat) if k_obj else ""
        cap_ready = f"{title_txt}\n\n{cap_body}".strip() if cap_body else title_txt
        tags_ready = " ".join("#" + h for h in _caption_tags(k_obj, plat)) if k_obj else ""
        cov_name = Path(str(k_obj.get("cover") or "")).name if k_obj.get("cover") else ""
        cov_url = f"/media/{p['job']}/{cov_name}" if (cov_name and p.get("job")) else ""
        show.append({
            **{k: v for k, v in p.items() if k != "user"},
            "creator_name": names.get(p["creator"], ""),
            "priority": p.get("priority", 3),
            "format": p.get("format", "9:16"),
            "made_at": made_ts,
            "queued_at": queued_ts,
            "duration": dur,
            "optimized": is_opt,
            "hook": k_obj.get("hook") or "",
            "thumb_text": k_obj.get("thumb_text") or "",
            "cover_url": cov_url,
            "cover_dl": (cov_url + "?download=1") if cov_url else "",
            "caption_ready": cap_ready,
            "hashtags_ready": tags_ready,
            "inbox_mode": bool(plat == "tiktok" and p.get("status") == "posted" and "v_inbox" in str(p.get("remote_id") or "")),
        })
    # last 14 days, oldest first, for the home graph
    days = [_day(time.time() - 86400 * i) for i in range(13, -1, -1)]
    made, posted, views = {d: 0 for d in days}, {d: 0 for d in days}, {d: 0 for d in days}
    for j in all_js:
        for k in j["clips"]:
            d = _day(k.get("made_at") or j["created"])
            if k.get("status") == "done" and d in made:
                made[d] += 1
    for p in _posts:
        pt = p.get("posted_at") or p.get("due_at") or 0
        if p.get("status") == "posted" and _day(pt) in posted:
            posted[_day(pt)] += 1
            views[_day(pt)] += p.get("views") or 0
    daily = [{"day": d, "clips": made[d], "posted": posted[d], "views": views[d]} for d in days]
    st = accounts.settings()
    return {"paused": bool(st.get("paused")), "paused_platforms": list(st.get("paused_platforms") or []),
            "platform_prefs": accounts.platform_prefs(st),
            "accounts": accounts.list_accounts(),
            "creators": rows, "posts": show, "daily": daily,
            "alerts": alerts.configured(), "recording": len(live.active_mine())}


def digest_text() -> str:
    o = overview()
    lines = ["VICE CUT AI daily summary"]
    for r in o["creators"]:
        lines.append(f"- {r['name']}: {r['state']}, {r['clips_today']} clips today, {r['posted_today']} posted today, "
                     f"{r['queued']} waiting, {r['failed']} failed, {r['views']} views")
    if not o["creators"]:
        lines.append("No creators set up yet.")
    if o["paused"]:
        lines.append("Posting is paused.")
    elif o.get("paused_platforms"):
        lines.append("Paused platforms: " + ", ".join(o["paused_platforms"]))
    return "\n".join(lines)


def start():
    with _lock:
        for p in _posts:
            if p["status"] == "posting":  # interrupted mid-upload last time
                p.update(status="failed", error="The studio closed during this upload. Check the account, then retry.")
        _save()
    jobs.on_clip_ready.append(on_clip_ready)
    threading.Thread(target=_run, daemon=True).start()
    threading.Thread(target=_stats, daemon=True).start()
    threading.Thread(target=alerts.digest_loop, args=(digest_text,), daemon=True).start()
    threading.Thread(target=alerts.keep_awake_loop,
                     args=(lambda: bool(live.active()) or any(p["status"] in OPEN for p in _posts),), daemon=True).start()
