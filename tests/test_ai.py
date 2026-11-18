"""The AI layer, with the network replaced: every kind of service is asked correctly and its answer is read
correctly. Nothing leaves this machine and no key is needed."""
import json
import os
import sys
import tempfile

os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-ai-")
os.environ["DATABASE_URL"] = ""
os.environ.update(PEAKCUT_AI_PROVIDER="gemini", PEAKCUT_AI_MODELS="model-a, model-b", PEAKCUT_AI_KEY="test-key-123456", PEAKCUT_AI_BASE_URL="",
                  PEAKCUT_AI_FALLBACK_PROVIDER="", PEAKCUT_AI_FALLBACK_MODELS="", PEAKCUT_AI_FALLBACK_KEY="",
                  PEAKCUT_AI_FALLBACK_KEY_FILE="", PEAKCUT_AI_FALLBACK_BASE_URL="")
sys.path.insert(0, ".")
from studio import ai, auth, brain, config, ctx, db, usage  # noqa: E402

db.init()
fails = []


def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  {extra}" if extra and not ok else ""))
    ok or fails.append(name)


class Reply:
    def __init__(self, status, data):
        self.status_code, self._data, self.text = status, data, json.dumps(data)

    def json(self):
        return self._data


CLIP = {"start": 1, "end": 20, "score": 80, "hook": "WOW", "title": "t", "description": "d", "hashtags": ["a"], "reason": "r"}
seen = []


def fake_post(url, json=None, headers=None, timeout=None, **_k):  # noqa: A002
    seen.append((url, json, headers or {}))
    if url.endswith(":generateContent"):
        return Reply(200, {"candidates": [{"content": {"parts": [{"text": globals()["json_text"]([CLIP])}]}}],
                           "usageMetadata": {"promptTokenCount": 100, "totalTokenCount": 130}})
    if url.endswith("/chat/completions"):
        if "temperature" in json:                      # a model that refuses an extra setting: asked again without it
            return Reply(400, {"error": {"message": "temperature is not supported"}})
        return Reply(200, {"choices": [{"message": {"content": "```json\n" + globals()["json_text"]({"items": [CLIP]}) + "\n```"}}],
                           "usage": {"prompt_tokens": 90, "completion_tokens": 20}})
    if url.endswith("/messages"):
        return Reply(200, {"content": [{"type": "text", "text": "Here you go: " + globals()["json_text"]({"items": [CLIP]})}],
                           "usage": {"input_tokens": 70, "output_tokens": 10}})
    return Reply(404, {})


def json_text(x):
    return json.dumps(x)


ai.httpx.post = fake_post


def use(provider, base=""):
    config.AI_PROVIDER, config.AI_BASE_URL = provider, base
    seen.clear()


use("gemini")
out, sent, wrote = ai.call("model-a", "hello", brain.SCHEMA)
check("gemini: answer and tokens", out == [CLIP] and (sent, wrote) == (100, 30), str((out, sent, wrote)))
check("gemini: key in the header, schema passed as is", seen[0][2].get("x-goog-api-key") == "test-key-123456"
      and seen[0][1]["generationConfig"]["responseSchema"] is brain.SCHEMA and seen[0][0].endswith("/model-a:generateContent"))

for name, host in (("openai", "api.openai.com"), ("xai", "api.x.ai"), ("grok", "api.x.ai"), ("groq", "api.groq.com"), ("openrouter", "openrouter.ai")):
    use(name)
    out, sent, wrote = ai.call("model-a", "hello", brain.SCHEMA)
    check(f"{name}: answer unwrapped from a code fence, tokens read", out == [CLIP] and (sent, wrote) == (90, 20), str((out, sent, wrote)))
    check(f"{name}: right address, key as a bearer token", host in seen[0][0] and seen[0][2].get("Authorization") == "Bearer test-key-123456")
check("a refused extra setting is dropped and asked again", len(seen) == 2 and "temperature" not in seen[1][1])
check("the schema is told in the prompt for chat models", '"type": "array"' in seen[1][1]["messages"][0]["content"])

use("anthropic")
out, sent, wrote = ai.call("model-a", "hello", brain.SCHEMA)
check("anthropic: answer found inside a sentence, tokens read", out == [CLIP] and (sent, wrote) == (70, 10), str((out, sent, wrote)))
check("anthropic: key and version headers", seen[0][2].get("x-api-key") == "test-key-123456" and "anthropic-version" in seen[0][2])

