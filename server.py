"""
FastAPI backend for the Blender AI Modeling Copilot.
Session-based iterative pipeline: the agent generates code, sees viewport
screenshots, and refines across multiple iterations until satisfied.
"""

import json
import os
import time
import uuid
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from openai import OpenAI
from pydantic import BaseModel

from agent_prompt import AGENT_SYSTEM_PROMPT

from pathlib import Path
load_dotenv(Path(__file__).resolve().parent / ".env")

app = FastAPI(title="Blender AI Copilot Backend")
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

MODEL = os.getenv("COPILOT_MODEL", "gpt-5-mini")
MAX_TOKENS = int(os.getenv("COPILOT_MAX_TOKENS", "16384"))
TEMPERATURE = 1

# ── Session store ────────────────────────────────────────────────────
# Each session holds the OpenAI message list so the model keeps context
# across iterations.  Keyed by session_id (UUID string).

_sessions: dict[str, dict[str, Any]] = {}
_SESSION_TTL = 3600  # seconds


def _cleanup_sessions() -> None:
    now = time.time()
    expired = [
        sid for sid, s in _sessions.items() if now - s["last_active"] > _SESSION_TTL
    ]
    for sid in expired:
        del _sessions[sid]


# ── Request / Response models ────────────────────────────────────────


class IterateRequest(BaseModel):
    session_id: str | None = None
    prompt: str
    previous_prompt: str = ""
    scene_context: dict
    screenshot_b64: str | None = None
    exec_result: str | None = None
    iteration: int = 0
    max_iterations: int = 8
    model: str | None = None


class IterateResponse(BaseModel):
    session_id: str
    reasoning: str = ""
    code: str | None = None
    is_complete: bool = False
    status_message: str = ""


# ── Endpoints ────────────────────────────────────────────────────────


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/iterate", response_model=IterateResponse)
def iterate(req: IterateRequest):
    _cleanup_sessions()

    if req.session_id and req.session_id in _sessions:
        session = _sessions[req.session_id]
        session["last_active"] = time.time()
        is_new_request = req.iteration == 0
        _append_user_turn(session["messages"], req, is_followup=is_new_request)
    else:
        session_id = uuid.uuid4().hex
        messages = [{"role": "system", "content": AGENT_SYSTEM_PROMPT}]
        _append_user_turn(messages, req, is_first=True)
        session = {
            "id": session_id,
            "messages": messages,
            "last_active": time.time(),
        }
        _sessions[session_id] = session

    _trim_old_images(session["messages"], keep_recent=3)

    try:
        assistant_msg = _call_llm(session["messages"], model=req.model)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"LLM error: {exc}")

    session["messages"].append({"role": "assistant", "content": assistant_msg})

    parsed = _parse_response(assistant_msg)

    return IterateResponse(
        session_id=session["id"],
        reasoning=parsed.get("reasoning", ""),
        code=parsed.get("code"),
        is_complete=parsed.get("is_complete", False),
        status_message=parsed.get("status_message", ""),
    )


# ── Helpers ──────────────────────────────────────────────────────────


def _append_user_turn(
    messages: list[dict],
    req: IterateRequest,
    *,
    is_first: bool = False,
    is_followup: bool = False,
) -> None:
    """Build a user message with text only and append it.

    is_first:    brand-new session, no prior context.
    is_followup: existing session but a NEW user request (second Generate click).
    neither:     mid-iteration turn within the same Generate run.
    """
    parts: list[str] = []

    if is_first:
        parts.append(f"USER REQUEST:\n{req.prompt}")
    elif is_followup:
        parts.append("─── FOLLOW-UP REQUEST ───")
        if req.previous_prompt:
            parts.append(f"Previous request was: \"{req.previous_prompt}\"")
        parts.append(f"NEW USER REQUEST:\n{req.prompt}")
        parts.append(
            "IMPORTANT: This is a modification of the existing scene. "
            "Read scene_context carefully. Only change what the user asked for. "
            "Use object names and semantic_role fields to identify targets."
        )
    else:
        result_note = req.exec_result or "success"
        parts.append(f"Code execution result: {result_note}")

    parts.append(
        f"\nCURRENT SCENE STATE:\n{json.dumps(req.scene_context, indent=2)}"
    )
    parts.append(f"\nITERATION: {req.iteration} of {req.max_iterations}")

    if req.iteration >= req.max_iterations - 1:
        parts.append(
            "\n⚠ This is the LAST iteration. Finalise now — set is_complete to true."
        )

    text_block = "\n".join(parts)
    messages.append({"role": "user", "content": text_block})


def _trim_old_images(messages: list[dict], keep_recent: int = 3) -> None:
    """
    Replace image_url blocks in older user messages with a text placeholder
    to keep the token count manageable.  Only the *keep_recent* most recent
    user messages retain their screenshots.
    """
    user_indices = [i for i, m in enumerate(messages) if m["role"] == "user"]
    cutoff = user_indices[-keep_recent] if len(user_indices) > keep_recent else -1

    for idx in user_indices:
        if idx >= cutoff:
            continue
        msg = messages[idx]
        if not isinstance(msg["content"], list):
            continue
        new_content = []
        for part in msg["content"]:
            if isinstance(part, dict) and part.get("type") == "image_url":
                new_content.append(
                    {"type": "text", "text": "[screenshot omitted for brevity]"}
                )
            else:
                new_content.append(part)
        msg["content"] = new_content


def _call_llm(messages: list[dict], model: str | None = None) -> str:
    response = client.chat.completions.create(
        model=model or MODEL,
        temperature=TEMPERATURE,
        max_completion_tokens=MAX_TOKENS,
        response_format={"type": "json_object"},
        messages=messages,
    )
    return response.choices[0].message.content


def _parse_response(raw: str) -> dict:
    """Parse the JSON response from the agent, tolerating minor issues."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Fallback: try to extract JSON from markdown fences
    import re

    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    return {
        "reasoning": "Failed to parse agent response.",
        "code": None,
        "is_complete": True,
        "status_message": "Error: could not parse agent response.",
    }


# ── Run ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
