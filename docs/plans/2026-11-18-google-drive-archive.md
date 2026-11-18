# Google Drive Archive & SSD Cleanup Implementation Plan

> **Execution Sub-Skill:** Use `executing-plans` or `subagent-driven-development` to implement this plan task-by-task with TDD.

**Goal:** Automatically archive finished/posted `.mp4` clips to a connected 2 TB+ Google Drive account via OAuth (`drive.file`) and delete heavy local `.mp4`, `.src`, and `source.*` files from the SSD while keeping thumbnails and on-demand playback/download/re-posting working transparently.

**Architecture:** Dedicated `studio/gdrive.py` module decoupled from social posting accounts (`studio/accounts.py`), reusing the server's Google OAuth credentials (`accounts.app_keys("google")`) and configured under `Settings -> Storage`. Clips stay on local SSD while rendering or waiting in `posting.py` (`scheduled`/`posting`), upload in 8 MB resumable chunks once all scheduled posts finish (or manually / after `auto_days`), verify exact byte count on Drive before unlinking local media, and stream back into a 1-hour local `TMP_DIR/drive_cache` via `gdrive.ensure_local(job, clip)` whenever `/media/`, `/pub/`, `.zip`, or late social posting needs the file.

**Tech Stack:** Python 3.11+, FastAPI, `httpx` (Google OAuth2 + Drive API v3 Resumable Upload & `alt=media`), Vanilla JS/HTML (`web/index.html`), PeakCut standalone test harness (`tests/run.py`).

---

## Context & Decisions Summary

- **Understanding Summary:**
  - Keep active recordings, buffers, rendering clips, and queued social posts on local SSD for zero latency.
  - Archive `.mp4` to `PeakCut/<Creator or General>/<YYYY-MM>/` on Google Drive immediately after all queued social posts for that clip reach `posted`.
  - Provide manual 1-click archive + periodic background sweep (`auto_days`, default `3`) to archive unposted clips and delete raw `source.*` videos once no clips remain pending.
  - Never delete a local file until Google Drive returns HTTP 200/201 with matching byte `size` and `job.json` is persisted.
- **Decision Log:**
  - *Direct OAuth API (`drive.file`) over Google Drive Desktop mount:* Prevents SQLite WAL locks and FFmpeg segment sync thrashing; works identically on Windows PC and Linux VPS.
  - *Dedicated `studio/gdrive.py` in `Settings -> Storage` over `accounts.py`:* Allows centralizing multiple YouTube/TikTok creator channels into a single 2 TB Google Drive account without polluting social posting caps, gaps, or view analytics.
  - *1-hour `TMP_DIR/drive_cache` via `ensure_local`:* Preserves FastAPI `FileResponse` HTTP `Range` support (`<video>` player, Safari, Instagram `/pub/` fetch) and `zip_clips` without holding files in RAM.

---

### Task 1: Core Google Drive OAuth, Folder Hierarchy & Resumable Upload (`studio/gdrive.py`)

**Files:**
- Create: `d:/PeakCut/studio/gdrive.py`
- Create: `d:/PeakCut/tests/test_gdrive.py`

**Step 1: Write failing unit tests for OAuth, folder creation, resumable upload verification, and on-demand cache**

Create `tests/test_gdrive.py` following `tests/test_connect.py` conventions (`TestClient`, temporary `PEAKCUT_DATA`, mocked `httpx`):
- Verify `can_connect()` reflects `accounts.app_keys("google")`.
- Verify `/oauth/gdrive/start` -> `/oauth/gdrive/callback` exchanges code, fetches user email & Drive quota, stores `gdrive.json` (`0600`), and never leaks tokens in `/api/gdrive`.
- Verify folder tree `PeakCut -> Creator Name -> YYYY-MM` is created once and cached across uploads.
- Verify `upload_file()` sends 8 MB chunks and **raises `RuntimeError` without deleting local file** if Drive reports a mismatched byte size or HTTP error.
- Verify `ensure_local(job, clip)` downloads missing archived `.mp4` into `TMP_DIR / "drive_cache"` and reuses cached copy on subsequent reads.

