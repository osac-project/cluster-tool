#!/usr/bin/env python3
"""Capture a bare three-node compact OpenShift cluster as a cluster-tool flavor."""

import argparse
import base64
import fcntl
import grp
import ipaddress
import json
import os
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path


NODE_NAMES = [f"master-0-{index}" for index in range(3)]
DISK_LAYOUT = [
    (0, "vda", "disk-0.qcow2", "os"),
    (0, "vdb", "disk-0-spare.qcow2", "spare"),
    (1, "vda", "disk-1.qcow2", "os"),
    (1, "vdb", "disk-1-spare.qcow2", "spare"),
    (2, "vda", "disk-2.qcow2", "os"),
    (2, "vdb", "disk-2-spare.qcow2", "spare"),
]


class CaptureError(Exception):
    pass


class StateCommittedError(CaptureError):
    """State was replaced, but syncing its directory entry failed."""


def display_command(command):
    """Return a command label without exposing arguments that may contain secrets."""
    executable = Path(str(command[0])).name
    arguments = [str(part) for part in command[1:]]
    if executable == "oc":
        operation = next((arg for arg in arguments if arg in {"get", "extract"}), None)
    elif executable == "virsh":
        operations = {"dominfo", "dumpxml", "vol-path", "shutdown", "domstate", "start"}
        operation = next((arg for arg in arguments if arg in operations), None)
    elif executable == "qemu-img":
        operations = {"info", "convert", "check"}
        operation = next((arg for arg in arguments if arg in operations), None)
    elif executable == "ssh":
        operation = "preflight"
    elif executable == "ssh-keygen":
        operation = "key-validation"
    elif executable == "openssl":
        operation = next((arg for arg in arguments if arg in {"pkey", "x509"}), None)
    else:
        operation = None
    return f"{executable} {operation}" if operation else executable


