"""Google Drive archive primitives: OAuth connect/disconnect, token refresh, concurrency safety, Trash & 404 folder
recovery, resumable chunked uploads with byte-size verification, secret redaction, and streaming local cache."""
import atexit
import contextlib
import json
import os
import re
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


class FakeStream:
    def __init__(self, status_code, chunks, headers=None):
        self.status_code = status_code
        self._chunks = chunks
        self.headers = headers or {}

    def iter_bytes(self, _chunk_size=8192):
        for c in self._chunks:
            yield c


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

    # ---- Stand-in Google OAuth + Drive v3 server (real Drive Trash semantics: trashed=True returns 200, deleted returns 404)
    state_mock = {
        "email": "vault@example.com",
        "limit": "2000000000000",
        "usage": "500000000000",
        "give_refresh": True,
        "give_access": True,
        "about_fail": False,
        "about_error_text": None,
        "folders": {},          # id -> {"name": ..., "parent": ..., "trashed": False}
        "next_fid": 1,
        "uploads": {},          # upload_url -> {"id": ..., "name": ..., "parent": ..., "size": int, "buf": bytearray()}
        "files": {},            # file_id -> bytes
        "report_size": None,
        "put_fail_text": None,
        "truncate_bytes": None,
        "bad_content_length": None,
        "on_token_refresh": None,
        "on_folder_create": None,
        "on_put": None,
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
                tok = {"expires_in": 3600}
                if state_mock["give_access"]:
                    tok["access_token"] = "gd-access-1"
                if state_mock["give_refresh"]:
                    tok["refresh_token"] = "gd-refresh-1"
                return FakeResp(200, tok)
            if gt == "refresh_token":
                hook = state_mock["on_token_refresh"]
                if hook:
                    hook()
                if (data or {}).get("refresh_token") != "gd-refresh-1":
                    return FakeResp(400, {"error": "invalid_grant"})
                tok = {"expires_in": 3600}
                if state_mock["give_access"]:
                    tok["access_token"] = "gd-access-refreshed"
                return FakeResp(200, tok)
            return FakeResp(400, {"error": "unsupported_grant_type"})

        if url.startswith("https://www.googleapis.com/drive/v3/files"):
            hook = state_mock["on_folder_create"]
            if hook:
                hook()
            parent = ((json or {}).get("parents") or ["root"])[0]
            # Real Drive v3: 404 ONLY if permanently deleted; trashed parent still accepts child (200 OK)!
            if parent != "root" and parent not in state_mock["folders"]:
                return FakeResp(404, {"error": {"code": 404, "message": "Parent not found"}})
            with mock_lock:
                fid = f"fld_{state_mock['next_fid']}"
                state_mock["next_fid"] += 1
                state_mock["folders"][fid] = {"name": json["name"], "parent": parent, "trashed": False}
            return FakeResp(200, {"id": fid, "trashed": False})

        if url == "https://www.googleapis.com/upload/drive/v3/files":
            parent = ((json or {}).get("parents") or [""])[0]
            # Real Drive v3: 404 ONLY if permanently deleted; trashed parent returns 200 OK!
            if parent not in state_mock["folders"]:
                return FakeResp(404, {"error": {"code": 404, "message": "Folder not found"}})
            size = int((headers or {}).get("X-Upload-Content-Length", "0"))
            with mock_lock:
                idx = state_mock["next_fid"]
                state_mock["next_fid"] += 1
                up_url = f"https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=up_secret_{idx}"
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
            if state_mock["about_error_text"]:
                return FakeResp(500, text=state_mock["about_error_text"])
            if state_mock["about_fail"]:
                return FakeResp(503, {"error": "unavailable"})
            return FakeResp(200, {
                "user": {"emailAddress": state_mock["email"]},
                "storageQuota": {"limit": state_mock["limit"], "usage": state_mock["usage"]},
            })

        if url == "https://www.googleapis.com/drive/v3/files" and params and "q" in params:
            q = params["q"]
            m_name = re.search(r"name='((?:\\.|[^'])*)'", q)
            m_par = re.search(r"'((?:\\.|[^'])*)' in parents", q)
            want_name = m_name.group(1).replace("\\'", "'").replace("\\\\", "\\") if m_name else ""
            want_par = m_par.group(1).replace("\\'", "'").replace("\\\\", "\\") if m_par else "root"
            found = [
                {"id": fid, "name": info["name"]}
                for fid, info in state_mock["folders"].items()
                if not info["trashed"] and info["name"] == want_name and info["parent"] == want_par
            ]
            return FakeResp(200, {"files": found[:1]})

        if url.startswith("https://www.googleapis.com/drive/v3/files/") and "?alt=media" not in url:
            fid = url.split("/files/")[1].split("?")[0]
            info = state_mock["folders"].get(fid)
            if not info:
                return FakeResp(404, {"error": {"code": 404, "message": "File not found"}})
            return FakeResp(200, {"id": fid, "name": info["name"], "trashed": info["trashed"]})

        return FakeResp(404, {"error": "not found"})

    @contextlib.contextmanager
    def fake_stream(method, url, headers=None, timeout=None, follow_redirects=None):
        with mock_lock:
            state_mock["calls"].append((method, url, None, "stream", headers or {}))
        if state_mock["media_delay"]:
            time.sleep(state_mock["media_delay"])
        fid = url.split("/files/")[1].split("?")[0]
        blob = state_mock["files"].get(fid)
        if blob is None:
            yield FakeStream(404, [])
            return
        send_blob = blob[:state_mock["truncate_bytes"]] if state_mock["truncate_bytes"] is not None else blob
        cl = state_mock["bad_content_length"] if state_mock["bad_content_length"] is not None else len(blob)
        mid = max(1, len(send_blob) // 2)
        yield FakeStream(200, [send_blob[:mid], send_blob[mid:]], headers={"Content-Length": str(cl)})

    def fake_put(url, content=None, headers=None, timeout=None):
        with mock_lock:
            state_mock["calls"].append(("PUT", url, None, len(content or b""), headers or {}))
        if state_mock["on_put"]:
            state_mock["on_put"]()
        if state_mock["put_fail_text"]:
            return FakeResp(502, text=state_mock["put_fail_text"])
        up = state_mock["uploads"].get(url)
        if not up:
            return FakeResp(404, {"error": "bad upload session"})
        rng = (headers or {}).get("Content-Range", "")
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
    gdrive.httpx.stream = fake_stream

    # ---- 2. OAuth flow, 24-byte state + 5000 cap, cross-user rejection, missing tokens, redaction
    with ctx.as_user(alice.id):
        now = time.time()
        for i in range(5010):
            gdrive._states[f"fill_{i}"] = (bob.id, now - 10 + i * 0.0001)
        gdrive._states["stale_state"] = (alice.id, now - 950)
        auth_url = gdrive.connect_url()
        check("expired states purged and _states capped at 5000",
              ("stale_state" not in gdrive._states, len(gdrive._states) <= 5000),
              (True, True))
        parsed = urlparse(auth_url)
        qs = parse_qs(parsed.query)
        state = qs["state"][0]
        check("state uses 24+ bytes of entropy", len(state) >= 32, True)
        check("connect_url targets Google OAuth with offline consent and drive.file scope",
              (parsed.netloc, qs["client_id"][0], qs["access_type"][0], qs["prompt"][0],
               "drive.file" in qs["scope"][0] and "userinfo.email" in qs["scope"][0]),
              ("accounts.google.com", "gdrive-client-123.apps", "offline", "consent", True))
        gdrive._states.clear()
        state = parse_qs(urlparse(gdrive.connect_url()).query)["state"][0]

    with ctx.as_user(bob.id):
        try:
            gdrive.connect_finish("code-xyz", state)
            check("cross-user OAuth callback is rejected", False)
        except ValueError:
            check("cross-user OAuth callback is rejected", True)

    with ctx.as_user(alice.id):
        # Missing access_token raises RuntimeError
        state_mock["give_access"] = False
        s_noacc = parse_qs(urlparse(gdrive.connect_url()).query)["state"][0]
        try:
            gdrive.connect_finish("code-noacc", s_noacc)
            check("missing access_token in connect_finish raises RuntimeError", False)
        except RuntimeError:
            check("missing access_token in connect_finish raises RuntimeError", True)
        state_mock["give_access"] = True

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

    # ---- 3. Settings validation, malformed config coercion, forget_mem, token refresh & concurrency races
    with ctx.as_user(alice.id):
        v0 = ctx.version(alice.id)
        s1 = gdrive.save_settings({"auto_after_post": False, "auto_days": 999})
        check("save_settings clamps auto_days to 365 and updates bool",
              (s1["auto_after_post"], s1["auto_days"], ctx.version(alice.id) > v0),
              (False, 365, True))
        s2 = gdrive.save_settings({"auto_days": -10})
        check("save_settings clamps negative auto_days to 0", s2["auto_days"], 0)
        try:
            gdrive.save_settings({"auto_days": True})
            check("save_settings rejects boolean auto_days", False)
        except ValueError:
            check("save_settings rejects boolean auto_days", True)
        gdrive.save_settings({"auto_after_post": True, "auto_days": 5})

        # Malformed JSON fields coerced safely in _copy_cfg
        malformed = gdrive._copy_cfg({
            "connected": None,
            "email": None,
            "refresh_token": None,
            "access_token": None,
            "expires": "not-a-float",
            "auto_after_post": None,
            "auto_days": True,
            "folders": ["not-a-dict"],
            "archived_bytes": "bad",
            "archived_count": None,
            "last_error": None,
        })
        check("_copy_cfg coerces malformed/null fields to safe DEFAULTS types",
              (malformed["connected"], malformed["email"], malformed["expires"], malformed["auto_after_post"],
               malformed["auto_days"], malformed["folders"], malformed["archived_bytes"], malformed["archived_count"],
               malformed["last_error"]),
              (False, "", 0.0, True, 3, {}, 0, 0, ""))

        # Force token expiry on disk and test forget_mem + _token refresh
        on_disk = json.loads(cfg_file.read_text(encoding="utf-8"))
        on_disk["expires"] = 0.0
        on_disk["archived_bytes"] = 123456
        on_disk["archived_count"] = 4
        cfg_file.write_text(json.dumps(on_disk), encoding="utf-8")
        gdrive.forget_mem(alice.id)

        cfg = gdrive._cfg()
        check("forget_mem reloads persisted values from disk", (cfg["archived_bytes"], cfg["archived_count"]), (123456, 4))

        # Race 1: save_settings() runs mid-flight during _token(cfg) -> settings MUST NOT be overwritten!
        state_mock["on_token_refresh"] = lambda: gdrive.save_settings({"auto_days": 42})
        tok_new = gdrive._token(cfg)
        state_mock["on_token_refresh"] = None
        check("expired token refreshes via OAuth refresh_token", tok_new, "gd-access-refreshed")
        persisted = json.loads(cfg_file.read_text(encoding="utf-8"))
        check("_token merge preserves concurrent save_settings update",
              (persisted["access_token"], persisted["auto_days"], cfg["auto_days"]),
              ("gd-access-refreshed", 42, 42))
        gdrive.save_settings({"auto_days": 5})

        # Valid cached token makes zero token POSTs
        calls_before = len(state_mock["calls"])
        check("unexpired token returned without HTTP call", gdrive._token(gdrive._cfg()), "gd-access-refreshed")
        check("no HTTP call when token is fresh", len(state_mock["calls"]), calls_before)

        # Missing access_token during _token refresh raises RuntimeError
        stale_cfg = gdrive._cfg()
        stale_cfg["expires"] = 0.0
        state_mock["give_access"] = False
        try:
            gdrive._token(stale_cfg)
            check("_token raises RuntimeError when refresh lacks access_token", False)
        except RuntimeError:
            check("_token raises RuntimeError when refresh lacks access_token", True)
        state_mock["give_access"] = True

        # Race 2: disconnect() runs mid-flight during _token(cfg) -> credentials MUST NOT resurrect!
        stale_cfg2 = gdrive._cfg()
        stale_cfg2["expires"] = 0.0
        state_mock["on_token_refresh"] = lambda: gdrive.disconnect()
        try:
            gdrive._token(stale_cfg2)
            check("_token aborts when user disconnected mid-refresh", False)
        except RuntimeError as e:
            check("_token aborts when user disconnected mid-refresh", "disconnected" in str(e).lower())
        state_mock["on_token_refresh"] = None
        after_disc = json.loads(cfg_file.read_text(encoding="utf-8"))
        check("disconnect during _token is never overwritten by stale refresh",
              (after_disc["connected"], after_disc["access_token"], after_disc["refresh_token"]),
              (False, "", ""))

        # Reconnect Alice to continue tests
        s_reconn = parse_qs(urlparse(gdrive.connect_url()).query)["state"][0]
        gdrive.connect_finish("code-back", s_reconn)

        # Quota error returns None gracefully
        state_mock["about_fail"] = True
        check("quota returns None gracefully on Drive API error", gdrive.quota(), None)
        state_mock["about_fail"] = False

    # ---- 4. Secret redaction in _check(), upload_mp4(), and public_status()["last_error"]
    with ctx.as_user(alice.id):
        raw_err = (
            "Auth failed: Bearer gd-access-1 refresh=gd-refresh-1 "
            "secret=gdrive-secret-xyz upload_id=up_secret_token_777"
        )
        redacted = gdrive._redact(raw_err, gdrive._cfg())
        check("_redact scrubs access_token, refresh_token, client_secret, Bearer and upload_id",
              any(x in redacted for x in ("gd-access-1", "gd-refresh-1", "gdrive-secret-xyz", "up_secret_token_777")),
              False)

        # Verify public_status()["last_error"] redacts leaked secrets from disk
        disk_leak = json.loads(cfg_file.read_text(encoding="utf-8"))
        disk_leak["last_error"] = raw_err
        cfg_file.write_text(json.dumps(disk_leak), encoding="utf-8")
        gdrive.forget_mem(alice.id)
        st_leak = gdrive.public_status(include_quota=False)
        check("public_status redacts secrets in last_error",
              any(x in st_leak["last_error"] for x in ("gd-access-1", "gd-refresh-1", "gdrive-secret-xyz", "up_secret_token_777")),
              False)
        # Verify _check() also redacts HTTP error bodies directly
        disk_leak["last_error"] = ""
        cfg_file.write_text(json.dumps(disk_leak), encoding="utf-8")
        gdrive.forget_mem(alice.id)
        state_mock["about_error_text"] = raw_err
        try:
            gdrive._check(gdrive.httpx.get("https://www.googleapis.com/drive/v3/about"), "Drive check", gdrive._cfg())
            check("_check scrubs secrets from HTTP error body", False)
        except RuntimeError as e:
            check("_check scrubs secrets from HTTP error body",
                  any(x in str(e) for x in ("gd-access-1", "gd-refresh-1", "gdrive-secret-xyz", "up_secret_token_777")),
                  False)
        state_mock["about_error_text"] = None

        # Race 3b: disconnect() during folder_for() must NOT overwrite disconnected state or store folders!
        cfg_disc_race = gdrive._cfg()
        state_mock["on_folder_create"] = lambda: gdrive.disconnect()
        _ = gdrive.folder_for(cfg_disc_race, "RaceCreator")
        state_mock["on_folder_create"] = None
        after_folder_disc = json.loads(cfg_file.read_text(encoding="utf-8"))
        check("disconnect during folder_for is never overwritten by folder merge",
              (after_folder_disc["connected"], after_folder_disc["folders"]),
              (False, {}))
        state_mock["folders"].clear()
        state_mock["next_fid"] = 1
        s_reconn2 = parse_qs(urlparse(gdrive.connect_url()).query)["state"][0]
        gdrive.connect_finish("code-back2", s_reconn2)

    # ---- 5. Folder hierarchy: sanitization, search-before-create, caching, Trash (200) & Deleted (404) recovery, concurrency
    ts_nov = time.mktime((2026, 11, 18, 12, 0, 0, 0, 0, -1))
    with ctx.as_user(alice.id):
        cfg = gdrive._cfg()
        # Race 3: save_settings() during folder_for() must not be overwritten!
        state_mock["on_folder_create"] = lambda: gdrive.save_settings({"auto_days": 19})
        state_mock["calls"].clear()
        fid1 = gdrive.folder_for(cfg, '  Bob\'s: <Stream>? /Clips*.. ', ts=ts_nov)
        state_mock["on_folder_create"] = None
        check("folder_for merge preserves concurrent save_settings update",
              (gdrive._cfg()["auto_days"], cfg["auto_days"]),
              (19, 19))
        gdrive.save_settings({"auto_days": 5})
        cfg = gdrive._cfg()

        created_names = [state_mock["folders"][k]["name"] for k in ("fld_1", "fld_2", "fld_3")]
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

        # Simulate drop_folder_cache: searches Drive and reuses existing folders without duplicating
        folder_count_before = len(state_mock["folders"])
        gdrive.drop_folder_cache(cfg)
        check("drop_folder_cache clears folders dict", cfg["folders"], {})
        fid_reused = gdrive.folder_for(cfg, '  Bob\'s: <Stream>? /Clips*.. ', ts=ts_nov)
        check("folder_for searches Drive before creating so reconnecting never duplicates folders",
              (fid_reused == fid1, len(state_mock["folders"])),
              (True, folder_count_before))

        # Cached parent TRASHED on Drive (returns 200 with trashed=True in real Drive v3!): folder_for detects Trash and rebuilds!
        ts_dec = time.mktime((2026, 12, 5, 12, 0, 0, 0, 0, -1))
        gdrive.folder_for(cfg, "General", ts=ts_nov)
        creator_fld_trashed = cfg["folders"]["c:General"]
        state_mock["folders"][creator_fld_trashed]["trashed"] = True
        fid_dec = gdrive.folder_for(cfg, "General", ts=ts_dec)
        new_parent = state_mock["folders"][fid_dec]["parent"]
        check("folder_for detects trashed=True parent (even when Drive returns 200) and rebuilds non-trashed folder",
              (state_mock["folders"][fid_dec]["name"], new_parent != creator_fld_trashed, not state_mock["folders"][new_parent]["trashed"]),
              ("2026-12", True, True))

        # Cached parent PERMANENTLY DELETED on Drive (404): folder_for also recovers!
        ts_jan = time.mktime((2027, 1, 5, 12, 0, 0, 0, 0, -1))
        creator_fld_deleted = cfg["folders"]["c:General"]
        state_mock["folders"].pop(creator_fld_deleted)
        fid_jan = gdrive.folder_for(cfg, "General", ts=ts_jan)
        check("folder_for recovers when cached parent folder was permanently deleted (404)",
              (state_mock["folders"][fid_jan]["name"], cfg["folders"]["c:General"] != creator_fld_deleted),
              ("2027-01", True))

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

    # ---- 6. Resumable multi-chunk upload + size mismatch + Trash (200) & 404 folder rejection + error redaction
    with ctx.as_user(alice.id):
        cfg = gdrive._cfg()
        target_folder = gdrive.folder_for(cfg, "StreamerX", ts=ts_nov)
        clip_dir = DATA_DIR / "job_up" / "clips"
        clip_dir.mkdir(parents=True, exist_ok=True)
        big_clip = clip_dir / "clip01.mp4"
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

        # PUT error scrubs secrets and upload_id
        small_clip = clip_dir / "clip02.mp4"
        small_clip.write_bytes(b"mp4data" * 500)
        state_mock["put_fail_text"] = "Failed at upload_id=up_secret_999 with Bearer gd-access-1"
        try:
            gdrive.upload_mp4(cfg, small_clip, target_folder, "clip02.mp4")
            check("upload_mp4 redacts secrets from PUT error", False)
        except RuntimeError as e:
            check("upload_mp4 redacts secrets from PUT error",
                  "up_secret_999" not in str(e) and "gd-access-1" not in str(e))
        state_mock["put_fail_text"] = None

        # Size mismatch must raise RuntimeError and keep local file
        state_mock["report_size"] = 999
        try:
            gdrive.upload_mp4(cfg, small_clip, target_folder, "clip02.mp4")
            check("upload_mp4 rejects byte-size mismatch", False)
        except RuntimeError as e:
            check("upload_mp4 rejects byte-size mismatch", "size mismatch" in str(e).lower() and small_clip.is_file())
        state_mock["report_size"] = None

        # Trashed folder on Drive (GET returns 200 trashed=True, POST would return 200!) raises FolderGone
        state_mock["folders"][target_folder]["trashed"] = True
        try:
            gdrive.upload_mp4(cfg, small_clip, target_folder, "clip02.mp4")
            check("upload_mp4 raises FolderGone when target folder is trashed=True", False)
        except gdrive.FolderGone:
            check("upload_mp4 raises FolderGone when target folder is trashed=True", True)

        # Permanently deleted folder on Drive (404) raises FolderGone
        state_mock["folders"].pop(target_folder)
        try:
            gdrive.upload_mp4(cfg, small_clip, target_folder, "clip02.mp4")
            check("upload_mp4 raises FolderGone when target folder returns 404", False)
        except gdrive.FolderGone:
            check("upload_mp4 raises FolderGone when target folder returns 404", True)

    # ---- 7. On-demand streaming cache: local hit, concurrent stream dedup, truncation checks, ASCII did, sweep_cache locks
    job_doc = {"id": "job_up", "user": alice.id, "dir": str(DATA_DIR / "job_up")}
    clip_local = {"id": "clip01", "file": "clip01.mp4", "drive": {"id": res["id"], "size": len(payload)}}
    state_mock["calls"].clear()
    got_local = gdrive.ensure_local(job_doc, clip_local)
    check("ensure_local returns SSD clip directly without touching Drive",
          (got_local == big_clip, len(state_mock["calls"])),
          (True, 0))

    # Remove local SSD clip to simulate archived state; verify concurrent ensure_local streams only once
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

    stream_calls = [c for c in state_mock["calls"] if c[3] == "stream" and "alt=media" in c[1]]
    cached_path = config.TMP_DIR / "drive_cache" / f"{alice.id}_{res['id']}.mp4"
    check("concurrent ensure_local streams file once into drive_cache",
          (len(stream_calls), all(p == cached_path for p in results), cached_path.read_bytes() == payload),
          (1, True, True))

    # Subsequent call hits drive_cache with 0 HTTP calls and refreshes mtime
    old_mtime = time.time() - 1200
    os.utime(cached_path, (old_mtime, old_mtime))
    state_mock["calls"].clear()
    got_cached = gdrive.ensure_local(job_doc, clip_local)
    check("second ensure_local reuses drive_cache and refreshes mtime",
          (got_cached == cached_path, len(state_mock["calls"]), cached_path.stat().st_mtime > old_mtime + 600),
          (True, 0, True))

    # Truncated download (< drive.size or < Content-Length) is rejected and leaves no .tmp or partial .mp4
    state_mock["files"]["drv_trunc_1"] = b"0123456789" * 100
    state_mock["truncate_bytes"] = 250
    trunc_clip = {"id": "ct", "drive": {"id": "drv_trunc_1", "size": 1000}}
    check("ensure_local rejects truncated stream shorter than expected size",
          gdrive.ensure_local(job_doc, trunc_clip),
          None)
    state_mock["truncate_bytes"] = None
    state_mock["bad_content_length"] = 9999
    check("ensure_local rejects mismatched Content-Length header",
          gdrive.ensure_local(job_doc, {"id": "ct2", "drive": {"id": "drv_trunc_1"}}),
          None)
    state_mock["bad_content_length"] = None

    # Invalid & non-ASCII Drive IDs and missing Drive files return None cleanly
    check("path traversal drive id rejected", gdrive.ensure_local(job_doc, {"id": "c", "drive": {"id": "../evil"}}), None)
    check("non-ASCII drive id rejected", gdrive.ensure_local(job_doc, {"id": "c", "drive": {"id": "drv_évil_1234"}}), None)
    check("missing remote file returns None", gdrive.ensure_local(job_doc, {"id": "c", "drive": {"id": "nonexistent_id"}}), None)
    check("no leftover .tmp files in drive_cache", list((config.TMP_DIR / "drive_cache").glob("*.tmp")), [])

    # sweep_cache respects held _key_lock on .mp4 and preserves active (<1800s) .tmp while deleting orphaned (>1800s) .tmp
    cdir = config.TMP_DIR / "drive_cache"
    locked_mp4 = cdir / f"{alice.id}_lockedclip.mp4"
    locked_mp4.write_bytes(b"in-use")
    expired_ts = time.time() - (gdrive.CACHE_TTL + 300)
    os.utime(locked_mp4, (expired_ts, expired_ts))

    stale_cache = cdir / f"{bob.id}_stale_entry.mp4"
    stale_cache.write_bytes(b"old")
    os.utime(stale_cache, (expired_ts, expired_ts))

    young_tmp = cdir / "active_download.tmp"
    young_tmp.write_bytes(b"downloading")
    young_ts = time.time() - 120
    os.utime(young_tmp, (young_ts, young_ts))

    orphan_tmp = cdir / "crashed_download.tmp"
    orphan_tmp.write_bytes(b"orphaned")
    orphan_ts = time.time() - 4000
    os.utime(orphan_tmp, (orphan_ts, orphan_ts))

    held_lk = gdrive._key_lock(locked_mp4.stem)
    held_lk.acquire()
    try:
        gdrive.sweep_cache(max_age=60)
    finally:
        held_lk.release()
    check("sweep_cache skips locked .mp4 and young .tmp, evicts unlocked expired .mp4 and orphaned .tmp",
          (locked_mp4.is_file(), young_tmp.is_file(), not stale_cache.exists(), not orphan_tmp.exists(), cached_path.is_file()),
          (True, True, True, True, True))
    young_tmp.unlink(missing_ok=True)

    # ---- 8. Disconnect purges user's cached .mp4s; forget(uid) cleans _mem, _states, _inflight and drive_cache
    bob_cache = cdir / f"{bob.id}_keepme.mp4"
    bob_cache.write_bytes(b"bob-video")
    with ctx.as_user(alice.id):
        v_pre = ctx.version(alice.id)
        disc = gdrive.disconnect()
        check("disconnect clears auth, keeps settings/totals, and purges user's cached .mp4s",
              (disc["connected"], disc["email"], disc["auto_days"], disc["archived_count"],
               gdrive.is_connected(), ctx.version(alice.id) > v_pre,
               not cached_path.exists(), not locked_mp4.exists(), bob_cache.is_file()),
              (False, "", 5, 4, False, True, True, True, True))

    # Populate state for bob and verify forget(bob.id)
    with ctx.as_user(bob.id):
        _ = gdrive._cfg()
        _ = gdrive.connect_url()
    gdrive._inflight.add((bob.id, "j1", "c1"))
    gdrive._inflight.add((alice.id, "j2", "c2"))
    gdrive.forget(bob.id)
    check("forget(uid) clears _mem, _states, _inflight and drive_cache only for that user",
          (bob.id not in gdrive._mem,
           all(v[0] != bob.id for v in gdrive._states.values()),
           (bob.id, "j1", "c1") not in gdrive._inflight,
           (alice.id, "j2", "c2") in gdrive._inflight,
           not bob_cache.exists()),
          (True, True, True, True, True))
    gdrive._inflight.clear()

    # ---- 9. Task 2: Post-publish Drive archiving queue, multi-account trigger, and SSD cleanup
    import zipfile
    from studio import account, creators, jobs, notices, posting, render, storage

    with ctx.as_user(alice.id):
        # Reconnect Alice's Google Drive cleanly for queue/archive tests
        state_mock["email"] = "alice-vault@example.com"
        s_t2 = parse_qs(urlparse(gdrive.connect_url()).query)["state"][0]
        gdrive.connect_finish("code-t2", s_t2)
        gdrive.save_settings({"auto_after_post": True, "auto_days": 3})

        cr_arch = creators.create({"name": "ProGamer"})
        # Register 3 social accounts for Alice (YouTube, TikTok, Instagram) with mocked uploaders
        accounts._put({"id": "yt_1", "platform": "youtube", "name": "YT Main", "added": time.time(),
                       "refresh_token": "r", "access_token": "a", "expires": time.time() + 3600})
        accounts._put({"id": "tt_1", "platform": "tiktok", "name": "TT Main", "added": time.time(),
                       "refresh_token": "r", "access_token": "a", "expires": time.time() + 3600, "username": "progamer"})
        accounts._put({"id": "yt_2", "platform": "youtube", "name": "YT Second", "added": time.time(),
                       "refresh_token": "r", "access_token": "a", "expires": time.time() + 3600})

        uploaded_social = []

        def fake_yt_upload(acc, path, title, desc, tags, privacy):
            uploaded_social.append((acc["id"], Path(path).read_bytes(), title))
            return f"vid_{acc['id']}", f"https://youtube.com/shorts/vid_{acc['id']}"

        def fake_tt_upload(acc, path, caption, privacy):
            uploaded_social.append((acc["id"], Path(path).read_bytes(), caption))
            return f"tt_{acc['id']}", f"https://tiktok.com/@progamer/video/1"

        orig_yt_upload = accounts.youtube_upload
        orig_tt_upload = accounts.tiktok_upload
        accounts.youtube_upload = fake_yt_upload
        accounts.tiktok_upload = fake_tt_upload

        def make_job_with_clips(clip_specs, creator_id=None, src_type="file", src_url="", created_at=None, status="done"):
            src = {"type": "url", "url": src_url, "name": "vod.mp4"} if src_type == "url" else {"type": "file", "path": "", "name": "vod.mp4"}
            j = jobs.create(src, {}, enqueue=False, creator=creator_id)
            jdir = Path(j["dir"])
            cdir_j = jdir / "clips"
            cdir_j.mkdir(parents=True, exist_ok=True)
            raw_video = jdir / "source.mp4"
            raw_video.write_bytes(b"RAW_SOURCE_VIDEO_" * 400)
            j["source_path"] = str(raw_video)
            if src_type == "file":
                j["source"]["path"] = str(raw_video)
            j["width"], j["height"], j["duration"] = 1920, 1080, 120.0
            if created_at is not None:
                j["created"] = created_at
            for sp in clip_specs:
                cid = sp["id"]
                mp4_bytes = sp.get("bytes", f"MP4_PAYLOAD_{j['id']}_{cid}".encode("utf-8") * 120)
                if sp.get("status", "done") == "done":
                    (cdir_j / f"{cid}.mp4").write_bytes(mp4_bytes)
                    (cdir_j / f"{cid}.jpg").write_bytes(b"\xff\xd8\xffJPGCOVER")
                    (cdir_j / f"{cid}.ass").write_text("[Script Info]\nTitle: test\n", encoding="utf-8")
                    (cdir_j / f"{cid}.json").write_text(json.dumps({"title": sp.get("title", cid)}), encoding="utf-8")
                src_piece_path = ""
                if sp.get("with_piece", True) and sp.get("status", "done") == "done":
                    piece = cdir_j / f"{cid}.src.ts"
                    piece.write_bytes(b"TS_PIECE_" * 200)
                    src_piece_path = str(piece)
                clip_dict = {
                    "id": cid,
                    "status": sp.get("status", "done"),
                    "file": f"{cid}.mp4" if sp.get("status", "done") == "done" else "",
                    "cover": f"{cid}.jpg" if sp.get("status", "done") == "done" else "",
                    "title": sp.get("title", f"Epic Moment {cid}"),
                    "hook": sp.get("hook", "WAIT FOR IT"),
                    "description": "Nice play",
                    "hashtags": ["gaming"],
                    "score": 92,
                    "start": 10.0,
                    "end": 25.0,
                    "version": sp.get("version", 1),
                    "made_at": sp.get("made_at", time.time()),
                }
                if src_piece_path:
                    clip_dict["src"] = src_piece_path
                j["clips"].append(clip_dict)
            j["status"] = status
            jobs.save(j)
            return j

        j_multi = make_job_with_clips(
            [
                {"id": "clip01", "title": "Clutch: 1v5 Ace!"},
                {"id": "clip02", "title": "Second Clip"},
                {"id": "clip03", "status": "error", "title": "Failed Clip"},
            ],
            creator_id=cr_arch["id"],
        )
        j_multi_dir = Path(j_multi["dir"])
        c1_mp4 = j_multi_dir / "clips" / "clip01.mp4"
        c1_src = j_multi_dir / "clips" / "clip01.src.ts"
        c1_jpg = j_multi_dir / "clips" / "clip01.jpg"
        c1_ass = j_multi_dir / "clips" / "clip01.ass"
        c1_json = j_multi_dir / "clips" / "clip01.json"
        c1_expected_bytes = c1_mp4.read_bytes()
        src_mp4 = j_multi_dir / "source.mp4"

        pub_before = jobs.public(j_multi)
        c1_pub_before = next(c for c in pub_before["clips"] if c["id"] == "clip01")
        check("jobs.public reports local=True and no drive before archive",
              (c1_pub_before.get("local"), "drive" in c1_pub_before),
              (True, False))

        # Schedule clip01 on TWO accounts (yt_1 and tt_1)
        queued_posts = posting.enqueue(j_multi, j_multi["clips"][0], ["yt_1", "tt_1"], now=True)
        check("has_open_posts is True when scheduled posts exist",
              (len(queued_posts), posting.has_open_posts(j_multi["id"], "clip01")),
              (2, True))

        # First post finishes -> second post is still scheduled -> clip MUST stay local!
        queued_posts[0]["status"] = "posting"
        posting._post(queued_posts[0])
        gdrive.drain_queue()
        check("after 1st of 2 posts, clip stays on local SSD and is not archived yet",
              (queued_posts[0]["status"], posting.has_open_posts(j_multi["id"], "clip01"),
               c1_mp4.is_file(), c1_src.is_file(), "drive" not in j_multi["clips"][0]),
              ("posted", True, True, True, True))

        # Second post finishes -> no open posts left -> clip01 archives to Drive & deletes .mp4 + .src.ts!
        count_before = gdrive.public_status(include_quota=False)["archived_count"]
        bytes_before = gdrive.public_status(include_quota=False)["archived_bytes"]
        storage._used["at"] = time.time()
        queued_posts[1]["status"] = "posting"
        posting._post(queued_posts[1])
        gdrive.drain_queue()

        c1_after = jobs.get(j_multi["id"])["clips"][0]
        st_after_c1 = gdrive.public_status(include_quota=False)
        check("after 2nd post finishes, clip01 is archived to Drive with verified byte size",
              (queued_posts[1]["status"], posting.has_open_posts(j_multi["id"], "clip01"),
               bool(c1_after.get("drive")), c1_after.get("drive", {}).get("size") == len(c1_expected_bytes)),
              ("posted", False, True, True))
        check("archiving unlinks clip01.mp4 and clip01.src.ts while keeping cover .jpg, .ass, and .json",
              (not c1_mp4.exists(), not c1_src.exists(), "src" not in c1_after,
               c1_jpg.is_file(), c1_ass.is_file(), c1_json.is_file()),
              (True, True, True, True, True, True))
        check("gdrive counters increment and storage._used cache invalidated",
              (st_after_c1["archived_count"] == count_before + 1,
               st_after_c1["archived_bytes"] == bytes_before + len(c1_expected_bytes),
               storage._used["at"] == 0),
              (True, True, True))
        log_msgs = [entry["msg"] for entry in creators.read_log(cr_arch["id"], 10)]
        check("creator log records Google Drive archive event",
              any("Archived to Google Drive" in m and "Clutch: 1v5 Ace!" in m for m in log_msgs),
              True)

        # source.mp4 MUST STILL EXIST because clip02 (status=done) is not archived yet!
        check("source.mp4 stays while clip02 is still unarchived",
              (src_mp4.is_file(), bool(jobs.get(j_multi["id"])["source_path"])),
              (True, True))

        # Manual 1-click archive on clip02 -> once clip02 archives (clip03 is status=error), source.mp4 is cleaned!
        check("request_archive queues clip02", gdrive.request_archive(j_multi["id"], "clip02"), True)
        check("duplicate enqueue_clip while inflight returns False",
              gdrive.enqueue_clip(alice.id, j_multi["id"], "clip02", manual=True), False)
        gdrive.drain_queue()
        j_multi_reloaded = jobs.get(j_multi["id"])
        on_disk_job = json.loads((j_multi_dir / "job.json").read_text(encoding="utf-8"))
        check("once all non-error clips are archived, source.mp4 is unlinked and source paths cleared",
              (not src_mp4.exists(),
               j_multi_reloaded["source_path"] == "",
               j_multi_reloaded["source"].get("path", "") == "",
               on_disk_job["source_path"] == ""),
              (True, True, True, True))

        pub_after = jobs.public(j_multi_reloaded)
        c1_pub_after = next(c for c in pub_after["clips"] if c["id"] == "clip01")
        check("jobs.public reports local=False and exposes drive metadata after archive",
              (c1_pub_after.get("local"), c1_pub_after.get("drive", {}).get("id") == c1_after["drive"]["id"]),
              (False, True))

        # request_archive validation checks
        try:
            gdrive.request_archive(j_multi["id"], "clip01")
            check("request_archive rejects already-archived clip", False)
        except ValueError as e:
            check("request_archive rejects already-archived clip", "already archived" in str(e).lower())

        try:
            gdrive.request_archive(j_multi["id"], "clip03")
            check("request_archive rejects non-done clip", False)
        except ValueError as e:
            check("request_archive rejects non-done clip", "not ready" in str(e).lower())

    # ---- 10. Re-posting an archived clip & account.zip_clips via ensure_local()
    with ctx.as_user(alice.id):
        # Evict local drive_cache first so both re-post and zip_clips must stream from Google Drive
        gdrive._purge_user_cache(alice.id)
        uploaded_social.clear()
        p_repost = posting.enqueue(jobs.get(j_multi["id"]), jobs.get(j_multi["id"])["clips"][0], ["yt_2"], now=True)
        p_repost[0]["status"] = "posting"
        posting._post(p_repost[0])
        check("re-posting an archived clip streams from Drive via ensure_local and succeeds",
              (p_repost[0]["status"], len(uploaded_social), uploaded_social[0][0], uploaded_social[0][1] == c1_expected_bytes),
              ("posted", 1, "yt_2", True))

        # Evict cache again and verify account.zip_clips downloads archived clips seamlessly
        gdrive._purge_user_cache(alice.id)
        zpath = account.zip_clips([(j_multi["id"], "clip01"), (j_multi["id"], "clip02")])
        with zipfile.ZipFile(zpath, "r") as zf:
            znames = zf.namelist()
            zdata = {n: zf.read(n) for n in znames}
        check("account.zip_clips packages archived clips via ensure_local",
              (len(znames) == 2, any(v == c1_expected_bytes for v in zdata.values())),
              (True, True))

    # ---- 11. jobs.request_render guard on archived clips vs URL-source jobs
    with ctx.as_user(alice.id):
        try:
            jobs.request_render(j_multi["id"], "clip01", {"hook": "NEW HOOK"})
            check("request_render raises friendly ValueError for archived file-upload clip without raw source", False)
        except ValueError as e:
            check("request_render raises friendly ValueError for archived file-upload clip without raw source",
                  "archived to google drive" in str(e).lower() and "cannot be re-made" in str(e).lower())

        j_url = make_job_with_clips(
            [{"id": "clip01", "title": "URL Clip"}],
            src_type="url",
            src_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )
        gdrive.request_archive(j_url["id"], "clip01")
        gdrive.drain_queue()
        check("URL-source job source.mp4 also cleaned after archive", not (Path(j_url["dir"]) / "source.mp4").exists(), True)

        orig_q_put = jobs._q.put
        queued_renders = []
        jobs._q.put = lambda item, user=None: queued_renders.append((item, user))
        try:
            jobs.request_render(j_url["id"], "clip01", {"hook": "REMAKE FROM URL"})
            check("URL-source job still allows request_render after archive",
                  (jobs.get(j_url["id"])["clips"][0]["status"], len(queued_renders)),
                  ("queued", 1))
        finally:
            jobs._q.put = orig_q_put

    # ---- 12. Upload failure: SSD files kept, last_error redacted, 3-strike retry backoff & bell notification + FolderGone retry
    with ctx.as_user(alice.id):
        j_fail = make_job_with_clips([{"id": "clip01", "title": "Tough Upload"}])
        jf_mp4 = Path(j_fail["dir"]) / "clips" / "clip01.mp4"
        jf_src = Path(j_fail["dir"]) / "clips" / "clip01.src.ts"
        jf_raw = Path(j_fail["dir"]) / "source.mp4"

        state_mock["put_fail_text"] = "502 upstream error Bearer gd-access-1 upload_id=up_secret_fail_1"
        t_fail0 = time.time()
        gdrive.request_archive(j_fail["id"], "clip01")
        gdrive.drain_queue()

        st_fail1 = gdrive.public_status(include_quota=False)
        r_entry1 = gdrive._retries.get((alice.id, j_fail["id"], "clip01"))
        check("1st upload failure leaves local .mp4, .src.ts, and source.mp4 untouched on SSD",
              (jf_mp4.is_file(), jf_src.is_file(), jf_raw.is_file(), "drive" not in jobs.get(j_fail["id"])["clips"][0]),
              (True, True, True, True))
        check("1st upload failure saves redacted last_error and schedules retry #1 in ~600s",
              ("502" in st_fail1["last_error"],
               "gd-access-1" not in st_fail1["last_error"],
               "up_secret_fail_1" not in st_fail1["last_error"],
               r_entry1 is not None and r_entry1[0] == 1 and r_entry1[1] >= t_fail0 + 590),
              (True, True, True, True))

        # Sweep before due_at does NOT prematurely re-queue
        sw_early = gdrive.sweep_user(alice.id, now=t_fail0 + 60)
        check("sweep_user before retry due_at does not re-queue failed clip", sw_early["queued"], 0)

        # Sweep after due_at triggers attempt #2 -> fails -> tries=2
        sw_due2 = gdrive.sweep_user(alice.id, now=r_entry1[1] + 1)
        check("sweep_user at due_at re-queues retry #2", sw_due2["queued"], 1)
        gdrive.drain_queue()
        r_entry2 = gdrive._retries.get((alice.id, j_fail["id"], "clip01"))
        check("2nd upload failure increments retry count to 2",
              r_entry2 is not None and r_entry2[0] == 2, True)

        # Sweep after 2nd due_at triggers attempt #3 -> fails -> pops _retries & records bell notification!
        notices_before = len(notices.mine()["items"])
        sw_due3 = gdrive.sweep_user(alice.id, now=r_entry2[1] + 1)
        gdrive.drain_queue()
        notices_after = notices.mine()["items"]
        check("3rd upload failure clears _retries, keeps local files, and sends Storage failure notification",
              (sw_due3["queued"] == 1,
               (alice.id, j_fail["id"], "clip01") not in gdrive._retries,
               jf_mp4.is_file(),
               len(notices_after) == notices_before + 1,
               notices_after[0]["kind"] == "post",
               notices_after[0]["link"] == "/app/settings/storage",
               "Tough Upload" in notices_after[0]["title"]),
              (True, True, True, True, True, True, True))
        state_mock["put_fail_text"] = None

        # FolderGone during archive_one upload_mp4: drops folder cache, recreates folder, and succeeds cleanly!
        cfg_fg = gdrive._cfg()
        fld_cached = gdrive.folder_for(cfg_fg, "", ts=jobs.get(j_fail["id"])["clips"][0]["made_at"])
        state_mock["folders"].pop(fld_cached, None)  # delete cached month folder right before archive_one
        res_fg = gdrive.archive_one(alice.id, j_fail["id"], "clip01", manual=True)
        check("archive_one recovers from FolderGone, clears last_error, and archives clip",
              (res_fg, bool(jobs.get(j_fail["id"])["clips"][0].get("drive")),
               gdrive.public_status(include_quota=False)["last_error"], not jf_mp4.exists()),
              ("archived", True, "", True))

    # ---- 13. Version guard: mid-upload re-render skips unlinking new local file; render_one clears stale drive info
    with ctx.as_user(alice.id):
        j_ver = make_job_with_clips([{"id": "clip01", "title": "Remake Race", "version": 1},
                                     {"id": "clip02", "title": "Keep Source Alive", "version": 1}])
        jv_mp4 = Path(j_ver["dir"]) / "clips" / "clip01.mp4"
        new_v2_bytes = b"BRAND_NEW_V2_RENDER_BYTES" * 100

        def simulate_concurrent_rerender():
            state_mock["on_put"] = None
            with jobs._lock:
                c_live = jobs.get(j_ver["id"])["clips"][0]
                jv_mp4.write_bytes(new_v2_bytes)
                c_live["version"] = 2
                jobs.save(jobs.get(j_ver["id"]))

        state_mock["on_put"] = simulate_concurrent_rerender
        ver_status = gdrive.archive_one(alice.id, j_ver["id"], "clip01", manual=True)
        state_mock["on_put"] = None
        c_ver_after = jobs.get(j_ver["id"])["clips"][0]
        check("version guard skips archiving and preserves new v2 local .mp4 when clip re-rendered mid-upload",
              (ver_status, "drive" not in c_ver_after, jv_mp4.is_file(), jv_mp4.read_bytes() == new_v2_bytes),
              ("skipped", True, True, True))

        # Now archive clip01 (v2) while clip02 keeps source.mp4 alive, then run render_one -> version becomes 3 & drive popped!
        check("archiving v2 succeeds cleanly", gdrive.archive_one(alice.id, j_ver["id"], "clip01", manual=True), "archived")
        check("v2 has drive metadata before re-render", bool(jobs.get(j_ver["id"])["clips"][0].get("drive")), True)

        orig_render_clip = render.render_clip

        def fake_render_clip(src, out_dir, cid, clip_d, near, w, h):
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"{cid}.mp4").write_bytes(b"V3_RENDERED_LOCAL")
            (out_dir / f"{cid}.jpg").write_bytes(b"\xff\xd8\xffJPG")
            return {"file": f"{cid}.mp4", "cover": f"{cid}.jpg"}

        render.render_clip = fake_render_clip
        try:
            c_to_rerender = jobs.get(j_ver["id"])["clips"][0]
            c_to_rerender["captions"] = False
            jobs.render_one(jobs.get(j_ver["id"]), c_to_rerender, [])
        finally:
            render.render_clip = orig_render_clip
        check("render_one (version > 1) pops stale drive dict so new local render is authoritative",
              (c_to_rerender["version"], "drive" not in c_to_rerender, (Path(j_ver["dir"]) / "clips" / "clip01.mp4").is_file()),
              (3, True, True))

    # ---- 14. sweep_user: auto_days age threshold, force=True, open scheduled post protection, 0-clip source cleanup & path safety
    with ctx.as_user(alice.id):
        now_sw = time.time()
        gdrive.save_settings({"auto_after_post": True, "auto_days": 3})

        # Clean up clip02 from j_ver first so j_ver doesn't affect counts
        gdrive.archive_one(alice.id, j_ver["id"], "clip01", manual=True)
        gdrive.archive_one(alice.id, j_ver["id"], "clip02", manual=True)

        j_old_unposted = make_job_with_clips(
            [{"id": "clip01", "title": "Old Unposted", "made_at": now_sw - 4 * 86400}],
            created_at=now_sw - 4 * 86400,
        )
        j_young_unposted = make_job_with_clips(
            [{"id": "clip01", "title": "Young Unposted", "made_at": now_sw - 1 * 86400}],
            created_at=now_sw - 1 * 86400,
        )
        j_old_scheduled = make_job_with_clips(
            [{"id": "clip01", "title": "Old But Scheduled", "made_at": now_sw - 5 * 86400}],
            created_at=now_sw - 5 * 86400,
        )
        posting.enqueue(j_old_scheduled, j_old_scheduled["clips"][0], ["yt_1"], now=False)

        j_zero_old = make_job_with_clips([], created_at=now_sw - 4 * 86400)
        j_zero_young = make_job_with_clips([], created_at=now_sw - 1 * 86400)

        # Path-safety test: an external file outside job["dir"] referenced in source_path must NEVER be unlinked!
        outside_file = DATA_DIR / "never_touch_external.mp4"
        outside_file.write_bytes(b"PRECIOUS_EXTERNAL_FILE")
        j_ext = make_job_with_clips([], created_at=now_sw - 5 * 86400)
        Path(j_ext["source_path"]).unlink(missing_ok=True)
        j_ext["source_path"] = str(outside_file)
        j_ext["source"]["path"] = str(outside_file)
        jobs.save(j_ext)

        sw1 = gdrive.sweep_user(alice.id, now=now_sw, force=False)
        gdrive.drain_queue()
        check("sweep_user archives old unposted clip and cleans old 0-clip job source",
              (sw1["queued"], sw1["cleaned"],
               bool(jobs.get(j_old_unposted["id"])["clips"][0].get("drive")),
               not (Path(j_zero_old["dir"]) / "source.mp4").exists()),
              (1, 1, True, True))
        check("sweep_user skips young unposted clip, skips clip with open scheduled post, and keeps young 0-clip source",
              ("drive" not in jobs.get(j_young_unposted["id"])["clips"][0],
               (Path(j_young_unposted["dir"]) / "clips" / "clip01.mp4").is_file(),
               "drive" not in jobs.get(j_old_scheduled["id"])["clips"][0],
               (Path(j_old_scheduled["dir"]) / "clips" / "clip01.mp4").is_file(),
               (Path(j_zero_young["dir"]) / "source.mp4").is_file()),
              (True, True, True, True, True))
        check("clean_job_source never unlinks files outside job['dir']",
              (outside_file.is_file(), jobs.get(j_ext["id"])["source_path"] == ""),
              (True, True))

        # force=True archives young unposted clip, while STILL skipping clip with open scheduled post!
        sw_force = gdrive.sweep_user(alice.id, now=now_sw, force=True)
        gdrive.drain_queue()
        check("sweep_user(force=True) archives young unposted clip while still skipping open scheduled post",
              (sw_force["queued"],
               bool(jobs.get(j_young_unposted["id"])["clips"][0].get("drive")),
               "drive" not in jobs.get(j_old_scheduled["id"])["clips"][0],
               (Path(j_old_scheduled["dir"]) / "clips" / "clip01.mp4").is_file()),
              (1, True, True, True))

        accounts.youtube_upload = orig_yt_upload
        accounts.tiktok_upload = orig_tt_upload

    # ---- 15. account.delete_account(uid) calls gdrive.forget(uid) including _retries
    del_user, _ = auth._create("delme@example.com", "delme", password_hash="x")
    with ctx.as_user(del_user.id):
        _ = gdrive._cfg()
        _ = gdrive.connect_url()
    gdrive._inflight.add((del_user.id, "jd", "cd"))
    gdrive._retries[(del_user.id, "jd", "cd")] = (1, time.time() + 600, False)
    del_cache = config.TMP_DIR / "drive_cache" / f"{del_user.id}_cached.mp4"
    del_cache.parent.mkdir(parents=True, exist_ok=True)
    del_cache.write_bytes(b"cached-video")
    del_res = account.delete_account(del_user.id)
    check("account.delete_account calls gdrive.forget(uid) clearing _mem, _states, _inflight, _retries and drive_cache",
          (del_res.get("ok"),
           del_user.id not in gdrive._mem,
           all(v[0] != del_user.id for v in gdrive._states.values()),
           (del_user.id, "jd", "cd") not in gdrive._inflight,
           (del_user.id, "jd", "cd") not in gdrive._retries,
           not del_cache.exists()),
          (True, True, True, True, True, True))

    print("ALL PASSED" if not fails else f"{len(fails)} failed")
finally:
    db.engine.dispose()
    shutil.rmtree(DATA_DIR, ignore_errors=True)

sys.exit(1 if fails else 0)
