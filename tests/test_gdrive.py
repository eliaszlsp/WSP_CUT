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

    print("ALL PASSED" if not fails else f"{len(fails)} failed")
finally:
    db.engine.dispose()
    shutil.rmtree(DATA_DIR, ignore_errors=True)

sys.exit(1 if fails else 0)
