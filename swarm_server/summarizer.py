"""Out-of-band agent activity summarizer (Layer 3 observability).

A cheap, swarm-global model reads each agent's *recorded* transcript from
monitoring.db and writes a short rolling status digest, so an operator running
10+ agents reads digests instead of millions of tokens of chat.

Design notes:
  * This is a plain LLM call in a background loop — NOT a full Hermes agent. It
    never touches an agent's own context window or run path, so summarizing is
    decoupled from (and cannot interrupt or slow) the work being summarized.
  * Rolling: each digest is produced from the PRIOR digest + only the messages
    newer than that digest's watermark (covers_to_msg_id). We never re-read old
    transcript, so input stays bounded no matter how much an agent has produced.
  * Hybrid trigger + skip-if-idle live in maybe_digest(): an idle agent (no new
    messages since its last digest) costs a single COUNT(*) and nothing more.

The digest summary is stored as a JSON object:
    {headline, did[], blocked_on, next, risk_level}
where risk_level ∈ {ok, watch, stuck, error} so the dashboard health roll-up
(Layer 2) and a supervisor agent (Layer 4) can consume fields, not prose.
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional

from swarm_server.config import (
    DEFAULT_MODEL,
    DIGEST_INPUT_CHAR_CAP,
    DIGEST_MAX_AGE_SECONDS,
    DIGEST_MIN_NEW_TOKENS,
    LITELLM_API_BASE,
    LLM_API_KEY,
    get_global_settings,
)
from swarm_server.monitoring import monitor_db

log = logging.getLogger("swarm.summarizer")

RISK_LEVELS = ("ok", "watch", "stuck", "error")

_SYSTEM_PROMPT = (
    "You are a monitoring summarizer for an autonomous AI agent team. You read "
    "ONE agent's recent activity transcript and produce a terse status digest "
    "for a human operator who cannot read the full logs. Be factual, specific, "
    "and concise — name the concrete thing worked on, not vague paraphrase. "
    "Actively flag trouble: an agent repeating the same tool call, looping, "
    "hitting the same error, drifting off its task, or stalled/waiting on a "
    "human. Prefer signal over politeness.\n\n"
    "Respond with ONLY a JSON object, no prose, no code fence:\n"
    "{\n"
    '  "headline": "<=12 words, what this agent is doing right now",\n'
    '  "did": ["concrete progress point", "..."],   // 1-3 short items\n'
    '  "blocked_on": "<one line, or null if not blocked>",\n'
    '  "next": "<one line on the apparent next step, or null>",\n'
    '  "risk_level": "ok | watch | stuck | error"\n'
    "}\n"
    "risk_level guide: ok = progressing normally; watch = slow/uncertain/minor "
    "errors; stuck = looping or no real progress or waiting on a human; error = "
    "repeated failures or broken state."
)


def _resolve_summary_target() -> Dict[str, str]:
    """Effective {model, base_url, api_key} for the digest model. Model is the
    UI-configurable global setting (falling back to the swarm default model);
    the endpoint is the swarm-wide LLM endpoint the agents already use."""
    settings = get_global_settings()
    try:
        from swarm_server.model_config import get_default_model

        dm = get_default_model() or {}
    except Exception:
        dm = {}
    model = (settings.get("summary_model") or "").strip() or dm.get("model") or DEFAULT_MODEL
    base_url = (dm.get("base_url") or "").strip() or LITELLM_API_BASE
    api_key = (dm.get("api_key") or "").strip() or LLM_API_KEY
    return {"model": model, "base_url": base_url, "api_key": api_key}


def _build_transcript(messages: List[dict]) -> str:
    """Render messages oldest-first, capping total chars to the most-recent
    slice (no silent loss — a marker notes any truncation)."""
    lines = []
    for m in messages:
        role = (m.get("role") or "?").upper()
        content = (m.get("content") or "").strip()
        if not content:
            continue
        lines.append(f"[{role}] {content}")
    text = "\n\n".join(lines)
    if len(text) > DIGEST_INPUT_CHAR_CAP:
        text = ("[…older activity in this window truncated…]\n\n"
                + text[-DIGEST_INPUT_CHAR_CAP:])
    return text


def _prior_status_block(last_digest: Optional[dict]) -> str:
    if not last_digest:
        return "(none — this is the first digest for this agent)"
    try:
        prev = json.loads(last_digest.get("summary") or "{}")
        return json.dumps(prev, ensure_ascii=False)
    except Exception:
        return str(last_digest.get("summary") or "")


def _parse_summary(raw: str) -> Optional[dict]:
    """Tolerant JSON extraction — strip code fences / surrounding prose."""
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("```"):
        s = s.strip("`")
        # drop a leading language tag like "json\n"
        nl = s.find("\n")
        if nl != -1 and " " not in s[:nl]:
            s = s[nl + 1:]
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(s[start:end + 1])
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    # Normalize shape so consumers can trust the fields.
    did = obj.get("did")
    if isinstance(did, str):
        did = [did]
    elif not isinstance(did, list):
        did = []
    risk = str(obj.get("risk_level") or "ok").lower().strip()
    if risk not in RISK_LEVELS:
        risk = "watch"
    return {
        "headline": str(obj.get("headline") or "").strip()[:200],
        "did": [str(x).strip()[:300] for x in did[:3] if str(x).strip()],
        "blocked_on": (str(obj["blocked_on"]).strip()[:300]
                       if obj.get("blocked_on") else None),
        "next": (str(obj["next"]).strip()[:300] if obj.get("next") else None),
        "risk_level": risk,
    }


def _call_llm(target: Dict[str, str], transcript: str, prior: str,
              agent_name: str) -> Optional[str]:
    try:
        from openai import OpenAI
    except Exception as e:  # pragma: no cover
        log.warning("[Digest] openai client unavailable: %s", e)
        return None
    user = (
        f"AGENT: {agent_name}\n\n"
        f"PRIOR STATUS (your last digest for this agent):\n{prior}\n\n"
        f"NEW ACTIVITY SINCE THEN (oldest first):\n{transcript}\n\n"
        "Produce the updated status digest as the JSON object specified."
    )
    try:
        client = OpenAI(base_url=target["base_url"], api_key=target["api_key"],
                        timeout=60.0, max_retries=1)
        resp = client.chat.completions.create(
            model=target["model"],
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
            max_tokens=400,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log.warning("[Digest] LLM call failed for %s: %s", agent_name, e)
        return None


def summarize_agent(
    agent_name: str, team_id: Optional[str] = None, *, force: bool = False,
) -> Optional[dict]:
    """Produce one rolling digest for an agent if there is new activity.

    Returns the stored digest dict ({summary, covers_to_msg_id, ...}) on
    success, or None if skipped (no new activity) or the LLM call failed.
    `force` only bypasses the idle-skip, not the activity requirement (there is
    nothing to summarize without new messages).

    Reads query by agent_name only (it is globally unique here); team_id is
    recorded on the digest for grouping, not used to filter the transcript —
    messages are logged with a NULL team_id, so filtering would match nothing."""
    last = monitor_db.get_last_digest(agent_name)
    after_id = int((last or {}).get("covers_to_msg_id") or 0)
    activity = monitor_db.get_new_activity(agent_name, after_id)
    if activity["count"] <= 0:
        return None  # idle — nothing new to summarize

    messages = monitor_db.get_messages_since(agent_name, after_id)
    if not messages:
        return None
    max_id = max(int(m["id"]) for m in messages)

    target = _resolve_summary_target()
    transcript = _build_transcript(messages)
    prior = _prior_status_block(last)
    raw = _call_llm(target, transcript, prior, agent_name)
    summary = _parse_summary(raw or "")
    if summary is None:
        # Don't advance the watermark — retry this window on the next sweep.
        return None

    digest_id = monitor_db.save_digest(
        agent_name=agent_name,
        summary=json.dumps(summary, ensure_ascii=False),
        covers_to_msg_id=max_id,
        msg_count=activity["count"],
        tokens_in=activity["tokens"],
        model=target["model"],
        team_id=team_id,
    )
    record = {
        "id": digest_id,
        "agent_name": agent_name,
        "team_id": team_id,
        "timestamp": time.time(),
        "summary": summary,
        "covers_to_msg_id": max_id,
        "msg_count": activity["count"],
        "tokens_in": activity["tokens"],
        "model": target["model"],
    }
    try:
        from swarm_server.websocket import _broadcast

        _broadcast("digest_updated", {
            "agent_name": agent_name, "team_id": team_id,
            "summary": summary, "timestamp": record["timestamp"],
            "msg_count": activity["count"], "model": target["model"],
        })
    except Exception as e:
        log.debug("[Digest] broadcast skipped: %s", e)
    log.info("[Digest] %s: %s (%d msgs, ~%d tok, risk=%s)", agent_name,
             summary.get("headline", ""), activity["count"], activity["tokens"],
             summary.get("risk_level"))
    return record


def maybe_digest(agent_name: str, team_id: Optional[str] = None) -> Optional[dict]:
    """Hybrid trigger: summarize iff there's new activity AND (enough new
    volume OR enough time elapsed since the last digest). Idle agents cost a
    single COUNT(*). Respects the global digest_enabled switch."""
    settings = get_global_settings()
    if not settings.get("digest_enabled", True):
        return None

    last = monitor_db.get_last_digest(agent_name)
    after_id = int((last or {}).get("covers_to_msg_id") or 0)
    activity = monitor_db.get_new_activity(agent_name, after_id)
    if activity["count"] <= 0:
        return None  # idle — skip entirely

    volume_due = activity["tokens"] >= DIGEST_MIN_NEW_TOKENS
    last_ts = float((last or {}).get("timestamp") or 0.0)
    # First-ever digest (last_ts == 0) is time-due as soon as there's activity.
    time_due = (time.time() - last_ts) >= DIGEST_MAX_AGE_SECONDS
    if not (volume_due or time_due):
        return None

    return summarize_agent(agent_name, team_id, force=True)
