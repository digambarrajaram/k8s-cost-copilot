#!/usr/bin/env python3
"""
validate-dashboard.py — Cross-check Grafana dashboard data against live cluster state.

Usage:
  python3 validate-dashboard.py

Requires:
  - kubectl access to the cluster
  - Prometheus API reachable (default: http://52.70.236.20:9090)
  - requests library (pip install requests)

Prints a side-by-side comparison of every dashboard metric vs kubectl ground truth.
"""

import subprocess
import json
import sys
import os
from typing import Any

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://52.70.236.20:9090")

# ── Helpers ────────────────────────────────────────────────────────────────────

def kubectl(cmd: str) -> str:
    """Run a kubectl command, return stdout or error message."""
    try:
        result = subprocess.run(
            f"kubectl {cmd}",
            shell=True, capture_output=True, text=True, timeout=15,
        )
        return result.stdout.strip() or result.stderr.strip()
    except Exception as e:
        return f"ERROR: {e}"


def kubectl_json(cmd: str) -> Any:
    """Run kubectl -o json and parse."""
    raw = kubectl(f"{cmd} -o json 2>/dev/null")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def prometheus_query(query: str) -> Any:
    """Run an instant PromQL query, return parsed result."""
    import urllib.request
    import urllib.parse
    url = f"{PROMETHEUS_URL}/api/v1/query?query={urllib.parse.quote(query)}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"status": "error", "error": str(e)}


def prom_value(result: dict) -> str:
    """Extract the first value from a Prometheus query result."""
    if result.get("status") != "success":
        return f"error: {result.get('error', 'unknown')}"
    data = result.get("data", {}).get("result", [])
    if not data:
        return "no data"
    val = data[0].get("value", [None, "0"])
    return str(val[1]) if len(val) > 1 else "no value"


def ok_or_fail(label: str, prom: str, k8s: str, tolerance: float = 0.1) -> str:
    """Compare two numeric-ish values within tolerance. Returns a status icon."""
    try:
        p = float(prom)
        k = float(k8s)
        if k == 0:
            return "✓" if p == 0 else "⚠ MISMATCH"
        diff = abs(p - k) / abs(k)
        return "✓" if diff <= tolerance else f"⚠ DIFF {diff:.1%}"
    except (ValueError, TypeError):
        # Non-numeric — just compare as strings
        p_str = str(prom).strip()
        k_str = str(k8s).strip()
        return "✓" if p_str == k_str else f"⚠ DIFF"


# ── Validation table ───────────────────────────────────────────────────────────

CHECKS = [
    # ── Cluster Overview ──
    ("Nodes count",
     "count(kube_node_info)",
     "get nodes --no-headers | wc -l"),

    ("Namespaces count",
     "count(kube_namespace_created)",
     "get namespaces --no-headers | wc -l"),

    ("Running pods",
     "count(kube_pod_status_phase{phase=\"Running\"} == 1)",
     "get pods --all-namespaces --field-selector=status.phase=Running --no-headers | wc -l"),

    ("Non-running pods",
     "count(kube_pod_info) - count(kube_pod_status_phase{phase=\"Running\"} == 1)",
     "get pods --all-namespaces --field-selector=status.phase!=Running --no-headers | grep -v Completed | wc -l"),

    # ── Deployments ready ──
    ("Deployments ready (mcp-test)",
     "sum(kube_deployment_status_replicas_ready{namespace=\"mcp-test\"})",
     "get deployments -n mcp-test -o json | python3 -c \"import sys,json; d=json.load(sys.stdin); print(sum(i['status'].get('readyReplicas',0) or 0 for i in d['items']))\""),

    # ── DaemonSets ready ──
    ("DaemonSets ready (mcp-test)",
     "sum(kube_daemonset_status_number_ready{namespace=\"mcp-test\"})",
     "get daemonsets -n mcp-test -o json | python3 -c \"import sys,json; d=json.load(sys.stdin); print(sum(i['status'].get('numberReady',0) or 0 for i in d['items']))\""),

    # ── Monitoring stack ──
    ("kube-state-metrics ready",
     "kube_deployment_status_replicas_ready{deployment=\"kube-state-metrics\"}",
     "get deployment kube-state-metrics -n mcp-test -o json 2>/dev/null | python3 -c \"import sys,json; d=json.load(sys.stdin); print(d.get('status',{}).get('readyReplicas',0) or 0)\""),

    ("node-exporter ready",
     "kube_daemonset_status_number_ready{daemonset=\"node-exporter\"}",
     "get daemonset node-exporter -n mcp-test -o json 2>/dev/null | python3 -c \"import sys,json; d=json.load(sys.stdin); print(d.get('status',{}).get('numberReady',0) or 0)\""),

    ("Prometheus ready",
     "kube_deployment_status_replicas_ready{deployment=\"prometheus-deployment\"}",
     "get deployment prometheus-deployment -n mcp-test -o json 2>/dev/null | python3 -c \"import sys,json; d=json.load(sys.stdin); print(d.get('status',{}).get('readyReplicas',0) or 0)\""),

    # ── Restarts ──
    ("Total pod restarts",
     "sum(kube_pod_container_status_restarts_total)",
     "get pods --all-namespaces -o json | python3 -c \"import sys,json; d=json.load(sys.stdin); print(sum(sum(c.get('restartCount',0) for c in p.get('status',{}).get('containerStatuses',[])) for p in d['items']))\""),
]


