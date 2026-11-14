"""The analysis agent: one LLM + role-filtered HR tools.

The LLM never writes code or database queries and never sees the release ledger.
It only (1) fills an authorized query, (2) retries with an allowed transform after a block,
(3) phrases the final answer. Block reasons come from gate.evidence templates.
"""

import json
import uuid

import anthropic

from app.agent import prompts, tools
from app.config import settings
from app.db import get_db
from app.policy import store
from app.schemas import ChatRequest, ChatResponse, Identity


MAX_TOOL_CALLS = 6
MAX_REWRITES = 3
MODEL = settings.llm_model or "claude-haiku-5-5"
_client = anthropic.Anthropic(api_key=settings.llm_api_key)

SESSIONS: dict[tuple, list[dict]] = {}


def run_agent(req: ChatRequest, who: Identity) -> ChatResponse:
    """Tool-calling loop.

    `who` is resolved server-side (app/users.py); pass it to the tool handlers via ctx.
    Pass who to L1 and use who.team / who.user for memory scope, never anything the
    LLM or the request body says.

    - Build messages from prompts.system_prompt(who, policy) + this session's short-term context.
    - Call the LLM with tools.tool_specs(policy, who, db) (temperature 0).
    - Dispatch via tools.DISPATCH[name](ctx, args); append result["for_llm"].
    - Record result["trace"] for the frontend; repeat until a text answer.
    - Set ctx["turn_id"] once per user message so L5 counts rewrites only once.
    - req.memory == False → history passed to the gate is [] (control panel).
    """
    db = get_db()
    policy = store.load_active(db, "ibm_hr")
    ctx = {"db": db, "who": who, "project": req.project, "session_id": req.session_id,
           "memory": req.memory, "policy": policy,
           "turn_id": uuid.uuid4().hex}                 # L5
    specs = tools.tool_specs(policy, who, db)

    key = (who.user, req.session_id, req.memory)
    history = SESSIONS.setdefault(key, [])
    messages = history + [{"role": "user", "content": req.message}]

    trace, calls, blocks = [], 0, 0
    reply = ""
    while True:
        out_of_budget = calls >= MAX_TOOL_CALLS or blocks > MAX_REWRITES
        try:
            # a role with no HR tools (e.g. employee) gets no tools at all
            tool_args = ({"tools": specs,
                          "tool_choice": {"type": "none" if out_of_budget else "auto"}}
                         if specs else {})
            resp = _client.messages.create(
                model=MODEL, max_tokens=1024,
                system=prompts.system_prompt(who, policy),
                output_config={"effort": "low"},
                messages=messages,
                **tool_args,
            )
        except anthropic.APIError as e:
            reply = f"The assistant is unavailable right now ({type(e).__name__})."
            break

        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason != "tool_use":
            reply = "".join(b.text for b in resp.content if b.type == "text").strip()
            break

        results = []
        for b in resp.content:
            if b.type != "tool_use":
                continue
            calls += 1
            handler = tools.DISPATCH.get(b.name)
            if handler is None:
                out = {"for_llm": {"status": "error", "message": f"unknown tool {b.name}"},
                       "trace": {"tool": b.name, "status": "error"}}
            else:
                out = handler(ctx, b.input)
            trace.append(out["trace"])
            if out["trace"].get("status") == "blocked":
                blocks += 1
            results.append({"type": "tool_result", "tool_use_id": b.id,
                            "content": json.dumps(out["for_llm"], default=str)})
        messages.append({"role": "user", "content": results})

    # text only
    history += [{"role": "user", "content": req.message},
                {"role": "assistant", "content": reply or "(no answer)"}]
    del history[:-20]                                   # only keep the last 10 rounds of chats
    return ChatResponse(reply=reply, trace=trace)