def run(command, *, timeout=60, input_bytes=None):
    """Run a command with bounded time and return its standard output."""
    try:
        result = subprocess.run(
            [str(part) for part in command],
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise CaptureError(
            f"Command timed out after {timeout} seconds: {display_command(command)}"
        ) from error
    if result.returncode:
        raise CaptureError(
            f"Command failed ({result.returncode}): {display_command(command)}"
        )
    return result.stdout


def run_visible(command, *, timeout=3600):
    """Run a command without forwarding tool output that may contain host details."""
    try:
        result = subprocess.run(
            [str(part) for part in command],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise CaptureError(
            f"Command timed out after {timeout} seconds: {display_command(command)}"
        ) from error
    if result.returncode:
        raise CaptureError(
            f"Command failed ({result.returncode}): {display_command(command)}"
        )


def require_programs(names):
    """Fail early when a required host command is unavailable."""
    missing = [name for name in names if shutil.which(name) is None]
    if missing:
        raise CaptureError(f"Missing required command(s): {', '.join(missing)}")


def parse_args():
    """Parse command-line options for a compact flavor capture."""
    parser = argparse.ArgumentParser(
        description=(
            "Capture a healthy, bare three-node compact OpenShift cluster into "
            "a local cluster-tool flavor; it does not upload the flavor."
        )
    )
    parser.add_argument("--flavor", required=True, help="New local flavor name")
    parser.add_argument(
        "--source-cluster",
        required=True,
        help="Source cluster label to store in flavor metadata",
    )
    parser.add_argument(
        "--vm-prefix",
        default="",
        help=(
            "Optional prefix of source libvirt domains before '-master-0-N'; "
            "omit it for master-0-0 through master-0-2"
        ),
    )
    parser.add_argument(
        "--source-primary-subnet",
        required=True,
        type=int,
        help="Third octet of the source primary subnet (for example, 123)",
    )
    parser.add_argument(
        "--api-address",
        required=True,
        help="Reachable source API IP address, without port",
    )
    parser.add_argument(
        "--api-server-name",
        required=True,
        help="TLS server name present in the source API serving certificate",
    )
    parser.add_argument(
        "--ssh-node-ip",
        help="IP of master-0-0 for the core SSH preflight (defaults to --api-address)",
    )
    parser.add_argument(
        "--kubeconfig",
        default=os.environ.get("KUBECONFIG"),
        help="Admin kubeconfig (defaults to KUBECONFIG)",
    )
    parser.add_argument(
        "--ssh-identity",
        required=True,
        help="Private key already trusted by core on the source nodes",
    )
    parser.add_argument(
        "--ssh-public-key",
        help="Matching public key (defaults to SSH-IDENTITY.pub)",
    )
    parser.add_argument(
        "--libvirt-uri",
        default="qemu:///system",
        help="Libvirt URI (default: qemu:///system)",
    )
    parser.add_argument(
        "--expected-memory-gib",
        type=int,
        default=24,
        help="Expected RAM per master in GiB (default: 24)",
    )
    parser.add_argument(
        "--expected-vcpus",
        type=int,
        default=12,
        help="Expected vCPU count per master (default: 12)",
    )
    parser.add_argument(
        "--expected-os-disk-gib",
        type=int,
        default=80,
        help="Expected vda size in GiB (default: 80)",
    )
    parser.add_argument(
        "--expected-spare-disk-gib",
        type=int,
        default=132,
        help="Expected vdb size in GiB (default: 132)",
    )
    parser.add_argument(
        "--shutdown-timeout",
        type=int,
        default=300,
        help="Seconds to wait for all masters to shut down (default: 300)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the prompt acknowledging temporary source-cluster downtime",
    )
    return parser.parse_args()


def validate_args(args):
    """Validate user-supplied names, addresses, and resource sizes."""
    for label, value in (
        ("flavor", args.flavor),
        ("source-cluster", args.source_cluster),
    ):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
            raise CaptureError(
                f"--{label} must contain only letters, digits, '.', '_' or '-'"
            )
    if args.vm_prefix and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.vm_prefix
    ):
        raise CaptureError(
            "--vm-prefix must contain only letters, digits, '.', '_' or '-'"
        )
    if not 1 <= args.source_primary_subnet <= 254:
        raise CaptureError("--source-primary-subnet must be between 1 and 254")
    try:
        api_address = ipaddress.ip_address(args.api_address)
        ssh_node_ip = ipaddress.ip_address(args.ssh_node_ip or args.api_address)
        if not isinstance(api_address, ipaddress.IPv4Address) or not isinstance(
            ssh_node_ip, ipaddress.IPv4Address
        ):
            raise CaptureError("The compact capture workflow requires IPv4 API and SSH addresses")
        args.api_address = str(api_address)
        args.ssh_node_ip = str(ssh_node_ip)
    except ValueError as error:
        raise CaptureError("Invalid API or SSH address") from error
    if not re.fullmatch(r"[A-Za-z0-9.-]+", args.api_server_name):
        raise CaptureError("--api-server-name must be a DNS name, without scheme or port")
    if not args.kubeconfig:
        raise CaptureError("Provide --kubeconfig or set KUBECONFIG")
    if args.shutdown_timeout < 1:
        raise CaptureError("--shutdown-timeout must be positive")
    if min(
        args.expected_memory_gib,
        args.expected_vcpus,
        args.expected_os_disk_gib,
        args.expected_spare_disk_gib,
    ) < 1:
        raise CaptureError("Expected memory, vCPU, and disk sizes must be positive")


def invoking_home():
    """Return the home directory cluster-tool uses for the invoking user."""
    invoking_user = os.environ.get("SUDO_USER")
    return Path(os.path.expanduser(f"~{invoking_user}")) if invoking_user else Path.home()


def server_config():
    """Load cluster-tool paths from the invoking user's configuration."""
    config_dir = invoking_home() / ".config" / "cluster-tool"
    config_file = config_dir / "config"
    if not config_file.is_file():
        raise CaptureError(
            "cluster-tool config is missing; initialize cluster-tool on this host first"
        )
    data_dir = None
    for line in config_file.read_text().splitlines():
        if line.startswith("CLUSTER_TOOL_DATA="):
            data_dir = line.split("=", 1)[1].strip().strip("'").strip('"')
            break
    if not data_dir:
        raise CaptureError("No CLUSTER_TOOL_DATA entry found in cluster-tool config")
    data_dir = data_dir.replace("${HOME}", str(config_dir.parent.parent))
    data_dir = data_dir.replace("$HOME", str(config_dir.parent.parent))
    if data_dir == "~":
        data_dir = str(config_dir.parent.parent)
    elif data_dir.startswith("~/"):
        data_dir = str(config_dir.parent.parent / data_dir[2:])
    data_dir = Path(os.path.expandvars(os.path.expanduser(data_dir)))
    return config_dir, config_dir, data_dir


def oc_command(args, *subcommand):
    """Build an oc command using the configured API endpoint and kubeconfig."""
    return [
        shutil.which("oc") or "oc",
        f"--kubeconfig={Path(args.kubeconfig).expanduser()}",
        f"--server=https://{args.api_address}:6443",
        f"--tls-server-name={args.api_server_name}",
        *subcommand,
    ]


def oc_json(args, *subcommand):
    """Run an oc query and decode its JSON response."""
    return json.loads(run(oc_command(args, *subcommand)))


def condition_map(conditions):
    """Index Kubernetes conditions by condition type."""
    return {condition.get("type"): condition.get("status") for condition in conditions}


def node_label(name):
    """Return an opaque index for a source node."""
    try:
        return f"node {NODE_NAMES.index(name)}"
    except ValueError:
        return "source node"


def vm_label(domain):
    """Return an opaque index for a source VM."""
    match = re.search(r"-master-0-(\d+)$", domain)
    return f"VM {match.group(1)}" if match else "source VM"


def is_ipv4(address):
    """Return whether an address is a valid IPv4 address."""
    try:
        return isinstance(ipaddress.ip_address(address), ipaddress.IPv4Address)
    except ValueError:
        return False


def verify_cluster(args):
    """Require a healthy bare three-node cluster before capture."""
    print("Checking source cluster readiness...")
    nodes = oc_json(args, "get", "nodes", "-o", "json").get("items", [])
    by_name = {node.get("metadata", {}).get("name"): node for node in nodes}
    if set(by_name) != set(NODE_NAMES):
        raise CaptureError("Expected exactly three source masters")
    node_ips = {}
    for name in NODE_NAMES:
        node = by_name[name]
        ready = any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in node.get("status", {}).get("conditions", [])
        )
        if not ready:
            raise CaptureError(f"{node_label(name)} is not Ready")
        addresses = [
            item.get("address")
            for item in node.get("status", {}).get("addresses", [])
            if item.get("type") == "InternalIP" and item.get("address")
        ]
        ipv4_addresses = [address for address in addresses if is_ipv4(address)]
        if not ipv4_addresses:
            raise CaptureError(f"Could not find an IPv4 address for {node_label(name)}")
        node_ips[name] = ipv4_addresses[0]

    source_network = ipaddress.ip_network(
        f"192.168.{args.source_primary_subnet}.0/24"
    )
    source_addresses = [args.api_address, *node_ips.values()]
    if any(ipaddress.ip_address(address) not in source_network for address in source_addresses):
        raise CaptureError(
            "The API and all source node addresses must match the configured source subnet"
        )

    namespaces = oc_json(args, "get", "namespaces", "-o", "json").get("items", [])
    add_on_namespaces = []
    for namespace in namespaces:
        name = namespace.get("metadata", {}).get("name", "")
        if name.startswith("osac-") or name in {
            "openbao",
            "openshift-cnv",
            "openshift-local-storage",
            "openshift-storage",
            "openshift-virtualization",
        }:
            add_on_namespaces.append(name)
    if add_on_namespaces:
        raise CaptureError(
            "Source is not a bare OpenShift snapshot; remove the detected add-ons "
            "before capture"
        )

    operators = oc_json(args, "get", "co", "-o", "json").get("items", [])
    unhealthy = []
    for operator in operators:
        conditions = condition_map(operator.get("status", {}).get("conditions", []))
        name = operator.get("metadata", {}).get("name", "<unknown>")
        if (
            conditions.get("Available") != "True"
            or conditions.get("Progressing") != "False"
            or conditions.get("Degraded") != "False"
        ):
            unhealthy.append(name)
    if unhealthy:
        raise CaptureError(
            "One or more ClusterOperators are not quiet and healthy"
        )

    mcp = oc_json(args, "get", "mcp", "master", "-o", "json")
    mcp_conditions = condition_map(mcp.get("status", {}).get("conditions", []))
    if (
        mcp_conditions.get("Updated") != "True"
        or mcp_conditions.get("Updating") != "False"
        or mcp_conditions.get("Degraded") != "False"
    ):
        raise CaptureError("The master MachineConfigPool is not updated and healthy")

    etcd_image = None
    for node_name in NODE_NAMES:
        pod = oc_json(
            args,
            "get",
            "pod",
            f"etcd-{node_name}",
            "-n",
            "openshift-etcd",
            "-o",
            "json",
        )
        if pod.get("status", {}).get("phase") != "Running":
            raise CaptureError(f"etcd pod for {node_label(node_name)} is not Running")
        statuses = pod.get("status", {}).get("containerStatuses", [])
        if not statuses or any(not item.get("ready") for item in statuses):
            raise CaptureError(f"etcd pod for {node_label(node_name)} is not fully Ready")
        image = next(
            (
                item.get("image")
                for item in pod.get("spec", {}).get("containers", [])
                if item.get("name") == "etcd"
            ),
            None,
        )
        if not image:
            raise CaptureError(f"Could not determine the etcd image for {node_label(node_name)}")
        if etcd_image is None:
            etcd_image = image
        elif image != etcd_image:
            raise CaptureError("The three etcd members are not using the same image")

    print("Source cluster is ready: 3 nodes, ClusterOperators, master MCP, and etcd.")
    return etcd_image, node_ips