use("custom", "http://127.0.0.1:1234/v1")
out, _s, _w = ai.call("model-a", "hello", brain.SCHEMA)
check("custom: uses the address from settings", seen[0][0] == "http://127.0.0.1:1234/v1/chat/completions" and out == [CLIP])

use("custom")
check("custom without an address is not ready", not ai.ready())
use("nonsense")
check("an unknown provider is not ready", not ai.ready())
use("ollama")
os.environ["PEAKCUT_AI_KEY"] = ""
config._kept.clear()
check("ollama needs no key", ai.ready() and ai.label().startswith("Ollama"))
out, _s, _w = ai.call("model-a", "hello", brain.SCHEMA)
check("ollama: no key header is sent", "Authorization" not in seen[0][2] and out == [CLIP])
use("gemini")
check("gemini without a key is not ready", not ai.ready())
os.environ["PEAKCUT_AI_KEY"] = "test-key-123456"
config._kept.clear()

# ---- the whole path: a call is made for a user and shows up in their usage, and only theirs
u1, _ = auth._create("ai-one@example.com", "One", password_hash="x")
u2, _ = auth._create("ai-two@example.com", "Two", password_hash="x")
words = [{"w": f"word{i}", "s": i * 0.5, "e": i * 0.5 + 0.4} for i in range(120)]
with ctx.as_user(u1.id):
    usage.set_context(None, "A test video")
    import numpy as np
    clips, info = brain.find_clips(words, np.zeros(60), 60.0, title="T", uploader="U")
    check("clips come back through the whole path", len(clips) == 1 and clips[0]["hook"] == "WOW" and info["model"] == "model-a", str((clips, info)))
    s = usage.summary({})
    check("the call is in this user's usage", s["calls_total"] == 1 and s["tokens_total"] == 130 and s["recent"][0]["where"] == "A test video", str(s))
    check("usage says which AI is set", s["ready"] and s["provider"] == "Gemini" and s["model"] == "model-a")
with ctx.as_user(u2.id):
    check("another user sees none of it", usage.summary({})["calls_total"] == 0)


def _raises(exc, fn):
    try:
        fn()
    except exc:
        return True
    return False


# ---- per-provider credential vault, legacy migration and fallback targets
(config.DATA_DIR / "ai.json").write_text(json.dumps({
    "provider": "openrouter", "models": ["openrouter/free"], "base_url": "", "key": "or-secret-key-9999"
}), encoding="utf-8")
ai._saved["at"] = None
pub = ai.save("gemini", "gemini-2.5-flash, gemini-2.5-pro", "", "gem-secret-key-1111",
              fallback_provider="openrouter", fallback_models=None, fallback_key=None)
check("legacy key and model kept for fallback without retyping",
      pub["ready"] and pub["fallback"]["ready"] and pub["fallback"]["key_hint"] == "9999"
      and pub["fallback"]["models"] == ["openrouter/free"]
      and ai.targets() == [("gemini", "gemini-2.5-flash"), ("gemini", "gemini-2.5-pro"), ("openrouter", "openrouter/free")],
      str(pub))
raw_saved = json.loads((config.DATA_DIR / "ai.json").read_text(encoding="utf-8"))
check("top-level ai.json fields stay backward compatible",
      raw_saved["provider"] == "gemini" and raw_saved["key"] == "gem-secret-key-1111")
check("public() never exposes any key",
      "gem-secret-key-1111" not in json.dumps(pub) and "or-secret-key-9999" not in json.dumps(pub))
check("redact hides both primary and fallback keys",
      ai.redact("bad gem-secret-key-1111 and or-secret-key-9999") == "bad <key> and <key>")
check("same provider for primary and fallback is refused",
      _raises(ValueError, lambda: ai.save("gemini", "gemini-2.5-flash", "", None, fallback_provider="gemini", fallback_models="m")))
check("custom fallback without an address is refused",
      _raises(ValueError, lambda: ai.save("gemini", "gemini-2.5-flash", "", None, fallback_provider="custom", fallback_models="m")))
check("fallback service without a saved key needs one",
      _raises(ValueError, lambda: ai.save("gemini", "gemini-2.5-flash", "", None, fallback_provider="openai", fallback_models="m")))

# direct provider_id call + see() failover + test() dual check
seen.clear()
out, sent, wrote = ai.call("openrouter/free", "hello", brain.SCHEMA, provider_id="openrouter")
check("call with explicit fallback provider uses its own url and key",
      out == [CLIP] and "openrouter.ai" in seen[0][0] and seen[0][2].get("Authorization") == "Bearer or-secret-key-9999")


