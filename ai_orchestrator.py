"""AI orchestrator connecting the chat UI to the MCP server and LLM.

Takes a natural-language question from the user (e.g. "what's the mass of
this part?" or "estimate the cost"), sends it to a local LLM via Ollama,
lets the model call MCP tools exposed by mcp_server.py to fetch live CAD
data, and returns a natural-language answer. Contains no CAD-specific
logic itself — all CAD access is mediated through the MCP tool layer,
which in turn is mediated through the CadAdapter interface. This module
never imports a CadAdapter subclass directly.
"""

import json
import sys
from contextlib import AsyncExitStack

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolResult
from ollama import AsyncClient

DEFAULT_MODEL = "llama3.2"
MAX_TOOL_CALL_ROUNDS = 5

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
        """
        read_stream, write_stream = await self._exit_stack.enter_async_context(
            stdio_client(self._server_params)
        )
        self._session = await self._exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await self._session.initialize()

        tools_result = await self._session.list_tools()
        self._tools_schema = [self._to_ollama_tool(t) for t in tools_result.tools]

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
            response = await self._ollama.chat(
                model=self._model,
                messages=self._history,
                tools=self._tools_schema,
            )
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
        """Invoke an MCP tool and flatten its result to a string for the LLM."""
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