def virsh_command(uri, *args):
    """Build a virsh command for the selected libvirt connection."""
    return ["virsh", "--connect", uri, *args]


def parse_dominfo(output, domain):
    """Read and validate CPU, memory, and state from domain information."""
    fields = {}
    for line in output.decode(errors="replace").splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip()
    state = fields.get("State", "").lower()
    if state != "running":
        raise CaptureError(f"{vm_label(domain)} is not running")
    try:
        vcpus = int(fields["CPU(s)"])
        memory_kib = int(fields["Max memory"].split()[0])
    except (KeyError, ValueError) as error:
        raise CaptureError(f"Could not read CPU and memory for {vm_label(domain)}") from error
    return vcpus, memory_kib


def resolve_domain_disks(uri, domain):
    """Map a source VM's vda and vdb devices to disk paths."""
    xml = ET.fromstring(run(virsh_command(uri, "dumpxml", domain)))
    result = {}
    for disk in xml.findall("./devices/disk"):
        if disk.get("device") == "cdrom":
            continue
        target_element = disk.find("target")
        source_element = disk.find("source")
        if target_element is None or source_element is None:
            continue
        target = target_element.get("dev")
        source = source_element.attrib
        if "file" in source:
            path = source["file"]
        elif "dev" in source:
            path = source["dev"]
        elif "pool" in source and "volume" in source:
            path = run(
                virsh_command(uri, "vol-path", "--pool", source["pool"], source["volume"])
            ).decode().strip()
        else:
            raise CaptureError(f"Unsupported disk source for {vm_label(domain)} {target}")
        if target in result:
            raise CaptureError(f"Duplicate disk target {target} in {vm_label(domain)}")
        if not Path(path).exists():
            raise CaptureError(f"Disk {target} for {vm_label(domain)} does not exist")
        result[target] = Path(path)
    if set(result) != {"vda", "vdb"}:
        raise CaptureError(
            f"{vm_label(domain)} must have exactly vda (OS) and vdb (spare); found "
            + ", ".join(sorted(result))
        )
    return result


