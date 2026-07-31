"""Manual smoke test for AiOrchestrator: LLM -> MCP tool calls -> live CAD data.

Run with a part open in SolidWorks (or CAD_ADAPTER=mock set) and Ollama
running locally:

    python test_orchestrator.py
"""

import asyncio

from ai_orchestrator import AiOrchestrator

QUESTIONS = [
    "What's the name of the part I currently have open?",
    "What's the mass of this part, in kilograms?",
    "What material is this part made of, and what's its density?",
    "How many features does this part have, and does it have any fillets?",
]


async def main() -> None:
    orchestrator = AiOrchestrator()
    await orchestrator.start()

    try:
        for question in QUESTIONS:
            print(f"Q: {question}")
            answer = await orchestrator.ask(question)
            print(f"A: {answer}\n")
    finally:
        await orchestrator.stop()


if __name__ == "__main__":
    asyncio.run(main())
