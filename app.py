"""Streamlit chat UI for the CAD AI copilot.

Provides the chat interface where the user asks natural-language
questions about the currently open CAD model and sees answers streamed
back from the AI orchestrator. Contains no CAD or LLM logic directly —
it only calls into ai_orchestrator.py, which handles the MCP/LLM
plumbing.
"""

import asyncio

import streamlit as st

from ai_orchestrator import DEFAULT_MODEL, AiOrchestrator

st.set_page_config(page_title="CAD AI Copilot", page_icon="🛠️")


def _get_event_loop() -> asyncio.AbstractEventLoop:
    """Return a single asyncio event loop kept alive for the whole session.

    Streamlit reruns this script on every interaction, so the orchestrator
    (and the MCP server subprocess it owns) must live in st.session_state
    rather than being recreated each run. Coroutines are driven with this
    same loop each time so that connection stays valid across reruns.
    """
    if "event_loop" not in st.session_state:
        st.session_state.event_loop = asyncio.new_event_loop()
    return st.session_state.event_loop


def _get_orchestrator() -> AiOrchestrator:
    """Return the session's AiOrchestrator, starting it on first use."""
    if "orchestrator" not in st.session_state:
        loop = _get_event_loop()
        orchestrator = AiOrchestrator()
        loop.run_until_complete(orchestrator.start())
        st.session_state.orchestrator = orchestrator
    return st.session_state.orchestrator


st.title("🛠️ CAD AI Copilot")
st.caption(f"Ask about the currently open CAD model · model: {DEFAULT_MODEL}")

try:
    orchestrator = _get_orchestrator()
    connection_error = None
except Exception as exc:  # noqa: BLE001 - surface any startup failure in the UI
    orchestrator = None
    connection_error = exc

if connection_error is not None:
    st.error(
        "Could not start the CAD copilot backend (MCP server / CAD adapter). "
        f"Details: {connection_error}"
    )
    st.stop()

if "messages" not in st.session_state:
    st.session_state.messages = []

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

if prompt := st.chat_input("e.g. What's the mass of this part?"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            loop = _get_event_loop()
            try:
                answer = loop.run_until_complete(orchestrator.ask(prompt))
            except Exception as exc:  # noqa: BLE001 - show the error, don't crash the app
                answer = (
                    "Something went wrong answering that question. "
                    f"Details: {exc}"
                )
        st.markdown(answer)

    st.session_state.messages.append({"role": "assistant", "content": answer})
