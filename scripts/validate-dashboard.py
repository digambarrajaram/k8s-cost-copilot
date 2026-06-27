#!/usr/bin/env python3
"""
validate-dashboard.py — Cross-check Grafana dashboard data against live cluster state.

Usage:
  python3 validate-dashboard.py

Requires:
  - kubectl access to the cluster
  - Prometheus API reachable (reads PROMETHEUS_URL from .env)
  - python-dotenv (pip install python-dotenv)

All connection settings are read from .env — no hardcoded IPs.
"""

import json
import os
import shlex
import subprocess
import sys
import urllib.parse
import urllib.request
from typing import Any

from dotenv import load_dotenv
load_dotenv()

# ── Configuration from .env ────────────────────────────────────────────────────
CLUSTER_IP = os.environ.get("CLUSTER_IP", "localhost")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", f"http://{CLUSTER_IP}:9090")
GRAFANA_URL = os.environ.get("GRAFANA_URL", f"http://{CLUSTER_IP}:3000")
GRAFANA_UID = os.environ.get("GRAFANA_PROMETHEUS_UID", "")
TARGET_NS = os.environ.get("K8S_TARGET_NAMESPACE", "mcp-test")


# ── Prometheus helpers ─────────────────────────────────────────────────────────

def prometheus_query(query: str) -> Any:
    """Run an instant PromQL query, return parsed result."""
    url = f"{PROMETHEUS_URL}/api/v1/query?query={urllib.parse.quote(query)}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"status": "error", "error": str(e)}


def prometheus_label_values(label: str, metric: str = "") -> list[str]:
    """Fetch label values via the Prometheus labels API (not PromQL)."""
    path = f"/api/v1/label/{urllib.parse.quote(label)}/values"
    if metric:
        path += f"?match[]={urllib.parse.quote(metric)}"
    url = f"{PROMETHEUS_URL}{path}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
            return data.get("data", []) if data.get("status") == "success" else []
    except Exception:
        return []


def prom_value(result: dict) -> str:
    """Extract the first value from a Prometheus query result."""
    if result.get("status") != "success":
        return f"error: {result.get('error', 'unknown')}"
    data = result.get("data", {}).get("result", [])
    if not data:
        return "no data"
    val = data[0].get("value", [None, "0"])
    return str(val[1]) if len(val) > 1 else "no value"


# ── kubectl helpers ────────────────────────────────────────────────────────────

