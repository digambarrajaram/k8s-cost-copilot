# agent.py
"""
Kubernetes Diagnosis Agent — LangGraph + MCP (SSE) + AWS Bedrock (Nova Pro)
With risk classification + human-approval gate for risky actions.

Graph flow:
  START
    → supervisor            (placeholder; add multi-agent routing here later)
    → k8s_diagnosis_agent   ↔ tools  (ReAct loop until no more tool calls)
    → risk_assessment
    → [LOW: END]
      [MEDIUM: execute_action → END]   (auto-approved)
      [HIGH: interrupt_before pause → human approves/rejects → execute_action or END]
"""

import asyncio
import os
import time
from typing import Annotated, Optional, TypedDict

from dotenv import load_dotenv
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from mcp import ClientSession
from mcp.client.sse import sse_client

import risk_classifier as rc
from prompts import build_diagnosis_system, build_executor_system #type: 
from utils.aws_session import get_bedrock_client

load_dotenv()

# ── Configuration ──────────────────────────────────────────────────────────────
MCP_SERVER_URL = os.environ["MCP_SERVER_URL"]
TARGET_NAMESPACE = os.environ.get("K8S_TARGET_NAMESPACE", "mcp-test")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "amazon.nova-pro-v1:0")
MAX_GRAPH_STEPS = int(os.environ.get("MAX_GRAPH_STEPS", "25"))

# Destructive keywords for belt-and-suspenders safety checks.
_DESTRUCTIVE_KW = frozenset(
    {"resources_delete", "pods_delete", "delete", "remove", "purge", "destroy"}
)

# Register RiskLevel for LangGraph checkpoint serialisation (pause/resume).
ALLOWED_MSGPACK = JsonPlusSerializer(
    allowed_msgpack_modules=[("risk_classifier", "RiskLevel")],
)


# ── State ──────────────────────────────────────────────────────────────────────
class State(TypedDict):
    task: str
    messages: Annotated[list[BaseMessage], add_messages]
    risk_level: Optional[str]       # "low" | "medium" | "high" | None
    suggested_action: Optional[str] # set by risk_assessment_node from diagnosis
    approval_status: Optional[str]  # "approved" | "rejected" | None


# ── LLM factory ───────────────────────────────────────────────────────────────
def _make_llm(temperature: float = 0.1) -> ChatBedrockConverse:
    return ChatBedrockConverse(
        model_id=BEDROCK_MODEL_ID,
        client=get_bedrock_client(AWS_REGION),
        temperature=temperature,
    )


# ── Retry helper ──────────────────────────────────────────────────────────────
def _invoke_with_retry(llm_callable, *, max_attempts: int = 2, sleep_s: float = 2.0):
    """
    Invoke an LLM callable with one retry on transient ModelErrorException.
    Nova Pro occasionally produces malformed tool-use sequences on first call.
    """
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            return llm_callable()
        except Exception as exc:
            last_exc = exc
            if "ModelErrorException" in type(exc).__name__ and attempt < max_attempts:
                print(f"  [RETRY] Transient model error (attempt {attempt}/{max_attempts}), "
                      f"retrying in {sleep_s}s…")
                time.sleep(sleep_s)
            else:
                raise
    raise last_exc  # unreachable but satisfies type checkers