def main():
    print(f"{'='*80}")
    print(f"DASHBOARD VALIDATION — Prometheus vs kubectl ground truth")
    print(f"Prometheus: {PROMETHEUS_URL}")
    print(f"{'='*80}")
    print(f"{'CHECK':40s} {'PROMETHEUS':>12s} {'KUBECTL':>12s} {'STATUS':>10s}")
    print(f"{'-'*40} {'-'*12} {'-'*12} {'-'*10}")

    for label, promql, kctl_cmd in CHECKS:
        prom_result = prometheus_query(promql)
        prom_val = prom_value(prom_result)
        k8s_val = kubectl(kctl_cmd).strip().split('\n')[-1]  # last line
        status = ok_or_fail(label, prom_val, k8s_val)
        print(f"{label:40s} {prom_val:>12s} {k8s_val:>12s} {status:>10s}")

    print(f"{'='*80}")

    # ── Bonus: show what Prometheus scrape targets are up ──
    print("\nPrometheus scrape targets (up/down):")
    up_result = prometheus_query("up")
    if up_result.get("status") == "success":
        for ts in up_result["data"]["result"]:
            job = ts["metric"].get("job", "?")
            instance = ts["metric"].get("instance", "?")
            val = ts["value"][1]
            icon = "✓" if val == "1" else "✗"
            print(f"  {icon} job={job:35s} instance={instance}")

    # ── Show top 5 pods by restart count from both sources ──
    print("\nTop 5 pods by restarts (Prometheus):")
    top = prometheus_query(
        "topk(5, kube_pod_container_status_restarts_total)"
    )
    if top.get("status") == "success":
        for ts in top["data"]["result"]:
            ns = ts["metric"].get("namespace", "?")
            pod = ts["metric"].get("pod", "?")
            print(f"  {ns}/{pod}: {ts['value'][1]}")

    print("\nTop 5 pods by restarts (kubectl):")
    top_kctl = kubectl(
        "get pods --all-namespaces --sort-by=.status.containerStatuses[0].restartCount "
        "-o json 2>/dev/null | python3 -c \"\n"
        "import sys,json\nd=json.load(sys.stdin)\n"
        "items=sorted(d['items'], key=lambda p: sum("
        "c.get('restartCount',0) for c in "
        "p.get('status',{}).get('containerStatuses',[])), reverse=True)\n"
        "for p in items[:5]:\n"
        "  ns=p['metadata']['namespace']\n"
        "  n=p['metadata']['name']\n"
        "  r=sum(c.get('restartCount',0) for c in "
        "p.get('status',{}).get('containerStatuses',[]))\n"
        "  print(f'  {ns}/{n}: {r}')\n"
        "\""
    )
    print(top_kctl)


if __name__ == "__main__":
    main()
