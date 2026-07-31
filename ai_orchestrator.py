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
import sys
from contextlib import AsyncExitStack

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolResult
from ollama import AsyncClient

DEFAULT_MODEL = "qwen2.5:7b-instruct"
MAX_TOOL_CALL_ROUNDS = 5


class OllamaUnavailableError(RuntimeError):
    """Raised when the local Ollama server can't be reached."""


class McpServerUnavailableError(RuntimeError):
    """Raised when the mcp_server.py subprocess can't be started or reached."""

SYSTEM_PROMPT = (
    "You are a CAD design copilot. You answer questions about the CAD "
    "model that is currently open, using the tools provided. Always call "
    "a tool to fetch part data rather than guessing or relying on prior "
    "conversation turns for numbers. Keep answers concise, cite units, "
    "and mention when an estimate (cost, carbon, DFM) is a rough "
    "approximation rather than an authoritative figure."
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
        self._server_params = StdioServerParameters(
            command=sys.executable, args=[mcp_server_path]
        )
        self._exit_stack = AsyncExitStack()
        self._session: ClientSession | None = None
        self._ollama = AsyncClient()
        self._tools_schema: list[dict] = []
        self._history: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

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

    async def ask(self, question: str) -> str:
        """Answer a natural-language question, calling MCP tools as needed.

        Appends to the running conversation history so follow-up questions
        retain context.
        """
        if self._session is None:
            raise RuntimeError("AiOrchestrator.start() must be called before ask().")

        self._history.append({"role": "user", "content": question})

        for _ in range(MAX_TOOL_CALL_ROUNDS):
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
            message = response.message
            self._history.append(message.model_dump(exclude_none=True))

            if not message.tool_calls:
                return message.content or ""

            for tool_call in message.tool_calls:
                tool_result = await self._call_mcp_tool(
                    tool_call.function.name, tool_call.function.arguments
                )
                self._history.append(
                    {
                        "role": "tool",
                        "tool_name": tool_call.function.name,
                        "content": tool_result,
                    }
                )

        return (
            "I wasn't able to reach a final answer after several tool calls. "
            "Try rephrasing the question."
        )

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
        result: CallToolResult = await self._session.call_tool(name, arguments)

        if result.structured_content is not None:
            return json.dumps(result.structured_content)
        return "\n".join(
            block.text for block in result.content if hasattr(block, "text")
        )

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
    return asyncio.run(_run_ai_orchestrator_async(user_message))


async def _run_ai_orchestrator_async(user_message: str) -> str:
    orchestrator = AiOrchestrator()

    try:
        await orchestrator.start()
    except McpServerUnavailableError as e:
        return f"Couldn't reach the CAD MCP server: {e}"

    try:
        return await orchestrator.ask(user_message)
    except OllamaUnavailableError as e:
        return f"Couldn't reach Ollama: {e}"
    except Exception as e:  # noqa: BLE001 - last resort, never crash the caller
        return f"Something went wrong answering that question: {e}"
    finally:
        await orchestrator.stop()