def fake_post_failover(url, json=None, headers=None, timeout=None, **_k):  # noqa: A002
    seen.append((url, json, headers or {}))
    if "generativelanguage" in url:
        return Reply(503, {"error": {"message": "overloaded gem-secret-key-1111"}})
    return Reply(200, {"choices": [{"message": {"content": '["ok"]'}}], "usage": {"prompt_tokens": 11, "completion_tokens": 4}})


ai.httpx.post = fake_post_failover
seen.clear()
pic_txt, pic_in, pic_out = ai.see("webcam?", b"\xff\xd8\xff\xd9")
check("see() falls back to secondary provider when primary fails",
      pic_txt == '["ok"]' and (pic_in, pic_out) == (11, 4) and "openrouter.ai" in seen[-1][0], str((pic_txt, seen)))

ai.httpx.post = lambda url, json=None, headers=None, timeout=None, **_k: (  # noqa: A002
    Reply(200, {"candidates": [{"content": {"parts": [{"text": '["ok"]'}]}}], "usageMetadata": {"promptTokenCount": 5, "totalTokenCount": 7}})
    if "generativelanguage" in url else
    Reply(200, {"choices": [{"message": {"content": '{"items":["ok"]}'}}], "usage": {"prompt_tokens": 6, "completion_tokens": 3}})
)
tst = ai.test()
check("test() verifies both primary and fallback",
      tst["ok"] and tst["model"] == "gemini-2.5-flash" and tst["fallback_ok"] is True and tst["fallback_model"] == "openrouter/free",
      str(tst))

pub_off = ai.save("gemini", "gemini-2.5-flash", "", None, fallback_provider="")
check("clearing fallback leaves saved profile intact",
      not pub_off["fallback"]["ready"] and ai.targets() == [("gemini", "gemini-2.5-flash")]
      and pub_off["saved_profiles"]["openrouter"]["has_key"] is True)

# ---- brain.ask_ai cascading across primary (429) -> fallback (200) and multi-key error redaction
brain.time.sleep = lambda _s: None
ai.save("gemini", "gemini-2.5-flash", "", "gem-secret-key-1111",
        fallback_provider="openrouter", fallback_models="openrouter/free", fallback_key=None)
seen.clear()


def fake_cascade(url, json=None, headers=None, timeout=None, **_k):  # noqa: A002
    seen.append((url, headers or {}))
    if "generativelanguage" in url:
        return Reply(429, {"error": {"message": "Resource exhausted gem-secret-key-1111"}})
    return Reply(200, {"choices": [{"message": {"content": globals()["json_text"]({"items": [CLIP]})}}],
                       "usage": {"prompt_tokens": 40, "completion_tokens": 15}})


ai.httpx.post = fake_cascade
with ctx.as_user(u1.id):
    usage.set_context(None, "Fallback video")
    import numpy as np
    clips_fb, info_fb = brain.find_clips(words, np.zeros(60), 60.0, title="T2", uploader="U2")
    check("brain.find_clips falls back from Gemini 429 to OpenRouter",
          len(clips_fb) == 1 and clips_fb[0]["hook"] == "WOW" and info_fb["tokens"] == 55
          and "openrouter/free" in str(info_fb["model"]) and ai.problem["text"] == "",
          str((clips_fb, info_fb, ai.problem)))

ai.httpx.post = lambda url, json=None, headers=None, timeout=None, **_k: (  # noqa: A002
    Reply(429, {"error": {"message": "quota gem-secret-key-1111"}}) if "generativelanguage" in url else
    Reply(401, {"error": {"message": "invalid key or-secret-key-9999"}})
)
with ctx.as_user(u1.id):
    check("both providers failing raises RuntimeError",
          _raises(RuntimeError, lambda: brain.ask_ai("hi", brain.SCHEMA, "Fail test")))
    check("problem names fallback failure and hides both keys",
          "OpenRouter" in ai.problem["text"] and "or-secret-key-9999" not in ai.problem["text"]
          and "gem-secret-key-1111" not in ai.problem["text"], ai.problem["text"])

