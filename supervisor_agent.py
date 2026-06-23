"""
Kubernetes Diagnosis Agent — LangGraph + MCP (SSE) + AWS Bedrock (Nova Pro)
With risk classification + human-approval gate for risky actions.

Flow:
  supervisor -> k8s_diagnosis_agent <-> tools  (loops until done investigating)
             -> risk_assessment
             -> [low: END] [medium: execute_action -> END] [high: notify_slack(DISABLED) -> approval -> execute_action or END]
             -> END

Notes:
- Slack notification and audit-log nodes are commented out (not needed yet).
- interrupt_before=["execute_action"] is still active so high-risk actions pause
  for human approval before executing.
"""

import os
import asyncio
from typing import Annotated, TypedDict, Optional

from dotenv import load_dotenv

from langgraph.graph import START, END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.checkpoint.memory import MemorySaver
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, BaseMessage

from mcp import ClientSession
from mcp.client.sse import sse_client
from langchain_mcp_adapters.tools import load_mcp_tools

import risk_classifier as rc
from utils.aws_session import get_bedrock_client
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

# Register risk_classifier types for LangGraph checkpoint serialization
# so they survive pause/resume without warnings.
ALLOWED_MSGPACK = JsonPlusSerializer(
    allowed_msgpack_modules=[("risk_classifier", "RiskLevel")],
)

load_dotenv()

MCP_SERVER_URL = os.environ["MCP_SERVER_URL"]
TARGET_NAMESPACE = os.environ.get("K8S_TARGET_NAMESPACE", "mcp-test")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "amazon.nova-pro-v1:0")
MAX_GRAPH_STEPS = int(os.environ.get("MAX_GRAPH_STEPS", "25"))


class State(TypedDict):
    task: str
    messages: Annotated[list[BaseMessage], add_messages]
    risk_level: Optional[str]
    suggested_action: Optional[str]
    approval_status: Optional[str]   # "approved" | "rejected" | None


diagnosis_llm = ChatBedrockConverse(
    model_id=BEDROCK_MODEL_ID,
    client=get_bedrock_client(AWS_REGION),
    temperature=0.1,
)


