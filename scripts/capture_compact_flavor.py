#!/usr/bin/env python3
"""Capture a bare three-node compact OpenShift cluster as a cluster-tool flavor."""

import argparse
import base64
import fcntl
import ipaddress
import json
import os
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


def display_command(command):
    return " ".join(str(part) for part in command)


def run(command, *, timeout=60, input_bytes=None):
    result = subprocess.run(
        [str(part) for part in command],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip()
        raise CaptureError(
            f"Command failed ({result.returncode}): {display_command(command)}"
            + (f"\n{detail}" if detail else "")
        )
    return result.stdout


def run_visible(command, *, timeout=None):
    result = subprocess.run([str(part) for part in command], timeout=timeout)
    if result.returncode:
        raise CaptureError(
            f"Command failed ({result.returncode}): {display_command(command)}"
        )


def require_programs(names):
    missing = [name for name in names if shutil.which(name) is None]
    if missing:
        raise CaptureError(f"Missing required command(s): {', '.join(missing)}")


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Capture a healthy, bare three-node compact OpenShift cluster into "
            "a cluster-tool flavor directory and register it for push."
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
        required=True,
        help="Prefix of source libvirt domains, before '-master-0-N'",
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
    for label, value in (
        ("flavor", args.flavor),
        ("source-cluster", args.source_cluster),
        ("vm-prefix", args.vm_prefix),
    ):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
            raise CaptureError(
                f"--{label} must contain only letters, digits, '.', '_' or '-'"
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
        raise CaptureError(f"Invalid API or SSH IP address: {error}") from error
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


def server_config():
    config_file = Path.home() / ".config" / "cluster-tool" / "config"
    if not config_file.is_file():
        raise CaptureError(
            f"{config_file} is missing; initialize cluster-tool on this host first"
        )
    data_dir = None
    for line in config_file.read_text().splitlines():
        if line.startswith("CLUSTER_TOOL_DATA="):
            data_dir = line.split("=", 1)[1].strip().strip("'").strip('"')
            break
    if not data_dir:
        raise CaptureError(f"No CLUSTER_TOOL_DATA entry found in {config_file}")
    data_dir = Path(os.path.expandvars(os.path.expanduser(data_dir)))
    state_dir = Path.home() / ".config" / "cluster-tool"
    # Match cluster-tool's locked_state() path when invoked through sudo.
    invoking_user = os.environ.get("SUDO_USER", "")
    lock_home = Path(os.path.expanduser(f"~{invoking_user}"))
    lock_dir = lock_home / ".config" / "cluster-tool"
    return state_dir, lock_dir, data_dir


def oc_command(args, *subcommand):
    return [
        shutil.which("oc") or "oc",
        f"--kubeconfig={Path(args.kubeconfig).expanduser()}",
        f"--server=https://{args.api_address}:6443",
        f"--tls-server-name={args.api_server_name}",
        *subcommand,
    ]


def oc_json(args, *subcommand):
    return json.loads(run(oc_command(args, *subcommand)))


def condition_map(conditions):
    return {condition.get("type"): condition.get("status") for condition in conditions}


def is_ipv4(address):
    try:
        return isinstance(ipaddress.ip_address(address), ipaddress.IPv4Address)
    except ValueError:
        return False


def verify_cluster(args):
    print("Checking source cluster readiness...")
    nodes = oc_json(args, "get", "nodes", "-o", "json").get("items", [])
    by_name = {node.get("metadata", {}).get("name"): node for node in nodes}
    if set(by_name) != set(NODE_NAMES):
        raise CaptureError(
            "Expected exactly master-0-0, master-0-1, and master-0-2; found: "
            + ", ".join(sorted(by_name))
        )
    node_ips = {}
    for name in NODE_NAMES:
        node = by_name[name]
        ready = any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in node.get("status", {}).get("conditions", [])
        )
        if not ready:
            raise CaptureError(f"Source node {name} is not Ready")
        addresses = [
            item.get("address")
            for item in node.get("status", {}).get("addresses", [])
            if item.get("type") == "InternalIP" and item.get("address")
        ]
        ipv4_addresses = [address for address in addresses if is_ipv4(address)]
        if not ipv4_addresses:
            raise CaptureError(f"Could not find an IPv4 InternalIP for {name}")
        node_ips[name] = ipv4_addresses[0]

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
            "Source is not a bare OpenShift snapshot; remove these add-on namespaces "
            "before capture: " + ", ".join(sorted(add_on_namespaces))
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
            "ClusterOperators are not quiet and healthy: " + ", ".join(unhealthy)
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
            raise CaptureError(f"etcd pod etcd-{node_name} is not Running")
        statuses = pod.get("status", {}).get("containerStatuses", [])
        if not statuses or any(not item.get("ready") for item in statuses):
            raise CaptureError(f"etcd pod etcd-{node_name} is not fully Ready")
        image = next(
            (
                item.get("image")
                for item in pod.get("spec", {}).get("containers", [])
                if item.get("name") == "etcd"
            ),
            None,
        )
        if not image:
            raise CaptureError(f"Could not determine etcd image from etcd-{node_name}")
        if etcd_image is None:
            etcd_image = image
        elif image != etcd_image:
            raise CaptureError("The three etcd members are not using the same image")

    print("Source cluster is ready: 3 nodes, ClusterOperators, master MCP, and etcd.")
    return etcd_image, node_ips


def virsh_command(uri, *args):
    return ["virsh", "--connect", uri, *args]


def parse_dominfo(output, domain):
    fields = {}
    for line in output.decode(errors="replace").splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip()
    state = fields.get("State", "").lower()
    if state != "running":
        raise CaptureError(f"Source VM {domain} is {state or 'in an unknown state'}, not running")
    try:
        vcpus = int(fields["CPU(s)"])
        memory_kib = int(fields["Max memory"].split()[0])
    except (KeyError, ValueError) as error:
        raise CaptureError(f"Could not read CPU and memory from virsh dominfo for {domain}") from error
    return vcpus, memory_kib


def resolve_domain_disks(uri, domain):
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
            raise CaptureError(f"Unsupported source for {domain} disk {target}")
        if target in result:
            raise CaptureError(f"Duplicate disk target {target} in {domain}")
        if not Path(path).exists():
            raise CaptureError(f"Disk path for {domain} {target} does not exist: {path}")
        result[target] = Path(path)
    if set(result) != {"vda", "vdb"}:
        raise CaptureError(
            f"{domain} must have exactly vda (OS) and vdb (spare); found "
            + ", ".join(sorted(result))
        )
    return result


def image_info(path):
    info = json.loads(run(["qemu-img", "info", "--output=json", path]))
    if info.get("format") not in ("qcow2", "raw"):
        raise CaptureError(f"Unsupported source disk format for {path}: {info.get('format')}")
    return info


def collect_source_plan(args):
    domains = []
    plan = {}
    expected_memory = args.expected_memory_gib * 1024 * 1024
    for index in range(3):
        domain = f"{args.vm_prefix}-master-0-{index}"
        domains.append(domain)
        vcpus, memory_kib = parse_dominfo(
            run(virsh_command(args.libvirt_uri, "dominfo", domain)), domain
        )
        if vcpus != args.expected_vcpus:
            raise CaptureError(
                f"{domain} has {vcpus} vCPUs; expected {args.expected_vcpus}"
            )
        if memory_kib != expected_memory:
            raise CaptureError(
                f"{domain} has {memory_kib} KiB RAM; expected {expected_memory} KiB"
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
                    f"{domain} {target} is {info.get('virtual-size')} bytes; "
                    f"expected {expected_bytes} ({expected_gib} GiB)"
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
    identity = Path(args.ssh_identity).expanduser()
    if not identity.is_file():
        raise CaptureError(f"SSH identity does not exist: {identity}")
    if not public_key.is_file():
        raise CaptureError(f"SSH public key does not exist: {public_key}")
    if stat.S_IMODE(identity.stat().st_mode) & 0o077:
        raise CaptureError(
            f"SSH identity permissions are too open: {identity}; run chmod 600 on it"
        )
    derived_key = run(["ssh-keygen", "-y", "-P", "", "-f", identity]).decode().strip().split()
    supplied_key = public_key.read_text().strip().split()
    if len(derived_key) < 2 or len(supplied_key) < 2 or derived_key[:2] != supplied_key[:2]:
        raise CaptureError(f"SSH public key does not match private identity: {public_key}")
    node_ips["master-0-0"] = args.ssh_node_ip
    for node_name in NODE_NAMES:
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
            f"core@{node_ips[node_name]}",
            "true",
        ]
        try:
            run(command, timeout=20)
        except CaptureError as error:
            raise CaptureError(
                f"SSH preflight failed for {node_name} ({node_ips[node_name]}). "
                "The public key must already be trusted by core on all three "
                f"masters. {error}"
            ) from error
    print("SSH key access verified on all three masters.")


def create_private_file(path, data, mode=0o600):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)
    if path.stat().st_size == 0:
        raise CaptureError(f"Refusing to keep an empty credential file: {path.name}")


