# Capture a three-node compact flavor

Use this workflow on the libvirt host that runs a healthy, three-node OpenShift
cluster installed by Assisted Installer. It creates a local cluster-tool flavor
from the three masters and their two disks each. Push the flavor with the
existing cluster-tool push command afterward.

This is the multi-node capture path; the existing cluster-tool snapshot command
is for a single-node cluster.

## What the script captures

For each master, the script maps vda to the OS disk and vdb to the spare disk,
producing this flavor layout:

- disk-0.qcow2 and disk-0-spare.qcow2
- disk-1.qcow2 and disk-1-spare.qcow2
- disk-2.qcow2 and disk-2-spare.qcow2

It also extracts the signing keys, admin client CA, ingress certificate CN,
and the SSH keypair already trusted by core. Flavor metadata records compact
topology, source subnet, RAM, vCPU count, and the etcd image.

The script does not install OSAC, OpenBao, LVMS, or other services. Capture a
bare OpenShift cluster so those components can be installed after cloning. It
stops if it finds OSAC namespaces or common OpenBao, CNV, or local-storage
namespaces; remove those add-ons before capturing.

## Before you start

Run as root on the libvirt host. The host must already be initialized for local
cluster-tool use. If invoked through sudo, the script reads config and state
from the invoking user's ~/.config/cluster-tool directory and uses the same
directory for locking. Required commands are virsh, qemu-img, oc, ssh, ssh-keygen, and openssl.

The defaults match the MNO flavor we created in this session:

- Three domains named master-0-0, master-0-1, and master-0-2 by default;
  an optional prefix can be added before the node name
- 24 GiB RAM and 12 vCPUs per master
- 80 GiB vda OS disk and 132 GiB vdb spare disk per master
- A healthy cluster: all three nodes Ready, ClusterOperators available and
  quiet, the master MachineConfigPool updated, and all three etcd pods Ready
  on the same image

The script checks these requirements before stopping any VMs. If your cluster
uses different VM or disk sizes, pass the corresponding --expected-* options.

Have the following information ready:

- The optional VM name prefix. Domains named master-0-0 through
  master-0-2 need no prefix. For names like cluster-master-0-0 through
  cluster-master-0-2, pass --vm-prefix cluster.
- The source primary subnet's third octet (for example, 123 for
  192.168.123.0/24). The script checks that the API address and all three
  node InternalIPs are inside 192.168.<octet>.0/24 before capture.
- A kubeconfig with cluster-admin access, the reachable API IP, and a DNS name
  present in the API serving certificate. The script uses the IP for the
  connection and the DNS name for TLS verification.
- Each libvirt domain must expose its guest interface MAC in domain XML, and
  SSH to each API node must be able to run `ip -j -4 address show`. The script
  matches each node's InternalIP interface MAC to its VM before shutdown.
- An SSH identity whose public key is already authorized for core on all three
  masters. The script checks SSH access to each node. The key must be usable
  non-interactively and have mode 0600. Its matching .pub file must be next
  to it unless --ssh-public-key is provided.
- Enough free space on the filesystem that contains the configured
  CLUSTER_TOOL_DATA/flavors directory. The preflight checks the flavor
  destination filesystem, sums the disks' virtual sizes as a conservative
  estimate of materialized output, then requires a 15% margin plus 1 GiB.
  Sparse qcow2 output may use less, but the check will not assume that it can.

The source VMs must be running when you start. The capture gracefully shuts
down all three VMs while copying disks, so the source cluster will be
unavailable for several minutes. The script tracks every VM sent a shutdown
request, waits for it to stop, restarts it, and confirms it returns to a
running state, including when disk capture fails. If recovery times out, it
reports that manual follow-up is required and preserves the staging directory;
a VM still shutting down may power off after the script exits. The script never
force-powers VMs off. Credential extraction has a 60-second timeout, and each
disk conversion has a one-hour timeout. The VMs are restarted after a conversion
timeout.

## Capture the flavor

Replace the example cluster name, IPs, subnet, and paths with those for your
source cluster. If the VM names have a prefix before master-0-N, pass it with
--vm-prefix; otherwise omit that option.

    cd /path/to/cluster-tool

    ./scripts/capture_compact_flavor.py \
      --flavor mno-3node-24gb \
      --source-cluster assisted-cluster \
      --source-primary-subnet 123 \
      --api-address 192.168.123.10 \
      --api-server-name api.assisted.example.com \
      --ssh-node-ip 192.168.123.10 \
      --kubeconfig /path/to/admin.kubeconfig \
      --ssh-identity /path/to/core-ssh-key

The script prompts you to type shutdown before it stops the VMs. For an
intentional non-interactive run, add --yes to acknowledge that downtime.

If your VM resources differ from the defaults, override the expected values.
For example:

    ./scripts/capture_compact_flavor.py \
      --flavor mno-3node-custom \
      --source-cluster assisted-cluster \
      --vm-prefix test-infra-cluster-assisted \
      --source-primary-subnet 123 \
      --api-address 192.168.123.10 \
      --api-server-name api.assisted.example.com \
      --kubeconfig /path/to/admin.kubeconfig \
      --ssh-identity /path/to/core-ssh-key \
      --expected-memory-gib 32 \
      --expected-vcpus 16 \
      --expected-os-disk-gib 120 \
      --expected-spare-disk-gib 200

On success, the flavor is registered in the same state.json used by
cluster-tool, and the six files are stored below
$CLUSTER_TOOL_DATA/flavors/<flavor>/. The script shares the cluster-tool state
lock and writes the updated state atomically. The crypto directory remains
root-only (mode 0700), so run compact-boot with root permissions while
preserving the home directory used to configure cluster-tool. For example, from
the account that owns that configuration:

    sudo env HOME="$HOME" /path/to/cluster-tool compact-boot \
      --flavor mno-3node-24gb --name clone-name --subnet 216 --host local

Do not loosen the crypto directory or key permissions. If you captured and
configured cluster-tool as root, use the normal root command instead.

Inspect the result before uploading:

    ./cluster-tool flavors --server local
    for disk in /var/lib/cluster-tool/flavors/mno-3node-24gb/*.qcow2; do
      qemu-img info "$disk"
    done

Check the actual flavor path if CLUSTER_TOOL_DATA is not
/var/lib/cluster-tool.

## Push to Quay

The capture keeps private credentials root-only. Run both commands with root
privileges while preserving the `HOME` used to configure cluster-tool. This
lets the push command read the same flavor state and data directory as the
capture. Run these commands from the account whose cluster-tool configuration
you used (not from a root login shell); the shell expands `$HOME` before sudo:

    sudo env HOME="$HOME" podman login quay.io

    sudo env HOME="$HOME" ./cluster-tool push mno-3node-24gb \
      --registry quay.io/your-org/cluster-flavors \
      --tag mno-3node-24gb \
      --server local

cluster-tool push does not capture disks; it only packages and uploads an
existing local flavor. If cluster-tool was configured under root's home, run
both commands directly as root instead.

The resulting reference is
quay.io/your-org/cluster-flavors:mno-3node-24gb. Restrict access to the
repository appropriately: the OCI image contains the flavor's private signing
keys and SSH private key, as required by the clone workflow.

On a destination host, pull the image with cluster-tool pull. Booting it as a
three-node clone requires a cluster-tool build that includes compact-boot
support.