**Step 2: Run test to verify it fails**

Run: `python tests/test_gdrive.py`
Expected: FAIL (`ModuleNotFoundError: No module named 'studio.gdrive'`).

**Step 3: Implement `studio/gdrive.py` core primitives**

Implement in `studio/gdrive.py`:
- `SCOPES = "https://www.googleapis.com/auth/drive.file https://www.googleapis.com/auth/userinfo.email"`
- `DEFAULTS = {"connected": False, "email": "", "refresh_token": "", "access_token": "", "expires": 0.0, "auto_after_post": True, "auto_days": 3, "folders": {}, "archived_bytes": 0, "archived_count": 0, "last_error": ""}`
- Per-user storage at `config.user_dir(ctx.uid()) / "accounts" / "gdrive.json"` (`0600` permissions).
- `can_connect()`, `redirect_uri()`, `connect_url()`, `connect_finish(code, state)`, `disconnect()`, `save_settings(patch)`, `public_status()`.
- `_token(cfg)` auto-refreshing via `https://oauth2.googleapis.com/token`.
- `quota()` calling `GET https://www.googleapis.com/drive/v3/about?fields=storageQuota,user`.
- `_folder_for(cfg, creator_name, ts)` creating/caching `PeakCut` -> `<creator_name or "General">` -> `<YYYY-MM>` folders (`mimeType = application/vnd.google-apps.folder`), recovering if a cached folder was trashed/404.
- `upload_mp4(cfg, path, folder_id, name)` using `POST https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&fields=id,size,webViewLink` + `PUT` chunks (`8 * 1024 * 1024`), asserting `int(done.get("size") or -1) == size`.
- `ensure_local(job, clip) -> Path | None` returning local `clips/<file>` if present, else downloading `https://www.googleapis.com/drive/v3/files/{id}?alt=media` to `config.TMP_DIR / "drive_cache" / f"{job['user']}_{did}.mp4"`.

**Step 4: Run test to verify core primitives pass**

Run: `python tests/test_gdrive.py`
Expected: Core unit checks PASS.

**Step 5: Commit**

```bash
git add studio/gdrive.py tests/test_gdrive.py
git commit -m "feat(storage): add Google Drive OAuth, folder tree and resumable upload module"
```

---

### Task 2: Safe Archiving Worker, Post-Publish Trigger & Periodic SSD Cleanup

**Files:**
- Modify: `d:/PeakCut/studio/gdrive.py`
- Modify: `d:/PeakCut/studio/posting.py:22-145`
- Modify: `d:/PeakCut/studio/account.py:78-167`
- Modify: `d:/PeakCut/studio/jobs.py:154-169,349-377`
- Test: `d:/PeakCut/tests/test_gdrive.py`

**Step 1: Expand `tests/test_gdrive.py` with lifecycle & safety tests**

Add checks in `tests/test_gdrive.py`:
- Clip queued on 2 social accounts (e.g. YouTube + TikTok): after 1st post completes (`TikTok` still `scheduled`), clip `.mp4` **stays on SSD**; after 2nd post completes, worker uploads `.mp4` to Drive, sets `clip["drive"]`, deletes `clip01.mp4` and `clip01.src.ts`, and keeps `clip01.jpg`.
- Upload failure simulation (HTTP 500): `clip01.mp4` **remains untouched on SSD**, `last_error` recorded.
- Unposted clip older than `auto_days` (e.g. 3 days) is archived by `sweep_user()` and raw `source.mp4` is deleted once all clips of that job are archived or past `auto_days` with no active/queued renders.
- Re-posting an already archived clip to a newly connected Instagram/TikTok account succeeds via `ensure_local()`.
- `zip_clips` succeeds on archived clips via `ensure_local()`.

**Step 2: Run test to verify new checks fail**

Run: `python tests/test_gdrive.py`
Expected: FAIL on lifecycle/trigger assertions.

**Step 3: Implement queue, hooks, and SSD cleanup**

