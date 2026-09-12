"""AI orchestrator connecting the chat UI to the MCP server and LLM.

Takes a natural-language question from the user (e.g. "what's the mass of
this part?" or "estimate the cost"), sends it to a local LLM via Ollama,
lets the model call MCP tools exposed by mcp_server.py to fetch live CAD
data, and returns a natural-language answer. Contains no CAD-specific
logic itself — all CAD access is mediated through the MCP tool layer,
which in turn is mediated through the CadAdapter interface. This module
never imports a CadAdapter subclass directly.
"""

import asyncio
import json
import logging
import os
import re
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolResult
from ollama import AsyncClient

from production_cost import PROCESSES

DEFAULT_MODEL = "qwen2.5:7b-instruct"
MAX_TOOL_CALL_ROUNDS = 8
_MAX_EMPTY_RESPONSE_RETRIES = 3

# Logs the exact tool name + full argument dict the model decided to send,
# for every tool call, right before it's executed against the live MCP
# session -- added specifically to diagnose live reports of follow-up
# questions ("what about with Injection Molding instead?", "for 10 units")
# silently landing on the wrong process/quantity. Goes to both the console
# (for `python desktop_app.py`/`streamlit run` foreground output) and
# tool_call_log.jsonl (one JSON object per line, so a session run
# elsewhere -- e.g. inside Streamlit's own process -- can still be
# inspected afterward without needing console scrollback).
_TOOL_CALL_LOGGER = logging.getLogger("cad_ai_copilot.tool_calls")
_TOOL_CALL_LOGGER.setLevel(logging.INFO)
if not _TOOL_CALL_LOGGER.handlers:
    _console_handler = logging.StreamHandler()
    _console_handler.setFormatter(logging.Formatter("%(message)s"))
    _TOOL_CALL_LOGGER.addHandler(_console_handler)

    _file_handler = logging.FileHandler(
        Path(__file__).parent / "tool_call_log.jsonl", encoding="utf-8"
    )
    _file_handler.setFormatter(logging.Formatter("%(message)s"))
    _TOOL_CALL_LOGGER.addHandler(_file_handler)
    _TOOL_CALL_LOGGER.propagate = False


class OllamaUnavailableError(RuntimeError):
    """Raised when the local Ollama server can't be reached."""


class McpServerUnavailableError(RuntimeError):
    """Raised when the mcp_server.py subprocess can't be started or reached."""


