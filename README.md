# cluster-tool

Instant OpenShift SNO clusters from snapshots. Distribute them as OCI images.

## The Problem

Installing an OpenShift Single Node cluster takes 30-45 minutes. For development and testing, you need fresh clusters frequently — sometimes several in parallel, sometimes on different machines. Waiting 45 minutes each time is not acceptable.

## The Solution

`cluster-tool` snapshots a running SNO cluster's VM disk, then boots independent clones from that snapshot in ~5 minutes. Each clone gets a unique identity — new cluster name, new certificates, new IP, new hostname — via the [recert](https://github.com/rh-ecosystem-edge/recert) tool.

Flavors can be pushed to an OCI registry (Quay.io) and pulled on any baremetal machine. A new developer can go from nothing to a running OpenShift cluster with CNV, MCE, or LVMS pre-installed in ~20 minutes.

**Before:** 45 minutes to install a cluster from scratch.
**After:** ~5 minutes to boot a clone, ~20 minutes including pull from registry.

## Concepts

**Flavor** — A reusable cluster template. It's a directory containing golden qcow2 disk image(s), SSH keys, CA signing keys, and VM specs (RAM, vCPUs). You create a flavor by snapshotting a running cluster, then boot new clusters from it. Examples: `osac-vmaas` (SNO with OSAC VMAAS), `osac-caas` (SNO with OSAC CAAS).

## Installation

```bash
git clone https://github.com/osac-project/cluster-tool.git
cd cluster-tool
./cluster-tool --help
```

Single Python 3 file, stdlib only, no installation needed.

## Requirements

**Client (your laptop):**
- Linux with NetworkManager (Fedora, RHEL, CentOS). No macOS or Windows support.
- Python 3

**Server (baremetal):**
- RHEL, CentOS, or Fedora (uses `dnf`)
- Root SSH access (passwordless): run `ssh-copy-id root@server` to set up
- Enough disk space: ~100 GB per flavor, overlays are small (copy-on-write)
- Enough RAM: each clone uses the flavor's spec (default: 64 GB RAM, 16 vCPUs)

Everything else (libvirt, qemu-kvm, podman, pigz, haproxy) is installed automatically by `connect`.

## Minimum Resource Requirements

Each clone boots a full OpenShift SNO virtual machine. Plan your server hardware accordingly.

### Per-Clone VM Defaults

| Resource | Default | Notes |
|----------|---------|-------|
| RAM | 64 GB | Set by the flavor at snapshot time |
| vCPUs | 16 | Set by the flavor at snapshot time |

### Server Sizing

| Component | Minimum (1 clone) | Recommended (3 clones) |
|-----------|--------------------|------------------------|
| RAM | 72 GB | 200 GB |
| CPU cores | 20 | 52 |
| Disk | 200 GB | 400 GB |

**Disk breakdown:**
- ~100 GB per flavor (golden qcow2 disk images, 60–90 GB each plus metadata)
- Overlays per clone are small (copy-on-write, only stores diffs)
- Reserve space for container storage (podman image cache, etcd/recert images)

### Network

| Port | Protocol | Purpose |
|------|----------|---------|
| 6443 | TCP | Kubernetes API (HAProxy SNI routing) |
| 443 | TCP | Ingress HTTPS (HAProxy SNI routing) |
| 80 | TCP | Ingress HTTP |

- Each clone allocates two `/24` subnets from the `192.168.x.0` range (primary + secondary at offset +18)
- Maximum ~90 clones per server before subnet exhaustion

## Where Does It Run?

cluster-tool runs on your **laptop** and connects to **baremetal servers** (beakers) via SSH. Your laptop is the control plane — it sends commands to the server where VMs actually run.

```
You (laptop)  --SSH-->  Baremetal server (VMs run here)
```

**Remote mode** (typical): You run `./cluster-tool` on your laptop. It SSHes into the server to manage VMs, disks, and networks.

**Local mode** (CI / running directly on the server): If you're already on the baremetal machine, use `--host local` when connecting. Commands run directly instead of over SSH.

```bash
# Remote: from your laptop
./cluster-tool connect myserver --host root@beaker.example.com --data-path /data/cluster-tool

# Local: from the baremetal machine itself
./cluster-tool connect local --host local --data-path /data/cluster-tool
```

**`--data-path`** is the directory on the server where cluster-tool stores everything heavy — golden disk images (60-90 GB each), per-clone overlay disks, and container storage. It must be on a partition with enough space. The root filesystem is often too small; this flag lets you point to a larger disk (e.g., `/data/cluster-tool` or `/home/cluster-tool`). If omitted during `connect`, the tool auto-detects the largest partition and suggests a path.

