"""agent-rewind (agent-tx): Zero-Trust Transactional Rewind & Compensation Runtime for AI Agents."""

from agent_rewind.adapters import (
    TransactionalRunnable,
    render_transaction_report,
    rewind_runnable,
    rewind_tools,
    to_mcp_tool_schema,
)
from agent_rewind.decorators import tx_tool
from agent_rewind.exceptions import (
    AgentTxError,
    CompensationFailedError,
    SecurityManipulationError,
    StateConflictError,
    StepExecutionError,
)
from agent_rewind.journal import (
    CallbackBackend,
    InMemoryBackend,
    JSONFileBackend,
    JSONLFileBackend,
    JournalBackend,
    TransactionJournal,
    create_journal_backend,
)
from agent_rewind.kernel import (
    AgentTransaction,
    get_current_idempotency_key,
    get_current_transaction,
)
from agent_rewind.mcp import (
    AutoDiscoveryEngine,
    CallableMCPTransport,
    HttpxTransport,
    MCPTransport,
    RemoteMCPGateway,
    ServerLeaseManager,
    StdlibHTTPTransport,
)
from agent_rewind.models import (
    ActionReceipt,
    DLQRecord,
    EffectType,
    Ref,
    StepRecord,
    StepStatus,
    TaintLevel,
    TransactionReport,
    TransactionState,
)
from agent_rewind.policy import PolicyGuard
from agent_rewind.protocols import (
    REWIND_A2A_EXTENSION,
    RewindA2AExecutor,
    RewindStateGraph,
    RewindSubAgentHost,
    RewindToolNode,
    TransactionalRemoteAgent,
    TransactionalStateGraph,
    extract_rewind_metadata,
    inject_rewind_metadata,
    rewind_agent,
    rewind_graph,
)
from agent_rewind.quarantine import QuarantinedExtractor
from agent_rewind.server import RewindServer
from agent_rewind.vault import DataVault

# Friendly alias for AgentTransaction
AgentRewind = AgentTransaction
rewind_tool = tx_tool

__all__ = [
    "ActionReceipt",
    "AgentRewind",
    "AgentTransaction",
    "AgentTxError",
    "AutoDiscoveryEngine",
    "CallbackBackend",
    "CallableMCPTransport",
    "CompensationFailedError",
    "DLQRecord",
    "DataVault",
    "EffectType",
    "HttpxTransport",
    "InMemoryBackend",
    "JSONFileBackend",
    "JSONLFileBackend",
    "JournalBackend",
    "MCPTransport",
    "PolicyGuard",
    "QuarantinedExtractor",
    "REWIND_A2A_EXTENSION",
    "Ref",
    "RemoteMCPGateway",
    "RewindA2AExecutor",
    "RewindServer",
    "RewindStateGraph",
    "RewindSubAgentHost",
    "RewindToolNode",
    "SecurityManipulationError",
    "ServerLeaseManager",
    "StateConflictError",
    "StdlibHTTPTransport",
    "StepExecutionError",
    "StepRecord",
    "StepStatus",
    "TaintLevel",
    "TransactionJournal",
    "TransactionReport",
    "TransactionState",
    "TransactionalRemoteAgent",
    "TransactionalRunnable",
    "TransactionalStateGraph",
    "create_journal_backend",
    "extract_rewind_metadata",
    "get_current_idempotency_key",
    "get_current_transaction",
    "inject_rewind_metadata",
    "render_transaction_report",
    "rewind_agent",
    "rewind_graph",
    "rewind_runnable",
    "rewind_tool",
    "rewind_tools",
    "to_mcp_tool_schema",
    "tx_tool",
]
