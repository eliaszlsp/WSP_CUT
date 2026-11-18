"""Brazilian Portuguese (PT-BR) ASR default, accent-safe captions, translation and output language selector."""
import os
import shutil
import sys
import tempfile

os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-ptbr-")
os.environ["DATABASE_URL"] = ""
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

print("ALL PASSED" if not fails else f"{len(fails)} failed")
shutil.rmtree(os.environ["PEAKCUT_DATA"], ignore_errors=True)
sys.exit(1 if fails else 0)
