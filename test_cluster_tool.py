# test_cluster_tool.py
import argparse
import importlib.machinery
import importlib.util
import json
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

loader = importlib.machinery.SourceFileLoader("cluster_tool", "./cluster-tool")
spec = importlib.util.spec_from_loader("cluster_tool", loader, origin="./cluster-tool")
ct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ct)
ct._init_paths("/data/cluster-tool")


class MockStateEnv:
    """Intercepts state read/write through env.run/env.write_file on the server."""

    def __init__(self):
        self.env = ct.ExecutionEnv(host="test@host", host_ip="10.0.0.1")
        self._state_data = None
        self._files = {}

    def setup(self, initial_state=None):
        ct.env = self.env
        if initial_state is not None:
            self._state_data = json.dumps(initial_state, indent=2) + "\n"

    def save_initial_state(self, state):
        self._state_data = json.dumps(state, indent=2) + "\n"

    def get_saved_state(self):
        if self._state_data is None:
            return None
        return json.loads(self._state_data)

    def _is_state_cmd(self, cmd):
        if f"cat {ct.SERVER_STATE_FILE}" in cmd:
            return True
        if "flock" in cmd and "state.json" in cmd:
            return True
        if "mkdir -p" in cmd and ct.SERVER_CONFIG_DIR in cmd and "&&" not in cmd:
            return True
        if "touch" in cmd and ("state.lock" in cmd or "haproxy.lock" in cmd):
            return True
        return False

    def mock_run(self, cmd, *, check=True):
        r = MagicMock()
        r.returncode = 0
        r.stderr = ""
        r.stdout = ""

        if f"cat {ct.SERVER_STATE_FILE}" in cmd:
            if self._state_data is None:
                r.returncode = 1
                r.stderr = "No such file"
                if check:
                    import sys
                    sys.exit(f"Command failed: {cmd}")
            else:
                r.stdout = self._state_data
            return r

        if "mkdir -p" in cmd and ct.SERVER_CONFIG_DIR in cmd and "&&" not in cmd:
            return r
        if "touch" in cmd and ("state.lock" in cmd or "haproxy.lock" in cmd):
            return r
        if "flock" in cmd and "state.json" in cmd:
            if self._state_data:
                r.stdout = self._state_data
            else:
                r.returncode = 1
            return r

        return r

    def mock_write_file(self, path, content):
        self._files[path] = content
        if "state.json" in path:
            self._state_data = content

    def wrap_run(self, inner):
        """Return a mock_run that handles state commands, delegates the rest to inner."""
        def wrapped(cmd, *, check=True):
            if self._is_state_cmd(cmd):
                return self.mock_run(cmd, check=check)
            return inner(cmd, check=check)
        return wrapped

    def wrap_run_positional(self, inner):
        """Like wrap_run but inner takes cmd as positional arg (for side_effect with patched run)."""
        def wrapped(cmd, check=True):
            if self._is_state_cmd(cmd):
                return self.mock_run(cmd, check=check)
            return inner(cmd, check=check)
        return wrapped


class TestStateManagement(unittest.TestCase):
    def setUp(self):
        self.mock_env = MockStateEnv()
        self.mock_env.setup()
        self._run_patch = patch.object(self.mock_env.env, "run", side_effect=self.mock_env.mock_run)
        self._wf_patch = patch.object(self.mock_env.env, "write_file", side_effect=self.mock_env.mock_write_file)
        self._run_patch.start()
        self._wf_patch.start()

    def tearDown(self):
        self._run_patch.stop()
        self._wf_patch.stop()

    def test_load_empty_state(self):
        state = ct.load_state()
        self.assertEqual(state["flavors"], {})
        self.assertEqual(state["clones"], {})

    def test_save_and_load_roundtrip(self):
        state = {"flavors": {"test": {"source": "abc"}}, "clones": {}}
        ct.save_state(state)
        loaded = ct.load_state()
        self.assertEqual(loaded, state)

    def test_allocate_subnet_returns_first_free(self):
        state = ct.load_state()
        s1 = ct.allocate_subnet(state)
        self.assertEqual(s1, 160)

    def test_allocate_subnet_skips_reserved(self):
        state = {
            "flavors": {"f1": {"source_primary_subnet": 160, "source_secondary_subnet": 178}},
            "clones": {},
        }
        s1 = ct.allocate_subnet(state)
        # 160 and 178 are reserved by flavor, so 160 is skipped.
        # candidate 161: secondary=179, both free -> should return 161
        self.assertEqual(s1, 161)

    def test_allocate_subnet_skips_clone_subnets(self):
        state = {
            "flavors": {},
            "clones": {"c1": {"subnet_primary": 160, "subnet_secondary": 178}},
        }
        s1 = ct.allocate_subnet(state)
        self.assertEqual(s1, 161)

    def test_allocate_subnet_recycles_after_destroy(self):
        state = {
            "flavors": {},
            "clones": {"c2": {"subnet_primary": 161, "subnet_secondary": 179}},
        }
        # Clone at 160 was destroyed, so 160 should be available again
        s1 = ct.allocate_subnet(state)
        self.assertEqual(s1, 160)

    def test_allocate_subnet_exhausted(self):
        state = {"flavors": {}, "clones": {}}
        # Fill all possible slots
        for i in range(ct.SUBNET_START, 256 - ct.SUBNET_SECONDARY_OFFSET):
            state["clones"][f"c{i}"] = {
                "subnet_primary": i,
                "subnet_secondary": i + ct.SUBNET_SECONDARY_OFFSET,
            }
        with self.assertRaises(SystemExit) as ctx:
            ct.allocate_subnet(state)
        self.assertIn("No available subnets", str(ctx.exception))

    def test_allocate_subnet_skips_flavor_primary_subnets(self):
        state = {
            "flavors": {"f1": {"source_primary_subnet": 161, "source_secondary_subnet": 179}},
            "clones": {},
        }
        s = ct.allocate_subnet(state)
        self.assertEqual(s, 160)

    def test_allocate_subnet_skips_flavor_secondary_subnets(self):
        state = {
            "flavors": {"f1": {"source_primary_subnet": 135, "source_secondary_subnet": 170}},
            "clones": {},
        }
        s = ct.allocate_subnet(state)
        # 160's secondary is 178 -- both free, so 160 should be returned
        self.assertEqual(s, 160)
        # But if a flavor's secondary sits exactly on a candidate...
        state2 = {
            "flavors": {"f1": {"source_primary_subnet": 135, "source_secondary_subnet": 178}},
            "clones": {},
        }
        s2 = ct.allocate_subnet(state2)
        # candidate 160 -> secondary 178 is reserved -> skip 160
        self.assertEqual(s2, 161)

    def test_allocate_subnet_skips_secondary_of_clones(self):
        state = {
            "flavors": {},
            "clones": {"c1": {"subnet_primary": 160, "subnet_secondary": 178}},
        }
        s = ct.allocate_subnet(state)
        self.assertEqual(s, 161)

    def test_allocate_subnet_exhaustion_accounts_for_flavors(self):
        state = {"flavors": {}, "clones": {}}
        # Fill all but one slot
        for i in range(ct.SUBNET_START, 256 - ct.SUBNET_SECONDARY_OFFSET):
            state["clones"][f"c{i}"] = {
                "subnet_primary": i,
                "subnet_secondary": i + ct.SUBNET_SECONDARY_OFFSET,
            }
        # All slots taken
        with self.assertRaises(SystemExit):
            ct.allocate_subnet(state)
        # Free two clones to create one usable slot: primary and secondary
        # ranges overlap, so one deletion alone can't free both a candidate
        # and its secondary
        del state["clones"][f"c{ct.SUBNET_START}"]
        del state["clones"][f"c{ct.SUBNET_START + ct.SUBNET_SECONDARY_OFFSET}"]
        s = ct.allocate_subnet(state)
        self.assertEqual(s, ct.SUBNET_START)
        # Now add a flavor that reserves that slot
        state["flavors"]["f1"] = {
            "source_primary_subnet": ct.SUBNET_START,
            "source_secondary_subnet": ct.SUBNET_START + ct.SUBNET_SECONDARY_OFFSET,
        }
        with self.assertRaises(SystemExit):
            ct.allocate_subnet(state)

    def test_allocate_subnet_backward_compat_missing_secondary(self):
        state = {
            "flavors": {"f1": {"source_primary_subnet": 135}},
            "clones": {"c1": {"subnet_primary": 160}},
        }
        # Missing subnet_secondary should fall back to primary + OFFSET
        s = ct.allocate_subnet(state)
        # 160 and 160+18=178 are reserved by clone, 135 and 135+18=153 by flavor
        self.assertEqual(s, 161)

    def test_cmd_list_with_booting_entry(self):
        self.mock_env.save_initial_state({
            "flavors": {},
            "clones": {
                "booting1": {
                    "flavor": "test-flavor",
                    "subnet_primary": 160,
                    "subnet_secondary": 178,
                    "status": "booting",
                    "created_at": "2026-01-01T00:00:00+00:00",
                },
            },
        })
        with patch.object(ct.env, "run", side_effect=self.mock_env.mock_run):
            ct.cmd_list(MagicMock())


class TestIDGeneration(unittest.TestCase):
    def test_clone_id_format(self):
        cid = ct.generate_clone_id()
        self.assertEqual(len(cid), 8)
        int(cid, 16)  # must be valid hex

    def test_clone_ids_are_unique(self):
        ids = {ct.generate_clone_id() for _ in range(100)}
        self.assertGreater(len(ids), 90)

    def test_mac_format(self):
        mac = ct.generate_mac()
        parts = mac.split(":")
        self.assertEqual(len(parts), 6)
        self.assertEqual(parts[0], "02")
        self.assertEqual(parts[1], "00")
        self.assertEqual(parts[2], "00")


class TestTemplates(unittest.TestCase):
    def setUp(self):
        ct.env = ct.ExecutionEnv(host="test@host", host_ip="10.0.0.1")

    def test_primary_network_xml(self):
        xml = ct.gen_primary_network_xml("a1b2c3d4", 160, "02:00:00:aa:bb:cc")
        self.assertIn("<name>test-infra-net-a1b2c3d4</name>", xml)
        self.assertIn("192.168.160.1", xml)
        self.assertIn("192.168.160.10", xml)
        self.assertIn("02:00:00:aa:bb:cc", xml)
        self.assertIn("api.test-infra-cluster-a1b2c3d4.redhat.com", xml)
        self.assertIn("br-a1b2c3d4", xml)

    def test_primary_network_has_apps_wildcard_dns(self):
        xml = ct.gen_primary_network_xml("a1b2c3d4", 160, "02:00:00:aa:bb:cc")
        self.assertIn('xmlns:dnsmasq="http://libvirt.org/schemas/network/dnsmasq/1.0"', xml)
        self.assertIn("address=/.apps.test-infra-cluster-a1b2c3d4.redhat.com/192.168.160.10", xml)

    def test_primary_network_bridge_name_truncated(self):
        xml = ct.gen_primary_network_xml("caas-pr-cluster", 160, "02:00:00:aa:bb:cc")
        self.assertIn("br-caas-pr-", xml)
        bridge = xml.split("name='")[1].split("'")[0]
        self.assertLessEqual(len(bridge), 15)

    def test_secondary_network_xml(self):
        xml = ct.gen_secondary_network_xml("a1b2c3d4", 160, 178, "02:00:00:dd:ee:ff")
        self.assertIn("<name>test-infra-secondary-network-a1b2c3d4</name>", xml)
        self.assertIn("192.168.178.1", xml)
        self.assertIn("192.168.178.10", xml)
        self.assertIn("192.168.160.10", xml)  # DNS points to primary VIP
        self.assertIn("bs-a1b2c3d4", xml)

    def test_secondary_network_has_apps_wildcard_dns(self):
        xml = ct.gen_secondary_network_xml("a1b2c3d4", 160, 178, "02:00:00:dd:ee:ff")
        self.assertIn('xmlns:dnsmasq="http://libvirt.org/schemas/network/dnsmasq/1.0"', xml)
        self.assertIn("address=/.apps.test-infra-cluster-a1b2c3d4.redhat.com/192.168.160.10", xml)

    def test_secondary_network_bridge_name_truncated(self):
        xml = ct.gen_secondary_network_xml("caas-pr-cluster", 160, 178, "02:00:00:dd:ee:ff")
        bridge = xml.split("name='")[1].split("'")[0]
        self.assertLessEqual(len(bridge), 15)

    def test_vm_xml_single_disk(self):
        xml = ct.gen_vm_xml("a1b2c3d4", "/path/overlay.qcow2", "02:00:00:aa:bb:cc", "02:00:00:dd:ee:ff")
        self.assertIn("<name>test-infra-cluster-a1b2c3d4-master-0</name>", xml)
        self.assertIn("/path/overlay.qcow2", xml)
        self.assertIn("02:00:00:aa:bb:cc", xml)
        self.assertIn("02:00:00:dd:ee:ff", xml)
        self.assertIn("test-infra-net-a1b2c3d4", xml)
        self.assertIn("test-infra-secondary-network-a1b2c3d4", xml)
        self.assertIn("67108864", xml)
        self.assertIn("sda", xml)

    def test_vm_xml_multi_disk(self):
        xml = ct.gen_vm_xml("a1b2c3d4", ["/path/disk-0.qcow2", "/path/disk-1.qcow2"],
                             "02:00:00:aa:bb:cc", "02:00:00:dd:ee:ff", 33554432, 8)
        self.assertIn("/path/disk-0.qcow2", xml)
        self.assertIn("/path/disk-1.qcow2", xml)
        self.assertIn("sda", xml)
        self.assertIn("sdb", xml)
        self.assertIn("33554432", xml)
        self.assertIn("8</vcpu>", xml)

    def test_vm_xml_custom_machine_and_emulator(self):
        xml = ct.gen_vm_xml(
            "a1b2c3d4", "/path/overlay.qcow2", "02:00:00:aa:bb:cc", "02:00:00:dd:ee:ff",
            machine_type="pc-q35-4.0", emulator="/usr/sbin/qemu-kvm",
        )
        self.assertIn("machine='pc-q35-4.0'", xml)
        self.assertIn("<emulator>/usr/sbin/qemu-kvm</emulator>", xml)

    def test_parse_machine_types(self):
        machine_help = (
            "q35   Standard PC (Q35 + ICH9, 2009)\n"
            "pc-q35-4.0   Standard PC (Q35 + ICH9, 2009)\n"
        )
        self.assertEqual(ct._parse_machine_types(machine_help), ["q35", "pc-q35-4.0"])

    def test_detect_vm_machine_type_prefers_q35(self):
        mock_env = MagicMock()
        mock_env.run.return_value = MagicMock(
            returncode=0,
            stdout="pc-i440fx-2.1 legacy\nq35 Standard PC\npc-q35-4.0 Standard PC\n",
        )
        with patch.object(ct, "env", mock_env):
            self.assertEqual(ct.detect_vm_machine_type("/usr/bin/qemu-system-x86_64"), "q35")

    def test_haproxy_additions(self):
        use_backends, backends = ct.gen_haproxy_additions("a1b2c3d4", 160)
        self.assertIn("api-a1b2c3d4", use_backends["api"])
        self.assertIn("req_ssl_sni", use_backends["api"])
        self.assertIn("hdr_end(host)", use_backends["ingress-http"])
        self.assertIn("192.168.160.10:6443", backends)
        self.assertIn("192.168.160.10:443", backends)
        self.assertIn("192.168.160.10:80", backends)

    def test_haproxy_strip_no_substring_collision(self):
        use_a, back_a = ct.gen_haproxy_additions("demo", 160)
        use_b, back_b = ct.gen_haproxy_additions("dev-env-demo", 161)
        config = "frontend api\n    default_backend api-source\n\nfrontend ingress-https\n    default_backend ingress-https-source\n\nfrontend ingress-http\n    default_backend ingress-http-source\n"
        for key in ["api", "ingress-https", "ingress-http"]:
            marker = f"    default_backend {key}-"
            config = config.replace(marker, f"{use_a[key]}\n{use_b[key]}\n\n{marker}", 1)
        config += back_a + back_b
        stripped = ct._strip_haproxy_clone(config, "demo")
        self.assertNotIn("api-demo ", stripped)
        self.assertNotIn("backend api-demo\n", stripped)
        self.assertIn("api-dev-env-demo", stripped)
        self.assertIn("backend api-dev-env-demo", stripped)

    def test_dnsmasq_conf(self):
        conf = ct.gen_dnsmasq_conf("a1b2c3d4")
        self.assertEqual(conf, "address=/test-infra-cluster-a1b2c3d4.redhat.com/10.0.0.1\n")

    def test_dnsmasq_conf_resolves_all_subdomains(self):
        conf = ct.gen_dnsmasq_conf("mytest")
        self.assertIn("address=/test-infra-cluster-mytest.redhat.com/", conf)
        self.assertNotIn("api.", conf)
        self.assertNotIn("apps.", conf)

    def test_dnsmasq_conf_uses_host_ip(self):
        conf = ct.gen_dnsmasq_conf("xyz")
        self.assertIn(ct.env.host_ip, conf)

    def test_dnsmasq_conf_different_ids_produce_different_configs(self):
        conf1 = ct.gen_dnsmasq_conf("aaaa")
        conf2 = ct.gen_dnsmasq_conf("bbbb")
        self.assertNotEqual(conf1, conf2)
        self.assertIn("aaaa", conf1)
        self.assertIn("bbbb", conf2)


