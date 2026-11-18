"""Talking to the AI service. You bring your own key, and choose the service and model in the dashboard
(Settings, AI) or in .env. What is saved from the dashboard is kept in ai.json in the data folder and wins.

    PEAKCUT_AI_PROVIDER   gemini | openai | xai | groq | openrouter | anthropic | ollama | custom
    PEAKCUT_AI_MODELS     one or more model ids, comma separated, tried in this order
    PEAKCUT_AI_KEY        your key (or PEAKCUT_AI_KEY_FILE, a file holding it)
    PEAKCUT_AI_BASE_URL   only for "custom" (any service that speaks the OpenAI chat format), or to override

Every service is asked for the same thing: JSON that follows a small schema. Gemini takes the schema directly.
The others are told the schema in the prompt and asked for a JSON object, which works with any chat model.

One picture can also be shown (`see`), which is how the webcam is looked for on a gaming stream. A model that
cannot look at pictures answers with something else, and that is simply taken as "it did not look"."""
import base64
import hashlib
import json
import os
import re
import threading
import time

import httpx

from . import config

# name -> (label shown on the pages, address, how it is spoken to, where to get a key, an example model id)
PROVIDERS = {
    "gemini": ("Gemini", "https://generativelanguage.googleapis.com/v1beta/models", "gemini", "https://aistudio.google.com/apikey", "gemini-2.5-flash"),
    "openai": ("OpenAI", "https://api.openai.com/v1", "openai", "https://platform.openai.com/api-keys", "gpt-4o-mini"),
    "xai": ("Grok", "https://api.x.ai/v1", "openai", "https://console.x.ai", "grok-4"),
    "groq": ("Groq", "https://api.groq.com/openai/v1", "openai", "https://console.groq.com/keys", "llama-3.3-70b-versatile"),
    "openrouter": ("OpenRouter", "https://openrouter.ai/api/v1", "openai", "https://openrouter.ai/keys", "google/gemini-2.5-flash"),
    "anthropic": ("Claude", "https://api.anthropic.com/v1", "anthropic", "https://console.anthropic.com", ""),
    "ollama": ("Ollama (on this machine)", "http://127.0.0.1:11434/v1", "openai", "https://ollama.com", "llama3.1"),
    "custom": ("Custom (OpenAI-compatible)", "", "openai", "", ""),
}
ALIASES = {"google": "gemini", "grok": "xai", "claude": "anthropic", "local": "ollama"}
NO_KEY = {"ollama", "custom"}       # a model running on your own machine needs no key

# ---------------------------------------------------------------- the settings in use
_lock = threading.Lock()
_saved = {"at": None, "data": {}}
_health: dict = {}      # (provider, model, cred_fingerprint) -> {"state", "ok", "ms", "at", "error"}


def _file():
    return config.DATA_DIR / "ai.json"


def _norm(p: str) -> str:
    s = str(p or "").strip().lower()
    return ALIASES.get(s, s)


def _from_dashboard() -> dict:
    """What was saved from the dashboard, read again only when the file changes."""
    f = _file()
    try:
        at = f.stat().st_mtime_ns
    except OSError:
        at = 0
    if _saved["at"] != at:
        try:
            data = json.loads(f.read_text(encoding="utf-8")) if at else {}
        except (OSError, ValueError):
            data = {}
        _saved.update(at=at, data=data if isinstance(data, dict) else {})
    return _saved["data"]


def _profiles(d: dict | None = None) -> dict:
    """Saved per-service settings (models, base_url, key), migrated seamlessly from .env and single-service ai.json files."""
    d = _from_dashboard() if d is None else d
    out = {}
    for env_p, env_ms, env_b, env_k in (
        (_norm(config.AI_FALLBACK_PROVIDER), list(config.AI_FALLBACK_MODELS), config.AI_FALLBACK_BASE_URL, config.ai_fallback_key()),
        (_norm(config.AI_PROVIDER), list(config.AI_MODELS), config.AI_BASE_URL, config.ai_key()),
    ):
        if env_p in PROVIDERS and (env_ms or env_b or env_k):
            out[env_p] = {"models": env_ms, "base_url": env_b, "key": env_k}
    raw = d.get("profiles")
    if isinstance(raw, dict):
        for pid, v in raw.items():
            np = _norm(pid)
            if np in PROVIDERS and isinstance(v, dict):
                out[np] = {"models": [str(m).strip() for m in (v.get("models") or []) if str(m).strip()],
                           "base_url": str(v.get("base_url") or "").rstrip("/"),
                           "key": str(v.get("key") or "")}
    fb = d.get("fallback")
    if isinstance(fb, dict) and _norm(fb.get("provider")) in PROVIDERS:
        fp = _norm(fb.get("provider"))
        out[fp] = {"models": [str(m).strip() for m in (fb.get("models") or []) if str(m).strip()],
                   "base_url": str(fb.get("base_url") or "").rstrip("/"),
                   "key": str(fb.get("key") or "")}
    lp = _norm(d.get("provider"))
    if lp in PROVIDERS:
        out[lp] = {"models": [str(m).strip() for m in (d.get("models") or []) if str(m).strip()],
                   "base_url": str(d.get("base_url") or "").rstrip("/"),
                   "key": str(d.get("key") or "")}
    return out


