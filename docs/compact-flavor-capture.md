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
cluster-tool use, with ~/.config/cluster-tool/config pointing to its data
directory. Required commands are virsh, qemu-img, oc, ssh, ssh-keygen, and openssl.

The defaults match the MNO flavor we created in this session:

- Three domains named <vm-prefix>-master-0-0, -master-0-1, and -master-0-2
- 24 GiB RAM and 12 vCPUs per master
- 80 GiB vda OS disk and 132 GiB vdb spare disk per master
- A healthy cluster: all three nodes Ready, ClusterOperators available and
  quiet, the master MachineConfigPool updated, and all three etcd pods Ready
  on the same image

The script checks these requirements before stopping any VMs. If your cluster
uses different VM or disk sizes, pass the corresponding --expected-* options.

Have the following information ready:

- The VM name prefix. For example, the VMs
  test-infra-cluster-edge16-master-0-0 through -0-2 use
  test-infra-cluster-edge16.
- The source primary subnet's third octet (for example, 123 for
  192.168.123.0/24).
- A kubeconfig with cluster-admin access, the reachable API IP, and a DNS name
  present in the API serving certificate. The script uses the IP for the
  connection and the DNS name for TLS verification.
- An SSH identity whose public key is already authorized for core on all three
  masters. The script checks SSH access to each node. The key must be usable
  non-interactively and have mode 0600. Its matching .pub file must be next
  to it unless --ssh-public-key is provided.
- Enough free space under the configured CLUSTER_TOOL_DATA directory. The
  script estimates the source disks' allocated size and requires a 15% margin
  plus 1 GiB.

The source VMs must be running when you start. The capture gracefully shuts
down all three VMs while copying disks, so the source cluster will be
unavailable for several minutes. The script tracks every VM sent a shutdown
request, waits for it to stop, restarts it, and confirms it returns to a
running state, including when disk capture fails. If recovery times out, it
reports the unresolved VM and preserves the staging directory; a VM still
shutting down may power off after the script exits. The script never
force-powers VMs off.

## Capture the flavor

Example based on the edge16-24g cluster created in this session. Replace the
cluster name, VM prefix, IPs, subnet, and paths with those for your source
cluster.

    cd /root/osac/cluster-tool

    ./scripts/capture_compact_flavor.py \
      --flavor mno-3node-24gb \
      --source-cluster edge16-24g \
      --vm-prefix test-infra-cluster-edge16 \
      --source-primary-subnet 123 \
      --api-address 192.168.123.64 \
      --api-server-name api.edge16-24g.redhat.com \
      --ssh-node-ip 192.168.123.64 \
      --kubeconfig /root/kubeconfig \
      --ssh-identity /home/kni/.ssh/id_rsa

The script prompts you to type shutdown before it stops the VMs. For an
intentional non-interactive run, add --yes to acknowledge that downtime.

If your VM resources differ from the defaults, override the expected values.
For example:

    ./scripts/capture_compact_flavor.py \
      --flavor mno-3node-custom \
      --source-cluster assisted-cluster \
      --vm-prefix test-infra-cluster-assisted \
      --source-primary-subnet 123 \
      --api-address 192.168.123.64 \
      --api-server-name api.assisted.example.com \
      --kubeconfig /root/kubeconfig \
      --ssh-identity /home/kni/.ssh/id_rsa \
      --expected-memory-gib 32 \
      --expected-vcpus 16 \
      --expected-os-disk-gib 120 \
      --expected-spare-disk-gib 200

On success, the flavor is registered in the same state.json used by
cluster-tool, and the six files are stored below
$CLUSTER_TOOL_DATA/flavors/<flavor>/. The script shares the cluster-tool state
lock and writes the updated state atomically.

Inspect the result before uploading:

    ./cluster-tool flavors --server local
    for disk in /var/lib/cluster-tool/flavors/mno-3node-24gb/*.qcow2; do
      qemu-img info "$disk"
    done

Check the actual flavor path if CLUSTER_TOOL_DATA is not
/var/lib/cluster-tool.

## Push to Quay

Log in as the same account that will push the image, then push the registered
flavor. cluster-tool push does not capture disks; it only packages and uploads
an existing local flavor.

    podman login quay.io

    ./cluster-tool push mno-3node-24gb \
      --registry quay.io/your-org/cluster-flavors \
      --tag mno-3node-24gb \
      --server local

The resulting reference is
quay.io/your-org/cluster-flavors:mno-3node-24gb. Restrict access to the
repository appropriately: the OCI image contains the flavor's private signing
keys and SSH private key, as required by the clone workflow.

On a destination host, pull the image with cluster-tool pull. Booting it as a
three-node clone requires a cluster-tool build that includes compact-boot
support.