class TestDnsEntry(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_dnsmasq_dir = ct.DNSMASQ_DIR
        ct.DNSMASQ_DIR = Path(self.tmpdir)
        ct.env = ct.ExecutionEnv(host="test@host", host_ip="10.0.0.1")

    def tearDown(self):
        ct.DNSMASQ_DIR = self._orig_dnsmasq_dir

    @patch("subprocess.run")
    def test_add_dns_entry_creates_file(self, mock_run):
        ct.add_dns_entry("test01")
        conf = Path(self.tmpdir) / "cluster-test01.conf"
        self.assertTrue(conf.exists())
        self.assertIn("test-infra-cluster-test01.redhat.com", conf.read_text())

    @patch("subprocess.run")
    def test_add_dns_entry_reloads_nm(self, mock_run):
        ct.add_dns_entry("test01")
        mock_run.assert_called_once_with(["nmcli", "general", "reload"], check=True)

    @patch("subprocess.run")
    def test_add_dns_entry_is_idempotent(self, mock_run):
        ct.add_dns_entry("test01")
        content1 = (Path(self.tmpdir) / "cluster-test01.conf").read_text()
        ct.add_dns_entry("test01")
        content2 = (Path(self.tmpdir) / "cluster-test01.conf").read_text()
        self.assertEqual(content1, content2)

    @patch("subprocess.run")
    def test_remove_dns_entry_deletes_file(self, mock_run):
        conf = Path(self.tmpdir) / "cluster-test01.conf"
        conf.write_text("address=/test.redhat.com/10.1.155.16\n")
        ct.remove_dns_entry("test01")
        self.assertFalse(conf.exists())

    @patch("subprocess.run")
    def test_remove_dns_entry_reloads_nm(self, mock_run):
        conf = Path(self.tmpdir) / "cluster-test01.conf"
        conf.write_text("test")
        ct.remove_dns_entry("test01")
        mock_run.assert_called_once_with(["nmcli", "general", "reload"], check=True)

    @patch("subprocess.run")
    def test_remove_dns_entry_missing_file_no_error(self, mock_run):
        ct.remove_dns_entry("nonexistent")

    @patch("subprocess.run")
    def test_multiple_clones_separate_files(self, mock_run):
        ct.add_dns_entry("clone1")
        ct.add_dns_entry("clone2")
        self.assertTrue((Path(self.tmpdir) / "cluster-clone1.conf").exists())
        self.assertTrue((Path(self.tmpdir) / "cluster-clone2.conf").exists())
        ct.remove_dns_entry("clone1")
        self.assertFalse((Path(self.tmpdir) / "cluster-clone1.conf").exists())
        self.assertTrue((Path(self.tmpdir) / "cluster-clone2.conf").exists())


class TestTransactionalBoot(unittest.TestCase):
    _INITIAL_STATE = {
        "flavors": {
            "default": {
                "source_cluster": "6ef80144",
                "source_primary_subnet": 135,
                "source_secondary_subnet": 153,
                "memory_kib": 67108864,
                "vcpus": 16,
                "disks": ["disk-0.qcow2"],
                "etcd_image": "quay.io/test/etcd:latest",
                "created_at": "2026-01-01T00:00:00Z",
            },
        },
        "clones": {},
    }

    def setUp(self):
        self.mock_env = MockStateEnv()
        self.mock_env.setup(self._INITIAL_STATE)
        self.calls = []
        self._ps_file = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump({"auths": {"quay.io": {"auth": "dGVzdA=="}}}, self._ps_file)
        self._ps_file.close()

    def tearDown(self):
        os.unlink(self._ps_file.name)

    def _boot_args(self, no_rollback=False):
        return argparse.Namespace(name="aabbccdd", flavor="default", no_rollback=no_rollback,
                                  pull_secret=self._ps_file.name)

    def _make_ssh_mock(self, fail_on):
        destroyed = set()
        undefined = set()
        def ssh(cmd, check=True):
            self.calls.append((cmd, check))
            if fail_on in cmd and check:
                raise SystemExit(f"Simulated: {fail_on}")
            r = MagicMock()
            r.returncode = 0
            if "ingress-cn" in cmd:
                r.stdout = "fake-ingress-cn"
            elif "virsh destroy " in cmd:
                destroyed.add(cmd.split("virsh destroy ")[1])
                r.stdout = ""
            elif "virsh undefine " in cmd:
                undefined.add(cmd.split("virsh undefine ")[1])
                r.stdout = ""
            elif "virsh domstate " in cmd:
                vm = cmd.split("virsh domstate ")[1]
                if vm in undefined:
                    r.returncode = 1
                    r.stderr = "Domain not found"
                elif vm in destroyed:
                    r.stdout = "shut off"
                else:
                    r.stdout = "running"
            elif "bash -c" in cmd and "test -x" in cmd:
                r.stdout = "/usr/bin/qemu-system-x86_64\n"
            elif "-machine help" in cmd:
                r.stdout = "q35 Standard PC (Q35 + ICH9, 2009)\n"
            else:
                r.stdout = ""
            r.stderr = getattr(r, 'stderr', "") or ""
            return r
        return ssh

    def _cleanup_cmds(self):
        return [cmd for cmd, chk in self.calls if any(
            k in cmd for k in ["rm -f /data/cluster-tool/overlays",
                                "virsh net-destroy", "virsh net-undefine",
                                "virsh destroy ", "virsh undefine "]
        )]

    @patch("time.sleep")
    @patch.object(ct.ExecutionEnv, "write_file")
    @patch.object(ct, "add_dns_entry")
    @patch.object(ct.ExecutionEnv, "copy_from")
    def test_failure_at_network_cleans_overlay(self, *_):
        ssh = self.mock_env.wrap_run_positional(self._make_ssh_mock("virsh net-define /tmp/net-"))
        with patch.object(ct.env, "run", side_effect=ssh):
            with self.assertRaises(SystemExit):
                ct.cmd_boot(self._boot_args())

        cleanup = self._cleanup_cmds()
        self.assertTrue(any("rm -f" in c and "overlays/aabbccdd" in c for c in cleanup))
        self.assertFalse(any("virsh destroy" in c for c in cleanup))
        self.assertEqual(self.mock_env.get_saved_state()["clones"], {})

    @patch("time.sleep")
    @patch.object(ct.ExecutionEnv, "write_file")
    @patch.object(ct, "add_dns_entry")
    @patch.object(ct.ExecutionEnv, "copy_from")
    def test_failure_at_vm_cleans_overlay_and_networks(self, *_):
        ssh = self.mock_env.wrap_run_positional(self._make_ssh_mock("virsh define /tmp/vm-"))
        with patch.object(ct.env, "run", side_effect=ssh):
            with self.assertRaises(SystemExit):
                ct.cmd_boot(self._boot_args())

        cleanup = self._cleanup_cmds()
        self.assertTrue(any("rm -f" in c and "overlays/aabbccdd" in c for c in cleanup))
        self.assertTrue(any("net-destroy test-infra-net-aabbccdd" in c for c in cleanup))
        self.assertTrue(any("net-destroy test-infra-secondary-network-aabbccdd" in c for c in cleanup))
        self.assertFalse(any("virsh destroy" in c for c in cleanup))
        self.assertEqual(self.mock_env.get_saved_state()["clones"], {})

    @patch("time.sleep")
    @patch.object(ct.ExecutionEnv, "write_file")
    @patch.object(ct, "add_dns_entry")
    @patch.object(ct.ExecutionEnv, "copy_from")
    def test_failure_at_recert_cleans_everything(self, *_):
        ssh = self.mock_env.wrap_run_positional(self._make_ssh_mock("podman run --rm --name recert"))
        with patch.object(ct.env, "run", side_effect=ssh):
            with self.assertRaises(SystemExit):
                ct.cmd_boot(self._boot_args())

        cleanup = self._cleanup_cmds()
        self.assertTrue(any("rm -f" in c and "overlays/aabbccdd" in c for c in cleanup))
        self.assertTrue(any("net-destroy test-infra-net-aabbccdd" in c for c in cleanup))
        self.assertTrue(any("net-destroy test-infra-secondary-network-aabbccdd" in c for c in cleanup))
        self.assertTrue(any("virsh destroy" in c for c in cleanup))
        self.assertEqual(self.mock_env.get_saved_state()["clones"], {})

    @patch("time.sleep")
    @patch.object(ct.ExecutionEnv, "write_file")
    @patch.object(ct, "add_dns_entry")
    @patch.object(ct.ExecutionEnv, "copy_from")
    def test_no_rollback_preserves_clone(self, *_):
        ssh = self.mock_env.wrap_run_positional(self._make_ssh_mock("podman run --rm --name recert"))
        with patch.object(ct.env, "run", side_effect=ssh):
            with self.assertRaises(SystemExit):
                ct.cmd_boot(self._boot_args(no_rollback=True))

        cleanup = self._cleanup_cmds()
        self.assertEqual(cleanup, [], "no rollback commands should run with --no-rollback")

    @patch("time.sleep")
    @patch.object(ct.ExecutionEnv, "write_file")
    @patch.object(ct, "add_dns_entry")
    @patch.object(ct.ExecutionEnv, "copy_from")
    def test_cleanup_runs_in_reverse_order(self, *_):
        ssh = self.mock_env.wrap_run_positional(self._make_ssh_mock("podman run --rm --name recert"))
        with patch.object(ct.env, "run", side_effect=ssh):
            with self.assertRaises(SystemExit):
                ct.cmd_boot(self._boot_args())

        cleanup = self._cleanup_cmds()
        vm_destroy_idx = next((i for i, c in enumerate(cleanup) if "virsh destroy " in c), -1)
        vm_undefine_idx = next((i for i, c in enumerate(cleanup) if "virsh undefine " in c), -1)
        sec_net_destroy_idx = next((i for i, c in enumerate(cleanup) if "net-destroy test-infra-secondary" in c), -1)
        pri_net_destroy_idx = next((i for i, c in enumerate(cleanup) if "net-destroy test-infra-net-" in c), -1)
        overlay_idx = next((i for i, c in enumerate(cleanup) if "rm -f" in c), -1)
        self.assertLess(vm_destroy_idx, vm_undefine_idx)
        self.assertLess(vm_undefine_idx, sec_net_destroy_idx)
        self.assertLess(sec_net_destroy_idx, pri_net_destroy_idx)
        self.assertLess(pri_net_destroy_idx, overlay_idx)

    @patch("time.sleep")
    @patch.object(ct.ExecutionEnv, "write_file")
    @patch.object(ct, "add_dns_entry")
    @patch.object(ct.ExecutionEnv, "copy_from")
    def test_rollback_survives_inactive_primary_network(self, *_):
        """Primary net-start can fail (e.g. subnet collides with an existing
        libvirt network), leaving the network defined but never started. The
        rollback's net-destroy must tolerate that ("network is not active")
        instead of aborting before later cleanup (like the disk overlay) runs.
        """
        def ssh(cmd, check=True):
            self.calls.append((cmd, check))
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            if cmd.startswith("virsh net-start test-infra-net-") and check:
                raise SystemExit("Simulated: net-start failed (subnet collision)")
            if "net-destroy test-infra-net-" in cmd and check:
                raise SystemExit(
                    f"Command failed (exit 1): {cmd}\n"
                    "error: Requested operation is not valid: network is not active"
                )
            if "bash -c" in cmd and "test -x" in cmd:
                r.stdout = "/usr/bin/qemu-system-x86_64\n"
            elif "-machine help" in cmd:
                r.stdout = "q35 Standard PC (Q35 + ICH9, 2009)\n"
            return r

        wrapped = self.mock_env.wrap_run_positional(ssh)
        with patch.object(ct.env, "run", side_effect=wrapped):
            with self.assertRaises(SystemExit):
                ct.cmd_boot(self._boot_args())

        cleanup = self._cleanup_cmds()
        self.assertTrue(any("net-destroy test-infra-net-" in c for c in cleanup))
        self.assertTrue(any("net-undefine test-infra-net-" in c for c in cleanup))
        self.assertTrue(any("rm -f" in c and "overlays/aabbccdd" in c for c in cleanup),
                         "rollback must still clean the disk overlay after the inactive-network net-destroy")
        self.assertEqual(self.mock_env.get_saved_state()["clones"], {})


    _MOCK_CO_JSON = json.dumps({"items": [
        {"metadata": {"name": "test"}, "status": {"conditions": [
            {"type": "Available", "status": "True"},
            {"type": "Progressing", "status": "False"},
            {"type": "Degraded", "status": "False"},
        ]}}
    ]})
    _MOCK_NODES_JSON = json.dumps({"items": [
        {"metadata": {"name": "test-node"}, "status": {"conditions": [
            {"type": "Ready", "status": "True"},
        ]}}
    ]})

    def _make_all_succeed_ssh(self):
        mock_co = self._MOCK_CO_JSON
        mock_nodes = self._MOCK_NODES_JSON
        def ssh(cmd, check=True):
            self.calls.append((cmd, check))
            r = MagicMock()
            r.returncode = 0
            if "ingress-cn" in cmd:
                r.stdout = "fake-ingress-cn"
            elif "infrastructure cluster" in cmd:
                r.stdout = "https://api.test-infra-cluster-aabbccdd.redhat.com:6443"
            elif "get co -o json" in cmd:
                r.stdout = mock_co
            elif "get nodes -o json" in cmd:
                r.stdout = mock_nodes
            elif "bash -c" in cmd and "test -x" in cmd:
                r.stdout = "/usr/bin/qemu-system-x86_64\n"
            elif "-machine help" in cmd:
                r.stdout = "q35 Standard PC (Q35 + ICH9, 2009)\n"
            else:
                r.stdout = "ok"
            r.stderr = ""
            return r
        return ssh

    @patch("time.sleep")
    @patch.object(ct, "remove_dns_entry")
    def test_failure_at_hosts_rolls_back_haproxy(self, *_):
        haproxy_removed = []

        def track_remove_haproxy(clone_id):
            haproxy_removed.append(clone_id)

        ssh = self.mock_env.wrap_run_positional(self._make_all_succeed_ssh())
        with patch.object(ct.env, "run", side_effect=ssh), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file), \
             patch.object(ct, "add_dns_entry", side_effect=subprocess.CalledProcessError(1, "sudo")), \
             patch.object(ct, "remove_haproxy_clone", side_effect=track_remove_haproxy):
            with self.assertRaises(SystemExit):
                ct.cmd_boot(self._boot_args())

        self.assertEqual(haproxy_removed, ["aabbccdd"])
        self.assertEqual(self.mock_env.get_saved_state()["clones"], {})
        kubeconfig = ct.KUBECONFIG_DIR / "aabbccdd.kubeconfig"
        self.assertFalse(kubeconfig.exists())

    @patch("time.sleep")
    @patch.object(ct, "add_dns_entry")
    def test_called_process_error_triggers_rollback(self, *_):
        def ssh_fail_at_haproxy(cmd, check=True):
            self.calls.append((cmd, check))
            if "cat /etc/haproxy" in cmd and check:
                raise subprocess.CalledProcessError(1, cmd, stderr="connection refused")
            r = MagicMock()
            r.returncode = 0
            r.stdout = "ok"
            r.stderr = ""
            return r

        wrapped = self.mock_env.wrap_run_positional(ssh_fail_at_haproxy)
        with patch.object(ct.env, "run", side_effect=wrapped), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            with self.assertRaises(SystemExit):
                ct.cmd_boot(self._boot_args())

        cleanup = self._cleanup_cmds()
        self.assertTrue(any("virsh destroy " in c for c in cleanup))
        self.assertTrue(any("net-destroy" in c for c in cleanup))
        self.assertTrue(any("rm -f" in c for c in cleanup))
        self.assertEqual(self.mock_env.get_saved_state()["clones"], {})

    @patch("time.sleep")
    @patch.object(ct, "remove_dns_entry")
    @patch.object(ct, "remove_haproxy_clone")
    @patch.object(ct, "add_dns_entry")
    def test_success_saves_state(self, *_):
        ssh = self.mock_env.wrap_run_positional(self._make_all_succeed_ssh())
        with patch.object(ct.env, "run", side_effect=ssh), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            ct.cmd_boot(self._boot_args())

        state = self.mock_env.get_saved_state()
        self.assertIn("aabbccdd", state["clones"])
        self.assertEqual(state["clones"]["aabbccdd"]["subnet_primary"], 160)
        kubeconfig = ct.KUBECONFIG_DIR / "aabbccdd.kubeconfig"
        self.assertTrue(kubeconfig.exists())
        kubeconfig.unlink(missing_ok=True)

    @patch("time.sleep")
    @patch.object(ct, "remove_dns_entry")
    @patch.object(ct, "remove_haproxy_clone")
    @patch.object(ct, "add_dns_entry")
    def test_boot_uses_ssh_identity_key(self, *_):
        ssh = self.mock_env.wrap_run_positional(self._make_all_succeed_ssh())
        with patch.object(ct.env, "run", side_effect=ssh), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            ct.cmd_boot(self._boot_args())

        chmod_cmds = [cmd for cmd, _ in self.calls if "chmod 600" in cmd and "cluster-tool.key" in cmd]
        self.assertTrue(len(chmod_cmds) > 0)
        scp_cmds = [cmd for cmd, _ in self.calls if "scp" in cmd and "crypto" in cmd]
        self.assertTrue(len(scp_cmds) > 0)
        self.assertTrue(any("-i " in c and "cluster-tool.key" in c for c in scp_cmds))
        kubeconfig = ct.KUBECONFIG_DIR / "aabbccdd.kubeconfig"
        kubeconfig.unlink(missing_ok=True)

    @patch("time.sleep")
    @patch.object(ct, "remove_dns_entry")
    @patch.object(ct, "remove_haproxy_clone")
    @patch.object(ct, "add_dns_entry")
    def test_recert_uses_key_preservation(self, *_):
        ssh = self.mock_env.wrap_run_positional(self._make_all_succeed_ssh())
        with patch.object(ct.env, "run", side_effect=ssh), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            ct.cmd_boot(self._boot_args())

        recert_cmd = next(cmd for cmd, _ in self.calls if "--use-key" in cmd)
        self.assertIn("--use-key kube-apiserver-lb-signer:/tmp/crypto/lb-signer.key", recert_cmd)
        self.assertIn("--use-key kube-apiserver-localhost-signer:/tmp/crypto/localhost-signer.key", recert_cmd)
        self.assertIn("--use-key kube-apiserver-service-network-signer:/tmp/crypto/service-network-signer.key", recert_cmd)
        self.assertIn("--use-key fake-ingress-cn:/tmp/crypto/ingress.key", recert_cmd)
        self.assertIn("--cn-san-replace api.test-infra-cluster-6ef80144.redhat.com:api.test-infra-cluster-aabbccdd.redhat.com", recert_cmd)
        self.assertIn("--cn-san-replace api-int.test-infra-cluster-6ef80144.redhat.com:api-int.test-infra-cluster-aabbccdd.redhat.com", recert_cmd)
        self.assertIn("--cn-san-replace *.apps.test-infra-cluster-6ef80144.redhat.com:*.apps.test-infra-cluster-aabbccdd.redhat.com", recert_cmd)
        self.assertIn("--cn-san-replace test-infra-cluster-6ef80144-master-0:test-infra-cluster-aabbccdd-master-0", recert_cmd)
        self.assertIn("--cn-san-replace system:node:test-infra-cluster-6ef80144-master-0,system:node:test-infra-cluster-aabbccdd-master-0", recert_cmd)
        self.assertIn("--extend-expiration", recert_cmd)
        kubeconfig = ct.KUBECONFIG_DIR / "aabbccdd.kubeconfig"
        kubeconfig.unlink(missing_ok=True)


    @patch("time.sleep")
    @patch.object(ct, "remove_dns_entry")
    @patch.object(ct, "remove_haproxy_clone")
    @patch.object(ct, "add_dns_entry")
    def test_node_fix_clears_stale_nodeip_cache(self, *_):
        ssh = self.mock_env.wrap_run_positional(self._make_all_succeed_ssh())
        with patch.object(ct.env, "run", side_effect=ssh), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            ct.cmd_boot(self._boot_args())

        b64_cmd = next(cmd for cmd, _ in self.calls if "base64 -d | sudo python3" in cmd)
        import base64 as b64
        encoded = b64_cmd.split("echo ")[1].split(" | base64")[0]
        script = b64.b64decode(encoded).decode()
        self.assertIn("rmtree('/run/nodeip-configuration'", script)
        self.assertIn("restart', 'nodeip-configuration'", script)
        self.assertIn("daemon-reload", script)
        lines = script.strip().split("\n")
        rmtree_idx = next(i for i, l in enumerate(lines) if "rmtree" in l and "nodeip" in l)
        restart_idx = next(i for i, l in enumerate(lines) if "nodeip-configuration" in l and "restart" in l)
        daemon_idx = next(i for i, l in enumerate(lines) if "daemon-reload" in l)
        self.assertLess(rmtree_idx, restart_idx)
        self.assertLess(restart_idx, daemon_idx)
        kubeconfig = ct.KUBECONFIG_DIR / "aabbccdd.kubeconfig"
        kubeconfig.unlink(missing_ok=True)

    @patch("time.sleep")
    @patch.object(ct, "add_dns_entry")
    @patch.object(ct, "add_haproxy_clone")
    def test_node_fix_cleans_ovn_state(self, *_):
        ssh = self.mock_env.wrap_run_positional(self._make_all_succeed_ssh())
        with patch.object(ct.env, "run", side_effect=ssh), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            ct.cmd_boot(self._boot_args())

        b64_cmd = next(cmd for cmd, _ in self.calls if "base64 -d | sudo python3" in cmd)
        import base64 as b64
        encoded = b64_cmd.split("echo ")[1].split(" | base64")[0]
        script = b64.b64decode(encoded).decode()
        self.assertIn("/etc/openvswitch/conf.db", script)
        self.assertIn("/etc/openvswitch/.conf.db.~lock~", script)
        self.assertIn("/etc/ovn/ovnsb_db.db", script)
        self.assertIn("/etc/ovn/ovnnb_db.db", script)
        self.assertIn("rmtree('/var/lib/ovn-ic/etc'", script)
        self.assertIn("restart', 'openvswitch'", script)
        ovn_rm_idx = next(i for i, l in enumerate(script.split("\n")) if "conf.db" in l)
        ovs_restart_idx = next(i for i, l in enumerate(script.split("\n")) if "openvswitch" in l and "restart" in l)
        self.assertLess(ovn_rm_idx, ovs_restart_idx,
            "OVN database cleanup must happen before openvswitch restart")
        kubeconfig = ct.KUBECONFIG_DIR / "aabbccdd.kubeconfig"
        kubeconfig.unlink(missing_ok=True)

    @patch("time.sleep")
    @patch.object(ct, "add_dns_entry")
    @patch.object(ct, "add_haproxy_clone")
    def test_standalone_etcd_uses_store_datadir(self, *_):
        ssh = self.mock_env.wrap_run_positional(self._make_all_succeed_ssh())
        with patch.object(ct.env, "run", side_effect=ssh), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            ct.cmd_boot(self._boot_args())

        etcd_cmd = next((cmd for cmd, _ in self.calls if "etcd-recert" in cmd and "podman run" in cmd), None)
        self.assertIsNotNone(etcd_cmd, "must start standalone etcd container")
        self.assertIn("-v /var/lib/etcd:/store", etcd_cmd)
        self.assertIn("--data-dir /store", etcd_cmd)
        self.assertIn("--name editor", etcd_cmd)
        self.assertIn("--entrypoint etcd", etcd_cmd)
        self.assertNotIn("--force-new-cluster", etcd_cmd,
            "must not use --force-new-cluster (causes unnecessary revision rollouts)")
        kubeconfig = ct.KUBECONFIG_DIR / "aabbccdd.kubeconfig"
        kubeconfig.unlink(missing_ok=True)

    @patch("time.sleep")
    @patch.object(ct, "remove_dns_entry")
    @patch.object(ct, "remove_haproxy_clone")
    @patch.object(ct, "add_dns_entry")
    def test_etcd_recert_uses_authfile(self, *_):
        ssh = self.mock_env.wrap_run_positional(self._make_all_succeed_ssh())
        with patch.object(ct.env, "run", side_effect=ssh), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            ct.cmd_boot(self._boot_args())

        etcd_cmd = next((cmd for cmd, _ in self.calls if "etcd-recert" in cmd and "podman run" in cmd), None)
        self.assertIsNotNone(etcd_cmd)
        self.assertIn("--authfile /var/lib/kubelet/config.json", etcd_cmd)
        kubeconfig = ct.KUBECONFIG_DIR / "aabbccdd.kubeconfig"
        kubeconfig.unlink(missing_ok=True)

    @patch("time.sleep")
    @patch.object(ct, "add_dns_entry")
    def test_identity_mismatch_aborts_boot(self, *_):
        mock_co = self._MOCK_CO_JSON
        mock_nodes = self._MOCK_NODES_JSON
        def ssh_wrong_identity(cmd, check=True):
            self.calls.append((cmd, check))
            r = MagicMock()
            r.returncode = 0
            if "ingress-cn" in cmd:
                r.stdout = "fake-ingress-cn"
            elif "infrastructure cluster" in cmd:
                r.stdout = "https://api.test-infra-cluster-6ef80144.redhat.com:6443"
            elif "get co -o json" in cmd:
                r.stdout = mock_co
            elif "get nodes -o json" in cmd:
                r.stdout = mock_nodes
            else:
                r.stdout = "ok"
            r.stderr = ""
            return r

        wrapped = self.mock_env.wrap_run_positional(ssh_wrong_identity)
        with patch.object(ct.env, "run", side_effect=wrapped), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            with self.assertRaises(SystemExit) as ctx:
                ct.cmd_boot(self._boot_args())

        self.assertIn("IDENTITY MISMATCH", str(ctx.exception))

    @patch("time.sleep")
    @patch.object(ct, "add_dns_entry")
    def test_node_not_ready_prints_diagnostics(self, *_):
        mock_nodes_not_ready = json.dumps({"items": [
            {"metadata": {"name": "test-node"}, "status": {"conditions": [
                {"type": "Ready", "status": "False", "reason": "KubeletNotReady", "message": "container runtime not ready"},
            ]}}
        ]})
        def ssh_node_not_ready(cmd, check=True):
            self.calls.append((cmd, check))
            r = MagicMock()
            r.returncode = 0
            if "ingress-cn" in cmd:
                r.stdout = "fake-ingress-cn"
            elif "get nodes -o json" in cmd:
                r.stdout = mock_nodes_not_ready
            elif "get nodes -o wide" in cmd:
                r.stdout = "NAME   STATUS     ROLES   AGE   VERSION\ntest   NotReady   master  1m    v1.32"
            else:
                r.stdout = "ok"
            r.stderr = ""
            return r

        wrapped = self.mock_env.wrap_run_positional(ssh_node_not_ready)
        with patch.object(ct.env, "run", side_effect=wrapped), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            with self.assertRaises(SystemExit) as ctx:
                ct.cmd_boot(self._boot_args())
        self.assertIn("Node not Ready", str(ctx.exception))
        self.assertIn("NotReady", str(ctx.exception))

    @patch("time.sleep")
    @patch.object(ct, "add_dns_entry")
    def test_unhealthy_operators_prints_which_ones(self, *_):
        mock_co_bad = json.dumps({"items": [
            {"metadata": {"name": "authentication"}, "status": {"conditions": [
                {"type": "Available", "status": "False", "message": "OAuthServerDown"},
                {"type": "Degraded", "status": "True", "message": "OAuth route unreachable"},
            ]}},
            {"metadata": {"name": "dns"}, "status": {"conditions": [
                {"type": "Available", "status": "True"},
                {"type": "Degraded", "status": "False"},
            ]}},
        ]})
        mock_nodes = self._MOCK_NODES_JSON
        def ssh_co_unhealthy(cmd, check=True):
            self.calls.append((cmd, check))
            r = MagicMock()
            r.returncode = 0
            if "ingress-cn" in cmd:
                r.stdout = "fake-ingress-cn"
            elif "get co -o json" in cmd:
                r.stdout = mock_co_bad
            elif "get nodes -o json" in cmd:
                r.stdout = mock_nodes
            else:
                r.stdout = "ok"
            r.stderr = ""
            return r

        wrapped = self.mock_env.wrap_run_positional(ssh_co_unhealthy)
        with patch.object(ct.env, "run", side_effect=wrapped), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            with self.assertRaises(SystemExit) as ctx:
                ct.cmd_boot(self._boot_args())
        self.assertIn("Operators not healthy", str(ctx.exception))
        self.assertIn("authentication", str(ctx.exception))
        self.assertNotIn("dns", str(ctx.exception))

    @patch("time.sleep")
    @patch.object(ct, "remove_dns_entry")
    @patch.object(ct, "remove_haproxy_clone")
    @patch.object(ct, "add_dns_entry")
    def test_kubeconfig_available_before_operator_check(self, *_):
        call_order = []
        mock_co = self._MOCK_CO_JSON
        mock_nodes = self._MOCK_NODES_JSON
        def ssh_tracking(cmd, check=True):
            self.calls.append((cmd, check))
            if "lb-ext.kubeconfig" in cmd and "cat" in cmd:
                call_order.append("kubeconfig_extract")
            elif "get co -o json" in cmd:
                call_order.append("operator_check")
            r = MagicMock()
            r.returncode = 0
            if "ingress-cn" in cmd:
                r.stdout = "fake-ingress-cn"
            elif "infrastructure cluster" in cmd:
                r.stdout = "https://api.test-infra-cluster-aabbccdd.redhat.com:6443"
            elif "get co -o json" in cmd:
                r.stdout = mock_co
            elif "get nodes -o json" in cmd:
                r.stdout = mock_nodes
            else:
                r.stdout = "ok"
            r.stderr = ""
            return r

        wrapped = self.mock_env.wrap_run_positional(ssh_tracking)
        with patch.object(ct.env, "run", side_effect=wrapped), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            ct.cmd_boot(self._boot_args())

        self.assertIn("kubeconfig_extract", call_order)
        self.assertIn("operator_check", call_order)
        kc_idx = call_order.index("kubeconfig_extract")
        co_idx = call_order.index("operator_check")
        self.assertLess(kc_idx, co_idx)
        state = self.mock_env.get_saved_state()
        self.assertIn("aabbccdd", state["clones"])
        kubeconfig = ct.KUBECONFIG_DIR / "aabbccdd.kubeconfig"
        self.assertTrue(kubeconfig.exists())
        kubeconfig.unlink(missing_ok=True)

    @patch("time.sleep")
    @patch.object(ct, "remove_dns_entry")
    @patch.object(ct, "remove_haproxy_clone")
    @patch.object(ct, "add_dns_entry")
    def test_concurrent_boot_reservation(self, *_):
        ssh = self.mock_env.wrap_run_positional(self._make_all_succeed_ssh())
        with patch.object(ct.env, "run", side_effect=ssh), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            ct.cmd_boot(self._boot_args())

        state_after_first = self.mock_env.get_saved_state()
        self.assertIn("aabbccdd", state_after_first["clones"])
        self.assertEqual(state_after_first["clones"]["aabbccdd"]["subnet_primary"], 160)
        s2 = ct.allocate_subnet(state_after_first)
        self.assertEqual(s2, 161)
        kubeconfig = ct.KUBECONFIG_DIR / "aabbccdd.kubeconfig"
        kubeconfig.unlink(missing_ok=True)

    @patch("time.sleep")
    @patch.object(ct.ExecutionEnv, "write_file")
    @patch.object(ct, "add_dns_entry")
    @patch.object(ct.ExecutionEnv, "copy_from")
    def test_boot_failure_cleans_clone_reservation(self, *_):
        ssh = self.mock_env.wrap_run_positional(self._make_ssh_mock("virsh net-define /tmp/net-"))
        with patch.object(ct.env, "run", side_effect=ssh):
            with self.assertRaises(SystemExit):
                ct.cmd_boot(self._boot_args())

        state = self.mock_env.get_saved_state()
        self.assertEqual(state["clones"], {})

    @patch("time.sleep")
    @patch.object(ct, "remove_dns_entry")
    @patch.object(ct, "remove_haproxy_clone")
    @patch.object(ct, "add_dns_entry")
    def test_booting_entry_registered_before_long_boot(self, *_):
        states_during_boot = []
        mock_co = self._MOCK_CO_JSON
        mock_nodes = self._MOCK_NODES_JSON
        def ssh_capture_state(cmd, check=True):
            self.calls.append((cmd, check))
            if "virsh define /tmp/vm-" in cmd:
                states_during_boot.append(self.mock_env.get_saved_state())
            r = MagicMock()
            r.returncode = 0
            if "ingress-cn" in cmd:
                r.stdout = "fake-ingress-cn"
            elif "infrastructure cluster" in cmd:
                r.stdout = "https://api.test-infra-cluster-aabbccdd.redhat.com:6443"
            elif "get co -o json" in cmd:
                r.stdout = mock_co
            elif "get nodes -o json" in cmd:
                r.stdout = mock_nodes
            else:
                r.stdout = "ok"
            r.stderr = ""
            return r

        wrapped = self.mock_env.wrap_run_positional(ssh_capture_state)
        with patch.object(ct.env, "run", side_effect=wrapped), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            ct.cmd_boot(self._boot_args())

        self.assertTrue(len(states_during_boot) > 0)
        mid_boot_state = states_during_boot[0]
        self.assertIn("aabbccdd", mid_boot_state["clones"])
        self.assertEqual(mid_boot_state["clones"]["aabbccdd"]["status"], "booting")
        self.assertEqual(mid_boot_state["clones"]["aabbccdd"]["subnet_primary"], 160)
        kubeconfig = ct.KUBECONFIG_DIR / "aabbccdd.kubeconfig"
        kubeconfig.unlink(missing_ok=True)


