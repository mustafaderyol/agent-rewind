"""Example 10: Official LangGraph `StateGraph` Multi-Agent Supervisor + Google A2A + `RemoteRunnable`.

Uses the real `langgraph.graph.StateGraph`, `START`, `END`, `langchain_core.messages.HumanMessage`,
and `AIMessage` together with `agent-rewind`'s `RewindStateGraph` / `rewind_graph`, `rewind_agent`,
and `RewindSubAgentHost`:

1. `remote_agent_1` (LangServe `RemoteRunnable` Sub-Agent):
   Executes 2 internal `@rewind_tool` mutations on its own server (`RewindSubAgentHost`)
   and returns a composite `undo_token`.
2. `remote_agent_2` (Google A2A Protocol Sub-Agent):
   Receives a standard A2A `tasks/send` request, mutates ERP stock, and returns a standard
   A2A `Task` with `metadata.rewind.undo_token`.
3. `remote_agent_3` (Analytics / Billing Sub-Agent):
   Crashes mid-execution!
4. The real compiled `StateGraph` (`rewind_graph(workflow.compile())`) automatically rewinds
   `remote_2` (via A2A `tasks/cancel`) and `remote_1` (rolling back both of its internal tools)!
"""

from typing import Any, TypedDict

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph

from agent_rewind import (
    RewindSubAgentHost,
    rewind_agent,
    rewind_graph,
    rewind_tool,
)

# ============================================================================
# 1. REMOTE SUB-AGENT 1: CRM Sub-Agent (Executes 2 internal tools on Host 1)
# ============================================================================

HOST_1_CRM_DB: dict[str, Any] = {"customers": {}, "tickets": []}


@rewind_tool(compensate=lambda args, res: HOST_1_CRM_DB["customers"].pop(args["cid"], None))
def crm_upsert_customer(cid: str, tier: str) -> dict[str, str]:
    HOST_1_CRM_DB["customers"][cid] = tier
    print(f"  [Remote Agent 1 Internal Tool 1] Upserted customer '{cid}' (tier={tier})")
    return {"cid": cid, "tier": tier}


@rewind_tool(compensate=lambda args, res: HOST_1_CRM_DB["tickets"].remove(res["ticket_id"]))
def crm_create_onboarding_ticket(cid: str) -> dict[str, str]:
    tid = f"TCK-{cid}"
    HOST_1_CRM_DB["tickets"].append(tid)
    print(f"  [Remote Agent 1 Internal Tool 2] Created onboarding ticket '{tid}'")
    return {"ticket_id": tid}


def remote_subagent_1_logic(payload: dict[str, Any]) -> AIMessage:
    crm_upsert_customer(cid="CUST-42", tier="ENTERPRISE")
    crm_create_onboarding_ticket(cid="CUST-42")
    return AIMessage(content="Remote Agent 1 completed CRM onboarding.")


remote_service_1_host = RewindSubAgentHost(remote_subagent_1_logic, name="remote_agent_1")


# ============================================================================
# 2. REMOTE SUB-AGENT 2: ERP Sub-Agent (Speaks Standard Google A2A Protocol)
# ============================================================================

HOST_2_ERP_DB: dict[str, int] = {"CLUSTER-NODE-H100": 8}


@rewind_tool(
    pre_state=lambda args: HOST_2_ERP_DB[args["resource"]],
    compensate=lambda args, res, pre: HOST_2_ERP_DB.update({args["resource"]: pre}),
)
def erp_allocate_cluster(resource: str, nodes: int) -> dict[str, Any]:
    HOST_2_ERP_DB[resource] -= nodes
    print(
        f"  [Remote Agent 2 A2A Tool] Allocated {nodes}x '{resource}' "
        f"(Remaining: {HOST_2_ERP_DB[resource]})"
    )
    return {"resource": resource, "remaining": HOST_2_ERP_DB[resource]}


def remote_subagent_2_logic(message: Any) -> dict[str, Any]:
    erp_allocate_cluster(resource="CLUSTER-NODE-H100", nodes=4)
    return {"summary": "Allocated 4 H100 cluster nodes"}


remote_service_2_a2a_host = RewindSubAgentHost(remote_subagent_2_logic, name="remote_agent_2")