## Getting a Cluster

### Option 1: Pull an existing flavor (recommended)

Pre-built flavors are available in Quay. Pull one and boot — no need to create a snapshot from scratch.

```bash
# One-time setup
sudo ./cluster-tool setup client
./cluster-tool connect myserver --host root@beaker.example.com --data-path /data/cluster-tool

# Pull a flavor from Quay and boot it
./cluster-tool pull quay.io/myorg/cluster-flavors:osac-vmaas
./cluster-tool boot --flavor osac-vmaas --name my-test

# Use it
export KUBECONFIG=~/.kube/my-test.kubeconfig
oc get nodes
```

### Option 2: Create your own flavor

Only needed if you need a custom cluster configuration that doesn't exist as a pre-built flavor. You need a running SNO cluster on the server first — any installation method works ([assisted-test-infra](https://github.com/openshift/assisted-test-infra), Assisted Installer, manual install).

```bash
# Find the source VM ID on the server:
# virsh list shows: test-infra-cluster-6ef80144-master-0
# The source ID is the 8 hex chars: 6ef80144

./cluster-tool snapshot --name my-flavor --source 6ef80144
./cluster-tool boot --flavor my-flavor --name my-test
```

## Quick Start

```bash
# One-time setup: configure DNS on your laptop
sudo ./cluster-tool setup client

# Connect to a baremetal server
./cluster-tool connect myserver --host root@myhost.example.com --data-path /data/cluster-tool

# Pull a pre-built flavor and boot (~15-20 min total)
./cluster-tool pull quay.io/myorg/cluster-flavors:osac-vmaas
./cluster-tool boot --flavor osac-vmaas --name my-test

# Use it
export KUBECONFIG=~/.kube/my-test.kubeconfig
oc get nodes

# Or create your own flavor from a running SNO cluster
./cluster-tool snapshot --name sno-64 --source 6ef80144
./cluster-tool boot --flavor sno-64 --name my-test-2

# Push to Quay for distribution
./cluster-tool push sno-64 --registry quay.io/myorg/cluster-flavors --tag sno-64

# On another machine: pull and boot
./cluster-tool connect other-server --host root@other.example.com --data-path /home/cluster-tool
./cluster-tool pull --server other-server quay.io/myorg/cluster-flavors:sno-64
./cluster-tool boot --server other-server --flavor sno-64 --name remote-test

# Tear down
./cluster-tool destroy my-test
./cluster-tool destroy --server other-server remote-test
```

## Server Registry

cluster-tool manages multiple baremetal servers. Connect once, use by alias.

```bash
# Connect to servers (installs packages, configures storage)
./cluster-tool connect rdu --host root@rdu-host.example.com --data-path /data/cluster-tool
./cluster-tool connect dell --host root@dell-host.example.com --data-path /home/cluster-tool

# List connected servers
./cluster-tool servers

# Set a default server
./cluster-tool use rdu

# Target a specific server with --server
./cluster-tool boot --server dell --flavor sno-cnv --name my-test

# Without --server, uses the default
./cluster-tool boot --flavor sno-cnv --name my-test
```

For CI, use `local` as the host when running directly on the baremetal machine:
```bash
./cluster-tool connect ci --host local --data-path /home/cluster-tool
```

## Commands

| Command | Description |
|---------|-------------|
| `setup client` | One-time DNS setup on your laptop (dnsmasq + polkit). Requires sudo. |
| `connect NAME --host HOST [--data-path PATH]` | Connect to a baremetal server. Installs packages, configures storage, registers the server locally. |
| `servers` | List all connected servers with default marker. |
| `use NAME` | Set the default server. |
| `snapshot --name NAME --source ID` | Create a snapshot flavor from a running cluster. Flattens disks, extracts crypto keys, injects SSH key. The private key is stored in the flavor's `crypto/` dir and travels with push/pull. |
| `boot --flavor NAME [--name ID] [--server S]` | Boot a fresh clone. Creates overlays, networks, runs recert, waits for operators. If `--name` is omitted, a random 8-character hex ID is generated. |
| `list [--server S]` | Show running clones. |
| `flavors [--delete NAME] [--server S]` | List or delete flavors. |
| `verify ID [--server S]` | Deploys a test pod that checks cluster DNS, external DNS resolution, and API access via service account. Reports PASS/FAIL per check. |
| `destroy ID\|--all [--server S]` | Tear down a clone. |
| `push NAME --registry REPO --tag TAG [--server S]` | Push a flavor to an OCI registry. Splits, compresses, builds multi-layer image. |
| `pull IMAGE [--server S]` | Pull a flavor from an OCI registry. Downloads, decompresses, reassembles, registers. |