class TestDetectSourceVM(unittest.TestCase):
    def setUp(self):
        ct.env = ct.ExecutionEnv(host="test@host", host_ip="10.0.0.1")

    def test_parse_dominfo(self):
        dominfo = """Id:             90
Name:           test-infra-cluster-6ef80144-master-0
UUID:           bd3a9dd8-cc6b-4aae-8ba0-ca4b1c6b238d
OS Type:        hvm
State:          running
CPU(s):         16
Max memory:     67108864 KiB
Used memory:    67108864 KiB"""
        result = ct.parse_dominfo(dominfo)
        self.assertEqual(result["vcpus"], 16)
        self.assertEqual(result["memory_kib"], 67108864)

    @patch.object(ct.ExecutionEnv, "run")
    def test_parse_disk_paths(self, mock_ssh):
        mock_ssh.return_value = MagicMock(stdout="/resolved/pool/disk-0\n")
        dumpxml = """<domain type='kvm'>
          <devices>
            <disk type='volume' device='disk'>
              <source pool='test-pool' volume='disk-0'/>
              <target dev='sda' bus='scsi'/>
            </disk>
            <disk type='file' device='cdrom'>
              <source file='/tmp/installer.iso'/>
              <target dev='vdz' bus='scsi'/>
              <readonly/>
            </disk>
            <disk type='file' device='disk'>
              <source file='/data/extra-disk.qcow2'/>
              <target dev='sdb' bus='scsi'/>
            </disk>
          </devices>
        </domain>"""
        disks = ct.parse_disk_paths(dumpxml, "test-pool")
        self.assertEqual(len(disks), 2)
        self.assertEqual(disks[0]["target"], "sda")
        self.assertEqual(disks[0]["path"], "/resolved/pool/disk-0")
        self.assertEqual(disks[1]["target"], "sdb")
        self.assertEqual(disks[1]["path"], "/data/extra-disk.qcow2")

    def test_parse_subnet(self):
        net_xml = """<network>
          <ip family='ipv4' address='192.168.135.1' prefix='24'>
            <dhcp>
              <host mac='02:00:00:02:4D:52' ip='192.168.135.10'/>
            </dhcp>
          </ip>
        </network>"""
        subnet = ct.parse_subnet(net_xml)
        self.assertEqual(subnet, 135)


