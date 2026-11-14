"""The analysis agent: one LLM + two tools, nothing more.

The LLM never writes code or database queries and never sees the release ledger.
It only (1) fills StructuredQuery, (2) retries with an allowed transform after a block,
(3) phrases the final answer. Block reasons come from gate.evidence templates.
"""
from app.schemas import ChatRequest, ChatResponse

MAX_TOOL_CALLS = 6
MAX_REWRITES = 3


def run_agent(req: ChatRequest) -> ChatResponse:
    """Tool-calling loop.

    - Build messages from prompts.SYSTEM_PROMPT + this session's short-term context.
    - Call the LLM with tools.TOOL_SPECS (temperature 0).
    - Dispatch tool calls to tools.handle_*; append results; repeat until a text answer.
    - Record every tool call + decision in `trace` for the frontend.
    - req.memory == False → history passed to the gate is [] (control panel).
    """
    raise NotImplementedError