- In `studio/posting.py`:
  - Add `has_open_posts(jid: str, cid: str) -> bool` checking `_mine()` for `status in OPEN` (`"scheduled"`, `"posting"`).
  - In `_post(p)`: if `not path.is_file()`, resolve `path = gdrive.ensure_local(job, clip) or path`.
  - After `p.update(status="posted", ...)`: if `not has_open_posts(p["job"], p["clip"])`, call `gdrive.on_clip_posted(p["user"], p["job"], p["clip"])`.
- In `studio/gdrive.py`:
  - Implement thread-safe `_queue`, `_inflight` set `(uid, jid, cid)`, worker thread `_worker()`, and `_archive_one(uid, jid, cid, manual=False)`:
    - Re-checks `clip["status"] == "done"`, not already archived (`not clip.get("drive")`), local `.mp4` exists, and (`manual` or `not posting.has_open_posts(jid, cid)`).
    - Uploads to Drive, verifies byte size, updates `clip["drive"] = {"id": ..., "url": ..., "size": ..., "at": ...}`, saves `job.json` via `jobs.save(job)`, then unlinks `clips/<file>` and `clips/<cid>.src*`.
    - Checks if all clips in `job` are done & archived; if so and `job.get("source_path")` / `job["source"].get("path")` exists inside `job["dir"]`, unlinks the heavy `source.*` file.
    - Invalidates disk usage cache (`storage._used["at"] = 0`).
  - Implement `sweep_all()` & `_sweep_loop()` (every 30 min + cache eviction for `TMP_DIR / "drive_cache"` > 3600s).
  - Implement `forget(uid)` for account deletion.
- In `studio/jobs.py`:
  - Expose `local_file` boolean on each public clip dict in `jobs.public(job)` (`"local": (Path(job["dir"]) / "clips" / c["file"]).is_file() if c.get("file") else False`) so the UI knows whether the `.mp4` is still on SSD or archived on Drive.
  - In `request_render`: if local source (`clip["src"]` or `job["source_path"]`) is gone and `job["source"]["type"] != "url"`, raise `ValueError("This clip was archived to Google Drive and its raw video was removed to free disk space, so it cannot be re-rendered.")`.
- In `studio/account.py`:
  - In `zip_clips`: if `not f.is_file()` and `clip.get("drive")`, resolve `f = gdrive.ensure_local(job, clip)`.
  - In `delete_account`: call `gdrive.forget(uid)`.

**Step 4: Run tests to verify they pass**

Run: `python tests/test_gdrive.py`
Expected: PASS.

**Step 5: Commit**

```bash
git add studio/gdrive.py studio/posting.py studio/account.py studio/jobs.py tests/test_gdrive.py
git commit -m "feat(storage): add post-publish Drive archiving queue and automatic SSD cleanup"
```

---

### Task 3: Server Routes, OAuth Endpoints & Media On-Demand Proxy (`studio/server.py`)

**Files:**
- Modify: `d:/PeakCut/studio/server.py:24-26,72-76,572-582,1026-1034,1366-1408,1445-1475`
- Modify: `d:/PeakCut/.env.example:77-83`
- Modify: `d:/PeakCut/DEPLOY.md:45-53`
- Test: `d:/PeakCut/tests/test_gdrive.py`

**Step 1: Add route & permission tests to `tests/test_gdrive.py`**

Test HTTP endpoints via `TestClient`:
- `GET /oauth/gdrive/start` and `GET /oauth/gdrive/callback` redirect to `/app/settings/storage?gdrive=connected`.
- `GET /api/gdrive`, `PUT /api/gdrive`, `DELETE /api/gdrive`, `POST /api/gdrive/sweep`, `POST /api/jobs/{jid}/clips/{cid}/archive`.
- Cross-site `Origin` is rejected (`403`); user isolation prevents user B from archiving or reading user A's clips (`404`).
- `GET /media/{jid}/clip01.mp4` and signed `GET /pub/{token}.mp4` return `200` with video bytes even after `clip01.mp4` was deleted from SSD (fetched via `gdrive.ensure_local`).

**Step 2: Run test to verify route checks fail**

Run: `python tests/test_gdrive.py`
Expected: FAIL (`404` on `/api/gdrive` and `/oauth/gdrive/start`).

