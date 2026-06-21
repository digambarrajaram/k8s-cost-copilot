from typing import TypedDict

from dotenv import load_dotenv
import os
import base64
from langgraph.graph import START, END, StateGraph #type: ignore
#from langgraph_supervisor import create_supervisor #type: ignore
#from langgraph.prebuilt import create_react_agent #type: ignore
from langchain_aws import ChatBedrockConverse   #type: ignore
from pathlib import Path

load_dotenv()

class State(TypedDict):
    task: str

    

bedrock_kwargs = {
    "model_id": "amazon.nova-pro-v1:0",
    "region_name": "us-east-1",
    "temperature": 0.4,
}


# Initialize Nova Pro for the high-reasoning Supervisor task
supervisor_llm = ChatBedrockConverse(**bedrock_kwargs)


def supervisor_node(state: State) -> dict:
    """Acts as the AI DevOps supervisor to delegate or conclude."""
    return k8s_diagnosis_agent(state)

def k8s_diagnosis_agent(state: State) -> dict:
    """Fetches real-time diagnostics, pods statuses, and error trace messages.You are an AI DevOps supervisor coordinating an automated troubleshooting team. role is to delegate work based on specialized knowledge.Delegate tasks to 'k8s_diagnosis_agent' to check Kubernetes cluster status, read logs, or fetch cluster states.When the worker discovers an issue, summarize the root cause and report the fix back to the user."""
    return supervisor_llm.invoke(state["text"])



# 3. Build the Graph
builder = StateGraph(State)

builder.add_node("supervisor_node", supervisor_node)
builder.add_node("k8s_diagnosis_agent", k8s_diagnosis_agent)

builder.add_edge(START, "supervisor_node")
builder.add_edge("supervisor_node", "k8s_diagnosis_agent")
builder.add_edge("k8s_diagnosis_agent", END)

app = builder.compile()

initial_input = {"text": "container is crashing due to OOM"}
result = app.invoke(initial_input)

print("\nFinal Result:", result["text"])