# ── Graph builder ──────────────────────────────────────────────────────────────
async def build_and_run_graph() -> None:
    print(f"Connecting to MCP server at {MCP_SERVER_URL}…")

    async with sse_client(MCP_SERVER_URL) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            print("Connected. Loading tool schemas…")

            all_tools = await load_mcp_tools(session)
            print(f"Loaded {len(all_tools)} tools: {[t.name for t in all_tools]}")

            # ── Tool split: read-only vs write ─────────────────────────────
            READ_ONLY_TOOLS = {
                "configuration_view",
                "events_list",
                "namespaces_list",
                "nodes_log",
                "nodes_stats_summary",
                "nodes_top",
                "pods_get",
                "pods_list",
                "pods_list_in_namespace",
                "pods_log",
                "pods_top",
                "resources_get",
                "resources_list",
            }
            ro_tools = [t for t in all_tools if t.name in READ_ONLY_TOOLS]
            rw_tools = [t for t in all_tools if t.name not in READ_ONLY_TOOLS]
            rw_desc = ", ".join(t.name for t in rw_tools)

            print(f"  Read-only (diagnosis): {[t.name for t in ro_tools]}")
            print(f"  Write     (executor):  {[t.name for t in rw_tools]}")

            diagnosis_llm = _make_llm(temperature=0.1).bind_tools(ro_tools)
            executor_llm = _make_llm(temperature=0).bind_tools(rw_tools)

            # ── Node: supervisor ───────────────────────────────────────────
            # Pass-through placeholder.  Add multi-agent routing here later.
            def supervisor_node(state: State) -> dict:
                return {}

            # ── Node: k8s_diagnosis_agent ──────────────────────────────────
            # Read-only ReAct agent.  Loops via tools_condition until it stops
            # requesting tool calls, then emits a RECOMMENDED ACTION block.
            def k8s_diagnosis_agent(state: State) -> dict:
                system_msg = SystemMessage(
                    content=build_diagnosis_system(TARGET_NAMESPACE, rw_desc)
                )
                response = _invoke_with_retry(
                    lambda: diagnosis_llm.invoke([system_msg] + state["messages"])
                )
                return {"messages": [response]}

            # ── Node: risk_assessment ──────────────────────────────────────
            # Classifies risk of the RECOMMENDED ACTION in the last message.
            # Also extracts the action text and stores it in State so the
            # executor doesn't need to re-parse the full message history.
            def risk_assessment_node(state: State) -> dict:
                last_msg = state["messages"][-1]
                findings = (
                    last_msg.content
                    if isinstance(last_msg.content, str)
                    else str(last_msg.content)
                )

                assessment = rc.classify_risk(state["task"], findings)

                # Extract the RECOMMENDED ACTION block from the diagnosis text
                # so the executor receives a focused, unambiguous instruction.
                action = _extract_recommended_action(findings)
                if not action:
                    # Fallback: use full findings — executor will still work.
                    action = findings.strip()

                print(
                    f"\n[RISK ASSESSMENT] level={assessment.level.value!r} "
                    f"requires_approval={assessment.requires_approval}\n"
                    f"  reason : {assessment.reason}\n"
                    f"  action : {action[:200]!r}"
                )
                return {
                    "risk_level": assessment.level.value,
                    "suggested_action": action,
                }

            # ── Node: execute_action ───────────────────────────────────────
            # Reached after auto-approval (MEDIUM) or human approval (HIGH).
            # interrupt_before=["execute_action"] pauses here for HIGH risk.
            async def execute_action_node(state: State) -> dict:
                action_desc = state["suggested_action"] or ""
                risk = str(state.get("risk_level", "unknown"))

                _print_execution_header(risk, action_desc)

                # Belt-and-suspenders: block destructive actions that somehow
                # arrived without HIGH classification.
                if _is_destructive(action_desc) and risk.lower() != "high":
                    return {
                        "messages": [AIMessage(content=(
                            f"⛔ EXECUTION BLOCKED\n"
                            f"Action contains a destructive keyword "
                            f"(delete/remove/purge) but risk was classified "
                            f"as '{risk}' — not HIGH.\n"
                            f"Destructive actions require HIGH classification "
                            f"and explicit human approval.\n\n"
                            f"Blocked action:\n{action_desc}"
                        ))]
                    }

                # Ask the executor LLM which write tool to call.
                exec_msg = _invoke_with_retry(lambda: executor_llm.invoke([
                    SystemMessage(content=build_executor_system(TARGET_NAMESPACE)),
                    HumanMessage(content=(
                        f"Execute this action now. "
                        f"Namespace is '{TARGET_NAMESPACE}'.\n\n"
                        f"{action_desc}"
                    )),
                ]))

                tool_calls = getattr(exec_msg, "tool_calls", None)
                if not tool_calls:
                    print("[EXECUTE] LLM produced no tool call — treating as no-op.")
                    return {
                        "messages": [AIMessage(
                            content=f"No tool call was made. Action description:\n{action_desc}"
                        )]
                    }

                # Call each requested write tool via the live MCP session.
                results: list[str] = []
                for tc in tool_calls:
                    tool_name = tc["name"]
                    tool_args = tc["args"]
                    print(f"[EXECUTE] {tool_name}({tool_args})")
                    result = await session.call_tool(tool_name, tool_args)
                    results.append(f"{tool_name}: {result.content}")
                    print(f"[EXECUTE] ✓ {result.content}")

                return {
                    "messages": [AIMessage(
                        content="Actions executed:\n" + "\n".join(results)
                    )]
                }

            # ── Routing functions ──────────────────────────────────────────
            def route_after_risk(state: State) -> str:
                level = str(state.get("risk_level", "low")).lower()
                action = (state.get("suggested_action") or "").strip()

                if level == "low" and not action:
                    return "end"
                # MEDIUM auto-executes; HIGH also routes to execute_action
                # but is gated by interrupt_before.
                return "execute"

            # ── Graph assembly ─────────────────────────────────────────────
            builder = StateGraph(State)
            builder.add_node("supervisor", supervisor_node)
            builder.add_node("k8s_diagnosis_agent", k8s_diagnosis_agent)
            builder.add_node("tools", ToolNode(ro_tools, handle_tool_errors=True))
            builder.add_node("risk_assessment", risk_assessment_node)
            builder.add_node("execute_action", execute_action_node)

            builder.add_edge(START, "supervisor")
            builder.add_edge("supervisor", "k8s_diagnosis_agent")

            # ReAct loop: tool calls → back to agent; no tool calls → assess risk.
            builder.add_conditional_edges(
                "k8s_diagnosis_agent",
                tools_condition,
                {"tools": "tools", END: "risk_assessment"},
            )
            builder.add_edge("tools", "k8s_diagnosis_agent")

            builder.add_conditional_edges(
                "risk_assessment",
                route_after_risk,
                {"end": END, "execute": "execute_action"},
            )
            builder.add_edge("execute_action", END)

            # Checkpointer required for interrupt_before pause/resume.
            checkpointer = MemorySaver(serde=ALLOWED_MSGPACK)
            app = builder.compile(
                checkpointer=checkpointer,
                interrupt_before=["execute_action"],
            )

            run_config = {
                "configurable": {"thread_id": "incident-1"},
                "recursion_limit": MAX_GRAPH_STEPS,
            }

            # ── Initial task ───────────────────────────────────────────────
            # Namespace scoping lives in the SystemMessage (built in
            # k8s_diagnosis_agent). The HumanMessage is a clean task only.
            initial_input: State = {
                "task": (
                    "Check whether kube-state-metrics Deployment and "
                    "node-exporter DaemonSet exist and are running"
                ),
                "messages": [
                    HumanMessage(content=(
                        "Check whether the kube-state-metrics Deployment and "
                        "the node-exporter DaemonSet exist and are running. "
                        "If they are already running and healthy, explicitly "
                        "state which namespace you checked and note that if "
                        "the user expected them to be absent or failing, they "
                        "should verify the correct namespace."
                    ))
                ],
                "risk_level": None,
                "suggested_action": None,
                "approval_status": None,
            }

            print(f"\n{'='*60}")
            print(f"TARGET NAMESPACE : {TARGET_NAMESPACE}")
            print(f"MCP SERVER       : {MCP_SERVER_URL}")
            print(f"MAX STEPS        : {MAX_GRAPH_STEPS}")
            print(f"{'='*60}\n")

            # ── First run (diagnosis + risk assessment) ────────────────────
            result = await app.ainvoke(initial_input, config=run_config)

            print("\n=== Reasoning trace ===")
            for msg in result["messages"]:
                role = msg.__class__.__name__
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                print(f"\n[{role}]\n{content}")
                if tc := getattr(msg, "tool_calls", None):
                    print(f"  → tool_calls: {tc}")

            # ── Human approval gate ──────────────────────────────────
            # interrupt_before=["execute_action"] pauses the graph for
            # EVERY risk level.  _handle_approval checks the actual
            # risk and only prompts for HIGH — MEDIUM auto-approves.
            snapshot = app.get_state(run_config)
            if snapshot.next:
                await _handle_approval(
                    app=app,
                    run_config=run_config,
                    result=result,
                    target_namespace=TARGET_NAMESPACE,
                )
            else:
                print("\n=== Final answer ===")
                print(result["messages"][-1].content)