def _slot_ready(slot: dict) -> bool:
    p = slot["provider"]
    addr = slot["base_url"] or PROVIDERS.get(p, ("", ""))[1]
    return bool(p in PROVIDERS and slot["models"] and addr and (slot["key"] or p in NO_KEY))


def current() -> dict:
    """Provider, models, address and key: what the dashboard saved, or else what .env says."""
    d = _from_dashboard()
    p = _norm(d.get("provider") or config.AI_PROVIDER)
    return {"provider": p,
            "models": list(d["models"]) if d.get("provider") else list(config.AI_MODELS),
            "base_url": (str(d.get("base_url") or "") if d.get("provider") else config.AI_BASE_URL).rstrip("/"),
            "key": str(d["key"]) if d.get("provider") and "key" in d else config.ai_key()}


def fallback() -> dict:
    """Optional secondary service tried when every model on the primary service fails."""
    d = _from_dashboard()
    prim = current()["provider"]
    if "fallback" in d:
        fb = d.get("fallback") if isinstance(d.get("fallback"), dict) else {}
        fp = _norm(fb.get("provider"))
        ms = [str(m).strip() for m in (fb.get("models") or []) if str(m).strip()]
        addr = str(fb.get("base_url") or "").rstrip("/")
        k = str(fb.get("key") or "")
    else:
        fp = _norm(config.AI_FALLBACK_PROVIDER)
        ms = list(config.AI_FALLBACK_MODELS)
        addr = config.AI_FALLBACK_BASE_URL
        k = config.ai_fallback_key()
    if fp == prim:
        fp = ""
    slot = {"provider": fp, "models": ms if fp else [], "base_url": addr if fp else "", "key": k if fp else ""}
    return {**slot, "ready": _slot_ready(slot), "label": PROVIDERS.get(fp, ("",))[0]}


def targets() -> list[tuple[str, str]]:
    """Ordered (provider_id, model_id) pairs to try: primary models first, then fallback models if ready."""
    out = []
    c = current()
    if _slot_ready(c):
        out.extend((c["provider"], m) for m in c["models"])
    fb = fallback()
    if fb["ready"] and fb["provider"] != c["provider"]:
        out.extend((fb["provider"], m) for m in fb["models"])
    return out


def _cfg_for(provider_id: str | None = None) -> dict:
    c = current()
    if not provider_id or _norm(provider_id) == c["provider"]:
        slot = c
    else:
        p = _norm(provider_id)
        fb = fallback()
        if p == fb["provider"]:
            slot = fb
        else:
            prof = _profiles().get(p, {})
            slot = {"provider": p, "models": list(prof.get("models") or []),
                    "base_url": str(prof.get("base_url") or "").rstrip("/"), "key": str(prof.get("key") or "")}
    p = slot["provider"]
    return {**slot, "address": slot["base_url"] or PROVIDERS.get(p, ("", ""))[1],
            "label": PROVIDERS.get(p, ("",))[0], "how": PROVIDERS[p][2]}


def redact(text: str) -> str:
    """Replace any known AI key (primary, fallback or saved in profiles) with <key>."""
    s = str(text or "")
    keys = {current()["key"], fallback()["key"], config.ai_key(), config.ai_fallback_key(),
            *(v.get("key", "") for v in _profiles().values())}
    for k in sorted((x for x in keys if x), key=len, reverse=True):
        s = s.replace(k, "<key>")
    return s


def provider() -> str:
    return current()["provider"]


def models() -> list:
    return current()["models"]


def key() -> str:
    return current()["key"]


def label() -> str:
    return PROVIDERS.get(provider(), ("",))[0]


def base_url() -> str:
    c = current()
    return c["base_url"] or PROVIDERS.get(c["provider"], ("", ""))[1]