def extract_secret(args, destination, resource, namespace, key):
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
    with os.fdopen(descriptor, "wb") as output:
        result = subprocess.run(command, stdout=output, stderr=subprocess.PIPE)
    if result.returncode:
        destination.unlink(missing_ok=True)
        detail = result.stderr.decode(errors="replace").strip()
        raise CaptureError(
            f"Could not extract {resource} key {key}"
            + (f": {detail}" if detail else "")
        )
    if destination.stat().st_size == 0:
        destination.unlink(missing_ok=True)
        raise CaptureError(f"Extracted {resource} key {key} was empty")
    os.chmod(destination, 0o600)


def extract_crypto(args, stage_dir, identity, public_key):
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
        raise CaptureError(f"Could not find a single CN in router-ca certificate: {subject}")
    create_private_file(crypto / "ingress-cn.txt", (match.group(1) + "\n").encode(), 0o644)

    create_private_file(crypto / "cluster-tool.key", identity.read_bytes(), 0o600)
    create_private_file(crypto / "cluster-tool.key.pub", public_key.read_bytes(), 0o644)
    print("Extracted signing keys, admin CA, ingress CN, and the trusted SSH keypair.")


def confirm_downtime(args, domains):
    print(
        "The next step will gracefully shut down the three source VMs, capture their "
        "six disks, and restart them. The source cluster will be unavailable during capture."
    )
    print("Source VMs: " + ", ".join(domains))
    if args.yes:
        return
    if not sys.stdin.isatty():
        raise CaptureError("Use --yes to acknowledge source-cluster downtime in non-interactive runs")
    response = input("Type 'shutdown' to continue: ").strip()
    if response != "shutdown":
        raise CaptureError("Capture cancelled")