def kubectl(cmd: str) -> str:
    """Run a kubectl command safely (no shell)."""
    try:
        result = subprocess.run(
            shlex.split(f"kubectl {cmd}"),
            capture_output=True, text=True, timeout=15,
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


# ── Pure-Python kubectl queries (no shell, no pipes) ───────────────────────────

def k8s_count(cmd: str) -> int:
    """Return count of lines from a kubectl get command."""
    raw = kubectl(f"{cmd} --no-headers 2>/dev/null")
    if raw.startswith("ERROR") or not raw:
        return 0
    return len(raw.strip().split("\n"))


def k8s_sum_json(cmd: str, jsonpath: str) -> int:
    """Sum numeric values from a kubectl -o json result using a path expression.

    jsonpath is a dotted path like 'status.readyReplicas'.
    """
    data = kubectl_json(cmd)
    if isinstance(data, dict) and "items" in data:
        total = 0
        for item in data["items"]:
            val = item
            for key in jsonpath.split("."):
                val = val.get(key, 0) or 0
            total += int(val) if val else 0
        return total
    if isinstance(data, dict):
        val = data
        for key in jsonpath.split("."):
            val = val.get(key, 0) or 0
        return int(val) if val else 0
    return 0


def k8s_total_restarts() -> int:
    """Sum restart counts across all pods in all namespaces."""
    data = kubectl_json("get pods --all-namespaces")
    if not isinstance(data, dict) or "items" not in data:
        return 0
    total = 0
    for p in data["items"]:
        for c in p.get("status", {}).get("containerStatuses", []):
            total += c.get("restartCount", 0)
    return total


def k8s_top_restarts(n: int = 5) -> list[tuple[str, str, int]]:
    """Return top N pods by restart count: [(namespace, name, restarts), ...]."""
    data = kubectl_json("get pods --all-namespaces")
    if not isinstance(data, dict) or "items" not in data:
        return []
    pods = []
    for p in data["items"]:
        ns = p["metadata"]["namespace"]
        name = p["metadata"]["name"]
        restarts = sum(
            c.get("restartCount", 0)
            for c in p.get("status", {}).get("containerStatuses", [])
        )
        pods.append((ns, name, restarts))
    pods.sort(key=lambda x: x[2], reverse=True)
    return pods[:n]


def k8s_non_running_pod_count() -> int:
    """Count pods not in Running or Succeeded phase."""
    data = kubectl_json("get pods --all-namespaces")
    if not isinstance(data, dict) or "items" not in data:
        return 0
    count = 0
    for p in data["items"]:
        phase = p.get("status", {}).get("phase", "")
        if phase not in ("Running", "Succeeded"):
            count += 1
    return count


def k8s_running_pod_count() -> int:
    """Count pods in Running phase."""
    data = kubectl_json("get pods --all-namespaces")
    if not isinstance(data, dict) or "items" not in data:
        return 0
    count = 0
    for p in data["items"]:
        if p.get("status", {}).get("phase", "") == "Running":
            count += 1
    return count


# ── Comparison helper ──────────────────────────────────────────────────────────

def ok_or_fail(label: str, prom: str, k8s: str, tolerance: float = 0.1) -> str:
    """Compare two numeric-ish values within tolerance. Returns a status string."""
    try:
        p = float(prom)
        k = float(k8s)
        if k == 0:
            return "✓" if p == 0 else "⚠ MISMATCH"
        diff = abs(p - k) / abs(k)
        return "✓" if diff <= tolerance else f"⚠ DIFF {diff:.1%}"
    except (ValueError, TypeError):
        p_str = str(prom).strip()
        k_str = str(k8s).strip()
        return "✓" if p_str == k_str else f"⚠ DIFF"


# ── Validation table ───────────────────────────────────────────────────────────
# Each tuple: (label, promql, kubectl_result_string_or_callable)

def build_checks() -> list[tuple[str, str, Any]]:
    """Build validation checks, reading namespace from .env."""
    ns = TARGET_NS
    return [
        ("Nodes count",
         "count(kube_node_info)",
         str(k8s_count("get nodes"))),

        ("Namespaces count",
         "count(kube_namespace_created)",
         str(k8s_count("get namespaces"))),

        ("Running pods",
         'count(kube_pod_status_phase{phase="Running"} == 1)',
         str(k8s_running_pod_count())),

        ("Non-running pods",
         'count(kube_pod_info) - count(kube_pod_status_phase{phase="Running"} == 1)',
         str(k8s_non_running_pod_count())),

        (f"Deployments ready ({ns})",
         f'sum(kube_deployment_status_replicas_ready{{namespace="{ns}"}})',
         str(k8s_sum_json(f"get deployments -n {ns}", "status.readyReplicas"))),

        (f"DaemonSets ready ({ns})",
         f'sum(kube_daemonset_status_number_ready{{namespace="{ns}"}})',
         str(k8s_sum_json(f"get daemonsets -n {ns}", "status.numberReady"))),

        ("kube-state-metrics ready",
         'kube_deployment_status_replicas_ready{deployment="kube-state-metrics"}',
         str(k8s_sum_json(f"get deployment kube-state-metrics -n {ns}", "status.readyReplicas"))),

        ("node-exporter ready",
         'kube_daemonset_status_number_ready{daemonset="node-exporter"}',
         str(k8s_sum_json(f"get daemonset node-exporter -n {ns}", "status.numberReady"))),

        ("Prometheus ready",
         'kube_deployment_status_replicas_ready{deployment="prometheus-deployment"}',
         str(k8s_sum_json(f"get deployment prometheus-deployment -n {ns}", "status.readyReplicas"))),

        ("Total pod restarts",
         "sum(kube_pod_container_status_restarts_total)",
         str(k8s_total_restarts())),
    ]


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    failures = 0
    checks = build_checks()

    print(f"{'='*80}")
    print(f"DASHBOARD VALIDATION — Prometheus vs kubectl ground truth")
    print(f"Prometheus: {PROMETHEUS_URL}")
    print(f"Target NS:  {TARGET_NS}")
    print(f"{'='*80}")
    print(f"{'CHECK':40s} {'PROMETHEUS':>12s} {'KUBECTL':>12s} {'STATUS':>10s}")
    print(f"{'-'*40} {'-'*12} {'-'*12} {'-'*10}")

    for label, promql, k8s_val in checks:
        prom_result = prometheus_query(promql)
        prom_val = prom_value(prom_result)
        status = ok_or_fail(label, prom_val, k8s_val)
        if "⚠" in status:
            failures += 1
        print(f"{label:40s} {prom_val:>12s} {k8s_val:>12s} {status:>10s}")

    print(f"{'='*80}")

    # ── Scrape targets ──
    print("\nPrometheus scrape targets (up/down):")
    up_result = prometheus_query("up")
    if up_result.get("status") == "success":
        for ts in up_result["data"]["result"]:
            job = ts["metric"].get("job", "?")
            instance = ts["metric"].get("instance", "?")
            val = ts["value"][1]
            icon = "✓" if val == "1" else "✗"
            print(f"  {icon} job={job:35s} instance={instance}")

    # ── Top 5 restarts (Prometheus) ──
    print("\nTop 5 pods by restarts (Prometheus):")
    top = prometheus_query("topk(5, kube_pod_container_status_restarts_total)")
    if top.get("status") == "success":
        for ts in top["data"]["result"]:
            ns = ts["metric"].get("namespace", "?")
            pod = ts["metric"].get("pod", "?")
            print(f"  {ns}/{pod}: {ts['value'][1]}")

    # ── Top 5 restarts (kubectl) ──
    print("\nTop 5 pods by restarts (kubectl):")
    for ns, name, restarts in k8s_top_restarts(5):
        print(f"  {ns}/{name}: {restarts}")

    # ── Filter validation ──────────────────────────────────────────────────
    print(f"\n{'='*80}")
    print("FILTER VALIDATION — Grafana template variables (namespace, pod, node)")
    print(f"{'='*80}")

    # Use correct Prometheus labels API (not PromQL label_values)
    ns_list = prometheus_label_values("namespace", "kube_pod_info")
    print(f"\n  Namespaces ({len(ns_list)}): {', '.join(ns_list[:10])}{'...' if len(ns_list) > 10 else ''}")

    pod_list = prometheus_label_values("pod", "kube_pod_info")
    print(f"  Pods ({len(pod_list)}): {', '.join(pod_list[:5])}{'...' if len(pod_list) > 5 else ''}")

    node_list = prometheus_label_values("node", "kube_node_info")
    print(f"  Nodes ({len(node_list)}): {', '.join(node_list)}")

    # Verify namespace filter effectiveness
    print(f"\n  Filter effectiveness (namespace filter):")
    if ns_list:
        test_ns = ns_list[0]
        global_running = prom_value(prometheus_query(
            'count(kube_pod_status_phase{phase="Running"} == 1)'))
        filtered_running = prom_value(prometheus_query(
            f'count(kube_pod_status_phase{{namespace="{test_ns}", phase="Running"}} == 1)'))
        works = global_running != filtered_running
        if not works:
            failures += 1
        print(f"    Running pods: global={global_running}, namespace={test_ns}={filtered_running} "
              f"{'✓ filter works' if works else '⚠ same — may be only 1 ns with pods'}")

        global_dep = prom_value(prometheus_query(
            'count(kube_deployment_status_replicas_available)'))
        filtered_dep = prom_value(prometheus_query(
            f'count(kube_deployment_status_replicas_available{{namespace="{test_ns}"}})'))
        works = global_dep != filtered_dep
        if not works:
            failures += 1
        print(f"    Deployments:    global={global_dep}, namespace={test_ns}={filtered_dep} "
              f"{'✓ filter works' if works else '⚠ same'}")

    # Verify pod filter effectiveness
    print(f"\n  Filter effectiveness (pod filter):")
    if pod_list:
        test_pod = pod_list[0]
        pod_info = prometheus_query(f'kube_pod_info{{pod="{test_pod}"}}')
        pod_ns = "?"
        if pod_info.get("status") == "success" and pod_info["data"]["result"]:
            pod_ns = pod_info["data"]["result"][0]["metric"].get("namespace", "?")
            print(f"    Test pod: {pod_ns}/{test_pod}")

        global_restart = prom_value(prometheus_query(
            'sum(kube_pod_container_status_restarts_total)'))
        filtered_restart = prom_value(prometheus_query(
            f'kube_pod_container_status_restarts_total{{namespace="{pod_ns}", pod="{test_pod}"}}'))
        works = global_restart != filtered_restart
        if not works:
            failures += 1
        print(f"    Restarts:       global={global_restart}, pod={test_pod}={filtered_restart} "
              f"{'✓ filter works' if works else '⚠ same'}")

    # Verify node filter effectiveness
    print(f"\n  Filter effectiveness (node filter):")
    if node_list:
        test_node = node_list[0]
        global_nodes = prom_value(prometheus_query('count(kube_node_info)'))
        filtered_nodes = prom_value(prometheus_query(
            f'count(kube_node_info{{node="{test_node}"}})'))
        # Single-node cluster: both return 1, which is correct
        works = global_nodes != filtered_nodes or int(float(global_nodes)) == 1
        if not works:
            failures += 1
        print(f"    Nodes:          global={global_nodes}, node={test_node}={filtered_nodes} "
              f"{'✓ filter works' if works else '⚠ same — single node cluster'}")

        global_ops = prom_value(prometheus_query(
            'sum(rate(kubelet_runtime_operations_total[5m]))'))
        filtered_ops = prom_value(prometheus_query(
            f'sum(rate(kubelet_runtime_operations_total{{node="{test_node}"}}[5m]))'))
        if filtered_ops in ("no data", "error"):
            failures += 1
            print(f"    Kubelet ops:    global={global_ops}, node={test_node}=✗ {filtered_ops} "
                  f"— metric may not carry 'node' label, try 'instance' instead")
        else:
            works = global_ops != filtered_ops
            if not works:
                failures += 1
            print(f"    Kubelet ops:    global={global_ops}, node={test_node}={filtered_ops} "
                  f"{'✓ filter works' if works else '⚠ same'}")

    # Dashboard variable health check
    print(f"\n  Dashboard variable status:")
    if not ns_list:
        failures += 1
        print(f"    Namespace dropdown: ✗ EMPTY — check datasource UID in dashboard JSON")
    else:
        print(f"    Namespace dropdown: ✓ populated ({len(ns_list)} values)")

    if not pod_list:
        failures += 1
        print(f"    Pod dropdown:       ✗ EMPTY — check datasource UID in dashboard JSON")
    else:
        print(f"    Pod dropdown:       ✓ populated ({len(pod_list)} values)")

    if not node_list:
        failures += 1
        print(f"    Node dropdown:      ✗ EMPTY — check datasource UID in dashboard JSON")
    else:
        print(f"    Node dropdown:      ✓ populated ({len(node_list)} values)")

    print(f"{'='*80}")

    if failures:
        print(f"\n{failures} validation(s) FAILED.")
        sys.exit(1)
    else:
        print(f"\nAll validations PASSED.")


if __name__ == "__main__":
    main()