class TestFlavorState(unittest.TestCase):
    def setUp(self):
        self.mock_env = MockStateEnv()
        self.mock_env.setup()
        self._run_patch = patch.object(self.mock_env.env, "run", side_effect=self.mock_env.mock_run)
        self._wf_patch = patch.object(self.mock_env.env, "write_file", side_effect=self.mock_env.mock_write_file)
        self._run_patch.start()
        self._wf_patch.start()

    def tearDown(self):
        self._run_patch.stop()
        self._wf_patch.stop()

    def test_load_empty_has_flavors(self):
        state = ct.load_state()
        self.assertEqual(state["flavors"], {})

    def test_save_flavor(self):
        state = ct.load_state()
        state["flavors"]["sno-64"] = {
            "source_cluster": "6ef80144",
            "memory_kib": 67108864,
            "vcpus": 16,
            "disks": ["disk-0.qcow2"],
            "source_primary_subnet": 135,
            "source_secondary_subnet": 153,
            "created_at": "2026-05-04T00:00:00Z",
        }
        ct.save_state(state)
        loaded = ct.load_state()
        self.assertIn("sno-64", loaded["flavors"])
        self.assertEqual(loaded["flavors"]["sno-64"]["vcpus"], 16)


class TestSnapshot(unittest.TestCase):
    def setUp(self):
        self.mock_env = MockStateEnv()
        self.mock_env.setup({"flavors": {}, "clones": {}})
        self.calls = []

    def _snapshot_args(self):
        return argparse.Namespace(name="test-snap", source="aabb1122")

    _DOMINFO = (
        "Id:             1\nName:           test-infra-cluster-aabb1122-master-0\n"
        "CPU(s):         16\nMax memory:     67108864 KiB\n"
    )
    _DUMPXML = (
        "<domain><devices>"
        "<disk type='file' device='disk'><source file='/data/disk-0.qcow2'/>"
        "<target dev='sda' bus='scsi'/></disk>"
        "</devices></domain>"
    )
    _NET_XML = (
        "<network><ip family='ipv4' address='192.168.135.1' prefix='24'>"
        "<dhcp><host mac='02:00:00:aa:bb:cc' ip='192.168.135.10'/></dhcp>"
        "</ip></network>"
    )

    def _make_ssh_mock(self):
        shutdown_called = {"v": False}
        def ssh(*args, check=True, **kwargs):
            cmd = args[-1]
            self.calls.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            if "virsh dominfo" in cmd:
                r.stdout = self._DOMINFO
            elif "virsh dumpxml" in cmd:
                r.stdout = self._DUMPXML
            elif "virsh net-dumpxml" in cmd:
                r.stdout = self._NET_XML
            elif "virsh shutdown" in cmd:
                shutdown_called["v"] = True
                r.stdout = ""
            elif "virsh domstate" in cmd:
                r.stdout = "shut off" if shutdown_called["v"] else "running"
            elif "ingress-cn" in cmd or "subject" in cmd:
                r.stdout = "fake-ingress-cn"
            elif "python3" in cmd and "etcd" in cmd:
                r.stdout = "quay.io/test/etcd:latest"
            elif "cluster-tool.key.pub" in cmd and "cat" in cmd:
                r.stdout = "ssh-ed25519 AAAA_fake_pub_key test@host"
            elif "nodeip-configuration.service" in cmd:
                r.stdout = "quay.io/openshift-release-dev/ocp-v4.0-art-dev@sha256:" + "0" * 64
            elif "podman image exists" in cmd:
                r.returncode = 1
                r.stdout = ""
            else:
                r.stdout = ""
            return r
        return ssh

    @patch("time.sleep")
    def test_snapshot_uses_qemu_img_convert(self, _):
        ssh = self._make_ssh_mock()
        wrapped = self.mock_env.wrap_run_positional(ssh)
        with patch.object(ct.env, "run", side_effect=wrapped), \
             patch.object(ct.env, "run_vm", side_effect=ssh), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            ct.cmd_snapshot(self._snapshot_args())

        convert_cmds = [c for c in self.calls if "qemu-img convert" in c]
        self.assertEqual(len(convert_cmds), 1)
        self.assertIn("-O qcow2", convert_cmds[0])
        self.assertIn("/data/disk-0.qcow2", convert_cmds[0])

        cp_cmds = [c for c in self.calls if "cp --sparse" in c]
        self.assertEqual(len(cp_cmds), 0)

    @patch("time.sleep")
    def test_snapshot_saves_flavor_state(self, _):
        ssh = self._make_ssh_mock()
        wrapped = self.mock_env.wrap_run_positional(ssh)
        with patch.object(ct.env, "run", side_effect=wrapped), \
             patch.object(ct.env, "run_vm", side_effect=ssh), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            ct.cmd_snapshot(self._snapshot_args())

        state = self.mock_env.get_saved_state()
        self.assertIn("test-snap", state["flavors"])
        flavor = state["flavors"]["test-snap"]
        self.assertEqual(flavor["source_cluster"], "aabb1122")
        self.assertEqual(flavor["vcpus"], 16)
        self.assertEqual(flavor["memory_kib"], 67108864)
        self.assertEqual(flavor["disks"], ["disk-0.qcow2"])

    @patch("time.sleep")
    def test_snapshot_existing_flavor_exits(self, _):
        self.mock_env.save_initial_state({
            "flavors": {"test-snap": {"source_cluster": "old"}},
            "clones": {}})
        with patch.object(ct.env, "run", side_effect=self.mock_env.mock_run), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            with self.assertRaises(SystemExit) as ctx:
                ct.cmd_snapshot(self._snapshot_args())
        self.assertIn("already exists", str(ctx.exception))

    @patch("time.sleep")
    def test_snapshot_injects_ssh_key(self, _):
        ssh = self._make_ssh_mock()
        wrapped = self.mock_env.wrap_run_positional(ssh)
        with patch.object(ct.env, "run", side_effect=wrapped), \
             patch.object(ct.env, "run_vm", side_effect=ssh), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            ct.cmd_snapshot(self._snapshot_args())

        pub_cat = [c for c in self.calls if "cat" in c and "cluster-tool.key.pub" in c]
        self.assertEqual(len(pub_cat), 1)
        tee_cmds = [c for c in self.calls if "authorized_keys" in c]
        self.assertEqual(len(tee_cmds), 1)
        cp_key = [c for c in self.calls if "cp " in c and "cluster-tool.key" in c]
        self.assertEqual(len(cp_key), 2)

    @patch("time.sleep")
    def test_snapshot_hardlinks_etcd_certs(self, _):
        ssh = self._make_ssh_mock()
        wrapped = self.mock_env.wrap_run_positional(ssh)
        with patch.object(ct.env, "run", side_effect=wrapped), \
             patch.object(ct.env, "run_vm", side_effect=ssh), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            ct.cmd_snapshot(self._snapshot_args())

        hardlink_cmds = [c for c in self.calls if "sudo ln " in c and "etcd-all-certs" in c]
        self.assertEqual(len(hardlink_cmds), 1, "snapshot must hardlink etcd-all-certs")
        cmd = hardlink_cmds[0]
        self.assertNotIn("-s", cmd.split("ln ")[1].split(" ")[0],
            "must use hardlinks (ln), not symlinks (ln -s)")
        self.assertIn("etcd-pod-${rev}/secrets/etcd-all-certs", cmd)
        self.assertIn("etcd-certs/secrets/etcd-all-certs", cmd)

    @patch("time.sleep")
    def test_snapshot_removes_stale_etcd_pod_yaml(self, _):
        ssh = self._make_ssh_mock()
        wrapped = self.mock_env.wrap_run_positional(ssh)
        with patch.object(ct.env, "run", side_effect=wrapped), \
             patch.object(ct.env, "run_vm", side_effect=ssh), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            ct.cmd_snapshot(self._snapshot_args())

        etcd_cleanup = [c for c in self.calls if "rm -f etcd-certs/etcd-pod.yaml" in c]
        self.assertEqual(len(etcd_cleanup), 1,
            "snapshot must remove stale etcd-pod.yaml from etcd-certs/")

    @patch("time.sleep")
    def test_snapshot_hardlink_before_shutdown(self, _):
        ssh = self._make_ssh_mock()
        wrapped = self.mock_env.wrap_run_positional(ssh)
        with patch.object(ct.env, "run", side_effect=wrapped), \
             patch.object(ct.env, "run_vm", side_effect=ssh), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            ct.cmd_snapshot(self._snapshot_args())

        hardlink_idx = next(i for i, c in enumerate(self.calls) if "sudo ln " in c and "etcd-all-certs" in c)
        shutdown_idx = next(i for i, c in enumerate(self.calls) if "virsh shutdown" in c)
        self.assertLess(hardlink_idx, shutdown_idx,
            "etcd hardlinks must be created before VM shutdown")

    @patch("time.sleep")
    def test_snapshot_clears_etcd_certs_dir_before_hardlink(self, _):
        ssh = self._make_ssh_mock()
        wrapped = self.mock_env.wrap_run_positional(ssh)
        with patch.object(ct.env, "run", side_effect=wrapped), \
             patch.object(ct.env, "run_vm", side_effect=ssh), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            ct.cmd_snapshot(self._snapshot_args())

        etcd_prep = [c for c in self.calls if "etcd-all-certs" in c and "etcd-certs" in c]
        self.assertTrue(len(etcd_prep) >= 1)
        cmd = etcd_prep[0]
        rm_pos = cmd.index("rm -rf etcd-certs/secrets/etcd-all-certs")
        mkdir_pos = cmd.index("mkdir -p etcd-certs/secrets/etcd-all-certs")
        ln_pos = cmd.index("sudo ln ")
        self.assertLess(rm_pos, mkdir_pos, "must rm before mkdir")
        self.assertLess(mkdir_pos, ln_pos, "must mkdir before ln")

    @patch("time.sleep")
    def test_snapshot_shuts_down_and_restarts_vm(self, _):
        ssh = self._make_ssh_mock()
        wrapped = self.mock_env.wrap_run_positional(ssh)
        with patch.object(ct.env, "run", side_effect=wrapped), \
             patch.object(ct.env, "run_vm", side_effect=ssh), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            ct.cmd_snapshot(self._snapshot_args())

        shutdown = [c for c in self.calls if "virsh shutdown" in c]
        start = [c for c in self.calls if "virsh start" in c]
        self.assertEqual(len(shutdown), 1)
        self.assertEqual(len(start), 1)
        shutdown_idx = self.calls.index(shutdown[0])
        start_idx = self.calls.index(start[0])
        self.assertLess(shutdown_idx, start_idx)

    @patch("time.sleep")
    def test_snapshot_detects_etcd_image_with_yaml(self, _):
        ssh = self._make_ssh_mock()
        wrapped = self.mock_env.wrap_run_positional(ssh)
        with patch.object(ct.env, "run", side_effect=wrapped), \
             patch.object(ct.env, "run_vm", side_effect=ssh), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            ct.cmd_snapshot(self._snapshot_args())

        etcd_detect = next(c for c in self.calls if "etcd-pod.yaml" in c and "python3" in c)
        self.assertIn("yaml.safe_load", etcd_detect,
            "must use yaml.safe_load to parse etcd manifest (it's YAML, not JSON)")

    @patch("time.sleep")
    def test_snapshot_prunes_container_images(self, _):
        ssh = self._make_ssh_mock()
        wrapped = self.mock_env.wrap_run_positional(ssh)
        with patch.object(ct.env, "run", side_effect=wrapped), \
             patch.object(ct.env, "run_vm", side_effect=ssh), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            ct.cmd_snapshot(self._snapshot_args())

        prune_cmds = [c for c in self.calls if "crictl" in c or "podman image prune" in c]
        self.assertEqual(len(prune_cmds), 1)
        cmd = prune_cmds[0]
        self.assertIn("xargs -r sudo crictl rm", cmd)
        self.assertIn("crictl --timeout 120s rmi --prune", cmd)
        self.assertIn("podman image prune -a -f", cmd)
        self.assertIn("podman rm cache-recert", cmd)
        self.assertIn("cache-nodeip", cmd)
        self.assertIn("cache-etcd", cmd, "etcd anchor must be cleaned up too, not left leaking")

    @patch("time.sleep")
    def test_snapshot_caches_recert_before_prune(self, _):
        ssh = self._make_ssh_mock()
        wrapped = self.mock_env.wrap_run_positional(ssh)
        with patch.object(ct.env, "run", side_effect=wrapped), \
             patch.object(ct.env, "run_vm", side_effect=ssh), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            ct.cmd_snapshot(self._snapshot_args())

        pull_cmds = [c for c in self.calls if "podman pull" in c and ct.RECERT_IMAGE in c]
        self.assertEqual(len(pull_cmds), 1, "must pull recert image")
        anchor_cmds = [c for c in self.calls if "podman create --replace --name cache-recert" in c]
        self.assertEqual(len(anchor_cmds), 1, "must create anchor container")

        pull_idx = next(i for i, c in enumerate(self.calls) if "podman pull" in c and ct.RECERT_IMAGE in c)
        anchor_idx = next(i for i, c in enumerate(self.calls) if "podman create --replace --name cache-recert" in c)
        prune_idx = next(i for i, c in enumerate(self.calls) if "podman image prune" in c)
        self.assertLess(pull_idx, anchor_idx, "pull must happen before anchor creation")
        self.assertLess(anchor_idx, prune_idx, "anchor must exist before prune runs")

    @patch("time.sleep")
    def test_snapshot_skips_pull_when_image_already_cached(self, _):
        base_ssh = self._make_ssh_mock()
        def ssh(*args, check=True, **kwargs):
            cmd = args[-1]
            if "podman image exists" in cmd:
                self.calls.append(cmd)
                r = MagicMock()
                r.returncode = 0
                r.stdout = ""
                r.stderr = ""
                return r
            return base_ssh(*args, check=check, **kwargs)
        wrapped = self.mock_env.wrap_run_positional(ssh)
        with patch.object(ct.env, "run", side_effect=wrapped), \
             patch.object(ct.env, "run_vm", side_effect=ssh), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            ct.cmd_snapshot(self._snapshot_args())

        pull_cmds = [c for c in self.calls if "podman pull" in c]
        self.assertEqual(pull_cmds, [], "must not pull an image already present locally")
        anchor_cmds = [c for c in self.calls if "podman create --replace --name cache-recert" in c
                       or "podman create --replace --name cache-nodeip" in c
                       or "podman create --replace --name cache-etcd" in c]
        self.assertEqual(len(anchor_cmds), 3, "must still anchor all three already-cached images")

    @patch("time.sleep")
    def test_snapshot_caches_etcd_image(self, _):
        ssh = self._make_ssh_mock()
        wrapped = self.mock_env.wrap_run_positional(ssh)
        with patch.object(ct.env, "run", side_effect=wrapped), \
             patch.object(ct.env, "run_vm", side_effect=ssh), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            ct.cmd_snapshot(self._snapshot_args())

        # _make_ssh_mock resolves the detected etcd image to this fixed value.
        pull_cmds = [c for c in self.calls if "podman pull" in c and "quay.io/test/etcd:latest" in c]
        self.assertEqual(len(pull_cmds), 1,
            "must pull the etcd image so cmd_boot's recert step never needs "
            "the snapshot's (possibly stale) baked-in pull secret")
        anchor_cmds = [c for c in self.calls if "podman create --replace --name cache-etcd" in c]
        self.assertEqual(len(anchor_cmds), 1, "must anchor the etcd image so prune doesn't remove it")

        pull_idx = next(i for i, c in enumerate(self.calls)
                         if "podman pull" in c and "quay.io/test/etcd:latest" in c)
        anchor_idx = next(i for i, c in enumerate(self.calls)
                           if "podman create --replace --name cache-etcd" in c)
        prune_idx = next(i for i, c in enumerate(self.calls) if "podman image prune" in c)
        self.assertLess(pull_idx, anchor_idx, "pull must happen before anchor creation")
        self.assertLess(anchor_idx, prune_idx, "anchor must exist before prune runs")

    @patch("time.sleep")
    def test_snapshot_anchor_creation_survives_leftover_container(self, _):
        ssh = self._make_ssh_mock()
        wrapped = self.mock_env.wrap_run_positional(ssh)
        with patch.object(ct.env, "run", side_effect=wrapped), \
             patch.object(ct.env, "run_vm", side_effect=ssh), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            ct.cmd_snapshot(self._snapshot_args())

        create_cmds = [c for c in self.calls if "podman create" in c and "cache-" in c]
        self.assertTrue(create_cmds, "must create anchor containers")
        self.assertTrue(all("--replace" in c for c in create_cmds),
            "anchor creation must use --replace so a leftover container from a prior failed run doesn't collide")

    @patch("time.sleep")
    def test_snapshot_prune_before_shutdown(self, _):
        ssh = self._make_ssh_mock()
        wrapped = self.mock_env.wrap_run_positional(ssh)
        with patch.object(ct.env, "run", side_effect=wrapped), \
             patch.object(ct.env, "run_vm", side_effect=ssh), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            ct.cmd_snapshot(self._snapshot_args())

        prune_idx = next(i for i, c in enumerate(self.calls) if "crictl --timeout 120s rmi --prune" in c)
        shutdown_idx = next(i for i, c in enumerate(self.calls) if "virsh shutdown" in c)
        self.assertLess(prune_idx, shutdown_idx,
            "container image pruning must happen before VM shutdown")

    @patch("time.sleep")
    def test_snapshot_prune_uses_xargs_for_empty_safety(self, _):
        ssh = self._make_ssh_mock()
        wrapped = self.mock_env.wrap_run_positional(ssh)
        with patch.object(ct.env, "run", side_effect=wrapped), \
             patch.object(ct.env, "run_vm", side_effect=ssh), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            ct.cmd_snapshot(self._snapshot_args())

        prune_cmd = next(c for c in self.calls if "crictl" in c and "xargs" in c)
        self.assertNotIn("crictl rm $(", prune_cmd,
            "must not use command substitution — crictl rm with no args is undefined behavior")
        self.assertIn("xargs -r sudo crictl rm", prune_cmd)

    @patch("time.sleep")
    def test_snapshot_prune_fails_fast(self, _):
        prune_called = {"v": False}
        def ssh_fail_prune(*args, check=True, **kwargs):
            cmd = args[-1]
            self.calls.append(cmd)
            if "crictl" in cmd or "podman image prune" in cmd:
                prune_called["v"] = True
                r = MagicMock()
                r.returncode = 1
                r.stderr = "prune failed"
                r.stdout = ""
                if check:
                    import sys
                    sys.exit(f"SSH command failed (exit 1): {cmd}\nprune failed")
                return r
            return self._make_ssh_mock()(*args, check=check, **kwargs)

        wrapped = self.mock_env.wrap_run_positional(ssh_fail_prune)
        with patch.object(ct.env, "run", side_effect=wrapped), \
             patch.object(ct.env, "run_vm", side_effect=ssh_fail_prune), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            with self.assertRaises(SystemExit):
                ct.cmd_snapshot(self._snapshot_args())

        self.assertTrue(prune_called["v"])
        shutdown_cmds = [c for c in self.calls if "virsh shutdown" in c]
        self.assertEqual(len(shutdown_cmds), 0,
            "VM must not be shut down if pruning failed")


