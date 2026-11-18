"""Brazilian Portuguese (PT-BR) ASR default, accent-safe captions, translation and output language selector."""
import os
import shutil
import sys
import tempfile

os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-ptbr-")
os.environ.update(DATABASE_URL="", PEAKCUT_ACCOUNTS="0", PEAKCUT_PUBLIC_URL="http://127.0.0.1:8765")
os.environ.pop("PEAKCUT_ASR_MODEL", None)
os.environ.pop("PEAKCUT_OUTPUT_LANG", None)
sys.path.insert(0, ".")

import numpy as np  # noqa: E402
from studio import brain, config, render  # noqa: E402

fails = []


def check(name, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got {got!r}, want {want!r}"))
    if not ok:
        fails.append(name)


def ws(text):
    return [{"w": w, "s": round(i * 0.5, 2), "e": round(i * 0.5 + 0.4, 2)} for i, w in enumerate(text.split())]


def joined(words):
    return " ".join(w["w"] for w in words)


# ---- Task 1: Parakeet v3 default, OUTPUT_LANGS, accent folding, translation logic & prompts
check("default ASR model is multilingual Parakeet v3 int8",
      config.ASR_MODEL_NAME, "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8")
check("default output language is pt", config.OUTPUT_LANG, "pt")
check("OUTPUT_LANGS offers pt, source and en",
      brain.OUTPUT_LANGS, {"pt": "Português (BR)", "source": "Same as spoken", "en": "English"})
check("clean_output_lang normalizes aliases and falls back to default",
      [brain.clean_output_lang(x) for x in ("pt", "pt-BR", "source", "original", "en", "en-US", "weird", "")],
      ["pt", "pt", "source", "source", "en", "en", "pt", "pt"])

# Accent-safe matching in render._key and brain._same_word
check("render._key folds Portuguese accents so highlight_words light up",
      (render._key("EXPLOSÃO!"), render._key("coração,")), (render._key("explosao"), "coracao"))
check("brain._same_word accepts Portuguese accent fixes and rejects unrelated rewrites",
      [brain._same_word("acao", "ação"), brain._same_word("e", "é"),
       brain._same_word("nao", "Não!"), brain._same_word("carro", "banana")],
      [True, True, True, False])

# Translation matrix
pt_live = ws("mano olha esse carro na pista agora")
en_live = ws("wait look at that car right there bro")
check("needs_translation handles pt, source and en cleanly",
      [brain.needs_translation("pt-BR", "pt"),
       brain.needs_translation("pt", "pt"),
       brain.needs_translation("en", "pt"),
       brain.needs_translation("es", "pt"),
       brain.needs_translation("en", "source"),
       brain.needs_translation("pt", "source"),
       brain.needs_translation("pt", "en")],
      [False, False, True, True, False, False, True])
check("live stream without lang metadata keeps PT speech and translates EN speech when output_lang=pt",
      [brain.needs_translation("", "pt", pt_live), brain.needs_translation("", "pt", en_live)],
      [False, True])

# Captions pipeline: PT->PT keeps 1:1 words with accents; EN->PT translates; EN->source keeps EN
prompts_seen = []
fixed_reply = []
trans_reply = []


def fake_ask_ai(prompt, schema=None, purpose="", timeout=None):
    prompts_seen.append((purpose, prompt))
    if purpose == "Caption translation":
        return list(trans_reply), "mock-model", 12
    if purpose == "Caption wording fix":
        return list(fixed_reply), "mock-model", 10
    return [{
        "start": 0.0, "end": 18.0, "score": 88, "category": "shock",
        "hook": "não acredito nessa explosão",
        "title": "Reação insana no trailer de GTA 6!",
        "description": "Olha esse detalhe absurdo em Vice City.",
        "hashtags": ["gta6", "vicecity", "cortes", "reação"],
        "highlight_words": ["explosão", "insana"],
        "reason": "Momento forte de reação.",
    }], "mock-model", 25


brain.ask_ai = fake_ask_ai

pt_src = ws("nao acredito nessa explosao mano")
fixed_reply[:] = ["Não", "acredito", "nessa", "explosão,", "mano!"]
pt_out = brain.captions_for(pt_src, "pt-BR", "Trailer GTA 6", output_lang="pt")
check("PT-BR video with output_lang=pt fixes accents 1:1 without translating",
      (joined(pt_out), [(w["s"], w["e"]) for w in pt_out]),
      ("Não acredito nessa explosão, mano!", [(w["s"], w["e"]) for w in pt_src]))

en_src = [{"w": w, "s": s, "e": s + 0.4} for w, s in (
    ("wait", 0.0), ("look", 0.5), ("at", 1.0), ("that.", 1.5),
    ("no", 4.0), ("way", 4.5), ("bro!", 5.0),
)]
trans_reply[:] = ["Espera, olha aquilo.", "Sem chance, mano!"]
en_to_pt = brain.captions_for(en_src, "en", "GTA VI", output_lang="pt", youtube_target=lambda: None)
check("English video with output_lang=pt translates captions into PT-BR on original line timings",
      ("Espera," in joined(en_to_pt) and "mano!" in joined(en_to_pt)
       and en_to_pt[0]["s"] == 0.0 and en_to_pt[-1]["e"] <= 5.45
       and any("Português" in p or "Brazilian Portuguese" in p for _, p in prompts_seen if "translation" in _.lower() or "Portuguese" in p)),
      True)

yt_pt = ws("espera olha aquilo agora")
fixed_reply[:] = ["Espera,", "olha", "aquilo", "agora!"]
en_yt_pt = brain.captions_for(en_src, "en", "GTA VI", output_lang="pt", youtube_target=lambda: yt_pt)
check("YouTube PT captions are used first when available and tidied in PT-BR",
      joined(en_yt_pt), "Espera, olha aquilo agora!")

fixed_reply[:] = ["Wait,", "look", "at", "that.", "No", "way,", "bro!"]
en_source = brain.captions_for(en_src, "en", "GTA VI", output_lang="source")
check("output_lang=source keeps spoken English without translating",
      joined(en_source), "Wait, look at that. No way, bro!")

# find_clips with output_lang="pt" vs "source"
prompts_seen.clear()
long_words = [{"w": f"palavra{i}", "s": float(i), "e": float(i) + 0.6} for i in range(40)]
clips_pt, _ = brain.find_clips(long_words, np.zeros(45), 40.0, title="Live GTA", max_clips=2, min_len=10, max_len=30, output_lang="pt")
check("find_clips(output_lang='pt') requests Brazilian Portuguese and preserves accents in hook/title/hashtags",
      bool(clips_pt) and clips_pt[0]["hook"] == "NÃO ACREDITO NESSA EXPLOSÃO"
      and "reação" in clips_pt[0]["hashtags"]
      and any("Brazilian Portuguese" in p for _, p in prompts_seen),
      True)

prompts_seen.clear()
brain.find_clips(long_words, np.zeros(45), 40.0, title="Live GTA", max_clips=2, min_len=10, max_len=30, output_lang="source")
check("find_clips(output_lang='source') requests the same language spoken in the transcript",
      any("same language" in p.lower() for _, p in prompts_seen),
      True)

# ---- Task 2: Creators, jobs, fixed_for cache invalidation on language change, live PT triggers & /api/status
from fastapi.testclient import TestClient  # noqa: E402
from studio import auth, creators, ctx, db, jobs, live, server  # noqa: E402

db.init()
owner = auth.local_user()
with ctx.as_user(owner.id):
    cr = creators.create({"name": "ViceLoop"})
    check("new creator defaults to output_lang='pt' and includes PT trigger phrase",
          (cr.get("output_lang"), "clipa" in cr.get("trigger_words", "")), ("pt", True))
    cr_en = creators.update(cr["id"], {"output_lang": "en"})
    check("creator output_lang updates to en", cr_en.get("output_lang"), "en")
    cr_bad = creators.update(cr["id"], {"output_lang": "klingon"})
    check("invalid creator output_lang normalizes safely to default", cr_bad.get("output_lang"), "pt")

    creators.update(cr["id"], {"output_lang": "source"})
    jb_inh = jobs.create({"type": "file", "path": "x.mp4", "name": "x.mp4"}, {}, enqueue=False, creator=cr["id"])
    check("job inherits creator output_lang when omitted", jb_inh["options"].get("output_lang"), "source")
    jb_ovr = jobs.create({"type": "file", "path": "y.mp4", "name": "y.mp4"}, {"output_lang": "en"}, enqueue=False, creator=cr["id"])
    check("job explicit output_lang overrides creator default", jb_ovr["options"].get("output_lang"), "en")

    # Cache invalidation in jobs._caption_words when switching clip output_lang from en -> pt
    jb_ovr["lang"] = "en"
    clip_obj = {"id": "clip01", "start": 0.0, "end": 6.0, "status": "done", "style": "bounce", "layout": "blur", "captions": True, "output_lang": "en"}
    jb_ovr["clips"] = [clip_obj]
    fixed_reply[:] = ["Wait,", "look", "at", "that.", "No", "way,", "bro!"]
    cap_en = jobs._caption_words(jb_ovr, clip_obj, en_src, 0.0)
    check("first render caches English captions under lang:en",
          ("Wait," in joined(cap_en), clip_obj.get("fixed_for")), (True, [0.0, 6.0, "lang:en"]))
    trans_reply[:] = ["Espera, olha aquilo.", "Sem chance, mano!"]
    jobs._q.put = lambda *a, **k: None
    jobs.request_render(jb_ovr["id"], "clip01", {"output_lang": "pt"})
    check("request_render updates clip output_lang to pt", clip_obj.get("output_lang"), "pt")
    cap_pt = jobs._caption_words(jb_ovr, clip_obj, en_src, 0.0)
    check("changing output_lang to pt invalidates fixed_for cache and translates captions",
          ("Espera," in joined(cap_pt), clip_obj.get("fixed_for")), (True, [0.0, 6.0, "lang:pt"]))

# Live PT-BR voice triggers ("clipa", "corta isso", and accent-folded custom "clipa aí")
dummy_live = type("DummyLive", (), {
    "CLIP_WORDS": live.Session.CLIP_WORDS,
    "SAID_GAP": 45,
    "last_said": -100.0,
    "said": None,
    "last_trigger": -100.0,
    "manual": [],
    "settings": {"trigger_words": "clipa aí"},
})()
live.Session._triggers(dummy_live, [{"w": "Clipa!", "s": 50.0, "e": 50.4}])
check("live built-in trigger catches spoken 'Clipa!'", bool(dummy_live.said and dummy_live.said["t"] == 50.0), True)
dummy_live.said = None
dummy_live.last_said = -100.0
live.Session._triggers(dummy_live, [{"w": "corta", "s": 120.0, "e": 120.3}, {"w": "isso", "s": 120.4, "e": 120.8}])
check("live built-in trigger catches two-word 'corta isso'", bool(dummy_live.said and dummy_live.said["t"] == 120.0), True)
live.Session._triggers(dummy_live, [{"w": "clipa", "s": 200.0, "e": 200.3}, {"w": "ai", "s": 200.4, "e": 200.7}])
check("live custom trigger folds accents so 'clipa aí' matches unaccented ASR 'clipa ai'",
      len(dummy_live.manual) == 1 and "clipa aí" in dummy_live.manual[0]["why"],
      True)

client = TestClient(server.app, base_url="http://127.0.0.1:8765")
st_json = client.get("/api/status").json()
check("/api/status exposes languages map and default_lang",
      (st_json.get("languages"), st_json.get("default_lang")),
      ({"pt": "Português (BR)", "source": "Same as spoken", "en": "English"}, "pt"))

# ---- Task 3: UI output language selectors in Composer, Creator Setup & Clip Editor + HTTP persistence
ORIGIN = {"Origin": "http://127.0.0.1:8765"}
r_cr = client.post("/api/creators", json={"name": "ViceLoopBR", "channel": ""}, headers=ORIGIN)
cid_http = r_cr.json().get("id")
r_put_cr = client.put(f"/api/creators/{cid_http}", json={"output_lang": "source"}, headers=ORIGIN)
sync_cr = next((x for x in client.get("/api/sync").json().get("creators", []) if x["id"] == cid_http), {})
check("HTTP creator PUT and /api/sync persist output_lang",
      (r_put_cr.status_code, sync_cr.get("output_lang")), (200, "source"))

app_html = client.get("/app").text
check("dashboard includes output language selector in Composer, Creator Setup and Clip Editor",
      all(tok in app_html for tok in ('id="o_lang"', 'data-seg="o_lang"', 'data-k="output_lang"', 'id="e_lang"', 'data-f="output_lang"')),
      True)

# ---- review fix pass: lang="" heuristics (short EN, Spanish with accents, PT->EN) & non-caching on AI failure
short_en = ws("I don't think he can win it now man")
spanish_acc = ws("hola amigo qué está pasando aquí en el juego")
pt_acc = ws("mano não acredito no que aconteceu aqui")
check("lang='' with output_lang='pt' detects short colloquial English and accented Spanish while keeping PT",
      [brain.needs_translation("", "pt", short_en),
       brain.needs_translation("", "pt", spanish_acc),
       brain.needs_translation("", "pt", pt_acc)],
      [True, True, False])
check("lang='' with output_lang='en' translates PT speech to English while keeping bare lang='' False",
      [brain.needs_translation("", "en", pt_acc),
       brain.needs_translation("", "en", short_en),
       brain.needs_translation("", "en")],
      [True, False, False])

with ctx.as_user(owner.id):
    jb_retry = jobs.create({"type": "file", "path": "z.mp4", "name": "z.mp4"}, {"output_lang": "pt"}, enqueue=False)
    jb_retry["lang"] = "en"
    clip_retry = {"id": "clip99", "start": 0.0, "end": 6.0, "status": "done", "style": "bounce", "layout": "blur", "captions": True, "output_lang": "pt"}
    jb_retry["clips"] = [clip_retry]

    def broken_ai(*a, **k):
        raise RuntimeError("429 quota exceeded")

    brain.ask_ai = broken_ai
    fallback_cap = jobs._caption_words(jb_retry, clip_retry, en_src, 0.0)
    check("when AI fails during translation, fallback captions render without poisoning fixed_for cache",
          ("wait" in joined(fallback_cap).lower(), clip_retry.get("fixed_for")),
          (True, None))

    brain.ask_ai = fake_ask_ai
    trans_reply[:] = ["Espera, olha aquilo.", "Sem chance, mano!"]
    recovered_cap = jobs._caption_words(jb_retry, clip_retry, en_src, 0.0)
    check("after AI recovers, re-making clip translates captions and caches fixed_for",
          ("Espera," in joined(recovered_cap), clip_retry.get("fixed_for")),
          (True, [0.0, 6.0, "lang:pt"]))

print("ALL PASSED" if not fails else f"{len(fails)} failed")
db.engine.dispose()
shutil.rmtree(os.environ["PEAKCUT_DATA"], ignore_errors=True)
sys.exit(1 if fails else 0)