# ---- review fixes: boundary redaction, timeout budget, key-hint threshold, .env migration, see() model fallback
# 1) Key straddling the 300-char slice boundary must be redacted before slicing
long_err = ("x" * 292) + "or-secret-key-9999" + ("y" * 40)
ai.httpx.post = lambda url, json=None, headers=None, timeout=None, **_k: Reply(400, long_err)  # noqa: A002
try:
    ai.call("openrouter/free", "hi", brain.SCHEMA, provider_id="openrouter")
    boundary_msg = ""
except Exception as e_b:  # noqa: BLE001
    boundary_msg = str(e_b)
check("redact runs before slicing so boundary keys never leak",
      "or-secret" not in boundary_msg and "9999" not in boundary_msg and "<key>" in boundary_msg, boundary_msg)

# 2) Live timeout budget is shared across targets instead of multiplied per target
ai.save("gemini", "g-1, g-2", "", "gem-secret-key-1111",
        fallback_provider="openrouter", fallback_models="openrouter/free", fallback_key="or-secret-key-9999")
timeouts_seen = []


def fake_timeout_cascade(url, json=None, headers=None, timeout=None, **_k):  # noqa: A002
    timeouts_seen.append((url, timeout))
    if "generativelanguage" in url:
        raise ai.httpx.ReadTimeout("timed out")
    return Reply(200, {"choices": [{"message": {"content": globals()["json_text"]({"items": [CLIP]})}}],
                       "usage": {"prompt_tokens": 10, "completion_tokens": 5}})


ai.httpx.post = fake_timeout_cascade
with ctx.as_user(u1.id):
    out_to, mod_to, _tok_to = brain.ask_ai("hi", brain.SCHEMA, "Live timeout test", timeout=12.0)
    check("timeout budget is split so hung primary leaves room for fallback",
          out_to == [CLIP] and "openrouter/free" in mod_to and all(t is not None and t < 10.0 for _, t in timeouts_seen[:2]),
          str(timeouts_seen))

# 3) Key hint requires >= 12 chars uniformly, and tiny < 8-char keys are rejected
pub_short = ai.save("gemini", "g-1", "", "short12345", fallback_provider="openrouter", fallback_models="m", fallback_key="short67890")
check("keys under 12 chars hide key_hint everywhere",
      pub_short["key_hint"] == "" and pub_short["fallback"]["key_hint"] == ""
      and pub_short["saved_profiles"]["gemini"]["key_hint"] == "" and pub_short["saved_profiles"]["openrouter"]["key_hint"] == "",
      str(pub_short))
check("tiny keys under 8 chars are refused",
      _raises(ValueError, lambda: ai.save("gemini", "g-1", "", "abc")))

# 4) see() tries second model on same provider when first returns 429
ai.save("gemini", "g-rate-limited, g-vision-ok", "", "gem-secret-key-11111111", fallback_provider="")
ai.httpx.post = lambda url, json=None, headers=None, timeout=None, **_k: (  # noqa: A002
    Reply(429, {"error": {"message": "rate limit"}}) if "g-rate-limited" in url else
    Reply(200, {"candidates": [{"content": {"parts": [{"text": "NO CAM"}]}}], "usageMetadata": {"promptTokenCount": 8, "totalTokenCount": 10}})
)
pic2, _, _ = ai.see("cam?", b"\xff\xd8\xff\xd9", timeout=10.0)
check("see() falls back to second model on same provider after 429", pic2 == "NO CAM", pic2)

# 5) .env-backed settings migrate into fallback without retyping key, and fallback-only .env works in test()/brief()
(config.DATA_DIR / "ai.json").unlink(missing_ok=True)
ai._saved["at"] = None
config.AI_PROVIDER, config.AI_MODELS = "openrouter", ["openrouter/free"]
os.environ["PEAKCUT_AI_KEY"] = "env-openrouter-key-7777"
config._kept.clear()
pub_env = ai.save("gemini", "g-1", "", "gem-secret-key-11111111", fallback_provider="openrouter")
check(".env primary migrates to fallback without retyping key or model",
      pub_env["fallback"]["ready"] and pub_env["fallback"]["models"] == ["openrouter/free"] and pub_env["fallback"]["key_hint"] == "7777",
      str(pub_env))

