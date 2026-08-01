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
10. Never add meta-commentary: no word counts, no mention of rules or \
instructions, no introductory sentence about what you are about to write. \
The first sentence of your reply must already be about the geographic area \
or the data.\
"""


def _strip_think(text: str) -> str:
    """Qwen3-family models may emit <think>...</think> blocks; remove them."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


_META_LINE = re.compile(
    r"(?i)^\W*(word count|total words?|word total|всего\s+\d+\s+слов)"
)
_PREAMBLE = re.compile(r"(?i)(rules?|instructions?|plain-language answer)")
_PARROT = re.compile(
    r"(?i)^\W*here('s| is)\b.*(answer|substance|response)|^\W*(sure|certainly|of course)[,.!]"
)


# Lines that begin a meta / self-critique / rule-grading block. Everything
# from the FIRST such line to the end is dropped — a small local model
# sometimes appends a rubric self-evaluation after the real answer (observed
# live M3.2: a "### Critique:" section quoting the rules). None of these
# markers occurs in a legitimate geohazard answer.
_META_SECTION = re.compile(
    r"(?i)^\s*(#{1,6}\s*)?("
    r"critique|self-?(critique|assessment|evaluation|review)|"
    r"rule\s*\d|let me (check|re-?read|verify|re-?check)|"
    r"i think the critique|in your response,? you|you (violated|satisfied|met|followed)|"
    r"wait,? the critique|checking (the )?rules?)\b"
)


def _truncate_meta(lines: list[str]) -> list[str]:
    for i, ln in enumerate(lines):
        if _META_SECTION.match(ln):
            return lines[:i]
    return lines


def _dedup(lines: list[str]) -> list[str]:
    """Collapse a looped answer: if the first substantial line recurs verbatim
    later, the model restarted its answer — cut at the recurrence (observed
    live M3.2/M3.4: the answer repeated ~4x with 'Here is the...' splices)."""
    anchor_idx = next((i for i, ln in enumerate(lines) if len(ln.strip()) >= 30), None)
    if anchor_idx is None:
        return lines
    anchor = lines[anchor_idx].strip()
    for j in range(anchor_idx + 1, len(lines)):
        if lines[j].strip() == anchor:
            return lines[:j]
    return lines


def _sanitize(text: str) -> str:
    """Deterministic backstop for §8.3 rules 9-10: small local models leak
    meta-commentary despite the prompt (observed live M2.3/M3.2). Strips
    word-count lines anywhere, leading rule/parrot preamble, and truncates any
    trailing self-critique / rule-grading section."""
    lines = [ln for ln in text.splitlines() if not _META_LINE.search(ln)]
    lines = _dedup(lines)
    lines = _truncate_meta(lines)
    # strip leading preamble / parrot lines
    while lines and (
        (_PREAMBLE.search(lines[0]) and lines[0].rstrip().endswith(":"))
        or _PARROT.match(lines[0])
    ):
        lines.pop(0)
    # strip trailing blank / preamble / parrot lines (e.g. a dangling
    # "Here is the plain-language answer:" left by a truncated loop)
    while lines and (
        not lines[-1].strip()
        or _PARROT.match(lines[-1])
        or (_PREAMBLE.search(lines[-1]) and lines[-1].rstrip().endswith(":"))
    ):
        lines.pop()
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines).strip()


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
        text = _sanitize(_strip_think(text))
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


# ====================================================================== M2.1
# LLM call #1: intent parsing (§5.2 responsibility 1, §8.2 call 1).
# Router policy: LLM first; on failure/low confidence the caller applies
# keyword rules; if still empty -> needs_clarification (a valid outcome).

INTENT_CONFIDENCE_THRESHOLD = 0.5

