#!/usr/bin/env python3
"""Create a VMaaS Helm snapshot: SNO + LVMS + CNV + MetalLB + OSAC.

Boots from a base SNO flavor, installs all OSAC components with VMaaS values,
validates health, then snapshots and pushes to OCI registry.

Usage:
    python scripts/snapshot_vmaas.py

Environment variables:
    SERVER          Baremetal server name (default: rdu07)
    BASE_FLAVOR     Base flavor to boot from (default: sno-4-22)
    PULL_SECRET     Path to pull-secret JSON (default: ~/.pull-secret.json)
    INSTALLER_DIR   Path to osac-installer checkout (default: ../osac-installer)
"""

from snapshot_lib import HealthCheck, SnapshotConfig, create_snapshot

config = SnapshotConfig(
    flavor_name="vmaas-4-22",
    values_file="values/vmaas-ci/values.yaml",
    extra_health_checks=[
        HealthCheck("HyperConverged", [
            "--for=condition=Available",
            "hyperconverged/kubevirt-hyperconverged",
            "-n", "openshift-cnv",
        ], command="wait"),
    ],
)

if __name__ == "__main__":
    create_snapshot(config)
