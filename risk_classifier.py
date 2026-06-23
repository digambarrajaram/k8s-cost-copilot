# risk_classifier.py
from enum import Enum
import os
from langchain_aws import ChatBedrockConverse
from pydantic import BaseModel, Field
from utils.aws_session import get_bedrock_client

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "amazon.nova-pro-v1:0")


class RiskLevel(str, Enum):
    LOW = "low"       # read-only findings, no action needed
    MEDIUM = "medium" # safe corrective actions (restart pod, scale deployment)
    HIGH = "high"     # destructive or costly — human approval required


class RiskAssessment(BaseModel):
    level: RiskLevel
    reason: str
    requires_approval: bool = Field(
        default=False,
        description="Whether human approval is required before executing. "
                    "Set True for HIGH risk, False for LOW/MEDIUM.",
    )
    suggested_action: str = Field(
        default="",
        description="The recommended corrective action to execute.",
    )


classifier_llm = ChatBedrockConverse(
    model_id="amazon.nova-pro-v1:0",
    client=get_bedrock_client(AWS_REGION),
).with_structured_output(RiskAssessment)


def classify_risk(task: str, findings: str) -> RiskAssessment:
    return classifier_llm.invoke([
        ("system", """You are a Kubernetes risk classifier.

Classify the RECOMMENDED ACTION into one of these levels:

LOW:    Purely informational — no mutation required or the work is
        already done. Nothing left to execute.
        (e.g. "all pods are healthy", "issue already resolved")

MEDIUM: Any mutation that is safe and reversible:
        - Create a service, configmap, or secret
        - Restart, scale up, or delete a single pod
        - Update a safe resource
        (e.g. "create service X", "delete pod Y", "scale up Z")

HIGH:   Destructive, risky, or irreversible:
        - Delete a namespace, deployment, or statefulset
        - Modify RBAC, network policies, or node config
        - Scale down a database (data loss risk)
        - Any action on production infrastructure

KEY RULE: If the findings RECOMMEND A SPECIFIC ACTION (create,
delete, scale, restart, etc.), that is at least MEDIUM. Only
classify as LOW if truly nothing needs to be done.

Always err toward HIGHER risk when uncertain."""),
        ("human", f"Task: {task}\nFindings & recommended action: {findings}")
    ])