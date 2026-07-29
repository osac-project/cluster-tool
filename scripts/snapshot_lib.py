"""Shared logic for OSAC snapshot creation scripts.

Each snapshot script (base, vmaas, caas) imports this module and calls
create_snapshot() with a SnapshotConfig describing the flavor.

Environment variables:
    INSTALLER_DIR   Path to osac-installer checkout (default: ../osac-installer)
    KUBELETCONFIG   Path to kubeletconfig.yaml (default: kubeletconfig.yaml in this dir)
    PULL_SECRET     Path to pull-secret JSON (default: ~/.pull-secret.json)
    SERVER          Baremetal server name (default: rdu07)
    BASE_FLAVOR     Base flavor to boot from (default: sno-4-22)
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


NAMESPACE = "osac-e2e-ci"
INSTALLER_DIR = Path(os.environ.get("INSTALLER_DIR", str(Path(__file__).resolve().parent.parent / "osac-installer")))
REGISTRY = "quay.io/osac-project/cluster-flavors"
KUBELETCONFIG = Path(os.environ.get("KUBELETCONFIG", str(Path(__file__).resolve().parent / "kubeletconfig.yaml")))
DEFAULT_PULL_SECRET = Path(os.environ.get("PULL_SECRET", str(Path.home() / ".pull-secret.json")))


@dataclass
class SnapshotConfig:
    flavor_name: str
    values_file: str
    extra_health_checks: list[HealthCheck] = field(default_factory=list)
    pre_setup: callable = field(default=lambda: None)
    server: str = field(default_factory=lambda: os.environ.get("SERVER", "rdu07"))
    base_flavor: str = field(default_factory=lambda: os.environ.get("BASE_FLAVOR", "sno-4-22"))
    pull_secret: Path = field(default_factory=lambda: Path(os.environ.get("PULL_SECRET", str(DEFAULT_PULL_SECRET))))


@dataclass
class HealthCheck:
    name: str
    args: list[str]
    command: str = "rollout"  # "rollout" -> oc rollout status, "wait" -> oc wait


# --- Shell helpers -----------------------------------------------------------


def run(args: list[str], *, check: bool = True, capture: bool = False,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    print(f"  $ {' '.join(args)}")
    return subprocess.run(args, check=check, text=True, capture_output=capture,
                          cwd=str(INSTALLER_DIR), env=env)


def oc(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return run(["oc", *args], check=check, capture=capture)


def oc_json(*args: str) -> dict:
    result = oc(*args, "-o", "json", capture=True)
    return json.loads(result.stdout)


def cluster_tool(*args: str) -> None:
    run(["cluster-tool", *args])


# --- Health checks -----------------------------------------------------------


def check_cluster_operators() -> None:
    print("--- Cluster Operators ---")
    data = oc_json("get", "clusteroperators")
    failed: list[str] = []
    for item in data["items"]:
        name: str = item["metadata"]["name"]
        conds: dict[str, str] = {
            c["type"]: c["status"]
            for c in item.get("status", {}).get("conditions", [])
        }
        if conds.get("Available") != "True" or conds.get("Degraded") != "False":
            avail = conds.get("Available", "?")
            degraded = conds.get("Degraded", "?")
            failed.append(f"  {name}: Available={avail}, Degraded={degraded}")
    if failed:
        print("UNHEALTHY cluster operators:")
        print("\n".join(failed))
        sys.exit(1)
    print("  All cluster operators healthy")


def check_no_crashing_pods(namespace: str) -> None:
    print(f"--- No crashing pods in {namespace} ---")
    data = oc_json("get", "pods", "-n", namespace)
    crashing: list[str] = []
    for pod in data["items"]:
        name: str = pod["metadata"]["name"]
        for cs in pod.get("status", {}).get("containerStatuses", []):
            restarts: int = cs.get("restartCount", 0)
            if restarts > 3:
                crashing.append(f"  {name}/{cs['name']}: {restarts} restarts")
            waiting: dict = cs.get("state", {}).get("waiting", {})
            reason: str = waiting.get("reason", "")
            if reason in ("CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull"):
                crashing.append(f"  {name}/{cs['name']}: {reason}")
    if crashing:
        print("UNHEALTHY pods:")
        print("\n".join(crashing))
        sys.exit(1)
    print("  No crashing pods")


def check_aap_controller_running() -> None:
    print("--- AAP Controller ---")
    aap_status = oc_json("get", "automationcontroller", "osac-aap-controller", "-n", NAMESPACE)
    conditions: list[dict] = aap_status.get("status", {}).get("conditions", [])
    running = any(c["type"] == "Running" and c["status"] == "True" for c in conditions)
    if not running:
        print(f"  AAP controller is not Running. Conditions: {conditions}")
        sys.exit(1)
    print("  OK: AAP controller Running")


COMMON_ROLLOUT_CHECKS: list[HealthCheck] = [
    # Prerequisites
    HealthCheck("cert-manager", ["deployment/cert-manager", "-n", "cert-manager"]),
    HealthCheck("cert-manager-webhook", ["deployment/cert-manager-webhook", "-n", "cert-manager"]),
    HealthCheck("trust-manager", ["deployment/trust-manager", "-n", "cert-manager"]),
    HealthCheck("Keycloak", ["deployment/keycloak-service", "-n", "keycloak"]),
    HealthCheck("AAP operator", ["deployment/automation-controller-operator-controller-manager", "-n", "ansible-aap"]),
    # Storage & Networking
    HealthCheck("LVMS operator", ["deployment/lvms-operator", "-n", "openshift-storage"]),
    HealthCheck("MetalLB controller", ["deployment/metallb-operator-controller-manager", "-n", "metallb-system"]),
    HealthCheck("MetalLB webhook", ["deployment/metallb-operator-webhook-server", "-n", "metallb-system"]),
    # OSAC
    HealthCheck("fulfillment-grpc-server", ["deployment/fulfillment-grpc-server", "-n", NAMESPACE]),
    HealthCheck("fulfillment-controller", ["deployment/fulfillment-controller", "-n", NAMESPACE]),
    HealthCheck("fulfillment-ingress-proxy", ["deployment/fulfillment-ingress-proxy", "-n", NAMESPACE]),
    HealthCheck("fulfillment-rest-gateway", ["deployment/fulfillment-rest-gateway", "-n", NAMESPACE]),
    HealthCheck("fulfillment-console-proxy", ["deployment/fulfillment-console-proxy", "-n", NAMESPACE]),
    HealthCheck("osac-operator", ["deployment/osac-operator", "-n", NAMESPACE]),
]


def run_health_checks(extra_checks: list[HealthCheck]) -> None:
    check_cluster_operators()

    all_checks = COMMON_ROLLOUT_CHECKS + extra_checks

    print("--- Component checks ---")
    for hc in all_checks:
        if hc.command == "rollout":
            oc("rollout", "status", *hc.args, "--timeout=120s")
        elif hc.command == "wait":
            oc("wait", *hc.args, "--timeout=120s")
        else:
            oc(*hc.args.split() if isinstance(hc.args, str) else hc.args)
        print(f"  OK: {hc.name}")

    print("--- Database ---")
    oc("rollout", "status", "deployment/postgres", "-n", NAMESPACE, "--timeout=120s")
    print("  OK: postgres")

    print("--- LVMS StorageClass ---")
    oc("get", "sc", "lvms-vg1")
    print("  OK: lvms-vg1 exists")

    check_aap_controller_running()
    check_no_crashing_pods(NAMESPACE)


# --- Scale down --------------------------------------------------------------


def find_aap_csv() -> str:
    """Find the AAP CSV name."""
    data = oc_json("get", "csv", "-n", "ansible-aap")
    for item in data["items"]:
        if item.get("status", {}).get("phase") != "Succeeded":
            continue
        deploys: list[dict] = (
            item.get("spec", {}).get("install", {}).get("spec", {}).get("deployments", [])
        )
        if any(d.get("name") == "automation-controller-operator-controller-manager" for d in deploys):
            return item["metadata"]["name"]
    print("ERROR: Could not find AAP CSV")
    sys.exit(1)


def scale_csv_to_zero(csv_name: str, namespace: str) -> None:
    """Scale all deployments in a CSV to 0 replicas."""
    csv_data = oc_json("get", "csv", csv_name, "-n", namespace)
    deploys: list[dict] = csv_data["spec"]["install"]["spec"]["deployments"]
    patch: list[dict] = [
        {"op": "replace",
         "path": f"/spec/install/spec/deployments/{i}/spec/replicas",
         "value": 0}
        for i in range(len(deploys))
    ]
    oc("patch", "csv", csv_name, "-n", namespace, "--type=json",
       "-p", json.dumps(patch))
    for d in deploys:
        oc("wait", f"deploy/{d['name']}", "-n", namespace,
           "--for=jsonpath={.spec.replicas}=0", "--timeout=60s")
    print(f"  Scaled {len(deploys)} operators to 0 via CSV {csv_name}")


def strip_credentials() -> None:
    """Remove personal credentials so they aren't baked into the snapshot.

    The refresh script recreates all three in Phase 2 (create_secrets)
    before any pods start in Phase 3.
    """
    print("  Deleting config-as-code-manifest-ig (AAP license)...")
    oc("delete", "secret", "config-as-code-manifest-ig", "-n", NAMESPACE,
       "--ignore-not-found")

    print("  Deleting quay-pull-secret...")
    oc("delete", "secret", "quay-pull-secret", "-n", NAMESPACE,
       "--ignore-not-found")

    print("  Stripping cluster pull-secret auths...")
    oc("delete", "secret", "pull-secret", "-n", "openshift-config")
    oc("create", "secret", "generic", "pull-secret", "-n", "openshift-config",
       '--from-literal=.dockerconfigjson={"auths":{}}',
       "--type=kubernetes.io/dockerconfigjson")

    print("  Verifying no credentials remain...")
    ps = oc_json("get", "secret", "pull-secret", "-n", "openshift-config")
    auths = json.loads(base64.b64decode(ps["data"][".dockerconfigjson"]))
    if auths.get("auths"):
        print(f"ERROR: pull-secret still has auths: {list(auths['auths'].keys())}")
        sys.exit(1)
    for name in ("config-as-code-manifest-ig", "quay-pull-secret"):
        result = oc("get", "secret", name, "-n", NAMESPACE, check=False, capture=True)
        if result.returncode == 0:
            print(f"ERROR: secret {name} still exists")
            sys.exit(1)

    print("  Credentials stripped and verified")


def scale_to_zero() -> None:
    # 1. Scale ALL operators that reconcile resources in our namespace.
    #    Must happen first so they don't fight operand scale-down.
    print("  Scaling AAP operators to 0 via CSV...")
    aap_csv = find_aap_csv()
    scale_csv_to_zero(aap_csv, "ansible-aap")

    # 2. Scale all deployments in OSAC namespace (operands + our components)
    print("  Scaling all deployments to 0...")
    oc("scale", "deploy", "--all", "-n", NAMESPACE, "--replicas=0")

    # 3. Scale StatefulSets (AAP postgres, redis)
    print("  Scaling all StatefulSets to 0...")
    oc("scale", "statefulset", "--all", "-n", NAMESPACE, "--replicas=0")

    # 4. Delete fulfillment postgres bare pod (may not exist on re-runs)
    print("  Deleting postgres pod...")
    oc("delete", "pod", "postgres", "-n", NAMESPACE,
       "--ignore-not-found", "--wait=true", "--timeout=60s")

    # 5. Delete all Job pods (completed, failed, and running)
    print("  Deleting all Job pods...")
    oc("delete", "pods", "-n", NAMESPACE, "-l", "job-name",
       "--ignore-not-found", "--wait=true", "--timeout=60s")

    # 6. Wait for everything to terminate
    print("  Waiting for all pods to terminate...")
    oc("wait", "pod", "--all", "-n", NAMESPACE, "--for=delete", "--timeout=300s")
    print("  All components scaled to zero")


# --- Main --------------------------------------------------------------------


def create_snapshot(config: SnapshotConfig) -> None:
    print(f"=== Creating {config.flavor_name} snapshot ===")
    print(f"Server: {config.server}")
    print(f"Base flavor: {config.base_flavor}")
    print(f"Values: {config.values_file}")
    print()

    # Step 1: Boot
    print(f"[1/10] Booting from {config.base_flavor}...")
    run(["cluster-tool", "destroy", config.flavor_name, "--server", config.server],
        check=False)
    if not config.pull_secret.exists():
        sys.exit(f"Pull secret not found: {config.pull_secret}")
    cluster_tool("boot", "--flavor", config.base_flavor,
                 "--name", config.flavor_name, "--server", config.server,
                 "--pull-secret", str(config.pull_secret))
    kubeconfig = Path.home() / f".kube/{config.flavor_name}.kubeconfig"
    os.environ["KUBECONFIG"] = str(kubeconfig)
    print(f"KUBECONFIG={kubeconfig}")

    # Step 2: Cluster prerequisites
    print("[2/10] Applying cluster prerequisites...")
    oc("patch", "networks.operator.openshift.io", "cluster", "--type=merge",
       "-p", json.dumps({"spec": {"defaultNetwork": {"ovnKubernetesConfig": {
           "gatewayConfig": {"ipv4": {"internalMasqueradeSubnet": "169.254.0.0/17"}}
       }}}}))
    oc("delete", "pod", "-n", "openshift-network-operator", "-l", "name=network-operator")
    print("  Waiting 180s for OVN to stabilize...")
    time.sleep(180)
    oc("rollout", "status", "ds/ovnkube-node",
       "-n", "openshift-ovn-kubernetes", "--timeout=300s")
    oc("wait", "--for=jsonpath={.status.connectionState.lastObservedState}=READY",
       "catalogsource/redhat-operators", "-n", "openshift-marketplace", "--timeout=300s")

    oc("apply", "-f", str(KUBELETCONFIG))
    oc("wait", "--for=condition=Updated", "mcp/master", "--timeout=600s")
    print("  Waiting 180s for MCP to settle...")
    time.sleep(180)

    # Step 3: Pre-setup hook
    print("[3/10] Running pre-setup hook...")
    config.pre_setup()

    # Step 4: Deploy via Helm
    print("[4/10] Deploying OSAC via Helm...")
    make_env = os.environ.copy()
    make_env["VALUES_FILE"] = config.values_file
    make_env["INSTALLER_NAMESPACE"] = NAMESPACE
    run(["make", "install"], env=make_env)
    print("  Waiting 60s for everything to settle...")
    time.sleep(60)

    # Step 5: Health validation
    print("[5/10] Validating cluster health...")
    run_health_checks(config.extra_health_checks)
    print()
    print("=== ALL HEALTH CHECKS PASSED ===")
    print()

    # Step 6: Scale to zero
    print("[6/10] Scaling everything to zero...")
    scale_to_zero()

    print("[7/10] Stripping credentials...")
    strip_credentials()

    print("[8/10] Waiting for MCO to propagate empty pull-secret to node...")
    oc("wait", "--for=condition=Updated", "mcp/master", "--timeout=600s")
    print("  MCO rollout complete")

    # Step 9: Snapshot
    print("[9/10] Creating snapshot...")
    run(["cluster-tool", "flavors", "--delete", config.flavor_name,
         "--server", config.server], check=False)
    cluster_tool("snapshot", "--name", config.flavor_name,
                 "--source", config.flavor_name, "--server", config.server)

    # Step 10: Push
    print("[10/10] Pushing to registry...")
    cluster_tool("push", config.flavor_name, "--registry", REGISTRY,
                 "--tag", config.flavor_name, "--server", config.server)

    print()
    print(f"=== Snapshot {config.flavor_name} created and pushed ===")
    print(f"Image: {REGISTRY}:{config.flavor_name}")
