"""Google Drive archive primitives: OAuth connect/disconnect, token refresh, folder hierarchy, resumable chunked
uploads with byte-size verification, and on-demand local cache."""
import atexit
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
TMP_BASE = ROOT / "data" / "tmp"
TMP_BASE.mkdir(parents=True, exist_ok=True)
os.environ["TEMP"] = os.environ["TMP"] = str(TMP_BASE)

DATA_DIR = Path(tempfile.mkdtemp(prefix="pc-gdrive-", dir=str(TMP_BASE)))
atexit.register(lambda: shutil.rmtree(DATA_DIR, ignore_errors=True))
os.environ.update(
    PEAKCUT_DATA=str(DATA_DIR),
    DATABASE_URL="",
    PEAKCUT_ACCOUNTS="1",
    PEAKCUT_PUBLIC_URL="https://clips.example.com",
    PEAKCUT_VERIFY_EMAIL="0",
    GOOGLE_CLIENT_ID="",
    GOOGLE_CLIENT_SECRET="",
    GOOGLE_CLIENT_FILE="",
)
sys.path.insert(0, str(ROOT))

from studio import accounts, auth, config, ctx, db  # noqa: E402
import studio.gdrive as gdrive  # noqa: E402

db.init()
fails = []


def check(name, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got {got!r}"))
    if not ok:
        fails.append(name)


class FakeResp:
    def __init__(self, status_code, data=None, headers=None, content=None, text=None):
        self.status_code = status_code
        self._data = data
        self.headers = headers or {}
        if content is not None:
            self.content = content
            self.text = text if text is not None else ""
        else:
            self.text = text if text is not None else (json.dumps(data) if data is not None else "")
            self.content = self.text.encode("utf-8")

    def json(self):
        if self._data is None:
            raise ValueError("No JSON body")
        return self._data


try:
    boss, _ = auth._create("boss@example.com", "boss", password_hash="x")
    alice, _ = auth._create("alice@example.com", "alice", password_hash="x")
    bob, _ = auth._create("bob@example.com", "bob", password_hash="x")

    # ---- 1. Before Google app setup
    with ctx.as_user(alice.id):
        check("cannot connect before Google keys exist", gdrive.can_connect(), False)
        check("redirect URI uses PUBLIC_URL", gdrive.redirect_uri(), "https://clips.example.com/oauth/gdrive/callback")
        st0 = gdrive.public_status()
        check("initial public status is disconnected and not ready",
              (st0["ready"], st0["connected"], st0["email"], st0["quota"], st0["queued"]),
              (False, False, "", None, 0))
        try:
            gdrive.connect_url()
            check("connect_url refuses when unconfigured", False)
        except ValueError:
            check("connect_url refuses when unconfigured", True)

    accounts.save_app("youtube", "gdrive-client-123.apps", "gdrive-secret-xyz")
    with ctx.as_user(alice.id):
        check("can_connect reflects Google app keys", gdrive.can_connect(), True)

    # ---- Stand-in Google OAuth + Drive v3 server
    state_mock = {
        "email": "vault@example.com",
        "limit": "2000000000000",
        "usage": "500000000000",
        "give_refresh": True,
        "about_fail": False,
        "folders": {},          # id -> {"name": ..., "parent": ..., "trashed": False}
        "next_fid": 1,
        "uploads": {},          # upload_url -> {"name": ..., "parent": ..., "size": int, "buf": bytearray(), "ReportSize": None}
        "files": {},            # file_id -> bytes
        "report_size": None,
        "calls": [],
        "media_delay": 0.0,
    }
    mock_lock = threading.Lock()

    def fake_post(url, data=None, json=None, headers=None, params=None, timeout=None, content=None):
        with mock_lock:
            state_mock["calls"].append(("POST", url, params, data or json, headers or {}))
        if url == "https://oauth2.googleapis.com/token":
            gt = (data or {}).get("grant_type")
            if gt == "authorization_code":
                tok = {"access_token": "gd-access-1", "expires_in": 3600}
                if state_mock["give_refresh"]:
                    tok["refresh_token"] = "gd-refresh-1"
                return FakeResp(200, tok)
            if gt == "refresh_token":
                if (data or {}).get("refresh_token") != "gd-refresh-1":
                    return FakeResp(400, {"error": "invalid_grant"})
                return FakeResp(200, {"access_token": "gd-access-refreshed", "expires_in": 3600})
            return FakeResp(400, {"error": "unsupported_grant_type"})

        if url.startswith("https://www.googleapis.com/drive/v3/files"):
            parent = ((json or {}).get("parents") or ["root"])[0]
            if parent != "root" and (parent not in state_mock["folders"] or state_mock["folders"][parent]["trashed"]):
                return FakeResp(404, {"error": {"code": 404, "message": "Parent not found"}})
            with mock_lock:
                fid = f"fld_{state_mock['next_fid']}"
                state_mock["next_fid"] += 1
                state_mock["folders"][fid] = {"name": json["name"], "parent": parent, "trashed": False}
            return FakeResp(200, {"id": fid})

        if url == "https://www.googleapis.com/upload/drive/v3/files":
            parent = ((json or {}).get("parents") or [""])[0]
            if parent not in state_mock["folders"] or state_mock["folders"][parent]["trashed"]:
                return FakeResp(404, {"error": {"code": 404, "message": "Folder not found"}})
            size = int((headers or {}).get("X-Upload-Content-Length", "0"))
            with mock_lock:
                idx = state_mock["next_fid"]
                state_mock["next_fid"] += 1
                up_url = f"https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=up_{idx}"
                state_mock["uploads"][up_url] = {
                    "id": f"drv_file_{idx}",
                    "name": json["name"],
                    "parent": parent,
                    "size": size,
                    "buf": bytearray(),
                }
            return FakeResp(200, {}, headers={"Location": up_url})

        return FakeResp(404, {"error": "not found"})

    def fake_get(url, params=None, headers=None, timeout=None, follow_redirects=None):
        with mock_lock:
            state_mock["calls"].append(("GET", url, params, None, headers or {}))
        if "/drive/v3/about" in url:
            if state_mock["about_fail"]:
                return FakeResp(503, {"error": "unavailable"})
            return FakeResp(200, {
                "user": {"emailAddress": state_mock["email"]},
                "storageQuota": {"limit": state_mock["limit"], "usage": state_mock["usage"]},
            })

        if url == "https://www.googleapis.com/drive/v3/files" and params and "q" in params:
            q = params["q"]
            # Parse name='...' and '<parent>' in parents (handling \' escapes)
            import re as _re
            m_name = _re.search(r"name='((?:\\.|[^'])*)'", q)
            m_par = _re.search(r"'((?:\\.|[^'])*)' in parents", q)
            want_name = m_name.group(1).replace("\\'", "'").replace("\\\\", "\\") if m_name else ""
            want_par = m_par.group(1).replace("\\'", "'").replace("\\\\", "\\") if m_par else "root"
            found = [
                {"id": fid, "name": info["name"]}
                for fid, info in state_mock["folders"].items()
                if not info["trashed"] and info["name"] == want_name and info["parent"] == want_par
            ]
            return FakeResp(200, {"files": found[:1]})

        if "?alt=media" in url or (params and params.get("alt") == "media"):
            if state_mock["media_delay"]:
                time.sleep(state_mock["media_delay"])
            fid = url.split("/files/")[1].split("?")[0]
            blob = state_mock["files"].get(fid)
            if blob is None:
                return FakeResp(404, {"error": "missing"})
            return FakeResp(200, content=blob)

        return FakeResp(404, {"error": "not found"})

    def fake_put(url, content=None, headers=None, timeout=None):
        with mock_lock:
            state_mock["calls"].append(("PUT", url, None, len(content or b""), headers or {}))
        up = state_mock["uploads"].get(url)
        if not up:
            return FakeResp(404, {"error": "bad upload session"})
        rng = (headers or {}).get("Content-Range", "")
        # bytes first-last/total
        spec, total_s = rng.replace("bytes ", "").split("/")
        first_s, last_s = spec.split("-")
        first, last, total = int(first_s), int(last_s), int(total_s)
        check_len = last - first + 1
        if check_len != len(content) or first != len(up["buf"]) or total != up["size"]:
            return FakeResp(400, {"error": f"bad range {rng}"})
        up["buf"].extend(content)
        if len(up["buf"]) < up["size"]:
            return FakeResp(308, None, headers={"Range": f"bytes=0-{last}"}, text="")
        state_mock["files"][up["id"]] = bytes(up["buf"])
        reported = state_mock["report_size"] if state_mock["report_size"] is not None else len(up["buf"])
        return FakeResp(200, {
            "id": up["id"],
            "size": str(reported),
            "webViewLink": f"https://drive.google.com/file/d/{up['id']}/view",
        })

    gdrive.httpx.post = fake_post
    gdrive.httpx.get = fake_get
    gdrive.httpx.put = fake_put

    # ---- 2. OAuth flow, state validation, cross-user rejection, missing refresh_token, redaction
    with ctx.as_user(alice.id):
        gdrive._states["stale_state"] = (alice.id, time.time() - 950)
        auth_url = gdrive.connect_url()
        check("expired states are purged on connect_url", "stale_state" not in gdrive._states, True)
        parsed = urlparse(auth_url)
        qs = parse_qs(parsed.query)
        state = qs["state"][0]
        check("connect_url targets Google OAuth with offline consent and drive.file scope",
              (parsed.netloc, qs["client_id"][0], qs["access_type"][0], qs["prompt"][0],
               "drive.file" in qs["scope"][0] and "userinfo.email" in qs["scope"][0]),
              ("accounts.google.com", "gdrive-client-123.apps", "offline", "consent", True))

    with ctx.as_user(bob.id):
        try:
            gdrive.connect_finish("code-xyz", state)
            check("cross-user OAuth callback is rejected", False)
        except ValueError:
            check("cross-user OAuth callback is rejected", True)

    with ctx.as_user(alice.id):
        # Missing refresh_token gives friendly guidance
        state_mock["give_refresh"] = False
        s_noref = parse_qs(urlparse(gdrive.connect_url()).query)["state"][0]
        try:
            gdrive.connect_finish("code-noref", s_noref)
            check("missing refresh_token raises friendly ValueError", False)
        except ValueError as e:
            check("missing refresh_token raises friendly ValueError", "Google account" in str(e))
        state_mock["give_refresh"] = True

        # Expired state on finish
        gdrive._states["expired_now"] = (alice.id, time.time() - 901)
        try:
            gdrive.connect_finish("code-exp", "expired_now")
            check("expired state rejected on connect_finish", False)
        except ValueError:
            check("expired state rejected on connect_finish", True)

        # Valid connect_finish
        s_ok = parse_qs(urlparse(gdrive.connect_url()).query)["state"][0]
        v_before = ctx.version(alice.id)
        pub = gdrive.connect_finish("code-good", s_ok)
        check("connect_finish bumps sync version", ctx.version(alice.id) > v_before, True)
        check("connect_finish returns connected status and quota",
              (pub["connected"], pub["email"], pub["quota"]),
              (True, "vault@example.com", {"limit_gb": 2000.0, "used_gb": 500.0, "free_gb": 1500.0}))
        check("tokens never appear in public_status",
              any(secret in json.dumps(pub) for secret in ("gd-access-1", "gd-refresh-1")),
              False)

        # Replay of same state fails
        try:
            gdrive.connect_finish("code-good", s_ok)
            check("one-time state cannot be replayed", False)
        except ValueError:
            check("one-time state cannot be replayed", True)

        cfg_file = config.user_dir(alice.id) / "accounts" / "gdrive.json"
        check("gdrive.json written on disk", cfg_file.is_file(), True)

    # ---- 3. Settings, forget_mem, token refresh, quota failure fallback
    with ctx.as_user(alice.id):
        v0 = ctx.version(alice.id)
        s1 = gdrive.save_settings({"auto_after_post": False, "auto_days": 999})
        check("save_settings clamps auto_days to 365 and updates bool",
              (s1["auto_after_post"], s1["auto_days"], ctx.version(alice.id) > v0),
              (False, 365, True))
        s2 = gdrive.save_settings({"auto_days": -10})
        check("save_settings clamps negative auto_days to 0", s2["auto_days"], 0)
        gdrive.save_settings({"auto_after_post": True, "auto_days": 5})

        # Force token expiry on disk and test forget_mem + _token refresh
        on_disk = json.loads(cfg_file.read_text(encoding="utf-8"))
        on_disk["expires"] = 0.0
        on_disk["archived_bytes"] = 123456
        on_disk["archived_count"] = 4
        cfg_file.write_text(json.dumps(on_disk), encoding="utf-8")
        gdrive.forget_mem(alice.id)

        cfg = gdrive._cfg()
        check("forget_mem reloads persisted values from disk", (cfg["archived_bytes"], cfg["archived_count"]), (123456, 4))
        tok_new = gdrive._token(cfg)
        check("expired token refreshes via OAuth refresh_token", tok_new, "gd-access-refreshed")
        persisted = json.loads(cfg_file.read_text(encoding="utf-8"))
        check("refreshed token is persisted to gdrive.json", persisted["access_token"], "gd-access-refreshed")

        # Valid cached token makes zero token POSTs
        calls_before = len(state_mock["calls"])
        check("unexpired token returned without HTTP call", gdrive._token(gdrive._cfg()), "gd-access-refreshed")
        check("no HTTP call when token is fresh", len(state_mock["calls"]), calls_before)

        # Quota error returns None gracefully
        state_mock["about_fail"] = True
        check("quota returns None gracefully on Drive API error", gdrive.quota(), None)
        state_mock["about_fail"] = False

    # ---- 4. Folder hierarchy: sanitization, search-before-create, caching, reconnect behavior, 404 recovery
    ts_nov = time.mktime((2026, 11, 18, 12, 0, 0, 0, 0, -1))
    with ctx.as_user(alice.id):
        cfg = gdrive._cfg()
        state_mock["calls"].clear()
        fid1 = gdrive.folder_for(cfg, '  Bob\'s: <Stream>? /Clips*.. ', ts=ts_nov)
        created_names = [
            state_mock["folders"][k]["name"]
            for k in ("fld_1", "fld_2", "fld_3")
        ]
        check("folder_for sanitizes creator name and builds PeakCut -> Creator -> YYYY-MM",
              created_names,
              ["PeakCut", "Bob's- -Stream- -Clips-", "2026-11"])
        check("folder IDs cached under root, c:, m: keys",
              ("root" in cfg["folders"], "c:Bob's- -Stream- -Clips-" in cfg["folders"], "m:Bob's- -Stream- -Clips-/2026-11" in cfg["folders"]),
              (True, True, True))

        # Second call for same creator & month makes zero HTTP requests
        state_mock["calls"].clear()
        fid2 = gdrive.folder_for(cfg, '  Bob\'s: <Stream>? /Clips*.. ', ts=ts_nov)
        check("cached folder_for makes zero Drive calls", (fid2 == fid1, len(state_mock["calls"])), (True, 0))

        # Empty creator falls back to General
        fid_gen = gdrive.folder_for(cfg, "   ...  ", ts=ts_nov)
        check("blank creator name falls back to General", state_mock["folders"][fid_gen]["name"], "2026-11")
        check("General creator folder created under PeakCut root",
              state_mock["folders"][cfg["folders"]["c:General"]]["name"], "General")

        # Simulate drop_folder_cache (e.g. after reconnect): searches Drive and reuses existing folders without duplicating!
        folder_count_before = len(state_mock["folders"])
        gdrive.drop_folder_cache(cfg)
        check("drop_folder_cache clears folders dict", cfg["folders"], {})
        fid_reused = gdrive.folder_for(cfg, '  Bob\'s: <Stream>? /Clips*.. ', ts=ts_nov)
        check("folder_for searches Drive before creating so reconnecting never duplicates folders",
              (fid_reused == fid1, len(state_mock["folders"])),
              (True, folder_count_before))

        # Cached parent trashed on Drive: folder_for automatically drops cache and rebuilds
        ts_dec = time.mktime((2026, 12, 5, 12, 0, 0, 0, 0, -1))
        gdrive.folder_for(cfg, "General", ts=ts_nov)
        creator_fld_old = cfg["folders"]["c:General"]
        state_mock["folders"][creator_fld_old]["trashed"] = True
        fid_dec = gdrive.folder_for(cfg, "General", ts=ts_dec)
        check("folder_for recovers when cached parent folder was trashed on Drive",
              (state_mock["folders"][fid_dec]["name"], cfg["folders"]["c:General"] != creator_fld_old),
              ("2026-12", True))

        # Reconnecting with the SAME email preserves cached folders
        s_same = parse_qs(urlparse(gdrive.connect_url()).query)["state"][0]
        gdrive.connect_finish("code-same", s_same)
        check("reconnecting same Google email preserves folder cache",
              bool(gdrive._cfg()["folders"].get("root")),
              True)

        # Reconnecting with a DIFFERENT email clears cached folders while keeping settings & archive counters
        state_mock["email"] = "second-drive@example.com"
        s_re = parse_qs(urlparse(gdrive.connect_url()).query)["state"][0]
        gdrive.connect_finish("code-re", s_re)
        cfg_after_re = gdrive._cfg()
        check("reconnecting different Google email clears folder cache and preserves counters/settings",
              (cfg_after_re["email"], cfg_after_re["folders"], cfg_after_re["auto_days"], cfg_after_re["archived_count"]),
              ("second-drive@example.com", {}, 5, 4))

    # ---- 5. Resumable multi-chunk upload (8 MB chunks) + size mismatch rejection + 404 folder signal
    with ctx.as_user(alice.id):
        cfg = gdrive._cfg()
        target_folder = gdrive.folder_for(cfg, "StreamerX", ts=ts_nov)
        clip_dir = DATA_DIR / "job_up" / "clips"
        clip_dir.mkdir(parents=True, exist_ok=True)
        big_clip = clip_dir / "clip01.mp4"
        # 2 * CHUNK + 12345 bytes -> 3 chunks (8 MB -> 308, 8 MB -> 308, 12345 B -> 200)
        payload = (b"ABCD1234" * ((2 * gdrive.CHUNK) // 8)) + (b"Z" * 12345)
        big_clip.write_bytes(payload)

        state_mock["calls"].clear()
        res = gdrive.upload_mp4(cfg, big_clip, target_folder, "My Great Clip.mp4")
        put_calls = [c for c in state_mock["calls"] if c[0] == "PUT"]
        check("16MB+ file uploaded in 3 resumable chunks (8MB, 8MB, tail)",
              [c[3] for c in put_calls],
              [gdrive.CHUNK, gdrive.CHUNK, 12345])
        check("Content-Range headers match exact byte offsets",
              (put_calls[0][4]["Content-Range"], put_calls[2][4]["Content-Range"]),
              (f"bytes 0-{gdrive.CHUNK - 1}/{len(payload)}",
               f"bytes {2 * gdrive.CHUNK}-{len(payload) - 1}/{len(payload)}"))
        check("upload_mp4 returns id, url, exact size and keeps local file on SSD",
              (res["id"].startswith("drv_file_"), res["size"] == len(payload), big_clip.is_file()),
              (True, True, True))
        check("uploaded bytes on Drive match local payload byte-for-byte",
              state_mock["files"][res["id"]] == payload, True)

        # Size mismatch must raise RuntimeError and keep local file
        small_clip = clip_dir / "clip02.mp4"
        small_clip.write_bytes(b"mp4data" * 500)
        state_mock["report_size"] = 999
        try:
            gdrive.upload_mp4(cfg, small_clip, target_folder, "clip02.mp4")
            check("upload_mp4 rejects byte-size mismatch", False)
        except RuntimeError as e:
            check("upload_mp4 rejects byte-size mismatch", "size mismatch" in str(e).lower() and small_clip.is_file())
        state_mock["report_size"] = None

        # Deleted folder on Drive (404) raises distinct FolderGone error
        state_mock["folders"][target_folder]["trashed"] = True
        try:
            gdrive.upload_mp4(cfg, small_clip, target_folder, "clip02.mp4")
            check("upload_mp4 signals deleted Drive folder on 404", False)
        except gdrive.FolderGone:
            check("upload_mp4 signals deleted Drive folder on 404", True)

    # ---- 6. On-demand cache: local hit, Drive fetch, cache reuse, concurrency dedup, bad id, sweep_cache
    job_doc = {"id": "job_up", "user": alice.id, "dir": str(DATA_DIR / "job_up")}
    clip_local = {"id": "clip01", "file": "clip01.mp4", "drive": {"id": res["id"]}}
    state_mock["calls"].clear()
    got_local = gdrive.ensure_local(job_doc, clip_local)
    check("ensure_local returns SSD clip directly without touching Drive",
          (got_local == big_clip, len(state_mock["calls"])),
          (True, 0))

    # Remove local SSD clip to simulate archived state; verify concurrent ensure_local downloads only once
    big_clip.unlink()
    state_mock["media_delay"] = 0.08
    results = []

    def fetch_worker():
        results.append(gdrive.ensure_local(job_doc, clip_local))

    threads = [threading.Thread(target=fetch_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    state_mock["media_delay"] = 0.0

    get_media_calls = [c for c in state_mock["calls"] if c[0] == "GET" and "alt=media" in c[1]]
    cached_path = config.TMP_DIR / "drive_cache" / f"{alice.id}_{res['id']}.mp4"
    check("concurrent ensure_local downloads file once into drive_cache",
          (len(get_media_calls), all(p == cached_path for p in results), cached_path.read_bytes() == payload),
          (1, True, True))

    # Subsequent call hits drive_cache with 0 HTTP calls and refreshes mtime
    old_mtime = time.time() - 1200
    os.utime(cached_path, (old_mtime, old_mtime))
    state_mock["calls"].clear()
    got_cached = gdrive.ensure_local(job_doc, clip_local)
    check("second ensure_local reuses drive_cache and refreshes mtime",
          (got_cached == cached_path, len(state_mock["calls"]), cached_path.stat().st_mtime > old_mtime + 600),
          (True, 0, True))

    # Invalid Drive IDs and missing Drive files return None cleanly
    check("invalid drive id rejected", gdrive.ensure_local(job_doc, {"id": "c", "drive": {"id": "../evil"}}), None)
    check("missing remote file returns None", gdrive.ensure_local(job_doc, {"id": "c", "drive": {"id": "nonexistent_id"}}), None)
    check("no leftover .tmp files in drive_cache", list((config.TMP_DIR / "drive_cache").glob("*.tmp")), [])

    # Streaming download path via httpx.stream
    import contextlib as _contextlib
    state_mock["files"]["drv_stream_99"] = b"STREAM_MP4_BYTES_" * 200

    class FakeStreamResp:
        status_code = 200

        def iter_bytes(self, _chunk_size=8192):
            data = state_mock["files"]["drv_stream_99"]
            yield data[:50]
            yield data[50:]

    @_contextlib.contextmanager
    def fake_stream(method, url, headers=None, timeout=None, follow_redirects=None):
        with mock_lock:
            state_mock["calls"].append((method, url, None, "stream", headers or {}))
        yield FakeStreamResp()

    gdrive.httpx.stream = fake_stream
    streamed = gdrive.ensure_local(job_doc, {"id": "clip_s", "drive": {"id": "drv_stream_99"}})
    gdrive.httpx.stream = gdrive._ORIG_HTTPX_STREAM
    check("ensure_local streams multi-chunk response to disk",
          streamed is not None and streamed.read_bytes() == state_mock["files"]["drv_stream_99"],
          True)

    # sweep_cache removes expired files and keeps fresh ones
    stale_cache = config.TMP_DIR / "drive_cache" / "stale_entry.mp4"
    stale_cache.write_bytes(b"old")
    stale_ts = time.time() - (gdrive.CACHE_TTL + 120)
    os.utime(stale_cache, (stale_ts, stale_ts))
    gdrive.sweep_cache()
    check("sweep_cache evicts expired files and keeps active cache",
          (not stale_cache.exists(), cached_path.is_file()),
          (True, True))

    # ---- 7. Disconnect preserves settings & archive totals while wiping credentials
    with ctx.as_user(alice.id):
        v_pre = ctx.version(alice.id)
        disc = gdrive.disconnect()
        check("disconnect clears auth and keeps settings/totals",
              (disc["connected"], disc["email"], disc["auto_days"], disc["archived_count"],
               gdrive.is_connected(), ctx.version(alice.id) > v_pre),
              (False, "", 5, 4, False, True))

    print("ALL PASSED" if not fails else f"{len(fails)} failed")
finally:
    db.engine.dispose()
    shutil.rmtree(DATA_DIR, ignore_errors=True)

sys.exit(1 if fails else 0)