def domain_mac_addresses(uri, domain):
    """Read interface MAC addresses configured on a libvirt domain."""
    try:
        xml = ET.fromstring(run(virsh_command(uri, "dumpxml", domain)))
    except ET.ParseError as error:
        raise CaptureError(
            f"Could not read network identity for {vm_label(domain)}"
        ) from error
    return {
        element.get("address", "").lower()
        for element in xml.findall("./devices/interface/mac")
        if element.get("address")
    }


def node_interface_mac(args, identity, node_name, node_ip):
    """Read the MAC address of the API node interface that owns its InternalIP."""
    command = [
        "ssh",
        "-i",
        str(identity),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=accept-new",
        f"core@{node_ip}",
        "ip",
        "-j",
        "-4",
        "address",
        "show",
    ]
    try:
        interfaces = json.loads(run(command, timeout=20))
    except (CaptureError, json.JSONDecodeError) as error:
        raise CaptureError(
            f"Could not verify network identity for {node_label(node_name)}"
        ) from error
    matches = {
        interface.get("address", "").lower()
        for interface in interfaces
        if any(
            item.get("local") == node_ip
            for item in interface.get("addr_info", [])
        )
        and interface.get("address")
    }
    if len(matches) != 1 or not re.fullmatch(
        r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", next(iter(matches), "")
    ):
        raise CaptureError(
            f"Could not verify network identity for {node_label(node_name)}"
        )
    return next(iter(matches))


def verify_domain_node_bindings(args, domains, node_ips, identity):
    """Require each source VM MAC to match the API node at the same index."""
    if len(set(node_ips.values())) != len(NODE_NAMES):
        raise CaptureError("Source API nodes do not have unique IPv4 addresses")
    node_macs = {
        name: node_interface_mac(args, identity, name, address)
        for name, address in node_ips.items()
    }
    if len(set(node_macs.values())) != len(NODE_NAMES):
        raise CaptureError("Source API nodes do not have unique network identities")
    for index, domain in enumerate(domains):
        try:
            macs = domain_mac_addresses(args.libvirt_uri, domain)
        except CaptureError as error:
            raise CaptureError(
                f"Could not verify network identity for VM {index}"
            ) from error
        if node_macs[NODE_NAMES[index]] not in macs:
            raise CaptureError(
                f"VM {index} does not match the expected API node; refusing capture"
            )
    print("Confirmed each capture VM matches its API node.")


def image_info(path):
    """Read and validate qcow2 or raw image metadata."""
    info = json.loads(run(["qemu-img", "info", "-U", "--output=json", path]))
    if info.get("format") not in ("qcow2", "raw"):
        raise CaptureError("Unsupported source disk format")
    return info


def collect_source_plan(args):
    """Validate master resources and disk layout before downtime."""
    domains = []
    plan = {}
    expected_memory = args.expected_memory_gib * 1024 * 1024
    for index in range(3):
        domain = (
            f"{args.vm_prefix}-master-0-{index}"
            if args.vm_prefix
            else f"master-0-{index}"
        )
        domains.append(domain)
        vcpus, memory_kib = parse_dominfo(
            run(virsh_command(args.libvirt_uri, "dominfo", domain)), domain
        )
        if vcpus != args.expected_vcpus:
            raise CaptureError(
                f"{vm_label(domain)} has an unexpected vCPU count"
            )
        if memory_kib != expected_memory:
            raise CaptureError(
                f"{vm_label(domain)} has an unexpected memory size"
            )
        disks = resolve_domain_disks(args.libvirt_uri, domain)
        for target, expected_gib in (
            ("vda", args.expected_os_disk_gib),
            ("vdb", args.expected_spare_disk_gib),
        ):
            info = image_info(disks[target])
            expected_bytes = expected_gib * 1024**3
            if info.get("virtual-size") != expected_bytes:
                raise CaptureError(
                    f"Disk {target} for {vm_label(domain)} has an unexpected size"
                )
            plan[(index, target)] = {
                "domain": domain,
                "path": disks[target],
                "info": info,
            }
    print(
        "Source VM specs verified: "
        f"{args.expected_memory_gib} GiB RAM, {args.expected_vcpus} vCPU, "
        f"{args.expected_os_disk_gib} GiB vda, {args.expected_spare_disk_gib} GiB vdb "
        "on each master."
    )
    return domains, plan


