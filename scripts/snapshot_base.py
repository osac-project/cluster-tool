#!/usr/bin/env python3
"""Create a base SNO snapshot: apply kubeletconfig, strip credentials, snapshot, push.

This script creates or refreshes the base SNO flavor that the OSAC-specific
snapshot scripts (snapshot_caas.py, snapshot_vmaas.py) build on top of.

Usage:
    SOURCE=<clone-id> KUBECONFIG=<path> python scripts/snapshot_base.py

Environment variables:
    SOURCE          (required) Clone ID of the running SNO instance
    KUBECONFIG      (required) Path to kubeconfig for the cluster
    FLAVOR_NAME     Name for the snapshot (default: sno-4-22)
    SERVER          Baremetal server name (default: rdu07)
    SNAPSHOT_TAG    Tag for push step (default: FLAVOR_NAME)
"""

import base64
import json
import os
import sys

from snapshot_lib import (
    check_cluster_operators, cluster_tool, oc, oc_json, run,
    KUBELETCONFIG, REGISTRY,
)


FLAVOR_NAME = os.environ.get("FLAVOR_NAME", "sno-4-22")
SOURCE = os.environ.get("SOURCE")
SERVER = os.environ.get("SERVER", "rdu07")
KUBECONFIG = os.environ.get("KUBECONFIG")

if not SOURCE:
    sys.exit("SOURCE env var required (clone ID, e.g. 35dfb389)")
if not KUBECONFIG:
    sys.exit("KUBECONFIG env var required")


def strip_cluster_pull_secret():
    """Strip only the cluster pull-secret (no OSAC namespace secrets)."""
    print("  Stripping cluster pull-secret auths...")
    oc("delete", "secret", "pull-secret", "-n", "openshift-config")
    oc("create", "secret", "generic", "pull-secret", "-n", "openshift-config",
       '--from-literal=.dockerconfigjson={"auths":{}}',
       "--type=kubernetes.io/dockerconfigjson")

    ps = oc_json("get", "secret", "pull-secret", "-n", "openshift-config")
    auths = json.loads(base64.b64decode(ps["data"][".dockerconfigjson"]))
    if auths.get("auths"):
        sys.exit(f"ERROR: pull-secret still has auths: {list(auths['auths'].keys())}")
    print("  Pull-secret stripped and verified empty")


if __name__ == "__main__":
    print(f"=== Creating base snapshot '{FLAVOR_NAME}' from {SOURCE} ===")
    print(f"Server: {SERVER}")
    print()

    print("[1/6] Verifying cluster health...")
    check_cluster_operators()

    print("[2/6] Applying kubeletconfig (maxPods=500)...")
    oc("apply", "-f", str(KUBELETCONFIG))
    oc("wait", "--for=condition=Updated", "mcp/master", "--timeout=600s")
    print("  KubeletConfig applied and MCP rolled out")

    print("[3/6] Stripping credentials...")
    strip_cluster_pull_secret()

    print("[4/6] Waiting for MCO to propagate empty pull-secret to node...")
    oc("wait", "--for=condition=Updated", "mcp/master", "--timeout=600s")
    print("  MCO rollout complete")

    print("[5/6] Snapshotting...")
    run(["cluster-tool", "flavors", "--delete", FLAVOR_NAME, "--server", SERVER], check=False)
    cluster_tool("snapshot", "--name", FLAVOR_NAME, "--source", SOURCE, "--server", SERVER)

    print("[6/6] Pushing to registry...")
    tag = os.environ.get("SNAPSHOT_TAG", FLAVOR_NAME)
    cluster_tool("push", FLAVOR_NAME, "--registry", REGISTRY, "--tag", tag, "--server", SERVER)

    print()
    print(f"=== Base snapshot '{FLAVOR_NAME}' created and pushed ===")
    print(f"Image: {REGISTRY}:{tag}")