## OCI Distribution

Flavors are distributed as OCI container images via any registry (Quay.io, Docker Hub, etc.).

**Push** splits each disk into 1 GB chunks, compresses with pigz (parallel gzip), and builds an OCI image with each chunk as a separate layer. Layers download in parallel (20 concurrent streams).

**Pull** downloads the image, extracts chunks, decompresses in parallel (16 concurrent pigz processes), and reassembles the disks. The flavor is registered and ready for `boot`.

```bash
# Push (runs on the server where the flavor lives)
./cluster-tool push sno-cnv --registry quay.io/myorg/flavors --tag sno-cnv

# Pull (runs on any server)
./cluster-tool pull --server target quay.io/myorg/flavors:sno-cnv

# Boot from the pulled flavor
./cluster-tool boot --server target --flavor sno-cnv --name my-clone
```

Measured timings (SNO with CNV+LVMS, 90 GB disk):

| Operation | Time |
|-----------|------|
| Push to Quay | ~27 min |
| Pull from Quay | ~10 min |
| Boot with recert | ~5-7 min |
| **Pull + Boot (nothing to running cluster)** | **~15-20 min** |

## What Happens During Setup

### `setup client` (your laptop)
1. Installs dnsmasq
2. Configures NetworkManager to use dnsmasq as DNS backend
3. Grants your user polkit permission to reload NetworkManager without sudo
4. Verifies resolv.conf points to 127.0.0.1

### `connect` (baremetal server)
1. Installs libvirt, qemu-kvm, podman, pigz, haproxy
2. Configures HAProxy with SNI-based frontends for API (6443), ingress HTTPS (443), and HTTP (80). Sets SELinux boolean for non-standard ports. Opens firewall ports if firewalld is active.
3. Generates an ed25519 SSH keypair for cross-machine VM access
4. Configures podman parallel downloads (20 concurrent layers instead of the default 6, to saturate the network link when pulling large OCI images)
5. Auto-detects storage (largest partition) or uses `--data-path`, since baremetal machines have different disk layouts and the root filesystem is often too small for 60-90 GB disk images
6. Writes server config (idempotent — never overwrites existing config)
7. Registers the server alias on the client

## What Happens During Boot

1. **Create disk overlay(s)** — qcow2 copy-on-write backed by the golden snapshot(s). Instant.
2. **Create networks** — isolated libvirt NAT networks with DNS entries. Bridge names truncated to 15-char Linux limit.
3. **Boot VM** — define and start with the flavor's RAM and vCPU specs.
4. **Wait for SSH** — poll until reachable using the flavor's SSH key.
5. **Run recert** — stop kubelet/crio, start standalone etcd (LCA-style, no `--force-new-cluster`), regenerate all certificates, rename cluster identity, fix PV node affinities, clean OVN/OVS state, configure dnsmasq and nodeip hint, restart services.
6. **Wait for health** — poll `/healthz` until API server is ready.
7. **Configure access** — extract kubeconfig, add HAProxy SNI entries, add dnsmasq DNS entry.
8. **Wait for operators** — poll all ClusterOperators until Available and not Degraded.
9. **Verify identity** — confirm apiServerURL matches expected domain.

If any step fails, all resources are rolled back automatically (transactional boot).

## What Happens During Snapshot

1. **Detect etcd image** — read from the running cluster's pod manifest.
2. **Extract crypto keys** — 4 CA signing keys + admin kubeconfig CA.
3. **Inject SSH key** — add cluster-tool's public key to the VM's authorized_keys for cross-machine boot.
4. **Copy SSH keypair** — store in the flavor's crypto dir (travels with push/pull).
5. **Prepare etcd for clean boot** — hardlink etcd cert files to prevent revision rollouts (see [INTERNALS.md](INTERNALS.md)).
6. **Shut down VM** — graceful shutdown, wait for "shut off".
7. **Flatten disk(s)** — `qemu-img convert` produces standalone qcow2 (no backing file dependencies).
8. **Restart VM** — bring the source cluster back up.

## Architecture

