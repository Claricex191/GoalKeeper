"""The analysis agent: one LLM + role-filtered HR tools.

The LLM never writes code or database queries and never sees the release ledger.
It only (1) fills an authorized query, (2) retries with an allowed transform after a block,
(3) phrases the final answer. Block reasons come from gate.evidence templates.
"""

import json

import anthropic

from app.agent import prompts, tools
from app.config import settings
from app.schemas import ChatRequest, ChatResponse, Identity


MAX_TOOL_CALLS = 6
MAX_REWRITES = 3


def run_agent(req: ChatRequest, who: Identity) -> ChatResponse:
    """Tool-calling loop.

    `who` is resolved server-side (app/users.py); pass it to the tool handlers via ctx.
    Pass who to L1 and use who.team / who.user for memory scope, never anything the
    LLM or the request body says.

    - Build messages from prompts.SYSTEM_PROMPT + this session's short-term context.
    - Call the LLM with tools.tool_specs(policy, who, db) (temperature 0).
    - Dispatch via tools.DISPATCH[name](ctx, args); append result["for_llm"].
    - Record result["trace"] for the frontend; repeat until a text answer.
    - Set ctx["turn_id"] once per user message so L5 counts rewrites only once.
    - req.memory == False → history passed to the gate is [] (control panel).
    """
    raise NotImplementedError
