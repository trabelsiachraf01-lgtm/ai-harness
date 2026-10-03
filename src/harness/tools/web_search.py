"""Web search is an OpenRouter *server tool*: the provider runs the search, so there is no
`run()` here, only the spec we hand to the model. How to write good queries is steered from
the system prompt (prompts.py), because the tool's own description isn't ours to edit.

Cost per search (OpenRouter engine pricing): exa ~$0.007, parallel ~$0.005, native ~$0.011 and
~4x the tokens. In one sample, exa and parallel returned slightly staler answers than native.
Searches dwarf the token cost of a chat turn, so max_uses caps them per model response.
"""


def web_search_spec(engine: str = "exa", max_uses: int = 3, max_results: int = 5) -> dict:
    return {
        "type": "openrouter:web_search",
        "parameters": {"engine": engine, "max_uses": max_uses, "max_results": max_results},
    }