(config.DATA_DIR / "ai.json").unlink(missing_ok=True)
ai._saved["at"] = None
config.AI_PROVIDER, config.AI_MODELS = "gemini", []
os.environ["PEAKCUT_AI_KEY"] = ""
config.AI_FALLBACK_PROVIDER, config.AI_FALLBACK_MODELS = "openrouter", ["openrouter/free"]
os.environ["PEAKCUT_AI_FALLBACK_KEY"] = "env-fb-secret-key-8888"
config._kept.clear()
ai.httpx.post = lambda url, json=None, headers=None, timeout=None, **_k: (  # noqa: A002
    Reply(200, {"choices": [{"message": {"content": '{"items":["ok"]}'}}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}})
)
with ctx.as_user(u1.id):
    usage.forget_all()
    br = usage.brief()
    tst_fb_only = ai.test()
    check("fallback-only .env works cleanly in usage.brief() and ai.test()",
          br["ready"] and br["provider"] == "OpenRouter" and tst_fb_only["ok"] and tst_fb_only["model"] == "openrouter/free",
          str((br, tst_fb_only)))

# ---- per-model connection health, independent fallback test, and fingerprint invalidation
ai.save("gemini", "g-missing, g-working", "", "gem-secret-key-11112222",
        fallback_provider="openrouter", fallback_models="openrouter/free", fallback_key="or-secret-key-33334444")
pub_init = ai.public()
check("newly saved models start as untested in checks",
      [c["state"] for c in pub_init.get("checks", [])] == ["untested", "untested", "untested"],
      str(pub_init.get("checks")))

ai.httpx.post = lambda url, json=None, headers=None, timeout=None, **_k: (  # noqa: A002
    Reply(404, {"error": {"message": "model g-missing not found"}}) if "g-missing" in url else
    Reply(200, {"candidates": [{"content": {"parts": [{"text": '["ok"]'}]}}], "usageMetadata": {"promptTokenCount": 5, "totalTokenCount": 7}})
    if "generativelanguage" in url else
    Reply(200, {"choices": [{"message": {"content": '{"items":["ok"]}'}}], "usage": {"prompt_tokens": 6, "completion_tokens": 3}})
)
res_all = ai.test(scope="all", raise_on_fail=False)
check("test(all) checks every primary and fallback model and records per-model health",
      res_all["ok"] and res_all["fallback_ok"] is True and [r["state"] for r in res_all["results"]] == ["error", "operational", "operational"]
      and all(isinstance(r["ms"], int) for r in res_all["results"]),
      str(res_all))

# Even when ALL primary models fail, fallback is still tested so the user knows backup is operational
ai.httpx.post = lambda url, json=None, headers=None, timeout=None, **_k: (  # noqa: A002
    Reply(429, {"error": {"message": "quota exhausted gem-secret-key-11112222"}}) if "generativelanguage" in url else
    Reply(200, {"choices": [{"message": {"content": '{"items":["ok"]}'}}], "usage": {"prompt_tokens": 6, "completion_tokens": 3}})
)
res_prim_down = ai.test(scope="all", raise_on_fail=False)
check("when primary is down, test(all) still verifies fallback is operational",
      res_prim_down["ok"] is False and res_prim_down["fallback_ok"] is True and res_prim_down["any_ok"] is True
      and [r["state"] for r in res_prim_down["results"]] == ["error", "error", "operational"]
      and "gem-secret-key-11112222" not in json.dumps(res_prim_down),
      str(res_prim_down))

with ctx.as_user(u1.id):
    usage.forget_all()
    br_states = usage.brief()
    check("usage.brief() exposes main_state=error and fallback_state=operational",
          (br_states.get("main_state"), br_states.get("fallback_state")) == ("error", "operational"),
          str(br_states))

# Independent fallback-only test and draft key redaction
seen.clear()
ai.httpx.post = lambda url, json=None, headers=None, timeout=None, **_k: (  # noqa: A002
    seen.append(url) or Reply(200, {"choices": [{"message": {"content": '{"items":["ok"]}'}}], "usage": {"prompt_tokens": 4, "completion_tokens": 2}})
)
res_fb = ai.test(scope="fallback", raise_on_fail=False)
check("test(scope='fallback') only contacts fallback service",
      res_fb["ok"] is True and len(res_fb["results"]) == 1 and res_fb["results"][0]["role"] == "fallback"
      and all("openrouter.ai" in u for u in seen),
      str((res_fb, seen)))

ai.httpx.post = lambda url, json=None, headers=None, timeout=None, **_k: Reply(401, {"error": {"message": "bad draft-secret-key-55556666"}})  # noqa: A002
res_draft = ai.test(scope="fallback", draft={"provider": "openrouter", "models": "openrouter/free", "key": "draft-secret-key-55556666"}, raise_on_fail=False)
check("draft test redacts unsaved draft key on failure",
      res_draft["ok"] is False and "draft-secret-key-55556666" not in json.dumps(res_draft),
      str(res_draft))