def ready() -> bool:
    return bool(targets())


def _hint(k: str) -> str:
    return k[-4:] if len(k) >= 12 else ""


def _fp(cfg: dict) -> str:
    addr = cfg.get("address") or cfg.get("base_url") or PROVIDERS.get(cfg["provider"], ("", ""))[1]
    return hashlib.sha256(f"{cfg['provider']}|{addr}|{cfg.get('key', '')}".encode("utf-8")).hexdigest()[:12]


def record_health(provider_id: str, model: str, ok: bool, ms: int = 0, error=None, cfg: dict | None = None, extra_key: str = "") -> dict:
    """Remember whether one (provider, model) answered and how fast, keyed by a hash of its address and key."""
    cfg = cfg or _cfg_for(provider_id)
    err = ""
    if not ok:
        raw = explain(error, provider_id=cfg["provider"], model=model) or redact(error)
        ek = str(extra_key or "").strip()
        if ek:
            raw = raw.replace(ek, "<key>")
        err = raw[:200]
    rec = {"state": "operational" if ok else "error", "ok": bool(ok), "ms": max(0, int(ms)), "at": time.time(), "error": err}
    with _lock:
        _health[(cfg["provider"], model, _fp(cfg))] = rec
    return rec


def health_rows() -> list[dict]:
    """Connection status for every configured primary and fallback model, in execution order."""
    c, fb = current(), fallback()
    rows = []
    for role, slot in (("primary", c), ("fallback", fb)):
        p = slot["provider"]
        if not p or p not in PROVIDERS or not slot["models"]:
            continue
        slot_ok = _slot_ready(slot)
        cfg = {**slot, "address": slot["base_url"] or PROVIDERS[p][1], "label": PROVIDERS[p][0], "how": PROVIDERS[p][2]}
        fp = _fp(cfg)
        for m in slot["models"]:
            rec = _health.get((p, m, fp))
            state = "incomplete" if not slot_ok else (rec["state"] if rec else "untested")
            rows.append({"order": len(rows) + 1, "role": role, "provider": p, "label": cfg["label"], "model": m,
                         "ready": slot_ok, "state": state, "ms": rec["ms"] if rec else None,
                         "at": rec["at"] if rec else None, "error": rec["error"] if rec else ""})
    return rows


def role_state(role: str, rows: list[dict] | None = None) -> str:
    rs = [r for r in (health_rows() if rows is None else rows) if r["role"] == role]
    if not rs or not any(r["ready"] for r in rs):
        return "off"
    if any(r["state"] == "operational" for r in rs):
        return "operational"
    if all(r["state"] == "error" for r in rs):
        return "error"
    return "untested"


def public() -> dict:
    """What the settings page may see. Keys never leave the server: only whether there is one, and
    its last four characters so the owner can tell which key it is."""
    c, fb, profs = current(), fallback(), _profiles()
    checks = health_rows()
    saved_public = {pid: {"models": v["models"], "base_url": v["base_url"],
                          "has_key": bool(v["key"]), "key_hint": _hint(v["key"])}
                    for pid, v in profs.items()}
    return {"provider": c["provider"], "models": c["models"], "base_url": c["base_url"], "has_key": bool(c["key"]),
            "key_hint": _hint(c["key"]), "ready": ready(), "label": label(),
            "saved_here": bool(_from_dashboard().get("provider")),
            "fallback": {"provider": fb["provider"], "models": fb["models"], "base_url": fb["base_url"],
                         "has_key": bool(fb["key"]), "key_hint": _hint(fb["key"]), "ready": fb["ready"], "label": fb["label"]},
            "checks": checks,
            "main_state": role_state("primary", checks),
            "fallback_state": role_state("fallback", checks),
            "saved_profiles": saved_public,
            "providers": [{"id": k, "label": v[0], "address": v[1], "needs_key": k not in NO_KEY, "key_url": v[3], "example": v[4]}
                          for k, v in PROVIDERS.items()]}


def _clean_models(model_ids, empty_msg: str) -> list[str]:
    if isinstance(model_ids, str):
        model_ids = model_ids.split(",")
    ms = [str(m).strip()[:120] for m in (model_ids or []) if str(m).strip()][:5]
    if not ms:
        raise ValueError(empty_msg)
    if any(not re.fullmatch(r"[\w.\-:/@]+", m) for m in ms):
        raise ValueError("A model id has letters, digits and . - : / only.")
    return ms