def preflight_ssh(args, public_key, node_ips):
    """Verify SSH access and read each source node's interface identity."""
    identity = Path(args.ssh_identity).expanduser()
    if not identity.is_file():
        raise CaptureError("SSH private identity file is missing")
    if not public_key.is_file():
        raise CaptureError("SSH public identity file is missing")
    if stat.S_IMODE(identity.stat().st_mode) & 0o077:
        raise CaptureError(
            "SSH identity permissions are too open; run chmod 600 on the private key"
        )
    derived_key = run(["ssh-keygen", "-y", "-P", "", "-f", identity]).decode().strip().split()
    supplied_key = public_key.read_text().strip().split()
    if len(derived_key) < 2 or len(supplied_key) < 2 or derived_key[:2] != supplied_key[:2]:
        raise CaptureError("SSH public key does not match the private identity")
    for node_name in NODE_NAMES:
        ssh_address = (
            args.ssh_node_ip if node_name == "master-0-0" else node_ips[node_name]
        )
        command = [
            "ssh",
            "-i",
            str(identity),
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "StrictHostKeyChecking=accept-new",
            f"core@{ssh_address}",
            "true",
        ]
        try:
            run(command, timeout=20)
        except CaptureError as error:
            raise CaptureError(
                f"SSH preflight failed for {node_label(node_name)}. The key must "
                "already be trusted by core on all three masters."
            ) from error
    print("SSH key access verified on all three masters.")