# Changing key resets health state back to untested
pub_new_key = ai.save("gemini", "g-working", "", "gem-brand-new-key-99990000",
                      fallback_provider="openrouter", fallback_models="openrouter/free", fallback_key=None)
check("changing primary key resets primary to untested while keeping unchanged fallback operational",
      [(c["role"], c["state"]) for c in pub_new_key["checks"]] == [("primary", "untested"), ("fallback", "operational")],
      str(pub_new_key["checks"]))

# ---- persistent error diagnostics (survives restart), actionable fix + detail, OpenRouter HTTP 200 error & <think>
ai.save("gemini", "gemini-3.8-flash", "", "gem-secret-key-44445555",
        fallback_provider="openrouter", fallback_models="openrouter/free", fallback_key="or-secret-key-66667777")
ai.httpx.post = lambda url, json=None, headers=None, timeout=None, **_k: (  # noqa: A002
    Reply(404, {"error": {"code": 404, "message": "models/gemini-3.8-flash is not found for API version v1beta"}})
    if "generativelanguage" in url else
    Reply(200, {"error": {"code": 429, "message": "Provider returned rate limit for openrouter/free or-secret-key-66667777"}})
)
res_diag = ai.test(scope="all", raise_on_fail=False)
r_gem = res_diag["results"][0]
r_or_err = res_diag["results"][1]
check("404 model error provides plain explanation, actionable fix, technical detail, and find_models action",
      r_gem["state"] == "error" and "gemini-3.8-flash" in r_gem.get("error", "")
      and bool(r_gem.get("fix")) and "404" in r_gem.get("detail", "")
      and r_gem.get("action") == "find_models",
      str(r_gem))
check("HTTP 200 OpenRouter error payload surfaces provider message instead of KeyError and redacts key",
      r_or_err["state"] == "error" and "KeyError" not in r_or_err.get("detail", "")
      and "rate limit" in (r_or_err.get("error", "") + " " + r_or_err.get("detail", "")).lower()
      and r_or_err.get("action") == "check_quota"
      and "or-secret-key-66667777" not in json.dumps(res_diag),
      str(r_or_err))

# Simulate full server restart: clear in-memory _health and _saved cache
ai._health.clear()
ai._saved["at"] = None
pub_after_restart = ai.public()
check("failed test states, fixes and details persist across server restart without leaking keys",
      [(c["role"], c["state"], c.get("action")) for c in pub_after_restart["checks"]] == [
          ("primary", "error", "find_models"), ("fallback", "error", "check_quota")
      ] and bool(pub_after_restart["checks"][0].get("detail")),
      str(pub_after_restart["checks"]))

# <think> reasoning tags before JSON on free models are stripped cleanly
ai.httpx.post = lambda url, json=None, headers=None, timeout=None, **_k: Reply(  # noqa: A002
    200, {"choices": [{"message": {"content": '<think>Drafting {"items":["wrong"]} first</think>\n{"items":["ok"]}'}}],
          "usage": {"prompt_tokens": 5, "completion_tokens": 5}}
)
ans_think, _, _ = ai.call("openrouter/free", "hi", {"type": "ARRAY", "items": {"type": "STRING"}}, provider_id="openrouter")
check("<think> reasoning block is ignored so final JSON answer parses cleanly",
      ans_think == ["ok"], str(ans_think))

# Gemini HTTP 200 blocked response without candidates gives actionable AIError instead of KeyError
ai.httpx.post = lambda url, json=None, headers=None, timeout=None, **_k: Reply(  # noqa: A002
    200, {"promptFeedback": {"blockReason": "SAFETY"}}
)
res_blocked = ai.test(scope="primary", raise_on_fail=False)
check("Gemini empty candidates response reports SAFETY reason instead of KeyError",
      "SAFETY" in (res_blocked["results"][0].get("detail", "") + res_blocked["results"][0].get("error", ""))
      and "KeyError" not in res_blocked["results"][0].get("detail", ""),
      str(res_blocked["results"][0]))

print("ALL PASSED" if not fails else f"{len(fails)} failed")
import shutil  # noqa: E402
db.engine.dispose()
shutil.rmtree(os.environ["PEAKCUT_DATA"], ignore_errors=True)
sys.exit(1 if fails else 0)