def shutdown_domains(args, domains):
    for domain in domains:
        run(virsh_command(args.libvirt_uri, "shutdown", domain))
    deadline = time.monotonic() + args.shutdown_timeout
    pending = set(domains)
    while pending and time.monotonic() < deadline:
        for domain in list(pending):
            state = run(virsh_command(args.libvirt_uri, "domstate", domain)).decode().strip().lower()
            if "shut off" in state:
                pending.remove(domain)
        if pending:
            print("Waiting for VMs to shut down: " + ", ".join(sorted(pending)), flush=True)
            time.sleep(2)
    if pending:
        raise CaptureError(
            "VMs did not shut down before the timeout: " + ", ".join(sorted(pending))
        )


def restart_domains(args, domains):
    errors = []
    for domain in domains:
        try:
            state = run(virsh_command(args.libvirt_uri, "domstate", domain)).decode().strip().lower()
            if "running" not in state:
                run(virsh_command(args.libvirt_uri, "start", domain))
                print(f"Restarted {domain}.")
        except (CaptureError, subprocess.TimeoutExpired) as error:
            errors.append(f"{domain}: {error}")
    return errors


def capture_disks(args, plan, stage_dir):
    for index, target, filename, _kind in DISK_LAYOUT:
        source = plan[(index, target)]["path"]
        destination = stage_dir / filename
        print(
            f"Capturing {filename} from {source} "
            f"({plan[(index, target)]['info'].get('virtual-size', 0) // 1024**3} GiB)...",
            flush=True,
        )
        run_visible(["qemu-img", "convert", "-p", "-O", "qcow2", source, destination])
        run(["qemu-img", "check", "-f", "qcow2", destination])
        output_info = image_info(destination)
        expected_size = plan[(index, target)]["info"]["virtual-size"]
        if output_info.get("virtual-size") != expected_size:
            raise CaptureError(f"Captured {filename} has an unexpected virtual size")
        if output_info.get("backing-filename"):
            raise CaptureError(f"Captured {filename} still has a backing file")
        try:
            shutil.chown(destination, user="qemu", group="qemu")
        except (LookupError, PermissionError):
            raise CaptureError(
                "Could not set captured disks to qemu:qemu ownership; "
                "check that libvirt's qemu account exists"
            )
        os.chmod(destination, 0o640)
        print(f"  verified {filename}", flush=True)