async def _handle_approval(
    app,
    run_config: dict,
    result: dict,
    target_namespace: str,
) -> None:
    """
    Approval gate for execute_action.

    interrupt_before pauses for ALL risk levels, so this function
    inspects the actual risk and:
      - LOW   → auto-reject (nothing to execute — should not reach here)
      - MEDIUM → auto-approve (safe, reversible actions)
      - HIGH   → interactive prompt (destructive / risky)
    """
    risk = str(result.get("risk_level", "low")).lower()
    action = str(result.get("suggested_action", ""))
    destructive = _is_destructive(action)

    # Belt-and-suspenders: if risk is HIGH but the destructive safety
    # check in execute_action_node would block it anyway, flag it here.
    if destructive and risk != "high":
        print(
            f"\n⚠️  WARNING: destructive keywords detected but risk={risk!r}. "
            f"The execute_action safety gate will block this."
        )

    # ── LOW risk: should not have actions — reject gracefully ──
    if risk == "low":
        print(f"\n[APPROVAL] Risk is LOW — nothing to execute. Skipping.")
        await app.aupdate_state(run_config, {"approval_status": "rejected"})
        return

    # ── MEDIUM risk: auto-approve (safe, reversible mutations) ──
    if risk == "medium":
        print(f"\n[APPROVAL] Risk is MEDIUM — auto-approving safe action.")
        print(f"  Namespace: {target_namespace}")
        print(f"  Action:    {action[:200]}")
        await app.aupdate_state(run_config, {"approval_status": "approved"})
        print("Executing…\n")
        final = await app.ainvoke(None, config=run_config)
        print("\n=== Execution result ===")
        for msg in final["messages"]:
            if isinstance(msg, AIMessage):
                print(msg.content)
        return

    # ── HIGH risk: interactive human approval ──
    print(f"\n{'='*60}")
    print("⏸  PAUSED — Human approval required")
    print(f"{'='*60}")
    print(f"Risk level : {risk.upper()}")
    if destructive:
        print("⚠️  TYPE       : DESTRUCTIVE "
              "(resources will be permanently deleted)")
    print(f"Namespace  : {target_namespace}")
    print(f"\nSuggested action:\n  {action}\n")
    print(f"{'='*60}")

    prompt = (
        "\n⚠️  DESTRUCTIVE action. Approve permanent deletion? [y/N]: "
        if destructive
        else "\nApprove this action? [y/N]: "
    )
    decision = input(prompt).strip().lower()

    if decision in ("y", "yes"):
        await app.aupdate_state(run_config, {"approval_status": "approved"})
        print("Approved. Resuming execution…\n")
        final = await app.ainvoke(None, config=run_config)
        print("\n=== Execution result ===")
        for msg in final["messages"]:
            if isinstance(msg, AIMessage):
                print(msg.content)
    else:
        await app.aupdate_state(run_config, {"approval_status": "rejected"})
        print("Rejected. Execution stopped. No changes were made.")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _is_destructive(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in _DESTRUCTIVE_KW)


def _extract_recommended_action(findings: str) -> str:
    """
    Pull the RECOMMENDED ACTION block from the diagnosis agent's output.
    Returns the block text if found, empty string otherwise.
    """
    marker = "RECOMMENDED ACTION:"
    idx = findings.find(marker)
    if idx == -1:
        return ""
    return findings[idx:].strip()


def _print_execution_header(risk: str, action: str) -> None:
    print(f"\n{'='*60}")
    print("EXECUTING")
    print(f"  Risk   : {risk.upper()}")
    print(f"  Action : {action[:300]}")
    if _is_destructive(action):
        print("  ⚠️  WARNING: This action involves DELETION!")
    print(f"{'='*60}")


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(build_and_run_graph())