INTENT_SYSTEM_PROMPT = """\
You classify a user's question about satellite-observable ground hazards for \
a map area they selected. The question may be in ANY language.

Respond with ONLY a JSON object — no prose, no markdown, no code fences:
{"hazard_types": [...], "confidence": <number 0.0-1.0>}

hazard_types is a subset of exactly these strings:
- "deformation": ground movement, subsidence, sinking, uplift, landslides, \
slope instability, cracks appearing in buildings or ground
- "flood": flooding, inundation, standing water, water extent
- "vegetation": vegetation loss or change, deforestation, bare soil \
exposure, crops/greenery disappearing

Include every hazard the question plausibly asks about. A general "is this \
area dangerous / что тут с грунтом" style question about ground conditions \
means ["deformation"]. If the question is unrelated to these hazards or too \
vague to classify, return {"hazard_types": [], "confidence": 0.0}.\
"""


class IntentParseFailed(RuntimeError):
    """LLM intent parse failed after retry; caller must use rule fallback."""


def _extract_json(text: str) -> dict:
    """Tolerate models wrapping JSON in prose/fences: take the first {...}."""
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not m:
        raise ValueError(f"no JSON object in: {text[:120]!r}")
    return json.loads(m.group(0))


def _validate_intent(obj: dict) -> tuple[list[str], float]:
    allowed = {"deformation", "flood", "vegetation"}
    hazards = obj.get("hazard_types")
    conf = obj.get("confidence")
    if not isinstance(hazards, list) or not all(h in allowed for h in hazards):
        raise ValueError(f"bad hazard_types: {hazards!r}")
    if not isinstance(conf, (int, float)) or not (0.0 <= float(conf) <= 1.0):
        raise ValueError(f"bad confidence: {conf!r}")
    # dedupe, keep order
    seen: list[str] = []
    for h in hazards:
        if h not in seen:
            seen.append(h)
    return seen, float(conf)


def parse_intent(question: str) -> tuple[list[str], float]:
    """Returns (hazard_types, confidence). Raises IntentParseFailed.
    Temperature 0, JSON-only, schema-validated, one retry (§8.2 call 1)."""
    from litellm import completion

    last_err: Exception | None = None
    for attempt in (1, 2):
        try:
            resp = completion(
                model=f"openai/{LLM_MODEL}",
                api_base=LLM_BASE_URL,
                api_key=LLM_API_KEY,
                messages=[
                    {"role": "system", "content": INTENT_SYSTEM_PROMPT},
                    {"role": "user", "content": question},
                ],
                temperature=0.0,
                max_tokens=200,
                timeout=min(LLM_TIMEOUT_SECONDS, 30.0),
            )
            text = _strip_think(resp.choices[0].message.content or "")
            return _validate_intent(_extract_json(text))
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise IntentParseFailed(f"{type(last_err).__name__}: {last_err}")


# Keyword-rule fallback (§5.2). Substring match, lowercased; covers the
# target audience's likely languages (en/ru + a few hy stems).
_RULE_KEYWORDS: dict[str, list[str]] = {
    "deformation": [
        "moving", "movement", "subsid", "sink", "landslide", "slope", "crack",
        "deform", "uplift", "settle",
        "двиг", "движ", "просед", "оседа", "провал", "оползн", "оползень",
        "трещин", "грунт", "смещ",
        "շարժ", "սողանք", "նստվածք", "ճաք",
    ],
    "flood": [
        "flood", "inundat", "standing water", "water extent", "submerg",
        "наводн", "затопл", "подтопл", "паводок", "разлив",
        "ջրհեղեղ", "հեղեղ",
    ],
    "vegetation": [
        "vegetation", "deforest", "bare soil", "trees", "forest loss", "ndvi",
        "greenery", "crop",
        "растительн", "вырубк", "обезлес", "лес исчез", "оголени", "посев",
        "բուսական", "անտառ",
    ],
}


def rule_intent(question: str) -> list[str]:
    q = question.lower()
    hits = [h for h, kws in _RULE_KEYWORDS.items() if any(k in q for k in kws)]
    return hits


CLARIFICATION_TEXT = (
    "I couldn't tell which hazard you're asking about. Please rephrase, "
    "mentioning one of: ground movement / subsidence / landslides, "
    "flooding, or vegetation loss.\n\n"
    "Не удалось понять, о какой опасности вы спрашиваете. Уточните, "
    "пожалуйста: движение/проседание грунта или оползни, наводнение, "
    "или потеря растительности."
)
