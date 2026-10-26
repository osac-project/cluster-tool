# Internals

Deep technical details on how cluster-tool handles snapshot recloning, OVN cleanup, etcd certificate management, and the forked recert image.

## Snapshot Recloning

A snapshot can be booted as a clone, and that clone can be snapshotted again, creating a chain of arbitrary depth (clone-of-clone-of-clone...). Making this work requires handling two subsystems that carry stale state from the previous identity: OVN networking and etcd certificate revisions.

### OVN Networking Cleanup (boot time)

OVN-Kubernetes stores the node's tunnel endpoint IP, connection strings, and system identity in several databases on disk. When a clone boots with a new IP, these stale values prevent OVN from starting — the node stays `NotReady` with "no CNI configuration file."

During boot (after recert, before starting kubelet), the node fix script deletes:
- `/etc/openvswitch/conf.db` and its lock file — the OVS configuration database with the old IP in `ovn-encap-ip`
- `/etc/ovn/ovnsb_db.db` and `ovnnb_db.db` — OVN southbound/northbound databases
- `/var/lib/ovn-ic/etc/` — OVN interconnect state including stale certificates

Then it restarts the `openvswitch` service. On the next boot, `configure-ovs.sh` recreates `br-ex` from scratch, and `ovnkube-node` rebuilds its state with the new IP. The node reaches `Ready` in ~60 seconds.

This follows the same pattern used by the [lifecycle-agent](https://github.com/openshift/lifecycle-agent) (LCA) during Image-Based Install.

### etcd Certificate Revision Hardlinks (snapshot time)

OpenShift's etcd operator uses a revision system for safe rollouts. It keeps two copies of the TLS certificates on disk:

- `etcd-certs/secrets/etcd-all-certs/` — the live certs mounted by the running etcd pod
- `etcd-pod-{rev}/secrets/etcd-all-certs/` — a revision snapshot from the last successful rollout

The operator periodically compares the live certificates (via the Kubernetes API) against the revision snapshot. If they differ, it triggers a full revision rollout — redeploying the etcd static pod, which cascades to kube-apiserver and other components. This takes ~7 minutes.

**The problem with recloning:** recert regenerates all certificates by scanning the filesystem (`--crypto-dir`) and the etcd database (`--etcd-endpoint`) independently. It deduplicates certificates by content — if two files have identical bytes, recert treats them as one certificate and writes the same regenerated output to both. If the bytes differ, it treats them as separate certificates and generates independent replacements with different serial numbers.

On a first-generation clone (from the original installer), the live certs and revision snapshot are byte-identical. Recert deduplicates them and writes matching output. No rollout.

On a second-generation clone (clone-of-clone), the previous recert already wrote different bytes to each location (because it treated them as separate certs — the same problem, recursively). Now recert sees two different certificates and generates two different replacements. The operator detects the mismatch and triggers a 7-minute rollout.

**The fix:** during `snapshot`, before shutting down the VM, the tool replaces the cert files in `etcd-certs/secrets/etcd-all-certs/` with **hardlinks** to the corresponding files in `etcd-pod-{rev}/secrets/etcd-all-certs/`. Hardlinks are filesystem entries that point to the same physical data on disk (same inode). When recert scans both paths, it finds identical bytes (same inode = same data), deduplicates them into one certificate, and writes one regenerated output. Both paths see the same result. The operator finds no mismatch. No rollout.

Hardlinks (not symlinks) are required because the etcd pod mounts `etcd-certs/` as a container volume. A symlink pointing to `../../etcd-pod-24/` would resolve outside the mount boundary inside the container — the target wouldn't exist. Hardlinks have no path to resolve; they are direct inode references that work identically inside and outside containers.

The hardlinks survive `qemu-img convert` (block-level copy preserves filesystem metadata), qcow2 COW overlays (both directory entries reference the same inode through the overlay), and recert's write mechanism (`std::fs::write` uses `O_TRUNC` which modifies the existing inode in-place without breaking the link).

The etcd operator's cert-sync controller could theoretically break hardlinks via `renameat2(RENAME_EXCHANGE)`, but it has a content-equality check that skips the swap when disk content matches the API. Since recert ensures consistency, the check passes and the hardlinks survive.

This step also removes any stale `etcd-pod.yaml` from `etcd-certs/` — a file the etcd operator's installer pod creates during revision rollouts that would cause recert to crash on subsequent boots.

### Standalone etcd for Recert (boot time)

Recert needs a live etcd endpoint to read and write certificate data. During boot, after stopping kubelet and crio, the tool starts a standalone etcd container:

```
podman run -d --name etcd-recert \
  --network host --privileged \
  -v /var/lib/etcd:/store \
  --entrypoint etcd \
  <etcd-image> \
  --name editor --data-dir /store
```

The volume mount uses a different container path (`/store` instead of `/var/lib/etcd`) following the lifecycle-agent convention. The `--name editor` flag is ignored by etcd when existing WAL data is present — etcd loads its identity from the WAL metadata. No `--force-new-cluster` flag is used; that flag is designed for multi-member disaster recovery and is unnecessary on a single-node cluster.

### Forked Recert Image

We use `quay.io/osac-project/recert:latest` with two fixes over upstream:

1. **Binary DER data handling** — upstream crashes on secrets with binary certificate data. The fork returns `None` for non-UTF-8 data instead of erroring.
2. **PersistentVolume node affinity** — upstream doesn't update PV node affinity hostnames during `--hostname`. The fork adds PV support in etcd encoding and hostname replacement.

## Compact cloning

`compact-clone` creates a clone directly from a live three-master source. `compact-boot` starts from a pulled compact flavor. Both paths restore the clone's etcd state, run Recert once, and apply the clone identity and recovery steps. They are implemented by `cluster-tool`; they are not baked into the OpenShift flavor. For the OSAC CI path, keep the flavor as the OpenShift baseline and install OSAC services, OpenBao, and local storage after the clone is ready. See the [README](README.md#compact-three-master-workflow) for command usage.

The compact path has been exercised end to end: an earlier `edge28` run reached clone readiness and `make install-infra` succeeded. That result describes the code used for that run, not every later change. The successful `sno-pr43` run on `f4c6143` covered the single-node `boot` path; it does not validate `compact-boot` or `compact-clone` on that revision. Record the tested commit alongside future compact-run results.