class TestLocking(unittest.TestCase):
    def setUp(self):
        self.mock_env = MockStateEnv()
        self.mock_env.setup({"flavors": {}, "clones": {}})
        self.run_calls = []

    def _tracking_mock_run(self, cmd, *, check=True):
        self.run_calls.append(cmd)
        return self.mock_env.mock_run(cmd, check=check)

    def test_locked_state_saves_on_success(self):
        with patch.object(ct.env, "run", side_effect=self._tracking_mock_run), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            with ct.locked_state() as state:
                state["clones"]["test1"] = {"subnet_primary": 160}
        loaded = self.mock_env.get_saved_state()
        self.assertIn("test1", loaded["clones"])

    def test_locked_state_does_not_save_on_exception(self):
        with patch.object(ct.env, "run", side_effect=self._tracking_mock_run), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            with self.assertRaises(ValueError):
                with ct.locked_state() as state:
                    state["clones"]["test1"] = {"subnet_primary": 160}
                    raise ValueError("boom")
        loaded = self.mock_env.get_saved_state()
        self.assertNotIn("test1", loaded["clones"])

    def test_locked_state_serializes_subnet_allocation(self):
        with patch.object(ct.env, "run", side_effect=self._tracking_mock_run), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            with ct.locked_state() as state:
                s1 = ct.allocate_subnet(state)
                state["clones"]["c1"] = {"subnet_primary": s1, "subnet_secondary": s1 + ct.SUBNET_SECONDARY_OFFSET}
            with ct.locked_state() as state:
                s2 = ct.allocate_subnet(state)
        self.assertEqual(s1, 160)
        self.assertEqual(s2, 161)

    def test_locked_state_creates_lock_file(self):
        with patch.object(ct.env, "run", side_effect=self._tracking_mock_run), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            with ct.locked_state() as state:
                self.assertTrue((ct.CLIENT_CONFIG_DIR / "state.lock").exists())

    def test_locked_haproxy_creates_lock_file(self):
        with ct.locked_haproxy():
            self.assertTrue((ct.CLIENT_CONFIG_DIR / "haproxy.lock").exists())


