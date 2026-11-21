"""Profile & global default cover manager (manual upload, AI generation, and per-platform variants)."""
import base64
import io
import json
import re
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from . import ai, config, ctx, creators, usage

COVER_W, COVER_H = 1080, 1920
WIDE_W, WIDE_H = 1280, 720
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
AI_IMAGE_MODELS = ("gemini-3.1-flash-image-preview", "gemini-3-pro-image", "gemini-2.5-flash-image")


def _creator_cover_path(cid: str, wide: bool = False) -> Path:
    name = "default-cover-wide.jpg" if wide else "default-cover.jpg"
    return creators.root(cid) / name


def _global_cover_path(wide: bool = False) -> Path:
    name = "default-cover-wide.jpg" if wide else "default-cover.jpg"
    return config.user_dir(ctx.uid()) / name


def _load_font(size: int, kind: str = "anton"):
    order = ("Anton-Regular.ttf", "ArchivoBlack-Regular.ttf", "Poppins-ExtraBold.ttf") if kind == "anton" else (
        "Poppins-ExtraBold.ttf", "Anton-Regular.ttf"
    )
    for name in order:
        p = config.FONTS_DIR / name
        if p.is_file():
            try:
                return ImageFont.truetype(str(p), size)
            except OSError:
                continue
    return ImageFont.load_default()