def _clean_address(p: str, address: str, label_prefix: str = "A") -> str:
    address = str(address or "").strip().rstrip("/")[:300]
    if address and not re.fullmatch(r"https?://[^\s]+", address):
        raise ValueError("The address has to start with http:// or https://")
    if p == "custom" and not address:
        raise ValueError(f"{label_prefix} custom service needs its address, for example http://127.0.0.1:1234/v1")
    return address


def save(provider_id: str, model_ids, address: str = "", new_key=None,
         fallback_provider: str | None = None, fallback_models=None, fallback_address: str | None = None, fallback_key=None) -> dict:
    """Save the choice made in the dashboard. `new_key` / `fallback_key` None keeps the key already stored for that
    service (the page never has it to send back); an empty string removes it."""
    p = _norm(provider_id)
    if p not in PROVIDERS:
        raise ValueError("Choose one of the AI services in the list.")
    ms = _clean_models(model_ids, "Enter the model to use.")
    address = _clean_address(p, address, "A")
    raw = _from_dashboard()
    profs = _profiles(raw)
    old = current()
    saved_p_key = profs.get(p, {}).get("key", "") or (old["key"] if old["provider"] == p else "")
    k = saved_p_key if new_key is None else str(new_key or "").strip()[:400]
    if not k and p not in NO_KEY:
        raise ValueError("Paste the key for this service.")
    if k and len(k) < 8:
        raise ValueError("That key looks too short. Paste the full key from the service.")
    profs[p] = {"models": ms, "base_url": address, "key": k}

    write_fb = True
    if fallback_provider is None:
        if "fallback" in raw:
            cur_fb = raw.get("fallback") if isinstance(raw.get("fallback"), dict) else {}
            fb_data = dict(cur_fb) if cur_fb and _norm(cur_fb.get("provider")) != p else {}
        elif _norm(config.AI_FALLBACK_PROVIDER) == p:
            fb_data = {}
        else:
            write_fb, fb_data = False, {}
    else:
        fp = _norm(fallback_provider)
        if fp in ("", "none"):
            fb_data = {}
        else:
            if fp not in PROVIDERS:
                raise ValueError("Choose one of the AI services in the list for the fallback.")
            if fp == p:
                raise ValueError("Choose a different service for the fallback, or add a second model above after a comma.")
            saved_fb = profs.get(fp, {})
            raw_fms = fallback_models
            if raw_fms is None or (isinstance(raw_fms, str) and not raw_fms.strip()) or raw_fms == []:
                raw_fms = saved_fb.get("models") or (old["models"] if old["provider"] == fp else []) or (
                    config.AI_FALLBACK_MODELS if fp == _norm(config.AI_FALLBACK_PROVIDER) else [])
            fms = _clean_models(raw_fms, "Enter the model to use for the fallback service.")
            raw_faddr = saved_fb.get("base_url", "") if fallback_address is None else fallback_address
            faddr = _clean_address(fp, raw_faddr, "A fallback")
            saved_fb_key = saved_fb.get("key", "") or (old["key"] if old["provider"] == fp else "") or (
                config.ai_fallback_key() if fp == _norm(config.AI_FALLBACK_PROVIDER) else "")
            fk = saved_fb_key if fallback_key is None else str(fallback_key or "").strip()[:400]
            if not fk and fp not in NO_KEY:
                raise ValueError("Paste the key for the fallback service.")
            if fk and len(fk) < 8:
                raise ValueError("That fallback key looks too short. Paste the full key from the service.")
            fb_data = {"provider": fp, "models": fms, "base_url": faddr, "key": fk}
            profs[fp] = {"models": fms, "base_url": faddr, "key": fk}

    payload = {"provider": p, "models": ms, "base_url": address, "key": k, "profiles": profs}
    if write_fb:
        payload["fallback"] = fb_data
    with _lock:
        f = _file()
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)        # the key is only for whoever runs the server
        except OSError:
            pass
        tmp.replace(f)
        _saved["at"] = None
    return public()


