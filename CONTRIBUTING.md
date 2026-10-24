# Contributing to `agent-rewind`

Thank you for your interest in contributing to **`agent-rewind`**! We welcome bug fixes, new framework adapters, MCP discovery improvements, and scenario examples.

## Development Setup

```bash
git clone https://github.com/mustafaderyol/agent-rewind.git
cd agent-rewind

# Install dependencies and dev extras using uv
uv sync --extra dev

# Run the unit and integration test suite
uv run pytest -v
```

## Running the Benchmark Suite

```bash
uv run python benchmarks/benchmark_suite.py
```

## Pull Request Checklist

1. Follow SOLID principles and strict Python type annotations (`mypy` / PEP 484).
2. Keep core runtime storage zero-DB by default (`src/agent_rewind/`) and extend official protocol SDKs (`mcp`, `a2a-sdk`, `langchain-core`, `langgraph`) via clean subclassing rather than reinventing protocols.
3. Add unit tests under `tests/` covering any new behavior or edge cases.
4. Use [Conventional Commits](https://www.conventionalcommits.org/) (e.g. `feat(mcp): add sse transport adapter`, `fix(vault): handle nested tuple refs`).
