"""MCP server exposing CAD model queries as tools for the AI orchestrator.

Wraps a CadAdapter instance (SolidWorksAdapter, MockAdapter, or any future
platform adapter) and publishes its capabilities — part info, mass,
material, features, and derived checks like cost/carbon estimates and
basic DFM checks — as MCP tools. This module knows nothing about which
concrete adapter is in use; it only depends on the CadAdapter interface,
so swapping CAD platforms requires no changes here.

Not yet implemented.
"""