def _save_cover_pair(im: Image.Image, dest: Path, dest_wide: Path):
    """Save 1080x1920 (9:16) and companion 1280x720 (16:9) JPEG covers atomically."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    rgb = im.convert("RGB")
    vert = ImageOps.fit(rgb, (COVER_W, COVER_H), method=Image.Resampling.LANCZOS)
    tmp = dest.with_suffix(".tmp.jpg")
    vert.save(tmp, format="JPEG", quality=93, optimize=True)
    tmp.replace(dest)

    # Compose 16:9 companion cover with blurred side backdrop when source isn't already widescreen
    if rgb.width / max(1, rgb.height) >= 1.35:
        wide = ImageOps.fit(rgb, (WIDE_W, WIDE_H), method=Image.Resampling.LANCZOS)
    else:
        bg = ImageOps.fit(rgb, (WIDE_W, WIDE_H), method=Image.Resampling.BILINEAR)
        bg = bg.filter(ImageFilter.GaussianBlur(radius=22))
        bg = ImageEnhance.Brightness(bg).enhance(0.45)
        fg = ImageOps.contain(rgb, (WIDE_W, WIDE_H), method=Image.Resampling.LANCZOS)
        wide = bg.copy()
        wide.paste(fg, ((WIDE_W - fg.width) // 2, (WIDE_H - fg.height) // 2))
    tmp_w = dest_wide.with_suffix(".tmp.jpg")
    wide.save(tmp_w, format="JPEG", quality=92, optimize=True)
    tmp_w.replace(dest_wide)


def save_uploaded_cover(raw_bytes: bytes, cid: str | None = None) -> dict:
    """Validate and store an uploaded image as the creator or global default cover."""
    if not raw_bytes or len(raw_bytes) > MAX_UPLOAD_BYTES:
        raise ValueError("Envie uma imagem válida de até 15 MB (JPG, PNG ou WebP).")
    try:
        with Image.open(io.BytesIO(raw_bytes)) as im:
            im.load()
            im = ImageOps.exif_transpose(im)
            if cid:
                c = creators.get(cid)
                if not c:
                    raise ValueError("Criador não encontrado.")
                d_v, d_w = _creator_cover_path(cid, False), _creator_cover_path(cid, True)
            else:
                d_v, d_w = _global_cover_path(False), _global_cover_path(True)
            _save_cover_pair(im, d_v, d_w)
    except ValueError:
        raise
    except Exception as e:
        raise ValueError("Arquivo de imagem inválido ou corrompido. Use JPG, PNG ou WebP.") from e
    if cid:
        creators.update(cid, {"cover_mode": "custom"})
    else:
        from . import accounts
        accounts.save_settings({"cover_enabled": True})
    ctx.bump()
    return status(cid)


def delete_cover(cid: str | None = None) -> dict:
    if cid:
        if not creators.get(cid):
            raise ValueError("Criador não encontrado.")
        for p in (_creator_cover_path(cid, False), _creator_cover_path(cid, True)):
            p.unlink(missing_ok=True)
        creators.update(cid, {"cover_mode": "clip"})
    else:
        for p in (_global_cover_path(False), _global_cover_path(True)):
            p.unlink(missing_ok=True)
        from . import accounts
        accounts.save_settings({"cover_enabled": False})
    ctx.bump()
    return status(cid)


def cover_file(cid: str | None = None, wide: bool = False) -> Path | None:
    """Resolve the active default cover JPEG path for a creator (or global when cid is None), or None."""
    if cid:
        c = creators.get(cid)
        if not c:
            return None
        mode = str(c.get("cover_mode") or "clip")
        if mode == "clip":
            return None
        if mode == "custom":
            p = _creator_cover_path(cid, wide=wide)
            if not p.is_file() and wide:
                p = _creator_cover_path(cid, wide=False)
            return p if p.is_file() else None
        if mode == "global":
            p = _global_cover_path(wide=wide)
            if not p.is_file() and wide:
                p = _global_cover_path(wide=False)
            return p if p.is_file() else None
        return None
    from . import accounts
    st = accounts.settings()
    if not st.get("cover_enabled"):
        return None
    p = _global_cover_path(wide=wide)
    if not p.is_file() and wide:
        p = _global_cover_path(wide=False)
    return p if p.is_file() else None


def should_inject_first_frame(cid: str | None = None) -> Path | None:
    """Return 9:16 default cover path if it should also be injected as frame 0 of rendered MP4s."""
    if cid:
        c = creators.get(cid)
        if not c or not c.get("cover_first_frame", True):
            return None
        return cover_file(cid, wide=False)
    from . import accounts
    st = accounts.settings()
    if not st.get("cover_first_frame", True):
        return None
    return cover_file(None, wide=False)


def status(cid: str | None = None) -> dict:
    if cid:
        p = _creator_cover_path(cid, False)
        c = creators.get(cid) or {}
        has_own = p.is_file()
        ts = int(p.stat().st_mtime) if has_own else 0
        g_has = _global_cover_path(False).is_file()
        return {
            "has_cover": has_own,
            "url": f"/api/creators/{cid}/cover?v={ts}" if has_own else "",
            "cover_mode": c.get("cover_mode") or "clip",
            "cover_first_frame": bool(c.get("cover_first_frame", True)),
            "has_global": g_has,
        }
    p = _global_cover_path(False)
    has = p.is_file()
    ts = int(p.stat().st_mtime) if has else 0
    from . import accounts
    st = accounts.settings()
    return {
        "has_cover": has,
        "url": f"/api/cover?v={ts}" if has else "",
        "cover_enabled": bool(st.get("cover_enabled")),
        "cover_first_frame": bool(st.get("cover_first_frame", True)),
    }


def _try_gemini_image(prompt: str, ref_bytes: bytes | None = None) -> bytes | None:
    """Attempt Gemini image generation using saved Gemini credentials even if another provider is primary."""
    try:
        cfg = ai._cfg_for("gemini")
        key = str(cfg.get("key") or "").strip()
        if not key:
            return None
        addr = (cfg.get("address") or ai.PROVIDERS["gemini"][1]).rstrip("/")
        parts = [{"text": prompt}]
        if ref_bytes:
            parts.append({"inline_data": {"mimeType": "image/jpeg", "data": base64.b64encode(ref_bytes).decode("ascii")}})
        body = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseModalities": ["IMAGE"]},
        }
        for model in AI_IMAGE_MODELS:
            try:
                r = httpx.post(
                    f"{addr}/{model}:generateContent",
                    json=body,
                    headers={"x-goog-api-key": key},
                    timeout=22.0,
                )
                if r.status_code != 200:
                    continue
                data = r.json()
                cparts = (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
                for part in cparts:
                    inline = part.get("inlineData") or part.get("inline_data")
                    if inline and inline.get("data"):
                        raw = base64.b64decode(inline["data"])
                        with Image.open(io.BytesIO(raw)) as im:
                            im.verify()
                        usage.record(800, 1200, "Capa padrão: IA visual")
                        return raw
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return None
    return None


def _ask_cover_direction(theme: str, creator_name: str, game: str) -> dict:
    """Ask the configured AI text model for high-CTR cover copy & palette."""
    fallback = {
        "badge": (game or "CLIPS").upper()[:18],
        "headline": (theme or creator_name or "VICE CUT AI").upper()[:26],
        "subline": "MELHORES MOMENTOS & CORTES",
        "accent": "#FF2E93",
        "accent2": "#36E2D0",
    }
    if not ai.ready():
        return fallback
    prompt = (
        "You are an expert viral Shorts/TikTok/Reels profile cover designer.\n"
        f"Creator name: {creator_name or 'Geral'}\n"
        f"Game / niche: {game or 'Gaming & Livestreams'}\n"
        f"User theme idea: {theme or 'High-retention viral gaming clips cover'}\n\n"
        "Return a compact JSON object with:\n"
        '- "badge": 1-2 short UPPERCASE words for a top pill (max 18 chars, Portuguese)\n'
        '- "headline": 2-4 punchy UPPERCASE words for center hero text (max 24 chars, Portuguese)\n'
        '- "subline": short tagline (max 34 chars, Portuguese)\n'
        '- "accent": vibrant hex color (#FF2E93, #FFE600, #36E2D0, #FF5522, #7B4DFF)\n'
        '- "accent2": contrasting neon hex color\n'
    )
    try:
        raw, sent, wrote = ai.ask(
            prompt,
            {
                "type": "OBJECT",
                "properties": {
                    "badge": {"type": "STRING"},
                    "headline": {"type": "STRING"},
                    "subline": {"type": "STRING"},
                    "accent": {"type": "STRING"},
                    "accent2": {"type": "STRING"},
                },
                "required": ["badge", "headline", "subline", "accent", "accent2"],
            },
            timeout=15.0,
            purpose="Capa padrão: direção de arte",
        )
        usage.record(sent, wrote, "Capa padrão: direção de arte")
        if isinstance(raw, dict):
            for k in ("badge", "headline", "subline"):
                v = re.sub(r"[\x00-\x1f\x7f]+", " ", str(raw.get(k) or "")).strip()
                if v:
                    fallback[k] = v[: (18 if k == "badge" else 28 if k == "headline" else 38)]
            for k in ("accent", "accent2"):
                hx = str(raw.get(k) or "").strip()
                if re.fullmatch(r"#[0-9a-fA-F]{6}", hx):
                    fallback[k] = hx.upper()
    except Exception:  # noqa: BLE001
        pass
    return fallback


def _hex_rgb(hx: str, default=(255, 46, 147)) -> tuple[int, int, int]:
    hx = str(hx or "").lstrip("#")
    if len(hx) == 6:
        try:
            return int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16)
        except ValueError:
            pass
    return default


def _compose_ai_poster(spec: dict, avatar_path: Path | None, creator_name: str) -> Image.Image:
    """Compose a bold 1080x1920 high-retention profile cover poster with lighting, badge and creator focus."""
    c1 = _hex_rgb(spec.get("accent"), (255, 46, 147))
    c2 = _hex_rgb(spec.get("accent2"), (54, 226, 208))

    canvas = Image.new("RGB", (COVER_W, COVER_H), (10, 7, 22))
    px = canvas.load()
    # Smooth diagonal ambient gradient
    for y in range(0, COVER_H, 4):
        ty = y / COVER_H
        r = int(12 + c1[0] * 0.24 * (1 - ty) + c2[0] * 0.14 * ty)
        g = int(8 + c1[1] * 0.16 * (1 - ty) + c2[1] * 0.18 * ty)
        b = int(26 + c1[2] * 0.28 * (1 - ty) + c2[2] * 0.24 * ty)
        row_col = (min(255, r), min(255, g), min(255, b))
        for dy in range(min(4, COVER_H - y)):
            for x in range(0, COVER_W, 8):
                for dx in range(min(8, COVER_W - x)):
                    px[x + dx, y + dy] = row_col

    # Optional creator avatar ambient backdrop + center medallion
    av_img = None
    if avatar_path and avatar_path.is_file():
        try:
            with Image.open(avatar_path) as raw_av:
                av_img = raw_av.convert("RGB")
        except Exception:  # noqa: BLE001
            av_img = None

    if av_img is not None:
        bg_av = ImageOps.fit(av_img, (COVER_W, COVER_H), method=Image.Resampling.BILINEAR)
        bg_av = bg_av.filter(ImageFilter.GaussianBlur(radius=38))
        bg_av = ImageEnhance.Brightness(bg_av).enhance(0.32)
        canvas = Image.blend(canvas, bg_av, 0.55)

    overlay = Image.new("RGBA", (COVER_W, COVER_H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    # Atmospheric glow pools
    od.ellipse([-140, 180, COVER_W + 140, 1280], fill=(*c1, 46))
    od.ellipse([80, 920, COVER_W + 200, COVER_H - 120], fill=(*c2, 34))
    overlay = overlay.filter(ImageFilter.GaussianBlur(radius=65))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")

    draw = ImageDraw.Draw(canvas)
    # Diagonal kinetic slash stripes across background
    for offset in (-260, 0, 260):
        draw.line([(0, 620 + offset), (COVER_W, 240 + offset)], fill=(c1[0] // 3, c1[1] // 3, c1[2] // 3), width=8)

    # Outer sleek neon frame
    draw.rounded_rectangle([32, 32, COVER_W - 32, COVER_H - 32], radius=44, outline=c1, width=6)
    draw.rounded_rectangle([46, 46, COVER_W - 46, COVER_H - 46], radius=34, outline=(255, 255, 255), width=2)

    # Creator circular avatar centerpiece if present, else dynamic geometric VI monogram emblem
    cx, cy, rad = COVER_W // 2, 620, 220
    draw.ellipse([cx - rad - 18, cy - rad - 18, cx + rad + 18, cy + rad + 18], outline=c2, width=10)
    draw.ellipse([cx - rad - 6, cy - rad - 6, cx + rad + 6, cy + rad + 6], outline=c1, width=8)
    if av_img is not None:
        av_fit = ImageOps.fit(av_img, (rad * 2, rad * 2), method=Image.Resampling.LANCZOS)
        mask = Image.new("L", (rad * 2, rad * 2), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, rad * 2, rad * 2], fill=255)
        canvas.paste(av_fit, (cx - rad, cy - rad), mask)
    else:
        draw.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=(18, 13, 38))
        emblem_font = _load_font(210, "anton")
        eb = draw.textbbox((0, 0), "VI", font=emblem_font)
        ew, eh = eb[2] - eb[0], eb[3] - eb[1]
        draw.text((cx - ew // 2, cy - eh // 2 - 22), "VI", font=emblem_font, fill=(255, 255, 255), stroke_width=6, stroke_fill=c1)

    # Top pill badge
    badge = spec.get("badge", "CLIPS").upper()
    bfont = _load_font(44, "poppins")
    bb = draw.textbbox((0, 0), badge, font=bfont)
    bw, bh = bb[2] - bb[0], bb[3] - bb[1]
    bx0, by0 = (COVER_W - bw) // 2 - 42, 200
    draw.rounded_rectangle([bx0, by0, bx0 + bw + 84, by0 + bh + 34], radius=999, fill=c1, outline=(255, 255, 255), width=3)
    draw.text((COVER_W // 2 - bw // 2, by0 + 14), badge, font=bfont, fill=(255, 255, 255))

    # Headline (1-2 centered impactful lines)
    headline = spec.get("headline", "VICE CUT AI").upper()
    words = headline.split()
    lines = [" ".join(words[: len(words) // 2]), " ".join(words[len(words) // 2 :])] if (len(words) >= 3 and len(headline) > 13) else [headline]
    hsize = 132
    hfont = _load_font(hsize, "anton")
    while hsize > 56:
        hfont = _load_font(hsize, "anton")
        if max(draw.textbbox((0, 0), ln, font=hfont, stroke_width=10)[2] for ln in lines) <= COVER_W - 140:
            break
        hsize -= 8

    y = 955
    for i, ln in enumerate(lines):
        lb = draw.textbbox((0, 0), ln, font=hfont, stroke_width=10)
        lw, lh = lb[2] - lb[0], lb[3] - lb[1]
        tx = (COVER_W - lw) // 2
        draw.text((tx + 6, y + 8), ln, font=hfont, fill=(0, 0, 0), stroke_width=10, stroke_fill=(0, 0, 0))
        draw.text((tx, y), ln, font=hfont, fill=(255, 255, 255) if i == 0 else c2, stroke_width=10, stroke_fill=(0, 0, 0))
        y += lh + 32

    # Subline ribbon + footer handle
    sub = spec.get("subline", "MELHORES MOMENTOS").upper()
    sfont = _load_font(38, "poppins")
    sb = draw.textbbox((0, 0), sub, font=sfont)
    sw, sh = sb[2] - sb[0], sb[3] - sb[1]
    sy = y + 36
    draw.rounded_rectangle([(COVER_W - sw) // 2 - 36, sy - 14, (COVER_W + sw) // 2 + 36, sy + sh + 22],
                           radius=18, fill=(14, 10, 28), outline=c2, width=3)
    draw.text(((COVER_W - sw) // 2, sy), sub, font=sfont, fill=(255, 255, 255))

    foot = (creator_name or "VICE CUT AI").upper()
    ffont = _load_font(32, "poppins")
    fb = draw.textbbox((0, 0), foot, font=ffont)
    draw.text(((COVER_W - (fb[2] - fb[0])) // 2, COVER_H - 165), foot, font=ffont, fill=(225, 220, 245))
    return canvas


def generate_ai_cover(prompt: str = "", cid: str | None = None) -> dict:
    """Generate a cohesive 9:16 profile cover via Gemini Image Gen or AI-directed Pillow poster fallback."""
    c = creators.get(cid) if cid else None
    if cid and not c:
        raise ValueError("Criador não encontrado.")
    cname = (c or {}).get("name") or ""
    game = (c or {}).get("game") or ""
    theme = str(prompt or "").strip()[:260] or (
        f"Capa icônica vertical de alto contraste para o canal {cname} ({game or 'gaming e lives'})"
        if cname else "Capa icônica vertical neon cyberpunk para canal de clipes e lives"
    )
    av_path = None
    if c:
        key = creators.platform_key(c.get("channel") or "")
        cand = creators.root(cid) / f"avatar-{key}.img"
        if cand.is_file():
            av_path = cand

    img_prompt = (
        "Create an ultra-crisp vertical 9:16 social media profile cover template (Reels / TikTok / YouTube Shorts) "
        f"with zero watermarks. Theme: {theme}. "
        + (f"Streamer / channel focus: {cname}. " if cname else "")
        + (f"Game / setting: {game}. " if game else "")
        + "High-contrast dark background with vibrant neon magenta and cyan rim lighting, bold centered composition, dramatic depth."
    )
    ai_bytes = _try_gemini_image(img_prompt, ref_bytes=av_path.read_bytes() if av_path else None)
    used_source = "gemini-image"
    if ai_bytes:
        with Image.open(io.BytesIO(ai_bytes)) as im:
            out_img = im.convert("RGB")
    else:
        used_source = "ai-composer"
        spec = _ask_cover_direction(theme, cname, game)
        out_img = _compose_ai_poster(spec, av_path, cname)

    if cid:
        d_v, d_w = _creator_cover_path(cid, False), _creator_cover_path(cid, True)
        _save_cover_pair(out_img, d_v, d_w)
        creators.update(cid, {"cover_mode": "custom"})
    else:
        d_v, d_w = _global_cover_path(False), _global_cover_path(True)
        _save_cover_pair(out_img, d_v, d_w)
        from . import accounts
        accounts.save_settings({"cover_enabled": True})
    ctx.bump()
    return {**status(cid), "source": used_source}
