"""AI orchestrator connecting the chat UI to the MCP server and LLM.

Takes a natural-language question from the user (e.g. "what's the mass of
this part?" or "estimate the cost"), sends it to a local LLM via Ollama,
lets the model call MCP tools exposed by mcp_server.py to fetch live CAD
data, and returns a natural-language answer. Contains no CAD-specific
logic itself — all CAD access is mediated through the MCP tool layer,
which in turn is mediated through the CadAdapter interface.

Not yet implemented.
"""