```
Client (laptop)                      Server (baremetal)
┌──────────────────┐    SSH          ┌────────────────────────────────────┐
│ cluster-tool     │────────────────▶│ libvirt VMs                        │
│                  │                 │                                    │
│ ~/.config/       │                 │ ┌──────────┐  ┌──────────┐         │
│   cluster-tool/  │                 │ │ my-test  │  │ my-test-2│         │
│   servers.json   │                 │ │ .160.10  │  │ .161.10  │         │
└──────────────────┘                 │ └──────────┘  └──────────┘         │
                                     │                                    │
                                     │ ~/.config/cluster-tool/            │
                                     │   state.json (flavors, clones)     │
                                     │   config (data path)               │
                                     │   cluster-tool.key (SSH key)       │
                                     │                                    │
                                     │ <data-path>/                       │
                                     │   flavors/ (golden snapshots)      │
                                     │   overlays/ (per-clone COW disks)  │
                                     │                                    │
                                     │ HAProxy (SNI routing :6443/:443)   │
                                     └────────────────────────────────────┘
```

- The tool runs on your laptop or directly on the server (`--host local`).
- State (flavors, clones, subnets) lives on the server.
- Client stores only the server registry (`servers.json`).
- Each clone gets its own isolated libvirt network with a unique subnet.
- Multiple clones share the server's IP address. HAProxy uses SNI (the hostname in the TLS handshake) to route API and ingress traffic to the correct clone's VM.

## Networking

Clones are assigned subnets sequentially starting from `192.168.160.0/24`. Each clone uses one primary subnet plus a secondary subnet at +18. Subnets are never reused automatically, so the practical limit is ~90 clones per server before the `192.168.x` range is exhausted. Destroyed clones do not free their subnet.

If you hit the limit, destroy all clones and manually reset the counter by editing `state.json` on the server (set `next_subnet` back to `160`).

## Cross-Machine Boot

Flavors pushed to a registry can be pulled and booted on any machine. This works because:

- **Standalone disks** — `snapshot` flattens COW overlays with `qemu-img convert`. No backing file dependencies.
- **SSH key injection** — `snapshot` injects cluster-tool's SSH public key into the VM's `authorized_keys`. `boot` uses the matching private key (stored in the flavor's crypto dir) to SSH into the VM for recert.
- **Recert** — generates fresh certificates, cluster name, IP, and hostname. The booted cluster has no connection to the original.

## Reliability

- **Transactional boot** — if any step fails, all resources are rolled back in reverse order.
- **State saved only on success** — no orphaned entries from failed boots.
- **Identity verification** — after recert, confirms the clone's `apiServerURL` matches.
- **Idempotent destroy** — works by clone ID alone, handles orphaned resources.
- **Idempotent HAProxy** — strip-before-insert prevents duplicates.
- **Idempotent setup** — `connect` never overwrites existing server config (shared machines safe).
- **Operator health gate** — boot waits for ALL ClusterOperators.
- **File locking** — concurrent boot/destroy safe via `fcntl` locks.
- **Fail-fast** — no silent error suppression.

## Recert Integration

Each clone gets a fully unique identity through recert:

- **New cluster name** — `test-infra-cluster-<8-hex-chars>` (via `--cluster-rename`)
- **New certificates** — all certs regenerated with new keys
- **New IP address** — each clone on a unique subnet (via `--ip`)
- **New hostname** — node name matches clone ID (via `--hostname`, `--cn-san-replace`)
- **Preserved CA signing keys** — the 4 kube-apiserver signing keys are preserved via `--use-key`
- **Full SAN replacement** — exact-match rules for API, ingress, hostname, and system:node entries
- **PV node affinity fix** — PersistentVolumes with node affinity get their hostname replaced in etcd
- **DNS configuration** — dnsmasq overrides and nodeip hint set via the official override mechanism

## Internals

For deep technical details on how snapshot recloning works (OVN cleanup, etcd certificate hardlinks, standalone etcd, forked recert image), see [INTERNALS.md](INTERNALS.md).

## Testing

```bash
CLUSTER_TOOL_HOST=test@host python3 test_cluster_tool.py -v
```

Tests cover: state management, server registry, ExecutionEnv (local/SSH), template generation, multi-disk VM XML, transactional rollback, recert flags, identity verification, file locking, bridge name truncation, dnsmasq config, manifest build/parse, push command flow, pull command flow, snapshot with SSH key injection, snapshot etcd hardlinks, OVN cleanup during boot, standalone etcd configuration, setup client/server, connect/servers/use commands.