def list_models(provider_id=None, address=None, with_key=None) -> list:
    """The model ids a service offers, so the settings page can show a list to pick from."""
    c, fb, profs = current(), fallback(), _profiles()
    p = _norm(provider_id or c["provider"])
    if p not in PROVIDERS:
        raise ValueError("Choose one of the AI services in the list.")
    saved = c if c["provider"] == p else fb if fb["provider"] == p else profs.get(p, {})
    k = str(with_key or "").strip() or str(saved.get("key") or "")
    base = str(address or "").strip().rstrip("/") or str(saved.get("base_url") or "") or PROVIDERS[p][1]
    if not base:
        raise ValueError("Enter the service's address first.")
    how = PROVIDERS[p][2]
    if how == "gemini":
        r = httpx.get(base, params={"pageSize": 200}, headers={"x-goog-api-key": k}, timeout=20)
    elif how == "anthropic":
        r = httpx.get(f"{base}/models", params={"limit": 200}, headers={"x-api-key": k, "anthropic-version": "2023-06-01"}, timeout=20)
    else:
        r = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {k}"} if k else {}, timeout=20)
    if r.status_code != 200:
        bad = redact(r.text).replace(k or chr(0), "<key>")[:200]
        raise AIError(f"HTTP {r.status_code} {bad}", provider_id=p)
    data = r.json()
    if how == "gemini":
        found = [m.get("name", "").split("/")[-1] for m in data.get("models", [])
                 if "generateContent" in (m.get("supportedGenerationMethods") or [])]
    else:
        found = [m.get("id", "") for m in data.get("data", []) if isinstance(m, dict)]
    return sorted({m for m in found if m})[:400]


def _draft_slot(draft: dict) -> tuple[dict, str]:
    p = _norm(draft.get("provider"))
    if p not in PROVIDERS:
        raise ValueError("Choose one of the AI services in the list.")
    profs, old = _profiles(), current()
    saved = profs.get(p, {})
    raw_ms = draft.get("models")
    if raw_ms is None or (isinstance(raw_ms, str) and not raw_ms.strip()) or raw_ms == []:
        raw_ms = saved.get("models") or (old["models"] if old["provider"] == p else [])
    ms = _clean_models(raw_ms, "Enter the model to test.")
    raw_addr = draft.get("base_url")
    addr = _clean_address(p, saved.get("base_url", "") if raw_addr is None else raw_addr, "A")
    typed_k = str(draft.get("key") or "").strip()[:400]
    k = typed_k or saved.get("key", "") or (old["key"] if old["provider"] == p else "")
    if not k and p not in NO_KEY:
        raise ValueError("Paste the key for this service first.")
    if k and len(k) < 8:
        raise ValueError("That key looks too short. Paste the full key from the service.")
    cfg = {"provider": p, "models": ms, "base_url": addr, "key": k,
           "address": addr or PROVIDERS[p][1], "label": PROVIDERS[p][0], "how": PROVIDERS[p][2]}
    return cfg, typed_k


def test(scope: str = "all", draft: dict | None = None, raise_on_fail: bool = True) -> dict:
    """Test configured (or draft) AI models independently, recording each model's live operational status."""
    scope = str(scope or "all").strip().lower()
    if scope not in ("all", "primary", "fallback"):
        raise ValueError("Unknown test scope.")
    plan: list[tuple[str, dict, list[str], str]] = []
    if isinstance(draft, dict) and draft.get("provider"):
        role = "fallback" if scope == "fallback" else "primary"
        dcfg, typed_k = _draft_slot(draft)
        plan.append((role, dcfg, dcfg["models"], typed_k))
    else:
        c, fb = current(), fallback()
        c_ready, fb_ready = _slot_ready(c), fb["ready"]
        if scope == "primary":
            if not c_ready:
                raise ValueError("Save a service, a model and a key first.")
            plan.append(("primary", _cfg_for(c["provider"]), c["models"], ""))
        elif scope == "fallback":
            if not fb_ready:
                raise ValueError("Choose a fallback service, a model and a key first.")
            plan.append(("fallback", _cfg_for(fb["provider"]), fb["models"], ""))
        else:
            if not c_ready and not fb_ready:
                raise ValueError("Save a service, a model and a key first.")
            if c_ready:
                plan.append(("primary", _cfg_for(c["provider"]), c["models"], ""))
            if fb_ready:
                plan.append(("fallback", _cfg_for(fb["provider"]), fb["models"], ""))

    schema = {"type": "ARRAY", "items": {"type": "STRING"}}
    prompt = 'Answer with a JSON array that holds the single string "ok".'
    fn_map = {"gemini": _gemini, "openai": _openai, "anthropic": _anthropic}
    results = []
    total_tokens, first_ans, first_exc = 0, "", None
    for role, cfg, ms, extra_k in plan:
        for m in ms:
            t0 = time.monotonic()
            try:
                out, sent, wrote = fn_map[cfg["how"]](m, prompt, schema, 35, cfg)
                elapsed = round((time.monotonic() - t0) * 1000)
                rec = record_health(cfg["provider"], m, True, elapsed, cfg=cfg)
                total_tokens += sent + wrote
                if not first_ans:
                    first_ans = str(out)[:80]
            except Exception as e:  # noqa: BLE001
                elapsed = round((time.monotonic() - t0) * 1000)
                rec = record_health(cfg["provider"], m, False, elapsed, error=e, cfg=cfg, extra_key=extra_k)
                if first_exc is None and (role == "primary" or scope == "fallback"):
                    first_exc = e
            results.append({"order": len(results) + 1, "role": role, "provider": cfg["provider"], "label": cfg["label"],
                            "model": m, "ok": rec["ok"], "state": rec["state"], "ms": rec["ms"], "at": rec["at"], "error": rec["error"]})

    prim_rows = [r for r in results if r["role"] == "primary"]
    fb_rows = [r for r in results if r["role"] == "fallback"]
    main_rows = fb_rows if scope == "fallback" or not prim_rows else prim_rows
    main_ok = any(r["ok"] for r in main_rows)
    any_ok = any(r["ok"] for r in results)
    if any_ok:
        clear_problem()
    if raise_on_fail and not main_ok and first_exc is not None:
        raise first_exc
    chosen_model = next((r["model"] for r in main_rows if r["ok"]), main_rows[0]["model"] if main_rows else "")
    prim_err = next((r["error"] for r in prim_rows if not r["ok"]), "")
    fb_ok = (any(r["ok"] for r in fb_rows)) if (fb_rows and prim_rows) else None
    fb_model = next((r["model"] for r in fb_rows if r["ok"]), fb_rows[0]["model"] if fb_rows else "")
    fb_err = next((r["error"] for r in fb_rows if not r["ok"]), "")
    return {"ok": main_ok, "any_ok": any_ok, "model": chosen_model, "tokens": total_tokens, "answer": first_ans,
            "primary_error": prim_err, "fallback_ok": fb_ok, "fallback_model": fb_model, "fallback_error": fb_err,
            "results": results, "checks": health_rows()}


