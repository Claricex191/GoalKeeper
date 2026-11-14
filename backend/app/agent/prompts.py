"""System prompt for the analysis agent.

TODO (Clarice + Qi Jia): write the prompt. It should cover:
- Role: HR analytics assistant for managers.
- Available fields and allowed values (generated from schemas whitelists).
- Always answer via the `query_hr` tool; never guess numbers.
- Before running, restate the understood query in one line ("I'll look up: ...").
- If a tool result is "blocked": do not argue; try one of `allowed_transforms`,
  closest to the original question first; after 3 failures call `request_review`.
- When cells are suppressed, say so plainly; never speculate about suppressed values.
- Use the provided evidence text verbatim when explaining a block.
"""

SYSTEM_PROMPT = """TODO"""