def create_private_file(path, data, mode=0o600):
    """Create a new file without overwriting it and set its mode."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)
    if path.stat().st_size == 0:
        raise CaptureError("Refusing to keep an empty credential file")


def extract_secret(args, destination, resource, namespace, key):
    """Extract one required credential with a bounded timeout."""
    command = oc_command(
        args,
        "extract",
        resource,
        "-n",
        namespace,
        f"--keys={key}",
        "--to=-",
    )
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            result = subprocess.run(
                command, stdout=output, stderr=subprocess.PIPE, timeout=60
            )
    except subprocess.TimeoutExpired as error:
        destination.unlink(missing_ok=True)
        raise CaptureError(
            "Timed out after 60 seconds while extracting required credential material"
        ) from error
    if result.returncode:
        destination.unlink(missing_ok=True)
        raise CaptureError("Could not extract required credential material")
    if destination.stat().st_size == 0:
        destination.unlink(missing_ok=True)
        raise CaptureError("Extracted credential material was empty")
    os.chmod(destination, 0o600)


def extract_crypto(args, stage_dir, identity, public_key):
    """Collect and validate the signing and SSH material for the flavor."""
    crypto = stage_dir / "crypto"
    crypto.mkdir(mode=0o700)
    os.chmod(crypto, 0o700)
    for resource, namespace, filename in (
        ("secret/loadbalancer-serving-signer", "openshift-kube-apiserver-operator", "lb-signer.key"),
        ("secret/localhost-serving-signer", "openshift-kube-apiserver-operator", "localhost-signer.key"),
        ("secret/service-network-serving-signer", "openshift-kube-apiserver-operator", "service-network-signer.key"),
        ("secret/router-ca", "openshift-ingress-operator", "ingress.key"),
    ):
        extract_secret(args, crypto / filename, resource, namespace, "tls.key")
        run(["openssl", "pkey", "-in", crypto / filename, "-noout", "-check"])

    extract_secret(
        args,
        crypto / "admin-kubeconfig-client-ca.crt",
        "configmap/admin-kubeconfig-client-ca",
        "openshift-config",
        "ca-bundle.crt",
    )
    os.chmod(crypto / "admin-kubeconfig-client-ca.crt", 0o644)

    encoded_cert = run(
        oc_command(
            args,
            "get",
            "secret/router-ca",
            "-n",
            "openshift-ingress-operator",
            "-o",
            r"jsonpath={.data.tls\.crt}",
        )
    ).strip()
    try:
        cert = base64.b64decode(encoded_cert, validate=True)
    except ValueError as error:
        raise CaptureError("Could not decode the router-ca certificate") from error
    subject = run(
        ["openssl", "x509", "-noout", "-subject", "-nameopt", "RFC2253"],
        input_bytes=cert,
    ).decode().strip()
    match = re.search(r"(?:^|,)CN=([^,]+)", subject.removeprefix("subject="))
    if not match:
        raise CaptureError("Router CA certificate has no usable common name")
    create_private_file(crypto / "ingress-cn.txt", (match.group(1) + "\n").encode(), 0o644)

    create_private_file(crypto / "cluster-tool.key", identity.read_bytes(), 0o600)
    create_private_file(crypto / "cluster-tool.key.pub", public_key.read_bytes(), 0o644)
    print("Extracted signing keys, admin CA, ingress CN, and the trusted SSH keypair.")


def confirm_downtime(args, domains):
    """Require explicit acknowledgment before stopping the source VMs."""
    print(
        "The next step will gracefully shut down the three source VMs, capture their "
        "six disks, and restart them. The source cluster will be unavailable during capture."
    )
    print("Source VMs: master indexes 0, 1, and 2")
    if args.yes:
        return
    if not sys.stdin.isatty():
        raise CaptureError("Use --yes to acknowledge source-cluster downtime in non-interactive runs")
    response = input("Type 'shutdown' to continue: ").strip()
    if response != "shutdown":
        raise CaptureError("Capture cancelled")


def shutdown_domains(args, domains, shutdown_requested):
    """Request graceful shutdown and wait for every source master to stop."""
    for domain in domains:
        shutdown_requested.add(domain)
        run(virsh_command(args.libvirt_uri, "shutdown", domain))
    deadline = time.monotonic() + args.shutdown_timeout
    pending = set(domains)
    while pending and time.monotonic() < deadline:
        for domain in list(pending):
            state = run(virsh_command(args.libvirt_uri, "domstate", domain)).decode().strip().lower()
            if "shut off" in state:
                pending.remove(domain)
        if pending:
            print(
                "Waiting for source VMs to stop: "
                + ", ".join(vm_label(domain) for domain in sorted(pending)),
                flush=True,
            )
            time.sleep(2)
    if pending:
        raise CaptureError(
            "Some source VMs did not stop before the shutdown timeout; "
            "recovery will wait once more, and manual follow-up may be required."
        )


def domain_state(args, domain, *, timeout=10):
    """Read the current libvirt state of one source domain."""
    return run(
        virsh_command(args.libvirt_uri, "domstate", domain), timeout=timeout
    ).decode().strip().lower()


def wait_for_domain_states(args, domains, predicate, *, timeout, description):
    """Wait a bounded time for source domains to reach a state."""
    pending = set(domains)
    last_state = {}
    deadline = time.monotonic() + timeout
    while pending:
        for domain in sorted(pending):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                state = domain_state(args, domain, timeout=min(10, remaining))
            except (CaptureError, subprocess.TimeoutExpired) as error:
                state = ""
                last_state[domain] = "unavailable"
            else:
                last_state[domain] = state or "empty state"
            if state and predicate(state):
                pending.remove(domain)
        if not pending or time.monotonic() >= deadline:
            break
        print(
            f"Waiting for source VMs to {description}: "
            + ", ".join(
                f"{vm_label(domain)} ({last_state.get(domain, 'state not checked')})"
                for domain in sorted(pending)
            ),
            flush=True,
        )
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    return {domain: last_state.get(domain, "state not checked") for domain in pending}


def restart_domains(args, shutdown_requested):
    """Restart stopped source VMs and report any recovery failures."""
    errors = []
    unresolved = wait_for_domain_states(
        args,
        shutdown_requested,
        lambda state: "shut off" in state,
        timeout=args.shutdown_timeout,
        description="stop before restart",
    )
    for domain, state in sorted(unresolved.items()):
        errors.append(
            f"{vm_label(domain)} did not stop during recovery; "
            "manual follow-up is required because it may power off after the script exits."
        )

    stopped = set(shutdown_requested) - set(unresolved)
    started = set()
    for domain in sorted(stopped):
        try:
            run(virsh_command(args.libvirt_uri, "start", domain))
            started.add(domain)
            print(f"Started {vm_label(domain)}; waiting for it to reach running state.")
        except (CaptureError, subprocess.TimeoutExpired) as error:
            try:
                state = domain_state(args, domain)
            except (CaptureError, subprocess.TimeoutExpired):
                state = "state unavailable"
            if "running" in state or "blocked" in state:
                started.add(domain)
            else:
                errors.append(f"{vm_label(domain)} could not be started")

    unresolved = wait_for_domain_states(
        args,
        started,
        lambda state: "running" in state or "blocked" in state,
        timeout=args.shutdown_timeout,
        description="return to running state",
    )
    for domain, state in sorted(unresolved.items()):
        errors.append(
            f"{vm_label(domain)} did not return to running state during recovery"
        )
    return errors


def capture_disks(args, plan, stage_dir):
    """Convert and verify all six source disks as standalone images."""
    for index, target, filename, _kind in DISK_LAYOUT:
        source = plan[(index, target)]["path"]
        destination = stage_dir / filename
        disk_number = index * 2 + (1 if target == "vdb" else 0) + 1
        print(
            f"Capturing disk {disk_number}/6 "
            f"({plan[(index, target)]['info'].get('virtual-size', 0) // 1024**3} GiB)...",
            flush=True,
        )
        previous_umask = os.umask(0o077)
        try:
            run_visible(
                ["qemu-img", "convert", "-p", "-O", "qcow2", source, destination],
                timeout=3600,
            )
        finally:
            os.umask(previous_umask)
        run(["qemu-img", "check", "-f", "qcow2", destination])
        output_info = image_info(destination)
        expected_size = plan[(index, target)]["info"]["virtual-size"]
        if output_info.get("virtual-size") != expected_size:
            raise CaptureError(f"Captured disk {disk_number} has an unexpected virtual size")
        if output_info.get("backing-filename"):
            raise CaptureError(f"Captured disk {disk_number} still has a backing file")
        try:
            shutil.chown(destination, user="qemu", group="qemu")
        except (LookupError, PermissionError):
            raise CaptureError(
                "Could not set captured disks to the libvirt service account"
            )
        os.chmod(destination, 0o640)
        print(f"  verified disk {disk_number}/6", flush=True)


def ensure_libvirt_account():
    """Require the account used to own captured disks before downtime."""
    try:
        pwd.getpwnam("qemu")
        grp.getgrnam("qemu")
    except KeyError as error:
        raise CaptureError(
            "The qemu user and group are required to own captured disks"
        ) from error


def ensure_free_space(destination_dir, plan):
    """Estimate capture output from allocated source data plus a safety margin."""
    allocated_total = 0
    virtual_total = 0
    virtual_size_fallbacks = 0
    for item in plan.values():
        info = item["info"]
        virtual_size = int(info["virtual-size"])
        allocated_size = info.get("actual-size")
        if not isinstance(allocated_size, int) or allocated_size <= 0:
            allocated_size = virtual_size
            virtual_size_fallbacks += 1
        allocated_total += allocated_size
        virtual_total += virtual_size

    required = int(allocated_total * 1.15) + 1024**3
    usage = shutil.disk_usage(destination_dir)
    if usage.free < required:
        raise CaptureError(
            f"Not enough free space for capture: need about "
            f"{required / 1024**3:.1f} GiB, have {usage.free / 1024**3:.1f} GiB"
        )
    print(
        f"Capture estimate: {allocated_total / 1024**3:.1f} GiB allocated "
        f"across source disks (virtual capacity {virtual_total / 1024**3:.1f} GiB); "
        f"{usage.free / 1024**3:.1f} GiB available."
    )
    if virtual_size_fallbacks:
        print(
            f"Used virtual-size fallback for {virtual_size_fallbacks} disk(s) "
            "without a valid actual-size value."
        )


def save_state_atomic(config_dir, state):
    """Atomically save state while keeping it readable by the invoking user."""
    target = config_dir / "state.json"
    try:
        existing_stat = target.stat()
    except FileNotFoundError:
        existing_stat = None
    mode = stat.S_IMODE(existing_stat.st_mode) if existing_stat else 0o600
    fd, temp_name = tempfile.mkstemp(prefix=".state-", dir=config_dir)
    directory_fd = None
    committed = False
    try:
        with os.fdopen(fd, "w") as output:
            sudo_uid = os.environ.get("SUDO_UID")
            sudo_gid = os.environ.get("SUDO_GID")
            if os.geteuid() == 0:
                if sudo_uid is not None and sudo_gid is not None:
                    os.fchown(output.fileno(), int(sudo_uid), int(sudo_gid))
                elif existing_stat is not None:
                    os.fchown(
                        output.fileno(), existing_stat.st_uid, existing_stat.st_gid
                    )
            os.fchmod(output.fileno(), mode)
            json.dump(state, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        # Open the directory before replacing state.json so a failure here cannot
        # occur after the state commit and trigger a rollback of the flavor.
        directory_fd = os.open(config_dir, os.O_DIRECTORY)
        os.replace(temp_name, target)
        committed = True
        try:
            os.fsync(directory_fd)
        except OSError as error:
            raise StateCommittedError(
                "state.json was replaced, but syncing its directory failed; "
                "the flavor remains registered"
            ) from error
    except Exception:
        if not committed:
            Path(temp_name).unlink(missing_ok=True)
        raise
    finally:
        if directory_fd is not None:
            try:
                os.close(directory_fd)
            except OSError as error:
                if committed:
                    raise StateCommittedError(
                        "state.json was replaced, but closing its directory failed; "
                        "the flavor remains registered"
                    ) from error
                raise


def restore_invoking_user_ownership(path):
    """Restore ownership to the user who invoked sudo."""
    if os.geteuid() != 0:
        return
    sudo_uid = os.environ.get("SUDO_UID")
    sudo_gid = os.environ.get("SUDO_GID")
    if sudo_uid is None or sudo_gid is None:
        return
    try:
        os.chown(path, int(sudo_uid), int(sudo_gid))
    except (OSError, ValueError) as error:
        raise CaptureError(
            "Could not restore invoking-user ownership on a cluster-tool state file"
        ) from error


def acquire_state_lock(state_dir, lock_dir):
    """Create and acquire cluster-tool's shared state lock."""
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    restore_invoking_user_ownership(state_dir)
    lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(lock_dir, 0o700)
    restore_invoking_user_ownership(lock_dir)
    lock_path = lock_dir / "state.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        restore_invoking_user_ownership(lock_path)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def ensure_flavor_name_available(state_dir, lock_dir, flavor_name, final_dir):
    """Reject state or directory name conflicts before stopping source VMs."""
    descriptor = acquire_state_lock(state_dir, lock_dir)
    try:
        state_path = state_dir / "state.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        if flavor_name in state.get("flavors", {}) or final_dir.exists():
            raise CaptureError(
                "Flavor name is already registered; choose another name or remove it first"
            )
    finally:
        os.close(descriptor)