# ============================================================================
# 3. REMOTE SUB-AGENT 3: Billing Sub-Agent (Fails mid-workflow!)
# ============================================================================


def remote_subagent_3_logic(payload: dict[str, Any]) -> AIMessage:
    print("  [Remote Agent 3] Attempting enterprise wire transfer provisioning...")
    raise RuntimeError("Remote Agent 3 Failed: Compliance Sanctions Service Unavailable (503)")


remote_service_3_host = RewindSubAgentHost(remote_subagent_3_logic, name="remote_agent_3")


# ============================================================================
# 4. OFFICIAL LANGGRAPH `StateGraph` MULTI-AGENT WORKFLOW
# ============================================================================


class AgentState(TypedDict):
    messages: list[Any]
    sender: str
    next_step: str


remote_agent_1 = rewind_agent(remote_service_1_host, name="remote_1")
remote_agent_2 = rewind_agent(remote_service_2_a2a_host, name="remote_2", protocol="a2a")
remote_agent_3 = rewind_agent(remote_service_3_host, name="remote_3")


def local_supervisor_node(state: AgentState) -> dict[str, str]:
    last_sender = state["sender"]
    routing_table = {
        "user": "remote_1",
        "remote_1": "remote_2",
        "remote_2": "remote_3",
        "remote_3": "FINISH",
    }
    chosen = routing_table.get(last_sender, "FINISH")
    print(f"[Supervisor] Routing next_step -> '{chosen}'")
    return {"next_step": chosen}


def remote_node_1(state: AgentState) -> dict[str, Any]:
    res = remote_agent_1.invoke({"messages": state["messages"]})
    return {"messages": [AIMessage(content=str(res))], "sender": "remote_1"}


def remote_node_2(state: AgentState) -> dict[str, Any]:
    a2a_request = {
        "jsonrpc": "2.0",
        "id": "a2a_req_2",
        "method": "tasks/send",
        "params": {
            "id": "task_cluster_alloc_1",
            "message": {"role": "user", "parts": [{"type": "text", "text": "Allocate 4 H100 nodes"}]},
        },
    }
    res = remote_agent_2.invoke(a2a_request)
    return {"messages": [AIMessage(content=str(res))], "sender": "remote_2"}


def remote_node_3(state: AgentState) -> dict[str, Any]:
    res = remote_agent_3.invoke({"messages": state["messages"]})
    return {"messages": [AIMessage(content=str(res))], "sender": "remote_3"}


def main() -> None:
    print("=== Official LangGraph StateGraph + Google A2A + RemoteRunnable Demo ===")

    # Build using the REAL `langgraph.graph.StateGraph`
    workflow = StateGraph(AgentState)
    workflow.add_node("supervisor", local_supervisor_node)
    workflow.add_node("remote_1", remote_node_1)
    workflow.add_node("remote_2", remote_node_2)
    workflow.add_node("remote_3", remote_node_3)

    workflow.add_edge(START, "supervisor")
    workflow.add_conditional_edges(
        "supervisor",
        lambda x: x["next_step"],
        {
            "remote_1": "remote_1",
            "remote_2": "remote_2",
            "remote_3": "remote_3",
            "FINISH": END,
        },
    )
    workflow.add_edge("remote_1", "supervisor")
    workflow.add_edge("remote_2", "supervisor")
    workflow.add_edge("remote_3", "supervisor")

    multi_agent_system = rewind_graph(workflow.compile(), suppress_errors=True)

    initial_state: AgentState = {
        "messages": [HumanMessage(content="Onboard CUST-42, allocate H100 cluster, and bill.")],
        "sender": "user",
        "next_step": "",
    }

    multi_agent_system.invoke(initial_state)
    tx = multi_agent_system.last_transaction
    assert tx is not None

    print(f"\nMulti-Agent Transaction State : {tx.state.value}")
    print(f"Compensated Sub-Agents (LIFO) : {tx.report.compensated_steps}")
    print(f"Host 1 CRM DB After Rollback  : {HOST_1_CRM_DB} (Expected: empty customers & tickets)")
    print(f"Host 2 ERP DB After Rollback  : {HOST_2_ERP_DB} (Expected: {{'CLUSTER-NODE-H100': 8}})")


if __name__ == "__main__":
    main()
