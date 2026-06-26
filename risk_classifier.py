# risk_classifier.py
"""
Risk classification for Kubernetes actions.

Single responsibility: given a diagnosis text, return a RiskLevel.
This module does NOT own suggested_action — that lives in State and
is set by the diagnosis agent. The classifier only rates risk.
"""
from enum import Enum
import os

from langchain_aws import ChatBedrockConverse
from pydantic import BaseModel, Field

from utils.aws_session import get_bedrock_client

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "amazon.nova-pro-v1:0")


class RiskLevel(str, Enum):
    LOW = "low"       # no mutation needed
    MEDIUM = "medium" # safe, reversible, non-destructive mutation
    HIGH = "high"     # destructive or irreversible — human approval required


class RiskAssessment(BaseModel):
    level: RiskLevel
    reason: str = Field(
        description="One sentence explaining the classification."
    )
    requires_approval: bool = Field(
        default=False,
        description=(
            "True only for HIGH risk. "
            "False for LOW and MEDIUM — those execute automatically."
        ),
    )
    # NOTE: suggested_action is intentionally removed from this schema.
    # The diagnosis agent owns and sets the action; the classifier only
    # rates the risk of that action. Mixing them caused the classifier to
    # override correct diagnoses.


_CLASSIFIER_SYSTEM = """\
You are a Kubernetes action risk classifier.

Your ONLY job: read the recommended action in the findings and classify its
risk level. Do NOT re-diagnose, do NOT change the action, do NOT second-guess
whether the diagnosis is correct.

══════════════════════════════════════════════════════════
CLASSIFICATION TABLE  (apply the FIRST matching rule)
══════════════════════════════════════════════════════════

LOW — No mutation will occur. Nothing will be executed.
  • Findings conclude everything is healthy / already running.
  • Issue is already resolved.
  • Recommended action is "None" or informational only.
  requires_approval: false

MEDIUM — Safe, reversible, non-destructive mutation.
  • Create a resource: ServiceAccount, ConfigMap, Secret, Service, Ingress
  • Scale UP a Deployment or ReplicaSet (increase replicas)
  • Restart a single pod (pods_delete on one failing pod to let it respawn)
  • Apply / patch a non-RBAC resource manifest
  requires_approval: false

HIGH — Destructive or risky.  Triggers (ANY ONE is sufficient):
  • Action contains: delete, remove, purge, destroy, resources_delete,
    pods_delete (deleting an entire workload, not a single-pod restart)
  • Modify RBAC, ClusterRole, ClusterRoleBinding, NetworkPolicy, or node config
  • Scale DOWN a StatefulSet, database, or storage workload
  • Any action that targets a production namespace
  • Deletion recommended when a create/fix would resolve the issue
  requires_approval: true

══════════════════════════════════════════════════════════
DECISION SHORTCUTS
══════════════════════════════════════════════════════════
Recommended action → Level
  "create X"          → MEDIUM
  "scale up X"        → MEDIUM
  "restart pod X"     → MEDIUM
  "delete X"          → HIGH  (always, no exceptions)
  "remove X"          → HIGH
  "None / no action"  → LOW

IMPORTANT: If uncertain between two levels, always choose the HIGHER one.
"""


classifier_llm = ChatBedrockConverse(
    model_id=BEDROCK_MODEL_ID,
    client=get_bedrock_client(AWS_REGION),
    temperature=0,          # deterministic classification
).with_structured_output(RiskAssessment)


def classify_risk(task: str, findings: str) -> RiskAssessment:
    """
    Classify the risk of the action recommended in `findings`.

    Args:
        task:     Short description of the overall task (for context).
        findings: Full text of the diagnosis agent's final message,
                  including its RECOMMENDED ACTION block.

    Returns:
        RiskAssessment with level, reason, and requires_approval.
    """
    return classifier_llm.invoke([
        ("system", _CLASSIFIER_SYSTEM),
        ("human", f"Task: {task}\n\nDiagnosis findings:\n{findings}"),
    ])