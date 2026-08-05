import asyncio
import contextlib
import os

from claude_agent_sdk import ClaudeAgentOptions, query

from archeon.cost import is_success_result, is_terminal_result

SYSTEM_PROMPT = (
    "You link git commits to issue-tracker tickets. Reply with exactly one "
    "ticket key from the offered candidates when the commit implements or "
    "fixes that ticket, otherwise reply NONE. Output only the key or NONE."
)

# Auth vars that outrank the Claude CLI's stored login in the SDK's
# resolution order. We remove them so the spawned CLI authenticates with its
# own login (CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`, or the
# credentials from `claude login`) — i.e. subscription auth, not API billing.
_API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


@contextlib.contextmanager
def _cli_auth_env():
    saved = {k: os.environ.pop(k) for k in _API_KEY_VARS if k in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)


class AgentClassifier:
    """Cheap-tier classifier backed by the Claude Agent SDK.

    Exposes a synchronous ``ask(prompt) -> str`` so it can be injected into
    the otherwise-synchronous recovery loop. Tools are disabled and turns are
    capped at one, so the agent behaves as a single-shot text classifier.

    Authenticates via the Claude CLI's own login (subscription/OAuth), not a
    raw API key — see ``_cli_auth_env``. Because of that, any cost recorded
    into an injected ``meter`` is API-equivalent, not actually billed.
    """

    def __init__(self, model: str, system_prompt: str = SYSTEM_PROMPT,
                 max_turns: int = 1, meter=None, stage: str = "") -> None:
        self._model = model
        self._system_prompt = system_prompt
        self._max_turns = max_turns
        # Optional archeon.cost.CostMeter. None (the default) means no
        # recording and byte-for-byte the pre-cost-accounting behavior.
        self._meter = meter
        self._stage = stage

    def ask(self, prompt: str) -> str:
        return asyncio.run(self._ask(prompt))

    async def _ask(self, prompt: str) -> str:
        options = ClaudeAgentOptions(
            model=self._model,
            system_prompt=self._system_prompt,
            allowed_tools=[],
            max_turns=self._max_turns,
        )
        result = ""
        with _cli_auth_env():
            async for message in query(prompt=prompt, options=options):
                if not is_terminal_result(message):
                    continue
                # Errored terminal messages (error_max_turns /
                # error_during_execution) still carry the cost fields, so
                # they are recorded too — they spent quota. The `.result`
                # read stays gated to success, so this branch never yields
                # text for an error subtype. In production the SDK raises
                # after an is_error result instead of just ending the
                # generator (the CLI process exits non-zero and query()
                # turns that into an exception), so `ask` propagates that
                # exception rather than returning "" — the record call just
                # below has already run by the time it does.
                if self._meter is not None:
                    self._meter.record(message, self._stage, self._model)
                if is_success_result(message) and hasattr(message, "result"):
                    result = message.result or ""
        return result.strip()