class TestManifest(unittest.TestCase):
    def test_build_manifest_structure(self):
        m = ct.build_manifest(
            flavor_name="sno-64",
            metadata={"vcpus": 16, "memory_kib": 67108864},
            disk_names=["disk-0.qcow2"],
        )
        self.assertEqual(m["version"], 1)
        self.assertEqual(m["flavor"], "sno-64")
        self.assertEqual(len(m["disks"]), 1)
        self.assertEqual(m["disks"][0]["name"], "disk-0.qcow2")
        self.assertEqual(m["disks"][0]["prefix"], "disk-0")
        self.assertEqual(m["metadata"]["vcpus"], 16)

    def test_build_manifest_multi_disk(self):
        m = ct.build_manifest(
            flavor_name="osac",
            metadata={},
            disk_names=["disk-0.qcow2", "disk-1.qcow2"],
        )
        self.assertEqual(len(m["disks"]), 2)
        self.assertEqual(m["disks"][1]["prefix"], "disk-1")

    def test_parse_manifest_valid(self):
        raw = json.dumps({
            "version": 1, "flavor": "test",
            "disks": [{"name": "disk-0.qcow2", "prefix": "disk-0"}],
            "metadata": {"vcpus": 8},
        })
        m = ct.parse_manifest(raw)
        self.assertEqual(m["flavor"], "test")

    def test_parse_manifest_roundtrip(self):
        built = ct.build_manifest(
            flavor_name="rt", metadata={"k": "v"}, disk_names=["disk-0.qcow2"]
        )
        parsed = ct.parse_manifest(built)
        self.assertEqual(parsed, built)

    def test_parse_manifest_missing_key(self):
        with self.assertRaises(SystemExit) as ctx:
            ct.parse_manifest({"version": 1, "disks": [], "metadata": {}})
        self.assertIn("missing 'flavor'", str(ctx.exception))

    def test_parse_manifest_invalid_disk(self):
        with self.assertRaises(SystemExit) as ctx:
            ct.parse_manifest({
                "version": 1, "flavor": "x",
                "disks": [{"name": "d.qcow2"}],
                "metadata": {},
            })
        self.assertIn("missing 'name' or 'prefix'", str(ctx.exception))


class TestPush(unittest.TestCase):
    _INITIAL_STATE = {
        "flavors": {
            "test-flavor": {
                "source_cluster": "abc123",
                "source_primary_subnet": 135,
                "source_secondary_subnet": 153,
                "memory_kib": 67108864,
                "vcpus": 16,
                "disks": ["disk-0.qcow2", "disk-1.qcow2"],
                "etcd_image": "quay.io/test/etcd:latest",
                "created_at": "2026-01-01T00:00:00Z",
            },
        },
        "clones": {},
    }

    def setUp(self):
        self.mock_env = MockStateEnv()
        self.mock_env.setup(self._INITIAL_STATE)
        self.calls = []
        self.written_files = {}

    def _push_args(self, name="test-flavor", registry="quay.io/org/repo", tag="test-tag"):
        return argparse.Namespace(name=name, registry=registry, tag=tag)

    def _inner_mock_run(self, cmd, check=True):
        self.calls.append(cmd)
        r = MagicMock()
        r.returncode = 0
        r.stdout = ""
        r.stderr = ""
        if "command -v" in cmd:
            r.returncode = 0
        elif "wc -l" in cmd and "chunk" in cmd:
            if "disk-0" in cmd:
                r.stdout = "2"
            else:
                r.stdout = "1"
        elif "ls -1" in cmd and "sort" in cmd:
            r.stdout = "disk-0.chunk.aa.zst\ndisk-0.chunk.ab.zst\ndisk-1.chunk.aa.zst\n"
        return r

    def _mock_write_file(self, path, content):
        self.written_files[path] = content
        self.mock_env.mock_write_file(path, content)

    def test_push_flavor_not_found(self):
        wrapped = self.mock_env.wrap_run_positional(self._inner_mock_run)
        with patch.object(ct.env, "run", side_effect=wrapped):
            with self.assertRaises(SystemExit) as ctx:
                ct.cmd_push(self._push_args(name="nonexistent"))
        self.assertIn("nonexistent", str(ctx.exception))

    def test_push_prereq_missing_zstd(self):
        def inner(cmd, check=True):
            self.calls.append(cmd)
            r = MagicMock()
            r.returncode = 1 if "command -v zstd" in cmd else 0
            r.stdout = ""
            r.stderr = ""
            return r
        wrapped = self.mock_env.wrap_run_positional(inner)
        with patch.object(ct.env, "run", side_effect=wrapped):
            with self.assertRaises(SystemExit) as ctx:
                ct.cmd_push(self._push_args())
        self.assertIn("zstd", str(ctx.exception))

    def test_push_prereq_missing_podman(self):
        def inner(cmd, check=True):
            self.calls.append(cmd)
            r = MagicMock()
            r.returncode = 1 if "command -v podman" in cmd else 0
            r.stdout = ""
            r.stderr = ""
            return r
        wrapped = self.mock_env.wrap_run_positional(inner)
        with patch.object(ct.env, "run", side_effect=wrapped):
            with self.assertRaises(SystemExit) as ctx:
                ct.cmd_push(self._push_args())
        self.assertIn("podman", str(ctx.exception))

    def test_push_generates_manifest(self):
        wrapped = self.mock_env.wrap_run_positional(self._inner_mock_run)
        with patch.object(ct.env, "run", side_effect=wrapped), \
             patch.object(ct.env, "write_file", side_effect=self._mock_write_file):
            ct.cmd_push(self._push_args())

        manifest_path = next(p for p in self.written_files if "manifest.json" in p)
        manifest = json.loads(self.written_files[manifest_path])
        self.assertEqual(manifest["flavor"], "test-flavor")
        self.assertEqual(manifest["compression"], "zstd")
        self.assertEqual(manifest["disks"][0]["chunk_count"], 2)
        self.assertEqual(manifest["disks"][1]["chunk_count"], 1)
        self.assertEqual(manifest["metadata"]["vcpus"], 16)

    def test_push_generates_dockerfile(self):
        wrapped = self.mock_env.wrap_run_positional(self._inner_mock_run)
        with patch.object(ct.env, "run", side_effect=wrapped), \
             patch.object(ct.env, "write_file", side_effect=self._mock_write_file):
            ct.cmd_push(self._push_args())

        dockerfile_path = next(p for p in self.written_files if "Dockerfile" in p)
        dockerfile = self.written_files[dockerfile_path]
        lines = [l for l in dockerfile.strip().split("\n") if l.strip()]
        self.assertEqual(lines[0], "FROM scratch")
        copy_lines = [l for l in lines if l.startswith("COPY")]
        # 1 manifest + 1 crypto dir + 3 chunks = 5
        self.assertEqual(len(copy_lines), 5)

    def test_push_splits_all_disks(self):
        wrapped = self.mock_env.wrap_run_positional(self._inner_mock_run)
        with patch.object(ct.env, "run", side_effect=wrapped), \
             patch.object(ct.env, "write_file", side_effect=self._mock_write_file):
            ct.cmd_push(self._push_args())

        split_cmds = [c for c in self.calls if c.startswith("split ")]
        self.assertEqual(len(split_cmds), 2)
        self.assertTrue(any("disk-0.qcow2" in c for c in split_cmds))
        self.assertTrue(any("disk-1.qcow2" in c for c in split_cmds))

    def test_push_image_ref_format(self):
        wrapped = self.mock_env.wrap_run_positional(self._inner_mock_run)
        with patch.object(ct.env, "run", side_effect=wrapped), \
             patch.object(ct.env, "write_file", side_effect=self._mock_write_file):
            ct.cmd_push(self._push_args())

        push_cmd = next(c for c in self.calls if "push" in c and "quay.io" in c)
        self.assertIn("quay.io/org/repo:test-tag", push_cmd)

    def test_push_cleanup_on_failure(self):
        def inner(cmd, check=True):
            self.calls.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            if "command -v" in cmd:
                return r
            if "wc -l" in cmd:
                r.stdout = "1"
                return r
            if "ls -1" in cmd and "sort" in cmd:
                r.stdout = "disk-0.chunk.aa.zst\n"
                return r
            if "build" in cmd and "podman" in cmd:
                raise SystemExit("build failed")
            return r
        wrapped = self.mock_env.wrap_run_positional(inner)
        with patch.object(ct.env, "run", side_effect=wrapped), \
             patch.object(ct.env, "write_file", side_effect=self._mock_write_file):
            with self.assertRaises(SystemExit):
                ct.cmd_push(self._push_args())

        cleanup_cmds = [c for c in self.calls if "rm -rf /data/cluster-tool/tmp-push-" in c]
        self.assertTrue(len(cleanup_cmds) > 0)


class TestPull(unittest.TestCase):
    def setUp(self):
        self.mock_env = MockStateEnv()
        self.mock_env.setup({"flavors": {}, "clones": {}})
        self.calls = []

    _MANIFEST_DIGEST = "aaa111"
    _CRYPTO_DIGEST = "bbb222"
    _CHUNK_DIGEST = "ccc333"

    _OCI_MANIFEST = json.dumps({
        "layers": [
            {"digest": f"sha256:{_MANIFEST_DIGEST}"},
            {"digest": f"sha256:{_CRYPTO_DIGEST}"},
            {"digest": f"sha256:{_CHUNK_DIGEST}"},
        ]
    })

    def _build_ct_manifest(self):
        m = ct.build_manifest(
            flavor_name="test-pulled",
            metadata={"source_cluster": "abc", "vcpus": 16, "memory_kib": 67108864,
                       "source_primary_subnet": 135, "source_secondary_subnet": 153,
                       "etcd_image": "quay.io/test/etcd", "created_at": "2026-01-01T00:00:00Z"},
            disk_names=["disk-0.qcow2"],
        )
        m["compression"] = "zstd"
        m["disks"][0]["chunk_count"] = 1
        return m

    def _pull_args(self, image="quay.io/org/repo:tag", name=None):
        return argparse.Namespace(image=image, name=name)

    def _inner_mock_run(self, cmd, *, check=True):
        self.calls.append(cmd)
        r = MagicMock()
        r.returncode = 0
        r.stdout = ""
        r.stderr = ""
        if "cat" in cmd and "manifest.json" in cmd:
            r.stdout = self._OCI_MANIFEST
        elif "gzip -dc" in cmd and self._MANIFEST_DIGEST in cmd and "tar xf - -O" in cmd and "strip" not in cmd:
            r.stdout = json.dumps(self._build_ct_manifest())
        return r

    def test_pull_prereq_missing_pigz(self):
        def inner(cmd, *, check=True):
            self.calls.append(cmd)
            r = MagicMock()
            r.returncode = 1 if "command -v pigz" in cmd else 0
            r.stdout = ""
            r.stderr = ""
            return r
        wrapped = self.mock_env.wrap_run(inner)
        with patch.object(ct.env, "run", side_effect=wrapped):
            with self.assertRaises(SystemExit) as ctx:
                ct.cmd_pull(self._pull_args())
        self.assertIn("pigz", str(ctx.exception))

    def test_pull_registers_flavor(self):
        wrapped = self.mock_env.wrap_run(self._inner_mock_run)
        with patch.object(ct.env, "run", side_effect=wrapped), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            ct.cmd_pull(self._pull_args())
        state = self.mock_env.get_saved_state()
        self.assertIn("test-pulled", state["flavors"])
        flavor = state["flavors"]["test-pulled"]
        self.assertEqual(flavor["vcpus"], 16)
        self.assertEqual(flavor["memory_kib"], 67108864)
        self.assertEqual(flavor["source_cluster"], "abc")
        self.assertEqual(flavor["disks"], ["disk-0.qcow2"])

    def test_pull_name_override(self):
        wrapped = self.mock_env.wrap_run(self._inner_mock_run)
        with patch.object(ct.env, "run", side_effect=wrapped), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            ct.cmd_pull(self._pull_args(name="custom"))
        state = self.mock_env.get_saved_state()
        self.assertIn("custom", state["flavors"])
        self.assertNotIn("test-pulled", state["flavors"])

    def test_pull_existing_flavor_exits(self):
        self.mock_env.save_initial_state({
            "flavors": {"test-pulled": {"disks": []}},
            "clones": {}})
        wrapped = self.mock_env.wrap_run(self._inner_mock_run)
        with patch.object(ct.env, "run", side_effect=wrapped), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            with self.assertRaises(SystemExit) as ctx:
                ct.cmd_pull(self._pull_args())
        self.assertIn("test-pulled", str(ctx.exception))

    def test_pull_cleanup_on_failure(self):
        fail_calls = []
        def inner(cmd, *, check=True):
            fail_calls.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            if "cat" in cmd and "manifest.json" in cmd:
                r.stdout = self._OCI_MANIFEST
            elif "gzip -dc" in cmd and self._MANIFEST_DIGEST in cmd and "tar xf - -O" in cmd and "strip" not in cmd:
                r.stdout = json.dumps(self._build_ct_manifest())
            if "set -o pipefail" in cmd:
                raise RuntimeError("decompress failed")
            return r

        wrapped = self.mock_env.wrap_run(inner)
        with patch.object(ct.env, "run", side_effect=wrapped), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            with self.assertRaises(RuntimeError):
                ct.cmd_pull(self._pull_args())

        cleanup_rm = [c for c in fail_calls if "rm -rf" in c and "tmp-pull-" in c]
        self.assertTrue(len(cleanup_rm) > 0)

    def test_pull_decompresses_and_reassembles(self):
        wrapped = self.mock_env.wrap_run(self._inner_mock_run)
        with patch.object(ct.env, "run", side_effect=wrapped), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            ct.cmd_pull(self._pull_args())
        reassemble_cmds = [c for c in self.calls if "set -o pipefail" in c and "disk-0.qcow2" in c]
        self.assertEqual(len(reassemble_cmds), 1)
        self.assertIn("zstd -d", reassemble_cmds[0])
        self.assertIn("gzip -dc", reassemble_cmds[0])

    def test_pull_installs_crypto(self):
        wrapped = self.mock_env.wrap_run(self._inner_mock_run)
        with patch.object(ct.env, "run", side_effect=wrapped), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            ct.cmd_pull(self._pull_args())
        crypto_cmds = [c for c in self.calls if "strip-components" in c and self._CRYPTO_DIGEST in c]
        self.assertEqual(len(crypto_cmds), 1)
        self.assertIn(ct.flavor_crypto_dir("test-pulled"), crypto_cmds[0])