def _format_list_assembly_components_answer(tool_result_json: str) -> str | None:
    """Turn list_assembly_components' JSON straight into a plain-language
    answer, without asking the LLM to re-express it.

    Why this exists: observed live with qwen2.5:7b-instruct that the model
    reliably picks the right tool for this question, but then either loops
    re-requesting a cost tool nobody asked for, returns a genuinely empty
    response, or -- worst -- hallucinates a fabricated answer unrelated to
    the real tool data (fake part names, fake report paths) instead of
    just reading the JSON that's already in its context. Since this tool's
    output is already exactly the shape a "list the components" answer
    needs, formatting it in plain Python is strictly more reliable than
    routing it back through a 7B model for restating.

    Returns None (caller falls through to the normal LLM path) if the
    JSON doesn't parse or found=False, so the "not an assembly" case still
    gets a model-composed answer from its message field.
    """
    try:
        data = json.loads(tool_result_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or not data.get("found"):
        return None

    components = data.get("components", [])
    sub_assembly_count = data.get("sub_assembly_count")
    sub_assembly_names = data.get("sub_assembly_names") or []
    sub_assembly_bit = (
        f", {sub_assembly_count} sub-assembly/assemblies "
        f"({', '.join(sub_assembly_names)})"
        if sub_assembly_count
        else ""
    )
    lines = [
        f"This assembly has {data.get('unique_part_count', len(components))} "
        f"unique component(s), {data.get('total_instance_count', '?')} "
        f"total instance(s){sub_assembly_bit}:",
        "",
    ]
    for c in components:
        material = c.get("material") or {}
        material_name = material.get("name", "unknown") if isinstance(material, dict) else material
        mass = c.get("mass_kg")
        mass_str = f"{mass:.3f} kg each" if isinstance(mass, (int, float)) else "mass unknown"
        lines.append(
            f"- {c.get('part_name', 'unnamed')}: quantity {c.get('quantity', '?')}, "
            f"{mass_str}, material {material_name}"
        )
    return "\n".join(lines)


def _format_bom_rows(rows: list[dict]) -> list[str]:
    lines = []
    for row in rows:
        mass = row.get("total_mass_kg")
        mass_str = f"{mass:.3f} kg total" if isinstance(mass, (int, float)) else "mass unknown"
        cost = row.get("total_cost_inr")
        cost_str = f"Rs {cost:,.2f} total" if isinstance(cost, (int, float)) else "cost unknown"
        lines.append(
            f"- {row.get('part_name', 'unnamed')} x{row.get('quantity_per_assembly', '?')} "
            f"({row.get('classification', '?')}): {mass_str}, {cost_str}"
        )
    return lines


def _format_missing_data(missing_data: list[dict]) -> list[str]:
    if not missing_data:
        return []
    lines = ["", "Missing data:"]
    for item in missing_data:
        lines.append(f"- {item.get('part_name', 'unnamed')}: {item.get('reason', 'unknown reason')}")
    return lines


def _format_get_assembly_bom_answer(tool_result_json: str) -> str | None:
    """Turn get_assembly_bom's JSON straight into a plain-language answer,
    without asking the LLM to re-express it.

    Same rationale as _format_list_assembly_components_answer: observed
    live with qwen2.5:7b-instruct that this tool's larger, nested JSON
    (bom rows + totals + missing_data) is where the model reliably fails
    to restate correctly -- looping on repeat calls until the dedup guard
    fires, or fabricating entirely fictitious parts/costs instead of
    reading the real data sitting in its context. Formatting it in plain
    Python sidesteps that restating step entirely.
    """
    try:
        data = json.loads(tool_result_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or not data.get("found"):
        return None

    totals = data.get("totals", {})
    lines = [
        f"Bill of Materials for {data.get('manufacturing_process', '?')}, "
        f"quantity {data.get('quantity', '?')}:",
        "",
        *_format_bom_rows(data.get("bom", [])),
        "",
        f"Totals: {totals.get('unique_part_count', '?')} unique part(s), "
        f"{totals.get('total_instance_count', '?')} total instance(s), "
        f"{totals.get('total_assembly_mass_kg', '?')} kg, "
        f"Rs {totals.get('total_assembly_cost_one_unit_inr', '?')} per assembly, "
        f"Rs {totals.get('total_assembly_cost_for_quantity_inr', '?')} for "
        f"{data.get('quantity', '?')} assemblies.",
    ]
    lines.extend(_format_missing_data(data.get("missing_data", [])))
    return "\n".join(lines)


def _format_get_assembly_cost_drivers_answer(tool_result_json: str) -> str | None:
    """Same rationale/pattern as _format_get_assembly_bom_answer, for the
    cost-drivers tool's near-identical JSON shape (rows under
    "cost_drivers" instead of "bom", already pre-sorted highest-cost-first
    by the tool itself)."""
    try:
        data = json.loads(tool_result_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or not data.get("found"):
        return None

    totals = data.get("totals", {})
    lines = [
        f"Cost drivers for {data.get('manufacturing_process', '?')}, "
        f"quantity {data.get('quantity', '?')} (highest cost first):",
        "",
        *_format_bom_rows(data.get("cost_drivers", [])),
        "",
        f"Totals: Rs {totals.get('total_assembly_cost_one_unit_inr', '?')} per assembly, "
        f"Rs {totals.get('total_assembly_cost_for_quantity_inr', '?')} for "
        f"{data.get('quantity', '?')} assemblies.",
    ]
    lines.extend(_format_missing_data(data.get("missing_data", [])))
    return "\n".join(lines)


def _format_missing_manufacturing_process_answer(tool_result_json: str) -> str | None:
    """Ask the user for a manufacturing process directly, in plain Python,
    instead of leaving that decision to the LLM's next round.

    Every cost-related tool (estimate_cost, get_assembly_bom,
    get_assembly_cost_drivers, export_bom) returns this exact found=False
    shape when called without a valid manufacturing_process, with a
    message instructing the model to ask the user then retry. Observed
    live: when the question didn't mention a process, the model
    sometimes re-calls the same tool with the same (missing) argument
    instead of asking -- which trips the dedup guard, and the model then
    produces a garbled non-answer ("it seems the previous call already
    provided the necessary information... could you specify which tool")
    because it's being told to "answer using that result" when the
    result was actually just an error asking for more input, not real
    data. Short-circuiting this specific found=False shape into a clean
    question is strictly more reliable than hoping the model asks
    correctly on its own.
    """
    try:
        data = json.loads(tool_result_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("found") is not False:
        return None
    message = data.get("message", "")
    if "manufacturing_process must be one of" not in message:
        return None
    return (
        "Which manufacturing process should I assume for this part/"
        "assembly's cost estimate -- CNC Machining, Injection Molding, "
        "or Sheet Metal?"
    )


# Tool name -> formatter, for tools whose output is reliable to render
# directly rather than routing back through the LLM. See
# _format_list_assembly_components_answer's docstring for why this exists.
_DETERMINISTIC_ANSWER_FORMATTERS = {
    "list_assembly_components": _format_list_assembly_components_answer,
    "get_assembly_bom": _format_get_assembly_bom_answer,
    "get_assembly_cost_drivers": _format_get_assembly_cost_drivers_answer,
}

_ASSEMBLY_REDIRECT_MARKER = "get_assembly_bom() or get_assembly_cost_drivers() instead"


def _is_estimate_cost_assembly_redirect(tool_result_json: str) -> bool:
    """True if estimate_cost() just refused because the open document is an
    assembly (see mcp_server.py's estimate_cost -- it returns found=False
    with this exact message rather than reading a nonexistent root-level
    material/mass).

    Why this needs its own check instead of just letting the model read
    the message and retry: observed live with qwen2.5:7b-instruct, told
    "call get_assembly_bom() ... with the same manufacturing_process/
    quantity", it does call get_assembly_bom() next -- but silently swaps
    in a different manufacturing_process (e.g. reverting Injection Molding
    back to CNC Machining) and/or a fabricated quantity (e.g. 1000 instead
    of the 10 actually requested), producing a confidently wrong answer
    instead of an error. Since the correct retry arguments are just
    "the exact arguments this failed call was given", the orchestrator can
    make that call itself and skip trusting the model to copy them
    correctly.
    """
    try:
        data = json.loads(tool_result_json)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(data, dict) or data.get("found") is not False:
        return False
    return _ASSEMBLY_REDIRECT_MARKER in data.get("message", "")


def _format_deterministic_answer(tool_name: str, tool_result_json: str) -> str | None:
    # Checked for every cost-capable tool regardless of name -- see
    # _format_missing_manufacturing_process_answer's docstring.
    missing_process = _format_missing_manufacturing_process_answer(tool_result_json)
    if missing_process is not None:
        return missing_process
    formatter = _DETERMINISTIC_ANSWER_FORMATTERS.get(tool_name)
    if formatter is None:
        return None
    return formatter(tool_result_json)

SYSTEM_PROMPT = (
    "You are a CAD design copilot. You answer questions about the CAD "
    "model that is currently open, using the tools provided. Always call "
    "a tool to fetch part data rather than guessing or relying on prior "
    "conversation turns for numbers. Keep answers concise, cite units, "
    "and mention when an estimate (cost, carbon, DFM) is a rough "
    "approximation rather than an authoritative figure. "
    "Only call the tools needed to answer exactly what was asked -- do "
    "NOT follow up a successful tool call with a cost/pricing/BOM tool "
    "'to be thorough' unless the user's question actually asked about "
    "cost, price, or manufacturing process. Once a tool has given you "
    "enough information to answer the question, stop calling tools and "
    "answer directly."
)


class AiOrchestrator:
    """Bridges the chat UI, a local LLM (via Ollama), and CAD data (via MCP).

    Owns an MCP client session connected to mcp_server.py — which in turn
    wraps whichever CadAdapter is configured — and a chat session with a
    local Ollama model. Tool definitions from MCP are handed to the LLM;
    when the model requests a tool call, this class executes it against
    the live MCP session and feeds the result back, looping until the
    model produces a final natural-language answer.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        mcp_server_path: str = "mcp_server.py",
    ) -> None:
        self._model = model
        # mcp's stdio_client only forwards a small, fixed allowlist of
        # environment variables to the subprocess by default (a security
        # measure against leaking secrets) -- CAD_ADAPTER isn't in that
        # allowlist, so it has to be passed through explicitly here or
        # mcp_server.py always falls back to its own default (SolidWorks),
        # ignoring CAD_ADAPTER=mock set on the parent process.
        server_env = (
            {"CAD_ADAPTER": os.environ["CAD_ADAPTER"]}
            if "CAD_ADAPTER" in os.environ
            else None
        )
        self._server_params = StdioServerParameters(
            command=sys.executable, args=[mcp_server_path], env=server_env
        )
        self._exit_stack = AsyncExitStack()
        self._session: ClientSession | None = None
        self._ollama = AsyncClient()
        self._tools_schema: list[dict] = []
        self._history: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        # Last `quantity`/`manufacturing_process` used in a *successful*
        # (found=True) cost-related tool call this session, and the
        # question text that triggered the tool call currently being
        # processed -- see _carry_over_unstated_params's docstring for why
        # these exist.
        self._last_quantity: int | None = None
        self._last_manufacturing_process: str | None = None
        self._current_question: str = ""
        # The name and parsed found=True JSON of the last successful
        # cost/BOM tool call made while answering the CURRENT ask() turn --
        # reset at the top of every ask() call. Lets a caller that wants
        # more than the final prose answer (e.g. a UI rendering a real
        # table instead of text lines) get at the same structured data the
        # deterministic text formatters already read, without re-parsing
        # the prose back into numbers. None for turns that never called a
        # cost/BOM tool, or where the call didn't succeed.
        self.last_structured_tool: str | None = None
        self.last_structured_result: dict | None = None

    async def start(self) -> None:
        """Launch the MCP server subprocess and fetch its tool definitions.

        Must be called (and awaited) before the first call to ask().

        Raises:
            McpServerUnavailableError: the mcp_server.py subprocess couldn't
                be launched or wouldn't complete the MCP handshake (e.g. the
                script path is wrong, or it crashed on startup).
        """
        try:
            read_stream, write_stream = await self._exit_stack.enter_async_context(
                stdio_client(self._server_params)
            )
            self._session = await self._exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await self._session.initialize()

            tools_result = await self._session.list_tools()
            self._tools_schema = [self._to_ollama_tool(t) for t in tools_result.tools]
        except Exception as e:
            raise McpServerUnavailableError(
                "Could not start the CAD MCP server (mcp_server.py). Check "
                "that it exists at the configured path and runs without "
                "errors on its own."
            ) from e

    async def stop(self) -> None:
        """Shut down the MCP server subprocess and release resources."""
        await self._exit_stack.aclose()
        self._session = None

    async def _chat_retrying_empty(self):
        """Call Ollama's chat endpoint, retrying if the model comes back
        with neither text nor a tool call.

        Observed live with qwen2.5:7b-instruct: occasionally the response
        has done_reason='stop' and a nonzero eval_count (so it genuinely
        generated tokens), but both message.content and message.tool_calls
        are empty -- the model's output apparently failed to parse into
        either shape and was silently dropped, with nothing recoverable
        from the response object. Retrying the identical request (same
        history, nothing appended in between) is NOT deterministic and
        often succeeds on the next attempt, so this just re-rolls instead
        of surfacing a blank answer to the user.
        """
        last_message = None
        for _ in range(_MAX_EMPTY_RESPONSE_RETRIES):
            try:
                response = await self._ollama.chat(
                    model=self._model,
                    messages=self._history,
                    tools=self._tools_schema,
                )
            except ConnectionError as e:
                # ollama's client already turns "server unreachable" into a
                # plain ConnectionError, so we don't need to inspect httpx
                # internals here.
                raise OllamaUnavailableError(
                    "Could not reach Ollama. Make sure it's running locally "
                    "(`ollama serve`) and try again."
                ) from e
            last_message = response.message
            if last_message.tool_calls or last_message.content:
                return last_message
        return last_message

    async def ask(self, question: str) -> str:
        """Answer a natural-language question, calling MCP tools as needed.

        Appends to the running conversation history so follow-up questions
        retain context.
        """
        if self._session is None:
            raise RuntimeError("AiOrchestrator.start() must be called before ask().")

        self._history.append({"role": "user", "content": question})
        self._current_question = question
        self.last_structured_tool = None
        self.last_structured_result = None

        # (tool_name, sorted-args-json) signatures already executed in this
        # ask() call -- see _execute_tool_call_deduped's docstring for why
        # this exists: qwen2.5:7b-instruct was observed live re-issuing the
        # exact same tool call (same name, same arguments) round after
        # round instead of ever producing a final answer, burning the whole
        # MAX_TOOL_CALL_ROUNDS budget on repeats of a call whose result it
        # already has.
        called_signatures: set[tuple[str, str]] = set()

        for _ in range(MAX_TOOL_CALL_ROUNDS):
            message = await self._chat_retrying_empty()
            self._history.append(message.model_dump(exclude_none=True))

            tool_calls = message.tool_calls
            if not tool_calls:
                leaked = self._parse_leaked_tool_call(message.content)
                if leaked is None:
                    return message.content or (
                        "The model didn't return a readable answer for that "
                        "question. Try rephrasing it."
                    )
                # qwen2.5:7b-instruct occasionally emits a tool call as
                # plain JSON text in `content` instead of Ollama's
                # structured `tool_calls` field (observed live: after a
                # tool result it wants to follow up with, e.g.
                # get_assembly_bom, but fails to format it as a real
                # tool_calls entry) -- without this, that leaked JSON is
                # mistaken for the final answer and shown to the user
                # verbatim (or as a blank bubble, if content was empty
                # instead). Treat it as the tool call it clearly meant to
                # be, rather than surfacing raw JSON as if it were a reply.
                name, arguments = leaked
                arguments = self._carry_over_unstated_params(name, arguments)
                tool_result = await self._execute_tool_call_deduped(
                    name, arguments, called_signatures
                )
                self._remember_params_if_successful(name, arguments, tool_result)
                self._history.append(
                    {"role": "tool", "tool_name": name, "content": tool_result}
                )
                effective_name = name
                if name == "estimate_cost" and _is_estimate_cost_assembly_redirect(tool_result):
                    tool_result = await self._auto_redirect_to_assembly_bom(arguments)
                    self._remember_params_if_successful("get_assembly_bom", arguments, tool_result)
                    self._history.append(
                        {"role": "tool", "tool_name": "get_assembly_bom", "content": tool_result}
                    )
                    effective_name = "get_assembly_bom"
                deterministic = _format_deterministic_answer(effective_name, tool_result)
                if deterministic is not None:
                    self._history.append({"role": "assistant", "content": deterministic})
                    return deterministic
                continue

            for tool_call in tool_calls:
                name = tool_call.function.name
                arguments = self._carry_over_unstated_params(
                    name, tool_call.function.arguments
                )
                tool_result = await self._execute_tool_call_deduped(
                    name, arguments, called_signatures
                )
                self._remember_params_if_successful(name, arguments, tool_result)
                self._history.append(
                    {
                        "role": "tool",
                        "tool_name": name,
                        "content": tool_result,
                    }
                )
                effective_name = name
                if name == "estimate_cost" and _is_estimate_cost_assembly_redirect(tool_result):
                    tool_result = await self._auto_redirect_to_assembly_bom(arguments)
                    self._remember_params_if_successful("get_assembly_bom", arguments, tool_result)
                    self._history.append(
                        {"role": "tool", "tool_name": "get_assembly_bom", "content": tool_result}
                    )
                    effective_name = "get_assembly_bom"
                deterministic = _format_deterministic_answer(effective_name, tool_result)
                if deterministic is not None:
                    self._history.append({"role": "assistant", "content": deterministic})
                    return deterministic

        return (
            "I wasn't able to reach a final answer after several tool calls. "
            "Try rephrasing the question."
        )

    # Every tool whose contract includes "if the question doesn't specify
    # X, ASK rather than guess" for manufacturing_process/quantity --
    # see mcp_server.py's docstrings for each. These are exactly the tools
    # a terse follow-up ("what about Injection Molding instead?", "for 10
    # units") can land on with a stale/wrong value, per the diagnosis
    # below.
    _COST_TOOLS = {
        "estimate_cost",
        "get_assembly_bom",
        "get_assembly_cost_drivers",
        "compare_materials",
        "get_cost_drivers",
        "highlight_cost_driver",
        "export_bom",
    }

    def _carry_over_unstated_params(self, name: str, arguments: dict) -> dict:
        """If `name` is a cost tool, override `quantity`/`manufacturing_process`
        with the last values a *successful* cost answer in this
        conversation actually used, for whichever of the two the current
        question gives no textual basis to change.

        Why this exists -- diagnosed live with tool-call argument logging
        (tool_call_log.jsonl) against qwen2.5:7b-instruct: a short
        follow-up like "What about with Injection Molding instead?" or
        "For 10 units" only ever changes ONE of the two parameters, but
        the model's tool call for that follow-up was observed to get the
        OTHER, unmentioned one wrong -- e.g. keeping the stated new
        process but reverting quantity to a stale/fabricated value, or
        vice versa, or (worst case) not calling a tool at all and asking
        a clarifying question instead. mcp_server.py's tool docstrings
        already tell the model to carry over unchanged values explicitly,
        and the system prompt's instructions can't force better adherence
        out of a 7B model, so this is enforced here instead: a question
        containing no digit has no textual basis for a new quantity, and
        a question containing none of PROCESSES's three names has no
        textual basis for a new process -- in either case, whatever the
        model put there gets overridden with the session's last known
        good value rather than trusted.
        """
        if name not in self._COST_TOOLS:
            return arguments

        overrides = {}
        question_lower = self._current_question.lower()

        if (
            "quantity" in arguments
            and self._last_quantity is not None
            and not re.search(r"\d", self._current_question)
            and arguments.get("quantity") != self._last_quantity
        ):
            overrides["quantity"] = self._last_quantity

        if (
            "manufacturing_process" in arguments
            and self._last_manufacturing_process is not None
            and not any(p.lower() in question_lower for p in PROCESSES)
            and arguments.get("manufacturing_process") != self._last_manufacturing_process
        ):
            overrides["manufacturing_process"] = self._last_manufacturing_process

        if overrides:
            _TOOL_CALL_LOGGER.info(
                json.dumps(
                    {
                        "question": self._current_question,
                        "tool": name,
                        "model_sent_arguments": arguments,
                        "overrode_to": overrides,
                        "reason": "question had no textual basis to change this param",
                    }
                )
            )
            return {**arguments, **overrides}
        return arguments

    def _remember_params_if_successful(self, name: str, arguments: dict, tool_result_json: str) -> None:
        """Record `quantity`/`manufacturing_process` from a cost tool call
        as the session's "last known good" values -- but only when the
        call actually succeeded (found=True), so a rejected/errored call
        (wrong process, assembly redirect, etc.) never overwrites a good
        prior value with a bogus or missing one.

        Also appends a compact system-role note to the conversation
        history stating those values explicitly (e.g. "Previous cost
        answer used: manufacturing_process=CNC Machining, quantity=100.").
        This is grounding for the model's own next tool call, on top of
        (not instead of) the deterministic override in
        _carry_over_unstated_params above -- that override is what
        actually guarantees correctness regardless of what the model
        does, but giving the model an explicit, easy-to-quote fact to
        work from (rather than making it infer "100" out of a wall of
        prior freeform chat text) should also make it less likely to
        pick a wrong value on tool calls this class doesn't cover, e.g.
        get_cost_drivers/highlight_cost_driver called for the first time
        after a get_assembly_bom answer.
        """
        if name not in self._COST_TOOLS:
            return
        try:
            data = json.loads(tool_result_json)
        except (json.JSONDecodeError, TypeError):
            return
        if not (isinstance(data, dict) and data.get("found") is True):
            return
        if "quantity" in arguments:
            self._last_quantity = arguments["quantity"]
        if "manufacturing_process" in arguments:
            self._last_manufacturing_process = arguments["manufacturing_process"]
        # Last successful cost/BOM call's raw data, for callers that want
        # to render structured UI (a real table) instead of prose -- see
        # this class's last_structured_tool/last_structured_result
        # docstring in __init__. Overwritten by each subsequent successful
        # call within the same ask() turn, so it ends up holding whichever
        # call actually backs the final answer (e.g. the get_assembly_bom
        # call an estimate_cost-on-assembly rejection auto-redirects to).
        self.last_structured_tool = name
        self.last_structured_result = data
        self._history.append(
            {
                "role": "system",
                "content": (
                    f"Previous cost answer used: manufacturing_process="
                    f"{self._last_manufacturing_process}, quantity="
                    f"{self._last_quantity}. If the user's next message "
                    "only changes one of these, carry the other one over "
                    "unchanged in your next tool call -- do not omit it "
                    "or substitute a different value."
                ),
            }
        )

    async def _auto_redirect_to_assembly_bom(self, arguments: dict) -> str:
        """Re-run the same cost request as get_assembly_bom(), reusing
        estimate_cost()'s original manufacturing_process/quantity verbatim
        instead of letting the model re-issue the call (and potentially
        corrupt those arguments -- see _is_estimate_cost_assembly_redirect's
        docstring). `quantity` defaults to 1 to match estimate_cost()'s own
        default, since a model call that omitted it should redirect the
        same way a call that included quantity=1 would.
        """
        redirect_arguments = {
            "manufacturing_process": arguments.get("manufacturing_process"),
            "quantity": arguments.get("quantity", 1),
        }
        return await self._call_mcp_tool("get_assembly_bom", redirect_arguments)

    async def _execute_tool_call_deduped(
        self, name: str, arguments: dict, called_signatures: set[tuple[str, str]]
    ) -> str:
        """Run an MCP tool call, unless this exact (name, arguments) pair
        was already executed earlier in this same ask() turn.

        Observed live: after a tool already answered the question, the
        model sometimes re-requests the identical call (or a different
        tool it doesn't need) repeatedly instead of stopping, exhausting
        MAX_TOOL_CALL_ROUNDS without ever producing text. A duplicate call
        can never return different data (nothing about the CAD document
        changed between rounds), so re-running it is pure waste; refusing
        it and telling the model so directly is a stronger, non-optional
        nudge than the system prompt's "stop calling tools" instruction,
        which the model doesn't reliably follow on its own.
        """
        signature = (name, json.dumps(arguments, sort_keys=True))
        if signature in called_signatures:
            return (
                "You already called this exact tool with these exact "
                "arguments earlier in this conversation -- its result is "
                "unchanged and is already above. Do not call it again. "
                "Answer the user's question now using that result."
            )
        called_signatures.add(signature)
        return await self._call_mcp_tool(name, arguments)

    async def _call_mcp_tool(self, name: str, arguments: dict) -> str:
        """Invoke an MCP tool and flatten its result to a string for the LLM.

        If the tool itself failed (e.g. SolidWorks isn't running, or no
        document is open), the MCP server has already turned that into a
        CallToolResult with is_error=True and a plain-language message in
        its content — there's nothing extra to catch here. That message is
        returned just like a successful result, fed back into the
        conversation as a "tool" message, and the model works it into its
        final answer (e.g. "I couldn't check that because no part is open
        in SolidWorks").
        """
        _TOOL_CALL_LOGGER.info(
            json.dumps(
                {
                    "question": self._current_question,
                    "tool": name,
                    "arguments": arguments,
                }
            )
        )
        result: CallToolResult = await self._session.call_tool(name, arguments)

        if result.structured_content is not None:
            return json.dumps(result.structured_content)
        return "\n".join(
            block.text for block in result.content if hasattr(block, "text")
        )

    def _parse_leaked_tool_call(self, content: str | None) -> tuple[str, dict] | None:
        """If `content` is actually a tool call the model failed to put in
        the structured `tool_calls` field, return (name, arguments);
        otherwise None. Only matches a name from `self._tools_schema`, so
        an ordinary text answer that happens to look JSON-ish is never
        misread as a tool call.

        Observed live with qwen2.5:7b-instruct: it sometimes wraps the
        leaked JSON in an XML-ish tag -- `<tool_response>\n{...}\n
        </tool_response>` -- rather than emitting bare JSON. A plain
        `json.loads(content)` chokes on the tag text and raises, so this
        case fell all the way through to being shown to the user as the
        literal raw `<tool_response>{...}</tool_response>` text instead
        of the tool actually being called. Falls back to parsing just the
        substring between the first "{" and the last "}" in `content`
        when parsing the whole string outright fails, which recovers the
        JSON regardless of what surrounds it.
        """
        if not content:
            return None
        parsed = self._try_parse_json_object(content)
        if parsed is None:
            return None
        name = parsed.get("name")
        arguments = parsed.get("arguments", {})
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return None
        known_names = {t["function"]["name"] for t in self._tools_schema}
        if name not in known_names:
            return None
        return name, arguments

    @staticmethod
    def _try_parse_json_object(text: str) -> dict | None:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass

        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            parsed = json.loads(text[start : end + 1])
        except (json.JSONDecodeError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _to_ollama_tool(tool) -> dict:
        """Convert an MCP tool definition to Ollama's function-calling schema."""
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.input_schema,
            },
        }


def run_ai_orchestrator(user_message: str) -> str:
    """Answer one question end-to-end: LLM -> (maybe) MCP tool calls -> LLM.

    This is a simple, self-contained entry point for one-off calls (e.g. a
    terminal test): it starts a fresh AiOrchestrator — which launches its
    own mcp_server.py subprocess — asks it one question, and shuts
    everything down again before returning. (The Streamlit UI instead keeps
    one long-lived AiOrchestrator per chat session via the class directly,
    so conversation history survives across turns; this function trades
    that away for the simplicity of "call it, get a string back".)

    The request/tool-call loop, in plain language:
      1. The user's question, plus the list of tools MCP says are
         available (get_mass, get_material, etc., each with the docstring
         from mcp_server.py as its description), are sent to the local
         Ollama model in one chat request.
      2. The model can respond two ways: a final natural-language answer,
         or a request to call one or more tools (it decides this itself,
         based on the tool descriptions — we never tell it which tool to
         use). If it's a final answer, we return it immediately.
      3. If it's a tool call, we actually execute it against the running
         MCP server, which forwards it to the real CadAdapter — live
         SolidWorks COM calls, unless CAD_ADAPTER=mock is set. Whatever
         comes back (real data, or a clear error like "no document open")
         is appended to the conversation as a "tool" message.
      4. We go back to step 1 with that tool result now part of the
         conversation, so the model can either call another tool or use
         what it just learned to produce a final answer.
      5. This repeats until the model stops calling tools (normal case) or
         MAX_TOOL_CALL_ROUNDS is hit (safety valve against infinite
         tool-calling loops), at which point we give up gracefully.

    Never raises: every failure mode (Ollama not running, the MCP server
    subprocess failing to start, a tool call failing, no CAD document
    open) is caught here and turned into a plain-language string instead
    of an exception, so callers never need a try/except around this.
    """
    answer, _tool_name, _data = asyncio.run(_run_ai_orchestrator_async(user_message))
    return answer


def run_ai_orchestrator_with_data(user_message: str) -> tuple[str, str | None, dict | None]:
    """Same one-shot call as run_ai_orchestrator(), but also returns the
    name and parsed found=True JSON of the last successful cost/BOM tool
    call this question triggered (both None if it never called one, or
    the call didn't succeed) -- see AiOrchestrator.last_structured_tool/
    last_structured_result's docstring. For a UI that wants to render a
    real table/breakdown card from the same numbers already backing the
    prose answer, instead of trying to re-parse that prose back into
    numbers.
    """
    return asyncio.run(_run_ai_orchestrator_async(user_message))


async def _run_ai_orchestrator_async(
    user_message: str,
) -> tuple[str, str | None, dict | None]:
    orchestrator = AiOrchestrator()

    try:
        await orchestrator.start()
    except McpServerUnavailableError as e:
        return f"Couldn't reach the CAD MCP server: {e}", None, None

    try:
        answer = await orchestrator.ask(user_message)
        return answer, orchestrator.last_structured_tool, orchestrator.last_structured_result
    except OllamaUnavailableError as e:
        return f"Couldn't reach Ollama: {e}", None, None
    except Exception as e:  # noqa: BLE001 - last resort, never crash the caller
        return f"Something went wrong answering that question: {e}", None, None
    finally:
        await orchestrator.stop()