def register_flavor(state_dir, lock_dir, stage_dir, flavor_name, metadata):
    """Lock, save, and atomically register the captured flavor."""
    descriptor = acquire_state_lock(state_dir, lock_dir)
    final_dir = stage_dir.parent / flavor_name
    moved = False
    saved = False
    try:
        state_path = state_dir / "state.json"
        if state_path.exists():
            state = json.loads(state_path.read_text())
        else:
            state = {"flavors": {}, "clones": {}}
        state.setdefault("flavors", {})
        state.setdefault("clones", {})
        if flavor_name in state["flavors"] or final_dir.exists():
            raise CaptureError(
                "Flavor name is already registered; choose another name or remove it first"
            )
        state["flavors"][flavor_name] = metadata
        os.replace(stage_dir, final_dir)
        moved = True
        try:
            save_state_atomic(state_dir, state)
            saved = True
        except StateCommittedError:
            # The state points at final_dir; never roll it back after replacement.
            saved = True
            raise
    except Exception:
        if moved and not saved and final_dir.exists():
            os.replace(final_dir, stage_dir)
        raise
    finally:
        os.close(descriptor)
    return final_dir


def main():
    """Run the capture workflow and register the resulting compact flavor."""
    args = parse_args()
    validate_args(args)
    if os.geteuid() != 0:
        raise CaptureError("Run this script as root on the libvirt host")
    require_programs(["virsh", "qemu-img", "oc", "ssh", "ssh-keygen", "openssl"])

    kubeconfig = Path(args.kubeconfig).expanduser()
    if not kubeconfig.is_file():
        raise CaptureError("Kubeconfig file is missing")
    args.kubeconfig = str(kubeconfig)
    identity = Path(args.ssh_identity).expanduser()
    public_key = Path(args.ssh_public_key).expanduser() if args.ssh_public_key else Path(str(identity) + ".pub")
    state_dir, lock_dir, data_dir = server_config()
    data_dir.mkdir(parents=True, exist_ok=True)
    flavors_dir = data_dir / "flavors"
    flavors_dir.mkdir(parents=True, exist_ok=True)
    final_dir = flavors_dir / args.flavor
    ensure_flavor_name_available(state_dir, lock_dir, args.flavor, final_dir)

    etcd_image, node_ips = verify_cluster(args)
    preflight_ssh(args, public_key, node_ips)
    domains, plan = collect_source_plan(args)
    verify_domain_node_bindings(args, domains, node_ips, identity)
    ensure_free_space(flavors_dir, plan)
    ensure_libvirt_account()

    stage_dir = Path(
        tempfile.mkdtemp(prefix=f".{args.flavor}.capture-", dir=flavors_dir)
    )
    os.chmod(stage_dir, 0o700)
    shutdown_started = False
    restarted = False
    shutdown_requested = set()
    try:
        extract_crypto(args, stage_dir, identity, public_key)
        confirm_downtime(args, domains)
        try:
            shutdown_started = True
            shutdown_domains(args, domains, shutdown_requested)
            capture_disks(args, plan, stage_dir)
        finally:
            original_error = sys.exc_info()[1]
            restart_errors = restart_domains(args, shutdown_requested)
            restarted = not restart_errors
            if restart_errors:
                message = (
                    "Failed to restore one or more source VMs:\n  "
                    + "\n  ".join(restart_errors)
                )
                if original_error is not None:
                    message += "\nThe capture operation also failed."
                    raise CaptureError(message) from original_error
                raise CaptureError(message)

        # Expose the directory only after capture is complete. The private
        # crypto subdirectory remains mode 0700.
        os.chmod(stage_dir, 0o755)
        disk_names = [entry[2] for entry in DISK_LAYOUT]
        metadata = {
            "topology": "compact",
            "source_cluster": args.source_cluster,
            "source_primary_subnet": args.source_primary_subnet,
            "memory_kib": args.expected_memory_gib * 1024 * 1024,
            "vcpus": args.expected_vcpus,
            "disks": disk_names,
            "etcd_image": etcd_image,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        registered = register_flavor(state_dir, lock_dir, stage_dir, args.flavor, metadata)
        stage_dir = None
        print("\nFlavor capture and registration completed successfully.")
        print("Inspect the local flavor, then push it with cluster-tool.")
    finally:
        if stage_dir and stage_dir.exists():
            if restarted or not shutdown_started:
                shutil.rmtree(stage_dir)
            else:
                print(
                    "Preserving the private capture staging directory for recovery.",
                    file=sys.stderr,
                )


def handle_termination(_signum, _frame):
    """Turn SIGTERM into an interrupt so normal cleanup can run."""
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_termination)
    try:
        main()
    except KeyboardInterrupt:
        print("Capture interrupted; check source VM state before continuing.", file=sys.stderr)
        sys.exit(130)
    except CaptureError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        print(
            "ERROR: A local command or state operation failed; "
            "check the source VM state before continuing.",
            file=sys.stderr,
        )
        sys.exit(1)