async def build_and_run_graph():
    print(f"Connecting to MCP server at {MCP_SERVER_URL}...")

    try:
        async with sse_client(MCP_SERVER_URL) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                print("Connected. Fetching cluster tool schemas...")

                langchain_tools = await load_mcp_tools(session)
                print(f"Loaded {len(langchain_tools)} Kubernetes tools: "
                      f"{[t.name for t in langchain_tools]}")

                # Split tools: read-only for diagnosis, write tools only
                # available in execute_action (gated by risk + approval).
                READ_ONLY = {
                    "configuration_view", "events_list", "namespaces_list",
                    "nodes_log", "nodes_stats_summary", "nodes_top",
                    "pods_get", "pods_list", "pods_list_in_namespace",
                    "pods_log", "pods_top", "resources_get", "resources_list",
                }
                ro_tools = [t for t in langchain_tools if t.name in READ_ONLY]
                rw_tools = [t for t in langchain_tools if t.name not in READ_ONLY]
                print(f"  Diagnosis (read-only): {[t.name for t in ro_tools]}")
                print(f"  Execute  (write):     {[t.name for t in rw_tools]}")

                # Diagnosis agent only gets read-only tools — it cannot
                # mutate anything. Destructive tools are reserved for
                # execute_action, which is gated by risk assessment + approval.
                llm_with_tools = diagnosis_llm.bind_tools(ro_tools)

                # -----------------------------------------------------
                # supervisor: pass-through placeholder. Wire real routing
                # here once a second specialist agent is added.
                # -----------------------------------------------------
                def supervisor_node(state: State) -> dict:
                    return {}

                # -----------------------------------------------------
                # k8s_diagnosis_agent: read-only investigation only.
                # When it detects a problem that needs a fix, it should
                # explain what action is needed — the execute_action node
                # (gated by risk + approval) will carry it out.
                # -----------------------------------------------------
                rw_names = [t.name for t in rw_tools]
                rw_desc = ", ".join(rw_names)

                def k8s_diagnosis_agent(state: State) -> dict:
                    system_instruction = SystemMessage(content=(
                        "You are an AI Kubernetes diagnosis agent. "
                        f"Investigate issues in the '{TARGET_NAMESPACE}' namespace. "
                        "You have READ-ONLY access to pods, deployments, events, "
                        "and logs. You CANNOT delete, restart, scale, or modify "
                        "anything yourself.\n\n"
                        "WHEN CREATING OR RECONFIGURING RESOURCES:\n"
                        "- Use resources_get to inspect EXISTING pods, deployments, "
                        "  and services to extract real selector labels, ports, and "
                        "  container specs.\n"
                        "- Never invent selectors or ports from scratch — copy "
                        "  them from the actual running resource the new object "
                        "  should target.\n"
                        "- For example, if asked to create a service for a "
                        "  deployment, FIRST run resources_get on that deployment "
                        "  to read its labels and container ports, THEN recommend "
                        "  the service using those exact values.\n\n"
                        "After diagnosing, recommend a corrective action. The "
                        "executor has access to these write tools:\n"
                        f"  {rw_desc}\n\n"
                        "IMPORTANT: Describe your recommended action by naming "
                        "the exact tool, namespace, resource name, and all "
                        "parameters. "
                        "For example: \"Use pods_delete to delete pod X in "
                        "namespace Y\" or \"Use resources_scale to scale "
                        "statefulset Z to 1 replica\".\n\n"
                        "NEVER tell the user to run kubectl or any manual "
                        "commands. ALWAYS recommend an action the executor can "
                        "perform using the available tools."
                    ))
                    # Nova Pro occasionally produces malformed tool-use
                    # sequences — retry once on that specific error.
                    import time
                    for attempt in (1, 2):
                        try:
                            response = llm_with_tools.invoke(
                                [system_instruction] + state["messages"]
                            )
                            return {"messages": [response]}
                        except Exception as exc:
                            err = str(exc)
                            if "ModelErrorException" in err and attempt == 1:
                                print(f"  [RETRY] Nova tool-use glitch, "
                                      f"retrying ({attempt}/2)...")
                                time.sleep(2)
                            else:
                                raise

                # -----------------------------------------------------
                # risk_assessment: runs once the diagnosis agent has
                # given its final answer (no more tool calls pending).
                # -----------------------------------------------------
                def risk_assessment_node(state: State) -> dict:
                    findings = state["messages"][-1].content
                    risk = rc.classify_risk(state["task"], findings)
                    # If the classifier didn't provide an action, use the
                    # full diagnosis findings as the action description so
                    # the executor has the tool-call details it needs.
                    action = risk.suggested_action.strip() or findings.strip()
                    print(f"\n[RISK ASSESSMENT] level={risk.level.value} "
                          f"action={action!r}")
                    return {
                        "risk_level": risk.level,
                        "suggested_action": action,
                    }

                # -----------------------------------------------------
                # notify_slack_node: DISABLED — uncomment when you add
                # a real Slack webhook integration.
                # -----------------------------------------------------
                # def notify_slack_node(state: State) -> dict:
                #     print(
                #         f"[SLACK - TODO real integration] Risk={state['risk_level']} "
                #         f"Suggested action={state['suggested_action']} "
                #         "-> waiting for human approval"
                #     )
                #     return {"approval_status": state.get("approval_status")}

                # -----------------------------------------------------
                # execute_action: reached after medium-risk auto-approval
                # or explicit human approval for high risk.
                # interrupt_before=["execute_action"] will pause execution
                # here until app.aupdate_state sets approval_status.
                # -----------------------------------------------------
                executor_llm = diagnosis_llm.bind_tools(rw_tools)

                async def execute_action_node(state: State) -> dict:
                    action_desc = state["suggested_action"]
                    print(f"\n[EXECUTE] Attempting: {action_desc!r}")

                    # Ask the LLM which write tool to call, with retry for
                    # Nova tool-use glitches.
                    import time
                    exec_msg = None
                    for attempt in (1, 2):
                        try:
                            exec_msg = executor_llm.invoke([
                                SystemMessage(content=(
                                    "You are a Kubernetes action executor. "
                                    "You have WRITE access to delete, scale, "
                                    "restart, and modify resources.\n\n"
                                    "CRITICAL RULES:\n"
                                    f"1. EVERY tool call MUST include "
                                    f"namespace='{TARGET_NAMESPACE}' — never "
                                    f"omit it or use 'default'.\n"
                                    "2. Call the tool ONCE with all correct "
                                    "parameters.\n"
                                    "3. Read the action description carefully "
                                    "and copy the exact resource name, labels, "
                                    "ports, and selector from it.\n"
                                    "4. For resources_create_or_update, pass "
                                    "namespace, name, apiVersion, kind, and "
                                    "body as separate parameters — do not "
                                    "embed them all in body."
                                )),
                                HumanMessage(content=(
                                    f"Execute this action in namespace "
                                    f"'{TARGET_NAMESPACE}':\n{action_desc}"
                                )),
                            ])
                            break
                        except Exception as exc:
                            if "ModelErrorException" in str(exc) and attempt == 1:
                                print(f"  [RETRY] Nova tool-use glitch "
                                      f"({attempt}/2)...")
                                time.sleep(2)
                            else:
                                raise

                    tool_calls = getattr(exec_msg, "tool_calls", None)
                    if not tool_calls:
                        print("[EXECUTE] LLM did not request a tool call — "
                              "action may be a no-op.")
                        return {
                            "messages": [
                                AIMessage(content=f"Executed (no tool needed): "
                                         f"{action_desc}")
                            ]
                        }

                    # Call each requested write tool via the MCP session.
                    results = []
                    for tc in tool_calls:
                        tool_name = tc["name"]
                        tool_args = tc["args"]
                        print(f"[EXECUTE] Calling {tool_name}({tool_args})...")
                        result = await session.call_tool(tool_name, tool_args)
                        results.append(f"{tool_name}: {result.content}")
                        print(f"[EXECUTE] Done: {result.content}")

                    return {
                        "messages": [
                            AIMessage(content="Actions executed:\n" +
                                      "\n".join(results))
                        ]
                    }

                # -----------------------------------------------------
                # log_to_audit_table: DISABLED — uncomment when you wire
                # a real audit store (DB table, S3, etc.).
                # -----------------------------------------------------
                # def log_to_audit_table(state: State) -> dict:
                #     print(
                #         "[AUDIT LOG] "
                #         f"task={state['task']!r} risk={state.get('risk_level')} "
                #         f"action={state.get('suggested_action')} "
                #         f"approval={state.get('approval_status')}"
                #     )
                #     return {}

                # -----------------------------------------------------
                # Routing functions
                # -----------------------------------------------------
                def route_after_risk(state: State) -> str:
                    level = state["risk_level"]
                    has_action = bool(
                        state.get("suggested_action", "").strip()
                    )
                    if level == "low" and not has_action:
                        # Nothing to do — diagnosis only.
                        return "end"
                    elif level in ("low", "medium"):
                        # Safe actions: auto-execute.
                        return "execute"
                    else:
                        # High risk: pause for human approval via
                        # interrupt_before=["execute_action"].
                        return "execute"

                def route_after_approval(state: State) -> str:
                    # Only reached for HIGH risk after interrupt_before resumes.
                    if state.get("approval_status") == "approved":
                        return "execute"
                    # Rejected or no decision: stop without acting.
                    return "end"

                # -----------------------------------------------------
                # Build the graph
                # -----------------------------------------------------
                builder = StateGraph(State)
                builder.add_node("supervisor", supervisor_node)
                builder.add_node("k8s_diagnosis_agent", k8s_diagnosis_agent)
                builder.add_node("tools", ToolNode(ro_tools, handle_tool_errors=True))
                builder.add_node("risk_assessment", risk_assessment_node)
                # builder.add_node("notify_slack", notify_slack_node)   # DISABLED
                builder.add_node("execute_action", execute_action_node)
                # builder.add_node("log", log_to_audit_table)           # DISABLED

                builder.add_edge(START, "supervisor")
                builder.add_edge("supervisor", "k8s_diagnosis_agent")

                # Think -> act -> observe loop:
                # If the agent requested tool calls, run them, then loop back.
                # Once it stops requesting tools, move on to risk assessment.
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

                # NOTE: route_after_approval is only active if you re-enable
                # notify_slack and add it back into the graph. For now,
                # interrupt_before pauses at execute_action directly and
                # resumes with aupdate_state + ainvoke(None).
                # builder.add_conditional_edges(
                #     "notify_slack",
                #     route_after_approval,
                #     {"execute": "execute_action", "end": END},
                # )

                builder.add_edge("execute_action", END)

                # A checkpointer is required for interrupt_before to pause-and-
                # resume rather than just stopping. thread_id lets you resume
                # this exact run later after a human approves.
                checkpointer = MemorySaver(serde=ALLOWED_MSGPACK)
                app = builder.compile(
                    checkpointer=checkpointer,
                    interrupt_before=["execute_action"],
                )
                #app.get_graph().print_ascii()
                run_config = {
                    "configurable": {"thread_id": "incident-1"},
                    "recursion_limit": MAX_GRAPH_STEPS,
                }

                initial_input: State = {
                    "task": "Kubernetes Diagnosis Agent",
                    "messages": [
                        HumanMessage(content=(
                            f"Check the {TARGET_NAMESPACE} namespace. "
                            "check and resolve the issue in prometheus-deployment-6b9447c6d7-hdm4x"
                        ))
                    ],
                    "risk_level": None,
                    "suggested_action": None,
                    "approval_status": None,
                }

                print("\nExecuting LangGraph workflow...\n")
                result = await app.ainvoke(initial_input, config=run_config)

                print("=== Full reasoning trace ===")
                for msg in result["messages"]:
                    role = msg.__class__.__name__
                    content = msg.content if isinstance(msg.content, str) else str(msg.content)
                    print(f"\n[{role}] {content}")
                    tool_calls = getattr(msg, "tool_calls", None)
                    if tool_calls:
                        print(f"  -> requested tool calls: {tool_calls}")

                # Check whether the graph paused before execute_action.
                state_snapshot = app.get_state(run_config)
                if state_snapshot.next:
                    print(
                        f"\n=== PAUSED before: {state_snapshot.next} ===\n"
                        f"Risk level: {result.get('risk_level')}\n"
                        f"Suggested action: {result.get('suggested_action')}\n"
                    )
                    # Interactive approval prompt.
                    decision = input(
                        "\nApprove this action? [y/N]: "
                    ).strip().lower()
                    if decision in ("y", "yes"):
                        await app.aupdate_state(
                            run_config, {"approval_status": "approved"}
                        )
                        print("Approved. Resuming execution...\n")
                        result = await app.ainvoke(None, config=run_config)
                        print("\n=== Action result ===")
                        for msg in result["messages"]:
                            if isinstance(msg, AIMessage):
                                print(msg.content)
                    else:
                        await app.aupdate_state(
                            run_config, {"approval_status": "rejected"}
                        )
                        print("Rejected. Execution stopped.")
                else:
                    print("\n=== Final answer ===")
                    print(result["messages"][-1].content)

    except Exception as exc:
        print(f"\nAgent run failed: {exc!r}")
        raise


if __name__ == "__main__":
    asyncio.run(build_and_run_graph())