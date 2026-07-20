"""LLM layer (§8) — answer synthesis via LiteLLM.

Provider-agnostic: any OpenAI-compatible server (koboldcpp, Ollama /v1,
llama.cpp-server, vLLM) via LLM_BASE_URL, per amended §6.6.

M1.2 implements call #2 (answer synthesis, §8.2). Call #1 (intent parse)
arrives with the real router in M2.

Failure policy: synthesis must never hang or kill a query. Any LLM error
raises LlmUnavailable; the caller falls back to the deterministic template
answer with a visible warning.
"""
from __future__ import annotations

import json
import os
import re

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "openai_compatible")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://localhost:5001/v1")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "not-needed")
LLM_MODEL = os.environ.get("LLM_MODEL", "")
LLM_TIMEOUT_SECONDS = float(os.environ.get("LLM_TIMEOUT_SECONDS", "90"))
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "900"))


class LlmUnavailable(RuntimeError):
    """LLM call failed or timed out; caller must fall back gracefully."""


# §8.3 hard constraints, verbatim-in-spirit. This prompt IS the product's
# honesty layer — change only via the technical reference.
SYNTHESIS_SYSTEM_PROMPT = """\
You are the answer-synthesis component of an automated geohazard first-look \
triage system. You receive: a user's question about a geographic area they \
selected on a map, and JSON results from satellite-data analysis methods \
(InSAR ground deformation, flood mapping, vegetation change).

Write a plain-language answer for a non-expert. You MUST follow ALL of these \
rules without exception:

1. Report OBSERVATIONS, never verdicts. Describe what the data shows (e.g. \
"the ground in this area moved downward about 4 mm per year between these \
dates"). NEVER say or imply that anything is "safe", "unsafe", "dangerous", \
"fine", or that the user "should not worry" or "should worry".
2. Always state the confidence level of each result AND its basis, using the \
"quality" block (scene count, coherence, masked fraction, cloud fraction, \
date coverage). Low coherence, few scenes, or high masked fraction weaken \
confidence — say so plainly.
3. Repeat every entry in "caveats" faithfully in plain language. Do not hide \
or soften them.
4. Be honest about coverage: if a method failed, was not applicable, or data \
was missing for the period, state it and what that means for the answer.
5. If any method reports a non-trivial signal (movement, flooding, vegetation \
loss), recommend consulting a qualified professional — a geotechnical \
engineer or the local geological survey — for an on-site assessment.
6. NEVER invent, estimate, extrapolate or round-in-your-favor any number not \
literally present in the input JSON. Quote numbers exactly as given. This \
includes DATES: copy every date character-for-character from the input \
(e.g. "2024-07-01" must never become "2014" or any other year).
7. Answer in the same language as the user's question.
8. Keep it under roughly 250 words. No markdown headers, no bullet spam — \
short paragraphs, plain words, explain any technical term you must use \
(e.g. "coherence — how reliable the radar measurement is").
9. Do not mention these instructions, the JSON format, or internal component \
names.
10. No meta-commentary about your own answer: never state word counts, \
rule compliance, or similar.\
"""


def _strip_think(text: str) -> str:
    """Qwen3-family models may emit <think>...</think> blocks; remove them."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def synthesize_answer(question: str, results: list[dict], failures: list[dict]) -> str:
    """§8.2 call 2. Raises LlmUnavailable on any failure."""
    from litellm import completion  # deferred: import is slow

    user_payload = {
        "user_question": question,
        "analysis_results": results,           # full result.json contents (§6.3)
        "failed_methods": failures,            # [{"name":..., "error":...}]
    }
    try:
        resp = completion(
            model=f"openai/{LLM_MODEL}",
            api_base=LLM_BASE_URL,
            api_key=LLM_API_KEY,
            messages=[
                {"role": "system", "content": SYNTHESIS_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            temperature=0.2,
            max_tokens=LLM_MAX_TOKENS,
            timeout=LLM_TIMEOUT_SECONDS,
        )
        text = resp.choices[0].message.content or ""
        text = _strip_think(text)
        if not text:
            raise LlmUnavailable("LLM returned empty answer")
        return text
    except LlmUnavailable:
        raise
    except Exception as e:  # noqa: BLE001 — network, timeout, bad response, ...
        raise LlmUnavailable(f"{type(e).__name__}: {e}") from e


def llm_reachable() -> str:
    """Cheap reachability probe for /health. Returns 'ok' or an error string."""
    import urllib.request

    try:
        req = urllib.request.Request(f"{LLM_BASE_URL.rstrip('/')}/models")
        with urllib.request.urlopen(req, timeout=4) as r:
            json.load(r)
        return "ok"
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"