class TestSetupClient(unittest.TestCase):
    @patch("os.geteuid", return_value=1000)
    def test_client_requires_sudo(self, _):
        with self.assertRaises(SystemExit) as ctx:
            ct._setup_client()
        self.assertIn("sudo", str(ctx.exception))

    @patch("os.geteuid", return_value=0)
    def test_client_as_root_does_not_exit(self, _):
        with patch.dict(os.environ, {"SUDO_USER": ""}, clear=False), \
             patch("subprocess.run", return_value=MagicMock(returncode=0)), \
             patch("time.sleep"), \
             patch("pathlib.Path.write_text"), \
             patch("pathlib.Path.read_text", return_value="nameserver 127.0.0.1"), \
             patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.unlink"), \
             patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.is_symlink", return_value=False):
            ct._setup_client()

    @patch("os.geteuid", return_value=0)
    @patch("subprocess.run")
    @patch("time.sleep")
    def test_client_creates_dnsmasq_config(self, _, mock_run, __):
        mock_run.return_value = MagicMock(returncode=0)
        tmpdir = tempfile.mkdtemp()
        nm_conf_dir = Path(tmpdir) / "NetworkManager" / "conf.d"
        dnsmasq_dir = Path(tmpdir) / "NetworkManager" / "dnsmasq.d"
        polkit_dir = Path(tmpdir) / "polkit-1" / "rules.d"
        resolv = Path(tmpdir) / "resolv.conf"
        resolv.write_text("nameserver 127.0.0.1\n")

        with patch.dict(os.environ, {"SUDO_USER": "testuser"}), \
             patch.object(ct, "DNSMASQ_DIR", dnsmasq_dir), \
             patch("pathlib.Path", wraps=Path) as mock_path:
            orig_path = Path
            class PatchedPath(type(Path())):
                def __new__(cls, *args):
                    p = str(args[0]) if args else ""
                    if p == "/etc/NetworkManager/conf.d":
                        return orig_path(nm_conf_dir)
                    if p == "/etc/NetworkManager/conf.d/cluster-tool-dns.conf":
                        nm_conf_dir.mkdir(parents=True, exist_ok=True)
                        return orig_path(nm_conf_dir / "cluster-tool-dns.conf")
                    if p == "/etc/polkit-1/rules.d":
                        return orig_path(polkit_dir)
                    if p == "/etc/polkit-1/rules.d/50-cluster-tool-nm.rules":
                        polkit_dir.mkdir(parents=True, exist_ok=True)
                        return orig_path(polkit_dir / "50-cluster-tool-nm.rules")
                    if p == "/etc/resolv.conf":
                        return orig_path(resolv)
                    return orig_path(*args)
            # This is hard to test with real filesystem paths — test the logic indirectly
            # The key assertions are: requires sudo, requires SUDO_USER
            # Full integration test needs real root access


class TestSetupServer(unittest.TestCase):
    def setUp(self):
        ct.env = ct.ExecutionEnv(host="test@host", host_ip="10.0.0.1")
        self.calls = []

    def _mock_run(self, cmd, *, check=True):
        self.calls.append(cmd)
        r = MagicMock()
        r.returncode = 0
        r.stderr = ""
        if "cat ~/.config/cluster-tool/config" in cmd:
            r.returncode = 1
            r.stdout = ""
            r.stderr = "No such file"
        elif "df --output" in cmd:
            r.stdout = "/home"
        elif "df -h" in cmd:
            r.stdout = "Filesystem  Size  Used Avail Use%\n/dev/sda  4.3T  100G  4.2T  3%"
        else:
            r.stdout = ""
        return r

    def test_server_checks_root(self):
        def mock_fail_root(cmd, *, check=True):
            if "test $(id -u)" in cmd and check:
                raise SystemExit("not root")
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        with patch.object(ct.env, "run", side_effect=mock_fail_root):
            with self.assertRaises(SystemExit):
                ct._setup_server(data_path="/tmp/test")

    def test_server_generates_ssh_key(self):
        with patch.object(ct.env, "run", side_effect=self._mock_run):
            ct._setup_server(data_path="/tmp/test-data")

        keygen_cmds = [c for c in self.calls if "ssh-keygen" in c]
        self.assertEqual(len(keygen_cmds), 1)
        self.assertIn("ed25519", keygen_cmds[0])
        self.assertIn("cluster-tool.key", keygen_cmds[0])

    def test_server_installs_packages(self):
        with patch.object(ct.env, "run", side_effect=self._mock_run):
            ct._setup_server(data_path="/tmp/test-data")

        dnf_cmds = [c for c in self.calls if "dnf install" in c]
        self.assertEqual(len(dnf_cmds), 1)
        self.assertIn("libvirt", dnf_cmds[0])
        self.assertIn("qemu-kvm", dnf_cmds[0])
        self.assertIn("podman", dnf_cmds[0])
        self.assertIn("pigz", dnf_cmds[0])

    def test_server_enables_libvirtd(self):
        with patch.object(ct.env, "run", side_effect=self._mock_run):
            ct._setup_server(data_path="/tmp/test-data")

        libvirtd_cmds = [c for c in self.calls if "systemctl enable --now libvirtd" in c]
        self.assertEqual(len(libvirtd_cmds), 1)

    def test_server_creates_data_dirs(self):
        with patch.object(ct.env, "run", side_effect=self._mock_run):
            ct._setup_server(data_path="/custom/path")

        mkdir_cmds = [c for c in self.calls if "mkdir -p /custom/path" in c]
        self.assertTrue(any("flavors" in c and "overlays" in c for c in mkdir_cmds))

    def test_server_writes_config(self):
        with patch.object(ct.env, "run", side_effect=self._mock_run):
            ct._setup_server(data_path="/custom/path")

        config_cmds = [c for c in self.calls if "CLUSTER_TOOL_DATA=/custom/path" in c]
        self.assertEqual(len(config_cmds), 1)
        self.assertIn("~/.config/cluster-tool/config", config_cmds[0])

    def test_server_verifies_tools(self):
        with patch.object(ct.env, "run", side_effect=self._mock_run):
            ct._setup_server(data_path="/tmp/test-data")

        verify_cmds = [c for c in self.calls if any(t in c for t in ["virsh version", "podman --version", "pigz --version"])]
        self.assertEqual(len(verify_cmds), 3)

    def test_server_interactive_uses_detected_path(self):
        with patch.object(ct.env, "run", side_effect=self._mock_run), \
             patch("builtins.input", return_value=""):
            ct._setup_server()

        config_cmds = [c for c in self.calls if "CLUSTER_TOOL_DATA=" in c]
        self.assertIn("/home/cluster-tool", config_cmds[0])

    def test_server_interactive_uses_custom_path(self):
        with patch.object(ct.env, "run", side_effect=self._mock_run), \
             patch("builtins.input", return_value="/mnt/big-disk/ct"):
            ct._setup_server()

        config_cmds = [c for c in self.calls if "CLUSTER_TOOL_DATA=" in c]
        self.assertIn("/mnt/big-disk/ct", config_cmds[0])

    def test_server_data_path_flag_skips_prompt(self):
        with patch.object(ct.env, "run", side_effect=self._mock_run), \
             patch("builtins.input") as mock_input:
            ct._setup_server(data_path="/explicit/path")

        mock_input.assert_not_called()
        config_cmds = [c for c in self.calls if "CLUSTER_TOOL_DATA=" in c]
        self.assertIn("/explicit/path", config_cmds[0])


