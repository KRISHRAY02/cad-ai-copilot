"""Simple terminal test for run_ai_orchestrator().

Type a question about the currently open CAD model and see the AI call
real MCP tools (live SolidWorks data, unless CAD_ADAPTER=mock is set) to
answer it. Type "exit" or "quit" to stop.

Requires Ollama running locally (`ollama serve`) with the model in
ai_orchestrator.DEFAULT_MODEL pulled.

    python ask_cad_copilot.py
"""

from ai_orchestrator import run_ai_orchestrator


def main() -> None:
    print("CAD AI Copilot (terminal test). Type 'exit' to quit.\n")
    while True:
        question = input("You: ").strip()
        if not question:
            continue
        if question.lower() in ("exit", "quit"):
            break

        answer = run_ai_orchestrator(question)
        print(f"AI: {answer}\n")


if __name__ == "__main__":
    main()