**Step 3: Wire routes and startup in `studio/server.py`**

- Import `gdrive` and call `gdrive.start()` in `_startup()`.
- Include `"gdrive": {"ready": gdrive.can_connect(), "connected": gdrive.is_connected()}` in `GET /api/status`.
- Add `/oauth/gdrive/start` and `/oauth/gdrive/callback` (redirecting back to `/app/settings/storage`).
- Add `GET /api/gdrive`, `PUT /api/gdrive`, `DELETE /api/gdrive`, `POST /api/gdrive/sweep`, and `POST /api/jobs/{jid}/clips/{cid}/archive` (invalidating `_disk.pop(ctx.uid(), None)` when freeing disk).
- In `media_file` (`/media/{jid}/{name}`) and `public_clip` (`/pub/{token}.mp4`): when `.mp4` is not on disk, look up matching clip and call `gdrive.ensure_local(job, clip)` before returning `404`.
- Document `/oauth/gdrive/callback` redirect URI in `.env.example` and `DEPLOY.md`.

**Step 4: Run test to verify all route & media checks pass**

Run: `python tests/test_gdrive.py`
Expected: PASS.

**Step 5: Commit**

```bash
git add studio/server.py .env.example DEPLOY.md tests/test_gdrive.py
git commit -m "feat(server): expose Google Drive archive routes and on-demand media fallback"
```

---

### Task 4: Dashboard UI — Storage Settings, Clip Badges & Manual Archive (`web/index.html`)

**Files:**
- Modify: `d:/PeakCut/web/index.html`

**Step 1: Add UI controls in `Settings -> Storage` and Clip Cards/Modal**

- **`Settings -> Storage` (`tab === "storage"`):**
  - Fetch `/api/gdrive` alongside `/api/storage` (and `/api/apps` when admin needs to configure Google OAuth Client ID/Secret directly from Storage if not set yet, displaying `/oauth/gdrive/callback` with Copy button).
  - Render **Google Drive archive** section:
    - Connection pill (`Connected · email` + Drive free/total GB bar + `Disconnect` button, or `Connect Google Drive` button).
    - Stats line: clips archived & GB freed from local SSD + `Archive eligible clips now` button (`data-act="gdsweep"`).
    - Settings switches/inputs (`data-act="gdsave"` on change):
      - `Archive clips to Google Drive right after posting` (`auto_after_post`).
      - `Archive unposted clips and remove raw videos after (days, 0 = off)` (`auto_days`).
    - Show `last_error` banner if Drive reported an error.
- **Clip Card (`clipHtml`) & Clip Editor (`drawClipDlg`):**
  - If `c.drive?.url`: show clickable pill `Drive` (`<a class="pill ok" href="..." target="_blank">Drive</a>`) and in editor show note when raw file was removed from SSD (disabling `Save and re-make clip` if `j.source?.type === "file" && !c.local`).
  - If `!c.drive && c.status === "done" && S.status.gdrive?.connected`: show compact `Archive to Drive` button (`data-act="gdclip"`) in clip card footer and clip modal footer.

**Step 2: Verify UI markup & JS syntax**

Run quick script/browser check + existing frontend-checking tests (`tests/test_ptbr.py`, `tests/test_captions.py`) to verify zero JS syntax errors and clean rendering.

**Step 3: Commit**

```bash
git add web/index.html
git commit -m "feat(ui): add Google Drive archive controls in Storage settings and clip cards"
```

---

### Task 5: Full Regression Suite Integration (`tests/run.py`)

**Files:**
- Modify: `d:/PeakCut/tests/run.py:70-72`

**Step 1: Register `test_gdrive.py` in `tests/run.py`**

Add `("Google Drive archiving and SSD cleanup", ["tests/test_gdrive.py"])` to `runs` in `tests/run.py`.

**Step 2: Run full PeakCut test suite**

Run: `python tests/run.py`
Expected: `All tests passed.` (all 23 test suites green, exit code `0`).

**Step 3: Commit**

```bash
git add tests/run.py
git commit -m "test(gdrive): register Google Drive archive suite in main test runner"
```