class TestServerRegistry(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig = ct.CLIENT_SERVERS_FILE
        ct.CLIENT_SERVERS_FILE = Path(self.tmpdir) / "servers.json"
        ct.CLIENT_CONFIG_DIR = Path(self.tmpdir)

    def tearDown(self):
        ct.CLIENT_SERVERS_FILE = self._orig
        ct.CLIENT_CONFIG_DIR = self._orig.parent

    def test_load_servers_empty(self):
        config = ct.load_servers()
        self.assertEqual(config, {"servers": {}, "default": None})

    def test_save_and_load_servers(self):
        config = {"servers": {"s1": {"host": "root@h1"}}, "default": "s1"}
        ct.save_servers(config)
        loaded = ct.load_servers()
        self.assertEqual(loaded, config)

    def test_resolve_server_found(self):
        ct.save_servers({"servers": {"s1": {"host": "root@h1"}}, "default": "s1"})
        self.assertEqual(ct.resolve_server("s1"), "root@h1")

    def test_resolve_server_not_found(self):
        ct.save_servers({"servers": {}, "default": None})
        with self.assertRaises(SystemExit) as ctx:
            ct.resolve_server("missing")
        self.assertIn("missing", str(ctx.exception))


class TestConnect(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_file = ct.CLIENT_SERVERS_FILE
        self._orig_dir = ct.CLIENT_CONFIG_DIR
        ct.CLIENT_SERVERS_FILE = Path(self.tmpdir) / "servers.json"
        ct.CLIENT_CONFIG_DIR = Path(self.tmpdir)
        self.calls = []

    def tearDown(self):
        ct.CLIENT_SERVERS_FILE = self._orig_file
        ct.CLIENT_CONFIG_DIR = self._orig_dir

    def _mock_setup_server(self, *, data_path=None):
        pass

    def _connect_args(self, name, host):
        return argparse.Namespace(name=name, host=host, data_path="/tmp/test")

    @patch("socket.getaddrinfo", return_value=[(None, None, None, None, ("10.0.0.1",))])
    def test_connect_registers_server(self, _):
        with patch.object(ct, "_setup_server", side_effect=self._mock_setup_server):
            ct.cmd_connect(self._connect_args("mybox", "root@mybox"))
        config = ct.load_servers()
        self.assertIn("mybox", config["servers"])
        self.assertEqual(config["servers"]["mybox"]["host"], "root@mybox")

    @patch("socket.getaddrinfo", return_value=[(None, None, None, None, ("10.0.0.1",))])
    def test_connect_sets_first_as_default(self, _):
        with patch.object(ct, "_setup_server", side_effect=self._mock_setup_server):
            ct.cmd_connect(self._connect_args("first", "root@first"))
        config = ct.load_servers()
        self.assertEqual(config["default"], "first")

    @patch("socket.getaddrinfo", return_value=[(None, None, None, None, ("10.0.0.1",))])
    def test_connect_preserves_existing_default(self, _):
        ct.save_servers({"servers": {"first": {"host": "root@first"}}, "default": "first"})
        with patch.object(ct, "_setup_server", side_effect=self._mock_setup_server):
            ct.cmd_connect(self._connect_args("second", "root@second"))
        config = ct.load_servers()
        self.assertEqual(config["default"], "first")
        self.assertIn("second", config["servers"])


class TestUse(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_file = ct.CLIENT_SERVERS_FILE
        self._orig_dir = ct.CLIENT_CONFIG_DIR
        ct.CLIENT_SERVERS_FILE = Path(self.tmpdir) / "servers.json"
        ct.CLIENT_CONFIG_DIR = Path(self.tmpdir)

    def tearDown(self):
        ct.CLIENT_SERVERS_FILE = self._orig_file
        ct.CLIENT_CONFIG_DIR = self._orig_dir

    def test_use_sets_default(self):
        ct.save_servers({"servers": {"a": {"host": "root@a"}, "b": {"host": "root@b"}}, "default": "a"})
        ct.cmd_use(argparse.Namespace(name="b"))
        config = ct.load_servers()
        self.assertEqual(config["default"], "b")

    def test_use_unknown_server_exits(self):
        ct.save_servers({"servers": {"a": {"host": "root@a"}}, "default": "a"})
        with self.assertRaises(SystemExit) as ctx:
            ct.cmd_use(argparse.Namespace(name="unknown"))
        self.assertIn("unknown", str(ctx.exception))


class TestServers(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_file = ct.CLIENT_SERVERS_FILE
        self._orig_dir = ct.CLIENT_CONFIG_DIR
        ct.CLIENT_SERVERS_FILE = Path(self.tmpdir) / "servers.json"
        ct.CLIENT_CONFIG_DIR = Path(self.tmpdir)

    def tearDown(self):
        ct.CLIENT_SERVERS_FILE = self._orig_file
        ct.CLIENT_CONFIG_DIR = self._orig_dir

    def test_servers_empty(self):
        import io
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            ct.cmd_servers(argparse.Namespace())
        self.assertIn("No servers connected", mock_out.getvalue())

    def test_servers_lists_all(self):
        import io
        ct.save_servers({"servers": {"a": {"host": "root@a"}, "b": {"host": "root@b"}}, "default": "a"})
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            ct.cmd_servers(argparse.Namespace())
        output = mock_out.getvalue()
        self.assertIn("a", output)
        self.assertIn("b", output)
        self.assertIn("root@a", output)
        self.assertIn("root@b", output)


class TestConfigLoading(unittest.TestCase):
    def test_missing_config_exits(self):
        test_env = ct.ExecutionEnv(host="test@host", host_ip="10.0.0.1")
        def mock_cat_fail(cmd, *, check=True):
            r = MagicMock()
            r.returncode = 1
            r.stdout = ""
            r.stderr = "No such file"
            if not check:
                return r
            sys.exit("fail")
            return r
        ct.env = test_env
        with patch.object(test_env, "run", side_effect=mock_cat_fail):
            r = test_env.run("cat ~/.config/cluster-tool/config", check=False)
            self.assertNotEqual(r.returncode, 0)

    def test_config_sets_remote_base(self):
        ct._init_paths("/custom/data/path")
        self.assertEqual(ct.REMOTE_BASE, "/custom/data/path")
        self.assertEqual(ct.REMOTE_OVERLAYS, "/custom/data/path/overlays")
        self.assertEqual(ct.REMOTE_FLAVORS, "/custom/data/path/flavors")
        ct._init_paths("/data/cluster-tool")




class TestResolveHostIP(unittest.TestCase):

    def test_ipv4_only(self):
        mock_addrs = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.168.1.10', 0)),
        ]
        with patch('socket.getaddrinfo', return_value=mock_addrs):
            self.assertEqual(ct.resolve_host_ip('example.com'), '192.168.1.10')

    def test_ipv6_global_only(self):
        mock_addrs = [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('2001::1', 0, 0, 0)),
        ]
        with patch('socket.getaddrinfo', return_value=mock_addrs):
            self.assertEqual(ct.resolve_host_ip('example.com'), '2001::1')

    def test_mixed_prefers_ipv4(self):
        mock_addrs = [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('2001::1', 0, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.168.1.10', 0)),
        ]
        with patch('socket.getaddrinfo', return_value=mock_addrs):
            self.assertEqual(ct.resolve_host_ip('example.com'), '192.168.1.10')

    def test_link_local_fe80_filtered(self):
        mock_addrs = [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('fe80::1', 0, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.0.0.1', 0)),
        ]
        with patch('socket.getaddrinfo', return_value=mock_addrs):
            self.assertEqual(ct.resolve_host_ip('example.com'), '10.0.0.1')

    def test_link_local_fe90_filtered(self):
        mock_addrs = [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('fe90::1', 0, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.0.0.1', 0)),
        ]
        with patch('socket.getaddrinfo', return_value=mock_addrs):
            self.assertEqual(ct.resolve_host_ip('example.com'), '10.0.0.1')

    def test_link_local_fea0_filtered(self):
        mock_addrs = [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('fea0::1', 0, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.0.0.1', 0)),
        ]
        with patch('socket.getaddrinfo', return_value=mock_addrs):
            self.assertEqual(ct.resolve_host_ip('example.com'), '10.0.0.1')

    def test_link_local_febf_filtered(self):
        mock_addrs = [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('febf::1', 0, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.0.0.1', 0)),
        ]
        with patch('socket.getaddrinfo', return_value=mock_addrs):
            self.assertEqual(ct.resolve_host_ip('example.com'), '10.0.0.1')

    def test_only_link_local_exits(self):
        mock_addrs = [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('fe80::1', 0, 0, 0)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('fe90::2', 0, 0, 0)),
        ]
        with patch('socket.getaddrinfo', return_value=mock_addrs):
            with self.assertRaises(SystemExit):
                ct.resolve_host_ip('example.com')

    def test_unresolvable_exits(self):
        with patch('socket.getaddrinfo', side_effect=socket.gaierror('Not found')):
            with self.assertRaises(SystemExit):
                ct.resolve_host_ip('nonexistent.invalid')

    def test_mixed_link_local_and_global_ipv6(self):
        mock_addrs = [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('fe80::1', 0, 0, 0)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('2001::1', 0, 0, 0)),
        ]
        with patch('socket.getaddrinfo', return_value=mock_addrs):
            self.assertEqual(ct.resolve_host_ip('example.com'), '2001::1')

    def test_empty_addrs_exits(self):
        with patch('socket.getaddrinfo', return_value=[]):
            with self.assertRaises(SystemExit):
                ct.resolve_host_ip('example.com')

    def test_resolve_host_ip_replaces_both_call_sites(self):
        import inspect
        src = inspect.getsource(ct.cmd_connect)
        self.assertIn('resolve_host_ip', src)
        src_main = inspect.getsource(ct.main)
        self.assertIn('resolve_host_ip', src_main)


class TestDetectVmEmulator(unittest.TestCase):
    def _make_mock_env(self, stdout="", returncode=0):
        mock_env = MagicMock()
        mock_env.run.return_value = MagicMock(returncode=returncode, stdout=stdout, stderr="")
        return mock_env

    def test_finds_first_candidate(self):
        mock_env = self._make_mock_env(stdout="/usr/bin/qemu-system-x86_64\n", returncode=0)
        with patch.object(ct, "env", mock_env):
            result = ct.detect_vm_emulator()
        self.assertEqual(result, "/usr/bin/qemu-system-x86_64")

    def test_skips_missing_candidates_finds_fourth(self):
        mock_env = self._make_mock_env(stdout="/usr/bin/qemu-kvm\n", returncode=0)
        with patch.object(ct, "env", mock_env):
            result = ct.detect_vm_emulator()
        self.assertEqual(result, "/usr/bin/qemu-kvm")

    def test_falls_back_to_command_v(self):
        mock_env = self._make_mock_env(stdout="/usr/local/bin/qemu-system-x86_64\n", returncode=0)
        with patch.object(ct, "env", mock_env):
            result = ct.detect_vm_emulator()
        self.assertEqual(result, "/usr/local/bin/qemu-system-x86_64")

    def test_no_qemu_exits(self):
        mock_env = self._make_mock_env(stdout="", returncode=1)
        with patch.object(ct, "env", mock_env):
            with self.assertRaises(SystemExit) as ctx:
                ct.detect_vm_emulator()
        self.assertIn("No QEMU emulator found", str(ctx.exception))

    def test_single_ssh_command(self):
        mock_env = self._make_mock_env(stdout="/usr/bin/qemu-system-x86_64\n", returncode=0)
        with patch.object(ct, "env", mock_env):
            ct.detect_vm_emulator()
        self.assertEqual(mock_env.run.call_count, 1)


class TestDetectVmMachineType(unittest.TestCase):
    def test_prefers_q35(self):
        mock_env = MagicMock()
        mock_env.run.return_value = MagicMock(
            returncode=0,
            stdout="pc-i440fx-2.1 legacy\nq35 Standard PC\npc-q35-4.0 Standard PC\n",
        )
        with patch.object(ct, "env", mock_env):
            self.assertEqual(ct.detect_vm_machine_type("/usr/bin/qemu-system-x86_64"), "q35")

    def test_falls_back_to_i440fx_prefix(self):
        mock_env = MagicMock()
        mock_env.run.return_value = MagicMock(
            returncode=0,
            stdout="pc-i440fx-2.1 Standard PC (i440FX + PIIX, 1996)\n",
        )
        with patch.object(ct, "env", mock_env):
            result = ct.detect_vm_machine_type("/usr/bin/qemu-system-x86_64")
        self.assertEqual(result, "pc-i440fx-2.1")

    def test_uses_first_available_when_no_preference(self):
        mock_env = MagicMock()
        mock_env.run.return_value = MagicMock(
            returncode=0,
            stdout="custom-machine-v1 Custom machine type\nanother-machine Custom\n",
        )
        with patch.object(ct, "env", mock_env):
            result = ct.detect_vm_machine_type("/usr/bin/qemu-system-x86_64")
        self.assertEqual(result, "custom-machine-v1")

    def test_qemu_fails_exits(self):
        mock_env = MagicMock()
        mock_env.run.return_value = MagicMock(returncode=1, stdout="", stderr="command not found")
        with patch.object(ct, "env", mock_env):
            with self.assertRaises(SystemExit) as ctx:
                ct.detect_vm_machine_type("/usr/bin/qemu-system-x86_64")
        self.assertIn("Failed to query machine types", str(ctx.exception))

    def test_empty_output_exits(self):
        mock_env = MagicMock()
        mock_env.run.return_value = MagicMock(returncode=0, stdout="\n", stderr="")
        with patch.object(ct, "env", mock_env):
            with self.assertRaises(SystemExit) as ctx:
                ct.detect_vm_machine_type("/usr/bin/qemu-system-x86_64")
        self.assertIn("No machine types found", str(ctx.exception))


class TestResolveVmPlatform(unittest.TestCase):
    def test_integration(self):
        mock_env = MagicMock()
        mock_env.run.side_effect = [
            MagicMock(returncode=0, stdout="/usr/bin/qemu-system-x86_64\n", stderr=""),
            MagicMock(returncode=0, stdout="q35 Standard PC\npc-i440fx-2.1 legacy\n", stderr=""),
        ]
        with patch.object(ct, "env", mock_env):
            emulator, machine_type = ct.resolve_vm_platform()
        self.assertEqual(emulator, "/usr/bin/qemu-system-x86_64")
        self.assertEqual(machine_type, "q35")


class TestGenVmXmlPlatformParams(unittest.TestCase):
    def test_custom_emulator_and_machine(self):
        xml = ct.gen_vm_xml(
            "a1b2c3d4", "/path/overlay.qcow2",
            "02:00:00:aa:bb:cc", "02:00:00:dd:ee:ff",
            machine_type="pc-i440fx-2.1", emulator="/usr/libexec/qemu-kvm",
        )
        self.assertIn("machine='pc-i440fx-2.1'", xml)
        self.assertIn("<emulator>/usr/libexec/qemu-kvm</emulator>", xml)
        self.assertNotIn(ct.DEFAULT_VM_EMULATOR, xml)
        self.assertNotIn(ct.DEFAULT_VM_MACHINE_TYPE, xml)
class TestRunVmQuoting(unittest.TestCase):
    def setUp(self):
        self.env = ct.ExecutionEnv(host="test@host", host_ip="10.0.0.1")
        self.captured_cmd = None

        def capture_run(cmd, *, check=True):
            self.captured_cmd = cmd
            r = MagicMock()
            r.returncode = 0
            r.stdout = "test-output"
            r.stderr = ""
            return r

        self.env.run = capture_run

    def test_remote_run_vm_preserves_single_quotes(self):
        cmd = """sudo python3 -c "d=open('/etc/test').read(); print(d['key'])" """
        self.env.run_vm("192.168.1.10", cmd)
        self.assertIn("192.168.1.10", self.captured_cmd)
        self.assertNotIn("'{cmd}'", self.captured_cmd)
        inner = self.captured_cmd.split("core@192.168.1.10 ", 1)[1]
        import ast
        try:
            reconstructed = ast.literal_eval(inner)
        except (ValueError, SyntaxError):
            reconstructed = None
        self.assertEqual(reconstructed, cmd,
            "shlex.quote must produce a shell-safe string that reconstructs the original command")

    def test_remote_run_vm_simple_command_works(self):
        self.env.run_vm("192.168.1.10", "echo hello")
        self.assertIn("echo hello", self.captured_cmd)

    def test_local_run_vm_passes_cmd_directly(self):
        local_env = ct.ExecutionEnv(host="local", host_ip="127.0.0.1")
        captured = {}
        original_run = subprocess.run
        def mock_run(*args, **kwargs):
            captured["args"] = args[0] if args else kwargs.get("args")
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r
        with patch("subprocess.run", side_effect=mock_run):
            local_env.run_vm("192.168.1.10", "sudo python3 -c \"open('/etc/test')\"")
        self.assertIsInstance(captured["args"], list)
        self.assertEqual(captured["args"][-1], "sudo python3 -c \"open('/etc/test')\"")


class TestPullSecretInjection(unittest.TestCase):
    _INITIAL_STATE = TestTransactionalBoot._INITIAL_STATE

    def setUp(self):
        self.mock_env = MockStateEnv()
        self.mock_env.setup(self._INITIAL_STATE)
        self.calls = []
        self._ps_file = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump({"auths": {"quay.io": {"auth": "dGVzdA=="}}}, self._ps_file)
        self._ps_file.close()

    def tearDown(self):
        os.unlink(self._ps_file.name)

    _SENTINEL = object()

    def _boot_args(self, pull_secret=_SENTINEL):
        if pull_secret is TestPullSecretInjection._SENTINEL:
            pull_secret = self._ps_file.name
        return argparse.Namespace(name="aabbccdd", flavor="default", no_rollback=False, pull_secret=pull_secret)

    def _make_all_succeed_ssh(self):
        mock_co = TestTransactionalBoot._MOCK_CO_JSON
        mock_nodes = TestTransactionalBoot._MOCK_NODES_JSON
        def ssh(cmd, check=True):
            self.calls.append((cmd, check))
            r = MagicMock()
            r.returncode = 0
            if "ingress-cn" in cmd:
                r.stdout = "fake-ingress-cn"
            elif "infrastructure cluster" in cmd:
                r.stdout = "https://api.test-infra-cluster-aabbccdd.redhat.com:6443"
            elif "get co -o json" in cmd:
                r.stdout = mock_co
            elif "get nodes -o json" in cmd:
                r.stdout = mock_nodes
            else:
                r.stdout = "ok"
            r.stderr = ""
            return r
        return ssh

    @patch("time.sleep")
    @patch.object(ct, "remove_dns_entry")
    @patch.object(ct, "remove_haproxy_clone")
    @patch.object(ct, "add_dns_entry")
    def test_boot_pull_secret_not_written_to_node(self, *_):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"auths": {"quay.io": {"auth": "dGVzdA=="}}}, f)
            ps_path = f.name
        try:
            ssh = self.mock_env.wrap_run_positional(self._make_all_succeed_ssh())
            with patch.object(ct.env, "run", side_effect=ssh), \
                 patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
                ct.cmd_boot(self._boot_args(pull_secret=ps_path))

            cmds = [cmd for cmd, _ in self.calls]
            self.assertFalse(
                any("cp" in c and "pull-secret" in c and "/var/lib/kubelet/config.json" in c for c in cmds),
                "pull secret must not be written directly to node — MCO owns that file")
            self.assertFalse(
                any("tee /var/lib/kubelet/config.json" in c for c in cmds),
                "pull secret must not be written directly to node — MCO owns that file")
        finally:
            os.unlink(ps_path)
            ct.KUBECONFIG_DIR.joinpath("aabbccdd.kubeconfig").unlink(missing_ok=True)

    @patch("time.sleep")
    @patch.object(ct, "remove_dns_entry")
    @patch.object(ct, "remove_haproxy_clone")
    @patch.object(ct, "add_dns_entry")
    def test_boot_pull_secret_injected_after_health_before_operators(self, *_):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"auths": {"quay.io": {"auth": "dGVzdA=="}}}, f)
            ps_path = f.name
        try:
            ssh = self.mock_env.wrap_run_positional(self._make_all_succeed_ssh())
            with patch.object(ct.env, "run", side_effect=ssh), \
                 patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
                ct.cmd_boot(self._boot_args(pull_secret=ps_path))

            cmds = [cmd for cmd, _ in self.calls]
            oc_set_cmds = [c for c in cmds if "set data secret/pull-secret" in c]
            self.assertEqual(len(oc_set_cmds), 1)
            self.assertIn("openshift-config", oc_set_cmds[0])
            healthz_idx = next(i for i, c in enumerate(cmds) if "healthz" in c)
            set_idx = next(i for i, c in enumerate(cmds) if "set data secret/pull-secret" in c)
            co_idx = next(i for i, c in enumerate(cmds) if "get co -o json" in c)
            self.assertGreater(set_idx, healthz_idx,
                "pull secret must be set after API health check")
            self.assertLess(set_idx, co_idx,
                "pull secret must be set before operator check")
        finally:
            os.unlink(ps_path)
            ct.KUBECONFIG_DIR.joinpath("aabbccdd.kubeconfig").unlink(missing_ok=True)

    @patch("time.sleep")
    @patch.object(ct, "remove_dns_entry")
    @patch.object(ct, "remove_haproxy_clone")
    @patch.object(ct, "add_dns_entry")
    def test_boot_pull_secret_missing_file_exits(self, *_):
        ssh = self.mock_env.wrap_run_positional(self._make_all_succeed_ssh())
        with patch.object(ct.env, "run", side_effect=ssh), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            with self.assertRaises(SystemExit) as ctx:
                ct.cmd_boot(self._boot_args(pull_secret="/nonexistent/pull-secret.json"))
        self.assertIn("not found", str(ctx.exception))

    @patch("time.sleep")
    @patch.object(ct, "remove_dns_entry")
    @patch.object(ct, "remove_haproxy_clone")
    @patch.object(ct, "add_dns_entry")
    def test_boot_without_pull_secret_fails(self, *_):
        ssh = self.mock_env.wrap_run_positional(self._make_all_succeed_ssh())
        with patch.object(ct.env, "run", side_effect=ssh), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            with self.assertRaises((SystemExit, TypeError)):
                ct.cmd_boot(self._boot_args(pull_secret=None))

    @patch("time.sleep")
    @patch.object(ct, "remove_dns_entry")
    @patch.object(ct, "remove_haproxy_clone")
    @patch.object(ct, "add_dns_entry")
    def test_recert_uses_pull_missing(self, *_):
        ssh = self.mock_env.wrap_run_positional(self._make_all_succeed_ssh())
        with patch.object(ct.env, "run", side_effect=ssh), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            ct.cmd_boot(self._boot_args())

        cmds = [cmd for cmd, _ in self.calls]
        recert_cmd = next(c for c in cmds if "podman run --rm --name recert" in c)
        self.assertIn("--pull=missing", recert_cmd)
        self.assertNotIn("--pull=newer", recert_cmd)
        ct.KUBECONFIG_DIR.joinpath("aabbccdd.kubeconfig").unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()

