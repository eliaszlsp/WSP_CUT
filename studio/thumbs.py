"""Hybrid magnetic 1280x720 JPEG thumbnail generator (local PIL composition + optional Gemini AI)."""
import base64
import io
import re
import shutil
import tempfile
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

from . import ai, config, usage
from .media import run

THUMB_W, THUMB_H = 1280, 720
AI_IMAGE_MODELS = ("gemini-3.1-flash-image-preview", "gemini-3-pro-image")


def _short_text(text: str) -> str:
    """Sanitize and trim thumbnail text to 2-4 uppercase words."""
    clean = re.sub(r"[\x00-\x1f\x7f]+", " ", str(text or "")).strip()
    words = [w for w in re.split(r"\s+", clean) if w]
    if not words:
        return ""
    return " ".join(words[:4]).upper()[:36]


def _load_font(size: int):
    for name in ("Anton-Regular.ttf", "Poppins-ExtraBold.ttf"):
        p = config.FONTS_DIR / name
        if p.is_file():
            try:
                return ImageFont.truetype(str(p), size)
            except OSError:
                continue
    return ImageFont.load_default()


def _try_ai_thumb(frame_bytes: bytes, text: str) -> bytes | None:
    """Attempt Gemini image generation/editing when Gemini is active; return raw image bytes or None."""
    try:
        if not ai.ready() or ai.provider() != "gemini":
            return None
        cfg = ai.current()
        addr = (cfg.get("base_url") or ai.PROVIDERS["gemini"][1]).rstrip("/")
        key = cfg.get("key") or ""
        if not key:
            return None
        short = _short_text(text)
        prompt = (
            "Create a high-contrast, magnetic 16:9 YouTube gaming/creator thumbnail from this frame. "
            "Boost subject lighting, sharpness, and dramatic color pop."
            + (f' Overlay bold impactful text: "{short}".' if short else "")
        )
        b64 = base64.b64encode(frame_bytes).decode("ascii")
        body = {
            "contents": [{
                "role": "user",
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mimeType": "image/jpeg", "data": b64}},
                ],
            }],
            "generationConfig": {"responseModalities": ["IMAGE"]},
        }
        for model in AI_IMAGE_MODELS:
            try:
                r = httpx.post(
                    f"{addr}/{model}:generateContent",
                    json=body,
                    headers={"x-goog-api-key": key},
                    timeout=18.0,
                )
                if r.status_code != 200:
                    continue
                data = r.json()
                parts = (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
                for part in parts:
                    inline = part.get("inlineData") or part.get("inline_data")
                    if inline and inline.get("data"):
                        raw = base64.b64decode(inline["data"])
                        with Image.open(io.BytesIO(raw)) as im:
                            im.verify()
                        return raw
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return None
    return None


def _add_vignette(canvas: Image.Image) -> Image.Image:
    """Add subtle bottom and left dark gradients so text stays readable."""
    overlay = Image.new("RGBA", (THUMB_W, THUMB_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    band_top = int(THUMB_H * 0.58)
    for y in range(band_top, THUMB_H):
        t = (y - band_top) / max(1, THUMB_H - band_top)
        alpha = int(175 * (t ** 1.35))
        draw.line([(0, y), (THUMB_W, y)], fill=(0, 0, 0, alpha))
    side_w = int(THUMB_W * 0.42)
    for x in range(side_w):
        t = 1.0 - (x / max(1, side_w))
        alpha = int(105 * (t ** 1.5))
        draw.line([(x, 0), (x, THUMB_H)], fill=(0, 0, 0, alpha))
    return Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")


def _paste_facecam(canvas: Image.Image, raw: Image.Image, facecam: list | None) -> bool:
    """Crop webcam from raw frame, enlarge slightly with bold border, and anchor over its own corner so it never duplicates."""
    if not facecam or len(facecam) != 4:
        return False
    try:
        fx, fy, fw, fh = (int(round(float(v))) for v in facecam)
    except (TypeError, ValueError):
        return False
    if fw <= 8 or fh <= 8:
        return False
    x1 = max(0, min(raw.width - 1, fx))
    y1 = max(0, min(raw.height - 1, fy))
    x2 = max(x1 + 1, min(raw.width, fx + fw))
    y2 = max(y1 + 1, min(raw.height, fy + fh))
    if (x2 - x1) <= 8 or (y2 - y1) <= 8:
        return False

    cam = raw.crop((x1, y1, x2, y2))
    cam = ImageEnhance.Contrast(cam).enhance(1.12)
    cam = ImageEnhance.Color(cam).enhance(1.15)
    sx, sy = THUMB_W / max(1, raw.width), THUMB_H / max(1, raw.height)
    orig_w, orig_h = int(round((x2 - x1) * sx)), int(round((y2 - y1) * sy))
    target_w = max(orig_w + 60, 340)
    target_h = max(orig_h + 36, min(320, int(round(cam.height * (target_w / max(1, cam.width))))))
    cam = ImageOps.fit(cam, (target_w, target_h), method=Image.Resampling.LANCZOS)

    border = 7
    outer = 4
    framed = Image.new("RGB", (target_w + (border + outer) * 2, target_h + (border + outer) * 2), (0, 0, 0))
    inner = Image.new("RGB", (target_w + border * 2, target_h + border * 2), (255, 230, 0))
    inner.paste(cam, (border, border))
    framed.paste(inner, (outer, outer))
    # Anchor over the same corner the webcam sits in, completely covering the smaller original
    cx, cy = (x1 + x2) / 2.0 / max(1, raw.width), (y1 + y2) / 2.0 / max(1, raw.height)
    px = (THUMB_W - framed.width - 18) if cx >= 0.5 else 18
    py = 18 if cy < 0.5 else (THUMB_H - framed.height - 64)
    canvas.paste(framed, (px, py))
    return cy >= 0.5 and cx >= 0.5


def _paste_collage_split(canvas: Image.Image, primary: Image.Image, secondary: Image.Image | None) -> None:
    """Compose a dynamic 2-panel YouTube collage on the right third with an angled high-contrast divider slash."""
    if secondary is not None:
        sec = secondary.copy()
    else:
        # Zoom in on the action center of the primary frame for a dramatic detail split panel
        w, h = primary.size
        zw, zh = max(16, int(w * 0.58)), max(16, int(h * 0.58))
        x0, y0 = max(0, (w - zw) // 2), max(0, (h - zh) // 3)
        sec = primary.crop((x0, y0, min(w, x0 + zw), min(h, y0 + zh)))
    sec = ImageEnhance.Contrast(sec).enhance(1.26)
    sec = ImageEnhance.Color(sec).enhance(1.30)
    sec = ImageEnhance.Sharpness(sec).enhance(1.22)
    panel_w = 505
    panel = ImageOps.fit(sec, (panel_w, THUMB_H), method=Image.Resampling.LANCZOS)

    right_layer = Image.new("RGB", (THUMB_W, THUMB_H), (0, 0, 0))
    right_layer.paste(panel, (THUMB_W - panel_w, 0))

    top_x, bot_x = 845, 780
    mask = Image.new("L", (THUMB_W, THUMB_H), 0)
    ImageDraw.Draw(mask).polygon([(top_x, 0), (THUMB_W, 0), (THUMB_W, THUMB_H), (bot_x, THUMB_H)], fill=255)
    canvas.paste(right_layer, (0, 0), mask)

    draw = ImageDraw.Draw(canvas)
    draw.line([(top_x, 0), (bot_x, THUMB_H)], fill=(0, 0, 0), width=16)
    draw.line([(top_x, 0), (bot_x, THUMB_H)], fill=(255, 230, 0), width=6)


def _pick_best_frames_ai(frames: list[Path]) -> tuple[Path, Path | None]:
    """Use the vision model (e.g. nvidia/nemotron-3.5-super-vl:free) to pick the most expressive hero frame."""
    valid = [f for f in frames if f.is_file() and f.stat().st_size > 200]
    if not valid:
        return frames[0], None
    if len(valid) == 1:
        return valid[0], None
    if not ai.ready():
        return valid[0], valid[-1]
    try:
        tile_w, tile_h = 320, 180
        strip = Image.new("RGB", (tile_w * len(valid), tile_h), (0, 0, 0))
        sdraw = ImageDraw.Draw(strip)
        for i, fp in enumerate(valid):
            with Image.open(fp) as im:
                tile = ImageOps.fit(im.convert("RGB"), (tile_w, tile_h), method=Image.Resampling.BILINEAR)
            strip.paste(tile, (i * tile_w, 0))
            sdraw.rectangle([i * tile_w + 6, 6, i * tile_w + 36, 36], fill=(0, 0, 0))
            sdraw.text((i * tile_w + 15, 10), str(i + 1), fill=(255, 230, 0))
        buf = io.BytesIO()
        strip.save(buf, format="JPEG", quality=82)
        prompt = (
            f"This contact sheet has {len(valid)} numbered video frames (1 to {len(valid)}). "
            "Which frame has the most dramatic action, clearest facial reaction, and least motion blur for a YouTube thumbnail? "
            "Reply with ONLY the single digit (1, 2, or 3)."
        )
        ans, sent, wrote = ai.see(prompt, buf.getvalue(), timeout=10.0)
        if sent or wrote:
            usage.record(sent, wrote, "Thumbnail: best frame")
        m = re.search(r"([1-9])", str(ans or ""))
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(valid):
                hero = valid[idx]
                others = [f for j, f in enumerate(valid) if j != idx]
                return hero, (others[-1] if others else None)
    except Exception:  # noqa: BLE001
        pass
    return valid[0], valid[-1]


def _draw_headline(canvas: Image.Image, text: str, has_facecam: bool) -> None:
    short = _short_text(text)
    if not short:
        return
    draw = ImageDraw.Draw(canvas)
    max_w = (THUMB_W - 440) if has_facecam else (THUMB_W - 120)
    words = short.split()
    if len(words) >= 3 and len(short) > 14:
        mid = len(words) // 2
        lines = [" ".join(words[:mid]), " ".join(words[mid:])]
    else:
        lines = [short]

    font_size = 112
    font = _load_font(font_size)
    while font_size > 48:
        font = _load_font(font_size)
        widest = max(draw.textbbox((0, 0), ln, font=font, stroke_width=8)[2] for ln in lines)
        if widest <= max_w:
            break
        font_size -= 6

    line_heights = []
    for ln in lines:
        bb = draw.textbbox((0, 0), ln, font=font, stroke_width=8)
        line_heights.append(bb[3] - bb[1])
    total_h = sum(line_heights) + max(0, len(lines) - 1) * 14
    x = 54
    y = THUMB_H - total_h - 68
    colors = ("#FFE600", "#FFFFFF")
    for idx, ln in enumerate(lines):
        bb = draw.textbbox((x, y), ln, font=font, stroke_width=8)
        pad_x, pad_y = 16, 8
        draw.rounded_rectangle(
            [bb[0] - pad_x, bb[1] - pad_y, bb[2] + pad_x, bb[3] + pad_y],
            radius=12,
            fill=(0, 0, 0),
            outline=(255, 230, 0) if idx == 0 else (255, 42, 42),
            width=3,
        )
        draw.rectangle(
            [bb[0] - pad_x, bb[1] - pad_y, bb[0] - pad_x + 10, bb[3] + pad_y],
            fill=(255, 32, 32) if idx == 0 else (255, 230, 0),
        )
        draw.text((x + 5, y + 5), ln, font=font, fill="#000000", stroke_width=8, stroke_fill="#000000")
        draw.text(
            (x, y),
            ln,
            font=font,
            fill=colors[idx % len(colors)],
            stroke_width=8,
            stroke_fill="#000000",
        )
        y += line_heights[idx] + 18


def _compose_local(
    frame_path: Path,
    out_jpg: Path,
    text: str = "",
    facecam: list | None = None,
    secondary_path: Path | None = None,
    collage: bool = True,
) -> Path:
    with Image.open(frame_path) as raw_in:
        raw = raw_in.convert("RGB")
    sec_raw = None
    if secondary_path and Path(secondary_path).is_file() and Path(secondary_path) != Path(frame_path):
        try:
            with Image.open(secondary_path) as sec_in:
                sec_raw = sec_in.convert("RGB")
        except Exception:  # noqa: BLE001
            sec_raw = None
    canvas = ImageOps.fit(raw, (THUMB_W, THUMB_H), method=Image.Resampling.LANCZOS)
    canvas = ImageEnhance.Contrast(canvas).enhance(1.22)
    canvas = ImageEnhance.Color(canvas).enhance(1.26)
    canvas = ImageEnhance.Sharpness(canvas).enhance(1.18)
    if collage:
        _paste_collage_split(canvas, raw, sec_raw)
    canvas = _add_vignette(canvas)
    has_cam = _paste_facecam(canvas, raw, facecam)
    _draw_headline(canvas, text, has_cam)
    canvas.save(out_jpg, format="JPEG", quality=92)
    return out_jpg


def generate(
    src: Path,
    out_jpg: Path,
    peak_t: float = 1.2,
    text: str = "",
    facecam: list | None = None,
    mode: str = "local",
    collage: bool = True,
) -> Path:
    """Extract frames around peak_t and build a 1280x720 collage JPEG thumbnail (AI with local fallback)."""
    out_jpg = Path(out_jpg)
    out_jpg.parent.mkdir(parents=True, exist_ok=True)
    tbase = config.TMP_DIR if len(str(config.TMP_DIR)) < 120 else None
    if tbase:
        tbase.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="pkth", dir=str(tbase) if tbase else None))
    try:
        frame_path = work / "frame.jpg"
        t = max(0.0, float(peak_t or 1.2))
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1", "-q:v", "2", str(frame_path)])
        if not frame_path.is_file():
            run(["ffmpeg", "-y", "-v", "error", "-ss", "0.00", "-i", str(src), "-frames:v", "1", "-q:v", "2", str(frame_path)])
        candidates = [frame_path] if frame_path.is_file() else []
        alt_times = [max(0.0, t - 2.5) if t >= 2.6 else max(0.0, t * 0.35), t + 2.5]
        for idx, at in enumerate(alt_times, 1):
            if abs(at - t) < 0.25:
                continue
            ap = work / f"alt{idx}.jpg"
            try:
                run(["ffmpeg", "-y", "-v", "error", "-ss", f"{at:.2f}", "-i", str(src), "-frames:v", "1", "-q:v", "2", str(ap)])
                if ap.is_file() and ap.stat().st_size > 200:
                    candidates.append(ap)
            except Exception:  # noqa: BLE001
                continue
        hero_path = frame_path
        sec_path = candidates[1] if len(candidates) > 1 else None
        if mode == "ai" and frame_path.is_file():
            if len(candidates) > 1:
                hero_path, sec_path = _pick_best_frames_ai(candidates)
            ai_bytes = _try_ai_thumb(hero_path.read_bytes(), text)
            if ai_bytes:
                try:
                    with Image.open(io.BytesIO(ai_bytes)) as im:
                        fitted = ImageOps.fit(im.convert("RGB"), (THUMB_W, THUMB_H), method=Image.Resampling.LANCZOS)
                        fitted.save(out_jpg, format="JPEG", quality=90)
                    return out_jpg
                except Exception:  # noqa: BLE001
                    pass
        return _compose_local(hero_path, out_jpg, text=text, facecam=facecam, secondary_path=sec_path, collage=collage)
    finally:
        shutil.rmtree(work, ignore_errors=True)
