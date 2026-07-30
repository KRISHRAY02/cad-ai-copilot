"""CAD adapter interfaces and implementations.

Exposes CadAdapter, the abstract contract every CAD-specific adapter
(SolidWorks, mock, etc.) must implement so the rest of the application
(MCP server, AI orchestrator, UI) can talk to any supported CAD platform
through a single, uniform interface.
"""
