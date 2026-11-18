"""Hybrid magnetic 1280x720 JPEG thumbnail generator (local PIL composition + optional Gemini AI)."""
import base64
import io
import re
import shutil
import tempfile
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

from . import ai, config
from .media import run

THUMB_W, THUMB_H = 1280, 720
AI_IMAGE_MODELS = ("gemini-2.5-flash-image", "gemini-2.0-flash-exp-image-generation")


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
    """Crop webcam from raw frame, enlarge with bold border, and paste in bottom-right."""
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
    target_w = 340
    target_h = max(120, min(320, int(round(cam.height * (target_w / max(1, cam.width))))))
    cam = ImageOps.fit(cam, (target_w, target_h), method=Image.Resampling.LANCZOS)

    border = 8
    framed = Image.new("RGB", (target_w + border * 2, target_h + border * 2), (255, 255, 255))
    framed.paste(cam, (border, border))
    px = THUMB_W - framed.width - 36
    py = THUMB_H - framed.height - 36
    canvas.paste(framed, (px, py))
    return True


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
    x = 56
    y = THUMB_H - total_h - 52
    colors = ("#FFE600", "#FFFFFF")
    for idx, ln in enumerate(lines):
        draw.text(
            (x, y),
            ln,
            font=font,
            fill=colors[idx % len(colors)],
            stroke_width=8,
            stroke_fill="#000000",
        )
        y += line_heights[idx] + 14


def _compose_local(frame_path: Path, out_jpg: Path, text: str = "", facecam: list | None = None) -> Path:
    with Image.open(frame_path) as raw_in:
        raw = raw_in.convert("RGB")
    canvas = ImageOps.fit(raw, (THUMB_W, THUMB_H), method=Image.Resampling.LANCZOS)
    canvas = ImageEnhance.Contrast(canvas).enhance(1.18)
    canvas = ImageEnhance.Color(canvas).enhance(1.22)
    canvas = _add_vignette(canvas)
    has_cam = _paste_facecam(canvas, raw, facecam)
    _draw_headline(canvas, text, has_cam)
    canvas.save(out_jpg, format="JPEG", quality=90)
    return out_jpg


def generate(
    src: Path,
    out_jpg: Path,
    peak_t: float = 1.2,
    text: str = "",
    facecam: list | None = None,
    mode: str = "local",
) -> Path:
    """Extract frame at peak_t and build a 1280x720 JPEG thumbnail (AI with local fallback)."""
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
        if mode == "ai" and frame_path.is_file():
            ai_bytes = _try_ai_thumb(frame_path.read_bytes(), text)
            if ai_bytes:
                try:
                    with Image.open(io.BytesIO(ai_bytes)) as im:
                        fitted = ImageOps.fit(im.convert("RGB"), (THUMB_W, THUMB_H), method=Image.Resampling.LANCZOS)
                        fitted.save(out_jpg, format="JPEG", quality=90)
                    return out_jpg
                except Exception:  # noqa: BLE001
                    pass
        return _compose_local(frame_path, out_jpg, text=text, facecam=facecam)
    finally:
        shutil.rmtree(work, ignore_errors=True)