class AIError(RuntimeError):
    def __init__(self, msg: str, provider_id: str | None = None, model: str | None = None):
        super().__init__(msg)
        self.provider_id = provider_id
        self.model = model


problem = {"text": "", "at": 0.0}       # why the AI last failed, for the dashboard; cleared when it answers again


def explain(e, provider_id: str | None = None, model: str | None = None) -> str:
    """What went wrong with an AI call, in words the owner can act on."""
    text, low = redact(e), redact(e).lower()
    pid = _norm(provider_id or getattr(e, "provider_id", None) or provider())
    svc = PROVIDERS.get(pid, (label() or "The AI service",))[0]
    name = model or getattr(e, "model", None) or (models() or ["?"])[0]
    if "404" in text or "not found" in low or "does not exist" in low or "unknown model" in low or "not_found" in low:
        return f"{svc} doesn't know the model \"{name}\". Open Settings, AI, press Find models and pick one from the list."
    if "401" in text or "403" in text or "api key" in low or "api_key" in low or "unauthorized" in low or "permission" in low:
        return f"{svc} refused the key. Paste a working key under Settings, AI."
    if "429" in text or "quota" in low or "rate limit" in low or "exhausted" in low:
        return f"{svc} says the key has hit its limit or has no credit left. Check the account with {svc}, or try again later."
    if "connect" in low or "timed out" in low or "timeout" in low or "getaddrinfo" in low or "refused" in low or "nodename" in low or "servname" in low or "nameresolutionerror" in low or "name resolution" in low:
        return f"PeakCut couldn't reach {svc}. Check the internet connection" + (" and that Ollama is running." if pid == "ollama" else ".")
    return ""


def note_problem(e, provider_id: str | None = None, model: str | None = None):
    import time
    why = explain(e, provider_id=provider_id, model=model)
    if why:
        problem.update(text=why, at=time.time())


def clear_problem():
    problem.update(text="", at=0.0)


def _fail(r, model, provider_id: str | None = None):
    text = redact(r.text)[:300]
    raise AIError(f"HTTP {r.status_code} {text}", provider_id=provider_id, model=model)


# ---------------------------------------------------------------- the schema, said in words for chat models
def _json_schema(s):
    """Gemini's schema spelling (ARRAY, OBJECT...) as ordinary JSON Schema."""
    if isinstance(s, dict):
        return {k: (v.lower() if k == "type" and isinstance(v, str) else _json_schema(v)) for k, v in s.items()}
    if isinstance(s, list):
        return [_json_schema(x) for x in s]
    return s


