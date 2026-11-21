# Long Videos (10–20 min), Derived Shorts, Priority Queue & Magnetic Thumbnails Implementation Plan

> **Execution Sub-Skill:** Use `executing-plans` or `subagent-driven-development` to implement this plan task-by-task with TDD.

**Goal:** Enable PeakCut to produce 16:9 (`1920x1080`) 10–20 minute YouTube videos (continuous story/match blocks and best-moment compilations), automatically extract derived 9:16 Shorts/Reels/TikToks from them, generate magnetic `1280x720` thumbnails (`Pillow`+`FFmpeg` default + optional Gemini image AI) uploaded via YouTube `thumbnails.set`, and route/prioritize uploads (`16:9` long video -> derived Shorts -> regular clips) with safe per-platform posting intervals (`20 min` YouTube, `>= 30 min` TikTok/Instagram).

**Architecture:**
- Extend `studio/creators.py` and `studio/jobs.py` with `long_mode` (`off | block | compile | both`), `long_min_len` / `long_max_len` (`600..1200s`), `derive_shorts` (`bool`), and `thumb_mode` (`local | ai`).
- Add `find_long_blocks(...)` and thumbnail text/frame hook generation to `studio/brain.py`, horizontal `1920x1080` & compilation rendering to `studio/render.py`, and `studio/thumbs.py` for magnetic `1280x720` thumbnail composition (`Pillow` + `FFmpeg` + optional Gemini image generation).
- Update `studio/posting.py` and `studio/accounts.py` so `16:9` long videos route exclusively to YouTube traditional (`watch?v=`, no `#Shorts`, with `thumbnails.set`), derived Shorts route to YouTube Shorts + TikTok + Instagram Reels, `priority` (`1` = long, `2` = derived Shorts, `3` = standalone clips) governs queue order, and per-platform minimum intervals prevent TikTok/Instagram anti-spam throttling.
- All rendered long videos and Shorts remain on SSD until their scheduled posts finish, then archive to Google Drive via existing `studio/gdrive.py` (`8 MB` resumable chunks).

**Tech Stack:** Python 3.11+, FFmpeg, Pillow (`PIL`), FastAPI, Gemini API, YouTube Data API v3 (`videos` + `thumbnails.set`), Vanilla JS/HTML (`web/index.html`).

---

### Task 1: Creator & Job Options for Long Videos, Thumbnails & Safe Posting Floors

**Files:**
- Modify: `d:/PeakCut/studio/creators.py`
- Modify: `d:/PeakCut/studio/jobs.py`
- Modify: `d:/PeakCut/studio/posting.py`
- Create: `d:/PeakCut/tests/test_long_videos.py`

**Steps:**
1. **RED:** Write unit tests in `tests/test_long_videos.py` verifying:
   - `creators.DEFAULTS` & `_clean` accept `long_mode` (`"off" | "block" | "compile" | "both"`), `long_min_len` (`300..1200`, default `600`), `long_max_len` (`600..1800`, default `1200`), `derive_shorts` (`bool`, default `True`), `thumb_mode` (`"local" | "ai"`, default `"local"`).
   - `posting.enqueue` assigns `priority`: `1` for `format == "16:9"`, `2` for derived Shorts (`parent_clip` set), `3` for standalone clips, and `_next_slot` enforces per-platform safety floor (`max(gap_min, 30)` for `tiktok` / `instagram`, `max(gap_min, 20)` for `youtube`).
   - `_run()` picks the lowest `priority` (then earliest `due_at`) due post first.
2. **Verify RED:** Run `python tests/test_long_videos.py` -> FAIL.
3. **GREEN:** Implement schema fields in `creators.py`, `jobs.py` (`clean_options` / `request_render` duration limit up to `1800s` for `16:9`), and priority + platform floor scheduling in `posting.py`.
4. **Verify GREEN & Commit:** `feat(posting): add long-video options, priority queue and per-platform gap floors`.

---

### Task 2: AI Long-Arc Detection & Horizontal 16:9 / Compilation Rendering

