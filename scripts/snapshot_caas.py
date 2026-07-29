#!/usr/bin/env python3
"""Create a CaaS Helm snapshot: SNO + LVMS + MCE + MetalLB + OSAC.

Boots from a base SNO flavor, installs all OSAC components with CaaS values,
validates health, then snapshots and pushes to OCI registry.

Usage:
    python scripts/snapshot_caas.py

Environment variables:
    SERVER          Baremetal server name (default: rdu07)
    BASE_FLAVOR     Base flavor to boot from (default: sno-4-22)
    PULL_SECRET     Path to pull-secret JSON (default: ~/.pull-secret.json)
    INSTALLER_DIR   Path to osac-installer checkout (default: ../osac-installer)
"""

from snapshot_lib import HealthCheck, SnapshotConfig, create_snapshot

config = SnapshotConfig(
    flavor_name="caas-4-22",
    values_file="values/caas-ci/values.yaml",
    extra_health_checks=[
        HealthCheck("MCE Available", [
            "--for=jsonpath={.status.phase}=Available",
            "multiclusterengine/multiclusterengine",
        ], command="wait"),
        HealthCheck("assisted-service", [
            "deployment/assisted-service", "-n", "multicluster-engine",
        ]),
    ],
)

if __name__ == "__main__":
    create_snapshot(config)