def _wrapped(prompt, schema):
    return (prompt + "\n\nAnswer with JSON only, no other text and no code fences: one object with a single key "
            "\"items\", whose value follows this JSON Schema:\n" + json.dumps(_json_schema(schema)))


def _json_chunks(text: str):
    """Every balanced {...} or [...] in the text, in the order they appear."""
    out, depth, start, in_str, esc = [], 0, None, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            if depth == 0:
                start = i
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0 and start is not None:
                out.append(text[start:i + 1])
                start = None
            elif depth < 0:
                depth = 0
    return out


def _parse(text):
    """The JSON in an answer, even if the model wrapped it in a code fence or a sentence.

    A model that is not careful sometimes puts a sentence in front of the JSON,
    or a second object after it, or forgets a comma. The JSON is looked for
    inside the answer rather than demanded of it."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    out = None
    try:
        out = json.loads(text)
    except ValueError:
        for chunk in _json_chunks(text):
            try:
                out = json.loads(chunk)
                break
            except ValueError:
                continue
        if out is None:
            m = re.search(r"[\[{].*[\]}]", text, re.S)
            if not m:
                raise AIError("The AI did not answer with JSON.")
            try:
                out = json.loads(m.group(0))
            except ValueError:
                raise AIError("The AI did not answer with JSON.")
    if isinstance(out, dict) and "items" in out:
        out = out["items"]
    return out


# ---------------------------------------------------------------- one call
def call(model: str, prompt: str, schema: dict, timeout: float | None = None, provider_id: str | None = None):
    """Ask one model. Returns (the parsed JSON answer, tokens sent, tokens written back).

    `timeout` is how long one answer may take. A live stream passes a short one, because a slow
    answer must not stop the recording."""
    cfg = _cfg_for(provider_id)
    return {"gemini": _gemini, "openai": _openai, "anthropic": _anthropic}[cfg["how"]](model, prompt, schema, timeout, cfg)


def _see_one(cfg: dict, model: str, prompt: str, b64: str, timeout: float | None) -> tuple[str, int, int]:
    how, addr, k, pid = cfg["how"], cfg["address"], cfg["key"], cfg["provider"]
    if how == "gemini":
        body = {"contents": [{"role": "user", "parts": [
            {"text": prompt},
            {"inline_data": {"mimeType": "image/jpeg", "data": b64}}]}]}
        r = httpx.post(f"{addr}/{model}:generateContent", json=body,
                       headers={"x-goog-api-key": k}, timeout=timeout or 120)
        if r.status_code != 200:
            _fail(r, model, pid)
        data = r.json()
        text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
        u = data.get("usageMetadata", {})
        sent = int(u.get("promptTokenCount") or 0)
        return text, sent, max(0, int(u.get("totalTokenCount") or 0) - sent)
    if how == "anthropic":
        body = {"model": model, "max_tokens": 300, "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
            {"type": "text", "text": prompt}]}]}
        r = httpx.post(f"{addr}/messages", json=body, timeout=timeout or 120,
                       headers={"x-api-key": k, "anthropic-version": "2023-06-01"})
        if r.status_code != 200:
            _fail(r, model, pid)
        data = r.json()
        text = "".join(p.get("text", "") for p in data.get("content") or [] if p.get("type") == "text")
        u = data.get("usage") or {}
        return text, int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)
    headers = {"Authorization": f"Bearer {k}"} if k else {}
    body = {"model": model, "stream": False, "messages": [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}}]}]}
    r = httpx.post(f"{addr}/chat/completions", json=body, headers=headers, timeout=timeout or 120)
    if r.status_code != 200:
        _fail(r, model, pid)
    data = _openai_body(r)
    text = (data["choices"][0]["message"].get("content") or "")
    u = data.get("usage") or {}
    return text, int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)


def see(prompt: str, image: bytes, timeout: float | None = None) -> tuple[str, int, int]:
    """Ask one model about one picture: (the answer as text, tokens in, tokens out).

    Nothing is broken by a service that cannot look at pictures, or a model that is only text: the answer
    comes back empty, or says something the caller does not understand, and the caller carries on without
    it. The picture is a JPEG, passed as base64 inside the ordinary message."""
    import time
    ts = targets()
    if not ts or not image:
        return "", 0, 0
    b64 = base64.b64encode(image).decode()
    deadline = (time.monotonic() + timeout) if timeout else None
    bad_provs, last = set(), None
    for i, (pid, model) in enumerate(ts):
        if pid in bad_provs:
            continue
        if deadline is not None:
            rem = deadline - time.monotonic()
            if rem <= 0:
                break
            left = len(ts) - i
            call_to = max(2.0, rem / min(left, 3)) if left > 1 else rem
        else:
            call_to = None
        t0 = time.monotonic()
        try:
            res = _see_one(_cfg_for(pid), model, prompt, b64, call_to)
            record_health(pid, model, True, round((time.monotonic() - t0) * 1000))
            return res
        except Exception as e:  # noqa: BLE001
            record_health(pid, model, False, round((time.monotonic() - t0) * 1000), error=e)
            last = e
            s = str(e).lower()
            if "http 401" in s or "http 403" in s or "connect" in s or "refused" in s or "nameresolution" in s:
                bad_provs.add(pid)
    raise last


def _gemini(model, prompt, schema, timeout=None, cfg=None):
    cfg = cfg or _cfg_for()
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json", "responseSchema": schema}}
    r = httpx.post(f"{cfg['address']}/{model}:generateContent", json=body, headers={"x-goog-api-key": cfg["key"]}, timeout=timeout or 120)
    if r.status_code != 200:
        _fail(r, model, cfg["provider"])
    data = r.json()
    text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    u = data.get("usageMetadata", {})
    sent = int(u.get("promptTokenCount") or 0)
    return json.loads(text), sent, max(0, int(u.get("totalTokenCount") or 0) - sent)   # the answer, and any thinking billed with it


def _openai_body(r):
    """The answer, whether the service sent plain JSON or an event stream.

    Some OpenAI-compatible servers answer with a stream of events unless the
    request says otherwise, so the answer is read from whichever shape arrived.
    A model that answers with a tool call instead of a message leaves the JSON in
    the call's arguments, so that is used too."""
    try:
        data = r.json()
    except ValueError:
        data = _from_events(r.text)
    for ch in data.get("choices") or []:
        msg = ch.get("message") or {}
        if (msg.get("content") or "").strip():
            continue
        for call in msg.get("tool_calls") or []:
            args = (call.get("function") or {}).get("arguments")
            if args:
                msg["content"] = args
                break
    return data