**Files:**
- Modify: `d:/PeakCut/studio/brain.py`
- Modify: `d:/PeakCut/studio/render.py`
- Modify: `d:/PeakCut/studio/jobs.py`
- Modify: `d:/PeakCut/studio/live.py`
- Test: `d:/PeakCut/tests/test_long_videos.py`

**Steps:**
1. **RED:** Add tests for:
   - `brain.find_long_blocks(...)` using 30-minute overlapping transcript windows to pick 10–20 minute continuous narrative blocks (`600..1200s`) plus `thumb_text` (2–4 punchy words) and `peak_t` (reaction timestamp for cover frame).
   - `render.render_long_block(...)` producing `1920x1080` `16:9` MP4 without vertical crop or Shorts hook overlay, and `render.render_compilation(...)` concatenating top highlight segments into a single `10–20 min` `1920x1080` MP4 normalized to `-14 LUFS`.
   - Automatic extraction of derived `9:16` Shorts (`parent_clip = long_clip_id`) from the internal peaks of each generated long video.
2. **Verify RED:** Run `python tests/test_long_videos.py` -> FAIL.
3. **GREEN:** Implement `find_long_blocks` in `brain.py`, `16:9` & compilation renderers in `render.py`, and pipeline wiring in `jobs.py` & `live.py` (with `buffer_min >= 30` support for live continuous blocks and end-of-stream compilation).
4. **Verify GREEN & Commit:** `feat(render): support 10-20 min 16:9 blocks, compilations and derived Shorts`.

---

### Task 3: Hybrid Magnetic Thumbnails (`1280x720`) & YouTube `thumbnails.set`

**Files:**
- Create: `d:/PeakCut/studio/thumbs.py`
- Modify: `d:/PeakCut/studio/accounts.py`
- Modify: `d:/PeakCut/studio/posting.py`
- Modify: `d:/PeakCut/studio/render.py`
- Test: `d:/PeakCut/tests/test_long_videos.py`

**Steps:**
1. **RED:** Add tests for:
   - `thumbs.generate(src, out_jpg, peak_t, text, facecam=None, mode="local")` creating a valid `1280x720` JPEG (extracting reaction frame at `peak_t`, zooming facecam/center, applying contrast/vignette + bold outlined `thumb_text`, and falling back gracefully to `local` if `mode="ai"` fails).
   - `accounts.youtube_upload(..., format="16:9", thumb_path=...)`: omits `#Shorts`, returns `https://www.youtube.com/watch?v={id}`, and calls `POST https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId={id}` with the `1280x720` JPEG (non-fatal warning if channel lacks phone verification `403`, never failing the video upload itself).
   - `posting._post(p)` routing: `16:9` clips only upload to `youtube` accounts; `9:16` Shorts upload to YouTube Shorts + TikTok + Instagram Reels.
2. **Verify RED:** Run `python tests/test_long_videos.py` -> FAIL.
3. **GREEN:** Implement `studio/thumbs.py`, update `accounts.youtube_upload`, and wire `posting._post` / `on_clip_ready` routing.
4. **Verify GREEN & Commit:** `feat(thumbs): add hybrid magnetic thumbnail generator and YouTube thumbnail upload`.

---

### Task 4: Dashboard UI (`web/index.html`) & Full Regression (`tests/run.py`)

**Files:**
- Modify: `d:/PeakCut/web/index.html`
- Modify: `d:/PeakCut/tests/run.py`
- Test: `d:/PeakCut/tests/test_long_videos.py`

**Steps:**
1. **RED:** Add UI assertions in `tests/test_long_videos.py` checking `Setup` controls (`long_mode`, `long_min_len`/`long_max_len`, `derive_shorts`, `thumb_mode`), `16:9` badge + thumbnail preview in clip cards/modal, priority indicator in `Post queue`, and `node --check` validation.
2. **GREEN:** Add surgical UI controls in `web/index.html` (preserving palette, zero `box-shadow`, colour-only hover) and register `tests/test_long_videos.py` in `tests/run.py`.
3. **Verify Full Suite & Commit:** Run `python tests/run.py` -> `All tests passed.`, commit `feat(ui): add long-video, derived Shorts and magnetic thumbnail controls`.