def ensure_free_space(data_dir, plan):
    total = sum(
        int(item["info"].get("actual-size", item["info"].get("virtual-size", 0)))
        for item in plan.values()
    )
    required = int(total * 1.15) + 1024**3
    usage = shutil.disk_usage(data_dir)
    if usage.free < required:
        raise CaptureError(
            f"Not enough free space under {data_dir}: need about "
            f"{required / 1024**3:.1f} GiB, have {usage.free / 1024**3:.1f} GiB"
        )
    print(
        f"Capture space check: about {total / 1024**3:.1f} GiB of source data; "
        f"{usage.free / 1024**3:.1f} GiB available."
    )


def save_state_atomic(config_dir, state):
    target = config_dir / "state.json"
    fd, temp_name = tempfile.mkstemp(prefix=".state-", dir=config_dir)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(state, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_name, target)
        directory_fd = os.open(config_dir, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        Path(temp_name).unlink(missing_ok=True)
        raise


def register_flavor(state_dir, lock_dir, stage_dir, flavor_name, metadata):
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = lock_dir / "state.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    final_dir = stage_dir.parent / flavor_name
    moved = False
    saved = False
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        state_path = state_dir / "state.json"
        if state_path.exists():
            state = json.loads(state_path.read_text())
        else:
            state = {"flavors": {}, "clones": {}}
        state.setdefault("flavors", {})
        state.setdefault("clones", {})
        if flavor_name in state["flavors"] or final_dir.exists():
            raise CaptureError(
                f"Flavor '{flavor_name}' already exists; choose another name or delete it first"
            )
        state["flavors"][flavor_name] = metadata
        os.replace(stage_dir, final_dir)
        moved = True
        save_state_atomic(state_dir, state)
        saved = True
    except Exception:
        if moved and not saved and final_dir.exists():
            os.replace(final_dir, stage_dir)
        raise
    finally:
        os.close(descriptor)
    return final_dir


def main():
    args = parse_args()
    validate_args(args)
    if os.geteuid() != 0:
        raise CaptureError("Run this script as root on the libvirt host")
    require_programs(["virsh", "qemu-img", "oc", "ssh", "ssh-keygen", "openssl"])

    kubeconfig = Path(args.kubeconfig).expanduser()
    if not kubeconfig.is_file():
        raise CaptureError(f"Kubeconfig does not exist: {kubeconfig}")
    args.kubeconfig = str(kubeconfig)
    identity = Path(args.ssh_identity).expanduser()
    public_key = Path(args.ssh_public_key).expanduser() if args.ssh_public_key else Path(str(identity) + ".pub")
    state_dir, lock_dir, data_dir = server_config()
    data_dir.mkdir(parents=True, exist_ok=True)
    flavors_dir = data_dir / "flavors"
    flavors_dir.mkdir(parents=True, exist_ok=True)
    final_dir = flavors_dir / args.flavor
    if final_dir.exists():
        raise CaptureError(f"Flavor directory already exists: {final_dir}")

    etcd_image, node_ips = verify_cluster(args)
    preflight_ssh(args, public_key, node_ips)
    domains, plan = collect_source_plan(args)
    ensure_free_space(data_dir, plan)

    stage_dir = Path(
        tempfile.mkdtemp(prefix=f".{args.flavor}.capture-", dir=flavors_dir)
    )
    os.chmod(stage_dir, 0o755)
    shutdown_started = False
    restarted = False
    try:
        extract_crypto(args, stage_dir, identity, public_key)
        confirm_downtime(args, domains)
        try:
            shutdown_started = True
            shutdown_domains(args, domains)
            capture_disks(args, plan, stage_dir)
        finally:
            restart_errors = restart_domains(args, domains)
            restarted = not restart_errors
            if restart_errors:
                raise CaptureError(
                    "Failed to restart one or more source VMs:\n  "
                    + "\n  ".join(restart_errors)
                )

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
        print(f"\nFlavor '{args.flavor}' created and registered at {registered}")
        print("Next: inspect it with './cluster-tool flavors --server local', then push it.")
    finally:
        if stage_dir and stage_dir.exists():
            if restarted or not shutdown_started:
                shutil.rmtree(stage_dir)
            else:
                print(
                    f"Preserving capture staging directory for recovery: {stage_dir}",
                    file=sys.stderr,
                )


def handle_termination(_signum, _frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_termination)
    try:
        main()
    except KeyboardInterrupt:
        print("Capture interrupted; source VM restart was attempted.", file=sys.stderr)
        sys.exit(130)
    except (CaptureError, subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