def _from_events(text: str) -> dict:
    """Read an answer out of a server-sent event stream."""
    parts, usage, calls = [], {}, {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            d = json.loads(chunk)
        except ValueError:
            continue
        usage = d.get("usage") or usage
        for ch in d.get("choices") or []:
            for piece in ((ch.get("message") or {}), (ch.get("delta") or {})):
                if piece.get("content"):
                    parts.append(piece["content"])
                for call in piece.get("tool_calls") or []:
                    i = call.get("index", 0)
                    calls[i] = calls.get(i, "") + ((call.get("function") or {}).get("arguments") or "")
    message = {"content": "".join(parts)}
    if not message["content"] and calls:
        message["content"] = "".join(calls.values())
    return {"choices": [{"message": message}], "usage": usage}


def _openai(model, prompt, schema, timeout=None, cfg=None):
    cfg = cfg or _cfg_for()
    headers = {"Authorization": f"Bearer {cfg['key']}"} if cfg["key"] else {}
    body = {"model": model, "messages": [{"role": "user", "content": _wrapped(prompt, schema)}], "stream": False}
    # Most services accept these two extras; a few models refuse one of them, so they are dropped on a refusal.
    extras = [{"temperature": 0.4, "response_format": {"type": "json_object"}}, {"response_format": {"type": "json_object"}}, {}]
    r = None
    for extra in extras:
        r = httpx.post(f"{cfg['address']}/chat/completions", json={**body, **extra}, headers=headers, timeout=timeout or 180)
        if r.status_code not in (400, 422):
            break
    if r.status_code != 200:
        _fail(r, model, cfg["provider"])
    data = _openai_body(r)
    u = data.get("usage") or {}
    return _parse(data["choices"][0]["message"].get("content")), int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)


def _anthropic(model, prompt, schema, timeout=None, cfg=None):
    cfg = cfg or _cfg_for()
    body = {"model": model, "max_tokens": 8192, "messages": [{"role": "user", "content": _wrapped(prompt, schema)}]}
    r = httpx.post(f"{cfg['address']}/messages", json=body, timeout=timeout or 180,
                   headers={"x-api-key": cfg["key"], "anthropic-version": "2023-06-01"})
    if r.status_code != 200:
        _fail(r, model, cfg["provider"])
    data = r.json()
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    u = data.get("usage") or {}
    return _parse(text), int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)
