---
name: spawn-cluster
description: Manage OpenShift SNO clusters — boot from snapshots (~5 min), snapshot running clusters, push/pull flavors to/from OCI registries, manage multiple baremetal servers. Use when you need a live cluster, want to distribute a cluster image, or manage the cluster lifecycle.
allowed-tools: Bash(cluster-tool *)
---

# Cluster Tool

Boot fresh OpenShift SNO clusters from golden snapshots in ~5 minutes. Each cluster gets a unique identity (certs, IP, hostname) and is fully independent. Distribute cluster images via OCI registries (Quay.io). Manage multiple baremetal servers.

## Where It Runs

cluster-tool runs on the **user's laptop** and connects to **baremetal servers** via SSH. The laptop is the control plane; VMs run on the server.

- **Remote mode** (typical): commands SSH into the server automatically.
- **Local mode** (CI / on the server itself): use `--host local` when connecting.

## First-Time Setup

Before any cluster operations, the user needs two things: client DNS setup and a server connection. Check if these are already done before running them.

### 1. Client DNS setup (once per laptop)

```bash
sudo cluster-tool setup client
```

Installs dnsmasq and configures NetworkManager so cluster domains resolve automatically. Requires sudo. Only needed once.

**Check if already done:** look for `/etc/NetworkManager/dnsmasq.d/` directory and `/etc/NetworkManager/conf.d/cluster-tool-dns.conf`.

### 2. Connect to a server (once per server)

```bash
# Remote: from the user's laptop
cluster-tool connect <alias> --host root@<hostname> --data-path <path>

# Local: running directly on the baremetal machine
cluster-tool connect <alias> --host local --data-path <path>
```

Installs all dependencies (libvirt, qemu-kvm, podman, pigz, haproxy), configures storage, and registers the server alias locally.

`--data-path` is where disk images and overlays are stored on the server. Must be on a partition with enough space (~100 GB per flavor). If omitted, the tool prompts interactively with the largest partition as the default. **Always pass `--data-path` explicitly** to avoid interactive prompts — use the largest partition on the server (e.g., `/home/cluster-tool`).

**Check if already done:** run `cluster-tool servers` — if the server is listed, it's already connected.

### 3. Set default server (optional)

```bash
cluster-tool use <alias>
```

The first server connected is automatically set as default. Only needed if you have multiple servers.

## Commands

### Boot a cluster

```bash
cluster-tool boot --flavor <flavor> --name <name> [--server <server>]
```

- `--flavor` — which snapshot to boot from. Run `cluster-tool flavors` to see available ones.
- `--name` — a short identifier (e.g., `pr-1234`, `e2e`). If omitted, a random 8-char hex ID is generated.
- `--server` — which server to boot on. Omit to use the default server.

### Get a flavor

If no flavors are available locally, pull one from Quay:

```bash
cluster-tool pull <image> [--server <server>]
```

### Snapshot a running cluster

```bash
cluster-tool snapshot --name <flavor-name> --source <clone-id> [--server <server>]
```

Creates a reusable flavor from a running clone. Flattens disks, extracts crypto keys, injects SSH key, hardlinks etcd certs for clean recloning.

### Push a flavor to a registry

```bash
cluster-tool push <flavor> --registry <repo> --tag <tag> [--server <server>]
```

Splits disks into 1GB chunks, compresses with pigz, builds an OCI image, pushes to registry.

### Pull a flavor from a registry

```bash
cluster-tool pull <image> [--server <server>]
```

Downloads, decompresses, reassembles disks, registers the flavor locally.

### Server management

```bash
cluster-tool connect <name> --host <user@host> --data-path <path>
cluster-tool servers
cluster-tool use <name>
```

### List / destroy / verify

```bash
cluster-tool flavors [--delete <name>]
cluster-tool list [--server <server>]
cluster-tool destroy <name> [--server <server>]
cluster-tool destroy --all [--server <server>]
cluster-tool verify <name> [--server <server>]
```

## After Boot

The tool prints the kubeconfig path. Use it:

```bash
export KUBECONFIG=~/.kube/<name>.kubeconfig
oc get nodes
oc get co
```

## Parallel Usage

Multiple boot/destroy commands can run in parallel safely. The tool uses file locking internally.

## What to tell the user

After booting, tell them:
1. The cluster name and flavor
2. The kubeconfig path (`~/.kube/<name>.kubeconfig`)
3. The `export KUBECONFIG=...` command to use it
4. That it takes ~5-9 minutes to be fully ready

After snapshot, tell them the flavor name and that they can push it with `cluster-tool push`.

After push, tell them the image reference (e.g., `quay.io/org/repo:tag`).

After pull, tell them the flavor is registered and ready for `boot`.

## Interpreting Arguments

If the user says:
- "spawn a cluster" / "I need a cluster" — run `boot` with a reasonable name
- "spawn 3 clusters" — run 3 `boot` commands in parallel with `&` and `wait`
- "tear down my clusters" / "clean up" — run `destroy --all`
- "what clusters are running" — run `list`
- "what flavors do we have" — run `flavors`
- "snapshot this cluster" / "save this as a flavor" — run `snapshot`
- "push this to quay" / "distribute this" — run `push`
- "pull the CNV flavor" / "get the cluster image" — run `pull`
- "connect to this machine" / "add a server" — run `connect`
- "which servers are connected" — run `servers`
