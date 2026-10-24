# Pluggable Storage & Transport Architecture

Enterprise and regulated environments frequently restrict which third-party libraries or database drivers can be installed. `agent-rewind` is architected with **Zero Required Dependencies** and clean Python `Protocol` interfaces so you can run purely in memory or plug in your own infrastructure.

---

## 1. Why Zero Database Dependencies by Default?

Traditional backend sagas store state in heavy relational databases (PostgreSQL, Oracle, SQLite) because workflows can span days. AI Agent tool-calling turns, however, typically complete within **50 milliseconds to 30 seconds**.

Requiring a database cluster just to undo 3 REST/MCP calls when the 4th call fails introduces unnecessary operational friction. Therefore, `agent-rewind` defaults to `InMemoryBackend` (pure Python RAM) while offering zero-dependency file persistence (`JSONFileBackend`) and event hooks (`CallbackBackend`).

---

## 2. Built-in Journal Backends

### A. `InMemoryBackend` (Default — Zero I/O)
Stores the transaction state machine, undo stack, idempotency cache, and SHA-256 hash-chained audit trail in process memory.

```python
from agent_rewind import AgentRewind, InMemoryBackend

backend = InMemoryBackend()
async with AgentRewind(journal=backend) as tx:
    ...
```

### B. `JSONFileBackend` (Crash Recovery & Local DLQ Persistence)
Persists transactional checkpoints and Dead-Letter Queue (DLQ) records to a single local JSON file using atomic temp-file renames (`os.replace`). Requires zero database drivers.

```python
from agent_rewind import AgentRewind, JSONFileBackend

journal = JSONFileBackend("./tx_journal.json")

# Recover interrupted transactions after an unexpected process crash:
recovered = await AgentRewind.recover_from_wal(
    journal=journal,
    tool_registry={"release_stock": release_stock_fn},
)
```

### C. `CallbackBackend` (OpenTelemetry, Datadog, Redis, or Webhook Sink)
Emits structured event dicts (`tx_start`, `step_commit`, `tx_status`, `dlq_push`, `audit`) to your own callback function while keeping runtime state in memory:

```python
from agent_rewind import AgentRewind, CallbackBackend

events = []
journal = CallbackBackend(on_event=lambda event, payload: events.append((event, payload)))

async with AgentRewind(journal=journal) as tx:
    ...
```

### D. Custom `JournalBackend` Protocol
Want to persist state in Redis or DynamoDB? Implement the `JournalBackend` structural protocol (`src/agent_rewind/journal.py`)—no subclassing required.

---

## 3. Pluggable MCP Transports

When using `RemoteMCPGateway`, you can choose how JSON-RPC 2.0 requests are transmitted:

| Transport Class | Dependencies | Best For |
| :--- | :--- | :--- |
| `StdlibHTTPTransport` *(Default)* | Python `urllib.request` + `asyncio.to_thread` | Zero-dependency environments, air-gapped containers, standard workloads |
| `HttpxTransport` | `httpx` (`pip install "agent-rewind[http]"`) | High-concurrency async connection pooling |
| `CallableMCPTransport` | None | Wrapping AWS Boto3, gRPC stubs, or custom corporate proxies (`async def send(url, payload) -> dict`) |

```python
from agent_rewind import CallableMCPTransport, RemoteMCPGateway


async def my_custom_rpc_sender(url: str, payload: dict) -> dict:
    # Forward JSON-RPC payload over gRPC, mTLS, or internal service mesh
    ...


gateway = await RemoteMCPGateway.discover(
    ["http://internal-mcp.corp/mcp"],
    transport=CallableMCPTransport(my_custom_rpc_sender),
)
```
