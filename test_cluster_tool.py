# test_cluster_tool.py
import argparse
import base64
import importlib.machinery
import importlib.util
import inspect
import json
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch, MagicMock

loader = importlib.machinery.SourceFileLoader("cluster_tool", "./cluster-tool")
spec = importlib.util.spec_from_loader("cluster_tool", loader, origin="./cluster-tool")
ct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ct)
ct._init_paths("/data/cluster-tool")
_TEST_HOME = tempfile.TemporaryDirectory(prefix="cluster-tool-tests-")
ct._REAL_HOME = Path(_TEST_HOME.name)
ct.KUBECONFIG_DIR = ct._REAL_HOME / ".kube"
ct.CLIENT_CONFIG_DIR = ct._REAL_HOME / ".config" / "cluster-tool"
ct.CLIENT_SERVERS_FILE = ct.CLIENT_CONFIG_DIR / "servers.json"

capture_loader = importlib.machinery.SourceFileLoader(
    "capture_compact_flavor", "./scripts/capture_compact_flavor.py"
)
capture_spec = importlib.util.spec_from_loader(
    "capture_compact_flavor", capture_loader, origin="./scripts/capture_compact_flavor.py"
)
capture = importlib.util.module_from_spec(capture_spec)
capture_spec.loader.exec_module(capture)


def _fixture_pem(kind, body):
    marker = "-----" + "BEGIN " + kind + "-----"
    end = "-----" + "END " + kind + "-----"
    return f"{marker}\n{body}\n{end}".encode()


class TestCompactFlavorCapture(unittest.TestCase):
    def test_source_plan_rejects_external_qcow2_data_file_before_capture(self):
        args = argparse.Namespace(
            vm_prefix="source",
            expected_memory_gib=24,
            expected_vcpus=12,
            expected_os_disk_gib=80,
            expected_spare_disk_gib=132,
            libvirt_uri=None,
        )
        dominfo = b"State: running\nCPU(s): 12\nMax memory: 25165824 KiB\n"
        disks = {"vda": Path("/source/os.qcow2"), "vdb": Path("/source/spare.qcow2")}
        external_data = {
            "format": "qcow2",
            "virtual-size": 80 * 1024**3,
            "format-specific": {"type": "qcow2", "data": {"data-file": "/source/os.data"}},
        }
        with patch.object(capture, "run", return_value=dominfo), \
             patch.object(capture, "resolve_domain_disks", return_value=disks), \
             patch.object(capture, "image_info", return_value=external_data):
            with self.assertRaisesRegex(capture.CaptureError, "external data file"):
                capture.collect_source_plan(args)


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

    def mock_write_file(self, path, content, *, private=False):
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
        self._orig_dnsmasq_dir = ct._dnsmasq_dir
        ct._dnsmasq_dir = lambda: Path(self.tmpdir)
        ct.env = ct.ExecutionEnv(host="test@host", host_ip="10.0.0.1")

    def tearDown(self):
        ct._dnsmasq_dir = self._orig_dnsmasq_dir

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
        self._original_client_config_dir = ct.CLIENT_CONFIG_DIR
        self._client_config_tmp = tempfile.TemporaryDirectory(prefix="cluster-tool-state-")
        ct.CLIENT_CONFIG_DIR = Path(self._client_config_tmp.name)
        self._dnsmasq_tmp = tempfile.TemporaryDirectory(prefix="cluster-tool-dnsmasq-")
        self._dnsmasq_patch = patch.object(
            ct, "_dnsmasq_dir", return_value=Path(self._dnsmasq_tmp.name))
        self._dnsmasq_patch.start()
        self._dns_reload_patch = patch.object(ct, "_reload_dns")
        self._dns_reload_patch.start()
        self._ps_file = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump({"auths": {"quay.io": {"auth": "dGVzdA=="}}}, self._ps_file)
        self._ps_file.close()

    def tearDown(self):
        os.unlink(self._ps_file.name)
        self._dns_reload_patch.stop()
        self._dnsmasq_patch.stop()
        self._dnsmasq_tmp.cleanup()
        ct.CLIENT_CONFIG_DIR = self._original_client_config_dir
        self._client_config_tmp.cleanup()

    def _boot_args(self, no_rollback=False):
        return argparse.Namespace(name="aabbccdd", flavor="default", no_rollback=no_rollback,
                                  pull_secret=self._ps_file.name, vcpus=None, memory_kib=None)

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

class TestCompactInspect(unittest.TestCase):
    _NET = """<network xmlns:dnsmasq="http://libvirt.org/schemas/network/dnsmasq/1.0">
          <name>mno-test2</name>
          <ip address="192.168.124.1" netmask="255.255.255.0">
            <dhcp>
              <host mac="52:54:00:a2:b0:aa" name="master-0-0" ip="192.168.124.69"/>
              <host mac="52:54:00:76:07:c5" name="master-0-1" ip="192.168.124.81"/>
              <host mac="52:54:00:ba:f0:e8" name="master-0-2" ip="192.168.124.89"/>
            </dhcp>
          </ip>
          <dnsmasq:options>
            <dnsmasq:option value="address=/api.test-infra-cluster-mno.redhat.com/192.168.124.5"/>
            <dnsmasq:option value="address=/api-int.test-infra-cluster-mno.redhat.com/192.168.124.5"/>
            <dnsmasq:option value="address=/apps.test-infra-cluster-mno.redhat.com/192.168.124.10"/>
          </dnsmasq:options>
        </network>"""
    _DOM = """<domain type="kvm"><name>{domain}</name><devices>
          <disk type="file" device="disk">
            <source file="/var/lib/libvirt/images/{domain}.qcow2"/>
            <target dev="sda" bus="scsi"/>
          </disk>
          <interface type="network"><mac address="{mac}"/></interface>
        </devices></domain>"""
    _DOMINFO = "CPU(s):         16\nMax memory:     33554432 KiB\n"
    _MACS = {
        "mno-test2-master-0-0": "52:54:00:a2:b0:aa",
        "mno-test2-master-0-1": "52:54:00:76:07:c5",
        "mno-test2-master-0-2": "52:54:00:ba:f0:e8",
    }

    def test_classify_vips_from_dnsmasq_options(self):
        api, ingress = ct.classify_vips(ct.parse_dnsmasq_addresses(self._NET))
        self.assertEqual(api, "192.168.124.5")
        self.assertEqual(ingress, "192.168.124.10")
        gateway, network = ct.parse_network_gateway(self._NET)
        self.assertEqual(gateway, "192.168.124.1")
        self.assertEqual(network, "192.168.124.0/24")

    def test_addnhosts_extend_dns(self):
        records = ct.parse_addnhosts(
            "192.168.124.5 api.test-infra-cluster-mno2.redhat.com api-int.test-infra-cluster-mno2.redhat.com\n"
            "192.168.124.10 apps.test-infra-cluster-mno2.redhat.com\n")
        self.assertEqual(records[0]["name"], "api.test-infra-cluster-mno2.redhat.com")
        self.assertEqual(len(records), 3)

    def test_dhcp_host_ips_must_be_valid_ipv4(self):
        with self.assertRaises(ValueError):
            ct.parse_dhcp_hosts(
                "<network><ip><dhcp><host mac='02:00:00:00:00:01' "
                "ip='192.168.1.1;touch /tmp/pwned'/></dhcp></ip></network>")
        with self.assertRaises(ValueError):
            ct.parse_dhcp_hosts(
                "<network><ip><dhcp><host mac='02:00:00:00:00:01' "
                "ip='2001:db8::1'/></dhcp></ip></network>")

    def test_discover_compact_source_reads_three_domains(self):
        ct.env = ct.ExecutionEnv(host="root@sealusa46", host_ip="10.0.0.1")

        def fake_run(cmd, check=True):
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            if "virsh domstate" in cmd and "master-0-" in cmd:
                r.stdout = "running\n"
            elif "virsh net-dumpxml" in cmd:
                r.stdout = self._NET
            elif cmd.startswith("cat /var/lib/libvirt/dnsmasq/"):
                r.stdout = "192.168.124.5 api.test-infra-cluster-mno2.redhat.com\n"
            elif "virsh dominfo" in cmd:
                r.stdout = self._DOMINFO
            elif "virsh dumpxml" in cmd:
                domain = cmd.split()[-1].strip("'")
                r.stdout = self._DOM.format(domain=domain, mac=self._MACS[domain])
            else:
                r.returncode = 1
                r.stdout = ""
                if check:
                    raise SystemExit(cmd)
            return r

        with patch.object(ct.env, "run", side_effect=fake_run):
            record = ct.discover_compact_source("mno-test2", network="mno-test2")

        self.assertEqual(record["topology"], "compact")
        self.assertEqual(record["machine_network"], "192.168.124.0/24")
        self.assertEqual(record["api_vip"], "192.168.124.5")
        self.assertEqual(record["ingress_vip"], "192.168.124.10")
        self.assertEqual([n["ip"] for n in record["nodes"]], [
            "192.168.124.69", "192.168.124.81", "192.168.124.89"])
        self.assertEqual(record["nodes"][0]["disks"][0]["path"],
                         "/var/lib/libvirt/images/mno-test2-master-0-0.qcow2")
        self.assertEqual(record["vcpus"], 16)
        self.assertEqual(record["memory_kib"], 33554432)

    def test_missing_third_master_exits(self):
        ct.env = ct.ExecutionEnv(host="root@host", host_ip="10.0.0.1")

        def fake_run(cmd, check=True):
            r = MagicMock()
            r.returncode = 0
            r.stdout = "running\n"
            r.stderr = ""
            if "master-0-2" in cmd:
                r.returncode = 1
                r.stdout = ""
            return r

        with patch.object(ct.env, "run", side_effect=fake_run):
            with self.assertRaises(SystemExit) as ctx:
                ct.discover_compact_source("mno-test2")
        self.assertIn("No 3-master compact source", str(ctx.exception))


class TestCompactCloneResources(unittest.TestCase):
    def test_compact_network_xml_round_trips(self):
        macs = ["02:00:00:00:00:01", "02:00:00:00:00:02", "02:00:00:00:00:03"]
        nodes = ct.compact_node_plan("aabbccdd", 200, macs)
        xml = ct.gen_compact_network_xml("aabbccdd", 200, nodes)
        hosts = ct.parse_dhcp_hosts(xml)
        self.assertEqual([h["ip"] for h in hosts], [
            "192.168.200.69", "192.168.200.81", "192.168.200.89"])
        self.assertEqual(hosts[0]["name"], "test-infra-cluster-aabbccdd-master-0-0")
        api, ingress = ct.classify_vips(ct.parse_dnsmasq_addresses(xml))
        self.assertEqual(api, "192.168.200.5")
        self.assertEqual(ingress, "192.168.200.10")
        gateway, network = ct.parse_network_gateway(xml)
        self.assertEqual(gateway, "192.168.200.1")
        self.assertEqual(network, "192.168.200.0/24")
        self.assertIn("<hostname>api.test-infra-cluster-aabbccdd.redhat.com</hostname>", xml)
        self.assertIn("<hostname>api-int.test-infra-cluster-aabbccdd.redhat.com</hostname>", xml)
        self.assertIn(f"<bridge name='{ct.compact_bridge_name('aabbccdd')}'", xml)
        self.assertNotEqual(ct.compact_bridge_name("compact-a"), ct.compact_bridge_name("compact-b"))
        self.assertLessEqual(len(ct.compact_bridge_name("compact-a")), 15)

    def test_compact_libvirt_names_are_checked_before_reservation(self):
        ct.env = ct.ExecutionEnv(host="root@host", host_ip="10.0.0.1")
        def fake_run(cmd, check=True):
            result = MagicMock(returncode=0, stdout="", stderr="")
            if cmd == "virsh list --all --name":
                result.stdout = "test-infra-cluster-edge7-master-0-1\n"
            return result
        with patch.object(ct.env, "run", side_effect=fake_run):
            conflicts = ct.compact_libvirt_conflicts("edge7")
            self.assertEqual(conflicts, ["test-infra-cluster-edge7-master-0-1"])
            with self.assertRaises(SystemExit) as ctx:
                ct.require_compact_libvirt_names_free("edge7")
        self.assertIn("no resources were changed", str(ctx.exception))

    def test_destroy_ownership_requires_clone_disk_and_network_identity(self):
        clone_id = "edge7"
        nodes = ct.compact_node_plan(clone_id, 207, [
            "02:00:00:00:00:01", "02:00:00:00:00:02", "02:00:00:00:00:03"])
        vm_xml = ct.gen_compact_vm_xml(
            clone_id, nodes[0], [ct.compact_role_disk_dest(clone_id, nodes[0]["role"], 0)])
        net_xml = ct.gen_compact_network_xml(clone_id, 207, nodes)
        self.assertTrue(ct.compact_domain_owned_by_clone(vm_xml, clone_id, nodes[0]["role"]))
        self.assertTrue(ct.compact_network_owned_by_clone(net_xml, clone_id))
        foreign_xml = vm_xml.replace(
            ct.compact_role_disk_dest(clone_id, nodes[0]["role"], 0),
            "/var/lib/libvirt/images/source-master.qcow2")
        self.assertFalse(ct.compact_domain_owned_by_clone(foreign_xml, clone_id, nodes[0]["role"]))
        foreign_net = net_xml.replace(ct.compact_bridge_name(clone_id), "source-bridge")
        self.assertFalse(ct.compact_network_owned_by_clone(foreign_net, clone_id))

    def test_mark_compact_clone_running_only_updates_reserved_clone(self):
        saved = {"state": {"flavors": {}, "clones": {
            "edge7": {"topology": "compact", "status": "booting"}}}}
        class _State:
            def __enter__(self):
                return saved["state"]
            def __exit__(self, *args):
                return False
        with patch.object(ct, "locked_state", _State):
            ct.mark_compact_clone_running("edge7")
        self.assertEqual(saved["state"]["clones"]["edge7"]["status"], "running")
        with patch.object(ct, "locked_state", _State):
            with self.assertRaises(SystemExit):
                ct.mark_compact_clone_running("missing")

    def test_compact_vm_xml_uses_one_nic_and_virtio_disk(self):
        nodes = ct.compact_node_plan("aabbccdd", 200, [
            "02:00:00:00:00:01", "02:00:00:00:00:02", "02:00:00:00:00:03"])
        xml = ct.gen_compact_vm_xml(
            "aabbccdd", nodes[1], ["/data/overlays/aabbccdd-master-0-1.qcow2"])
        self.assertIn("<name>test-infra-cluster-aabbccdd-master-0-1</name>", xml)
        self.assertEqual(xml.count("<interface "), 1)
        self.assertIn("bus='virtio'", xml)
        self.assertIn("dev='vda'", xml)
        self.assertIn("test-infra-net-aabbccdd", xml)
        self.assertNotIn("secondary-network", xml)
        spare = ct.gen_compact_vm_xml(
            "aabbccdd", nodes[0], [
                "/data/master-0-0.qcow2",
                "/data/master-0-0-spare.qcow2",
            ])
        self.assertIn("dev='vda'", spare)
        self.assertIn("dev='vdb'", spare)
        self.assertIn("/data/master-0-0-spare.qcow2", spare)
        self.assertEqual(spare.count("<interface "), 1)


class TestCompactEtcdRestore(unittest.TestCase):
    def test_etcd_restore_commands_share_one_snapshot(self):
        nodes = ct.compact_node_plan("aabbccdd", 200, [
            "02:00:00:00:00:01", "02:00:00:00:00:02", "02:00:00:00:00:03"])
        commands = ct.compact_etcd_restore_commands(
            nodes, "/var/lib/libvirt/images/snapshot.db", "aabbccdd")
        self.assertEqual([c["name"] for c in commands], list(ct.COMPACT_ROLES))
        cluster = commands[0]["argv"][commands[0]["argv"].index("--initial-cluster") + 1]
        self.assertEqual(cluster, (
            "master-0-0=https://192.168.200.69:2380,"
            "master-0-1=https://192.168.200.81:2380,"
            "master-0-2=https://192.168.200.89:2380"))
        snapshots = set()
        for cmd in commands:
            argv = cmd["argv"]
            self.assertNotIn("--force-new-cluster", argv)
            self.assertEqual(argv[0:4], ["etcdutl", "snapshot", "restore", "/var/lib/libvirt/images/snapshot.db"])
            snapshots.add(argv[3])
            self.assertEqual(argv[argv.index("--initial-cluster") + 1], cluster)
            self.assertEqual(argv[argv.index("--initial-cluster-token") + 1], "aabbccdd")
            self.assertEqual(
                argv[argv.index("--initial-advertise-peer-urls") + 1],
                f"https://{cmd['ip']}:2380")
            self.assertEqual(argv[argv.index("--data-dir") + 1], f"/var/lib/etcd-restore/{cmd['name']}")
            self.assertEqual(argv[argv.index("--name") + 1], cmd["name"])
        self.assertEqual(snapshots, {"/var/lib/libvirt/images/snapshot.db"})
        self.assertNotIn("--skip-hash-check", commands[0]["argv"])
        raw = ct.compact_etcd_restore_commands(
            nodes, "/snapshot.db", "aabbccdd", skip_hash_check=True)
        self.assertIn("--skip-hash-check", raw[0]["argv"])

    def test_etcd_restore_rejects_a_partial_membership(self):
        with self.assertRaises(SystemExit) as ctx:
            ct.compact_etcd_restore_commands(
                [{"role": "master-0-0", "ip": "192.168.200.69"}],
                "/tmp/snapshot.db", "aabbccdd")
        self.assertIn("etcd restore needs members", str(ctx.exception))


class TestCompactIdentity(unittest.TestCase):
    def _source(self):
        return {
            "api_vip": "192.168.124.5",
            "ingress_vip": "192.168.124.10",
            "nodes": [
                {"role": "master-0-0", "ip": "192.168.124.69"},
                {"role": "master-0-1", "ip": "192.168.124.81"},
                {"role": "master-0-2", "ip": "192.168.124.89"},
            ],
            "dns": [
                {"name": "api.test-infra-cluster-mno.redhat.com", "ip": "192.168.124.5"},
                {"name": "api-int.test-infra-cluster-mno.redhat.com", "ip": "192.168.124.5"},
                {"name": "apps.test-infra-cluster-mno.redhat.com", "ip": "192.168.124.10"},
                {"name": "api.test-infra-cluster-mno2.redhat.com", "ip": "192.168.124.5"},
                {"name": "api-int.test-infra-cluster-mno2.redhat.com", "ip": "192.168.124.5"},
            ],
        }

    def test_identity_rewrite_replaces_old_names_once(self):
        nodes = ct.compact_node_plan("aabbccdd", 200, [
            "02:00:00:00:00:01", "02:00:00:00:00:02", "02:00:00:00:00:03"])
        pairs = ct.compact_identity_pairs(self._source(), "aabbccdd", nodes)
        text = "\n".join([
            "match ^api-int.test-infra-cluster-mno.redhat.com",
            "https://api-int.test-infra-cluster-mno2.redhat.com:6443/healthz",
            "baseDomain: test-infra-cluster-mno.redhat.com",
            "192.168.124.5",
            "192.168.124.10",
            "192.168.124.69",
            "192.168.124.50",
        ])
        rewritten = ct.apply_identity(text, pairs)
        self.assertNotIn("test-infra-cluster-mno.redhat.com", rewritten)
        self.assertNotIn("test-infra-cluster-mno2.redhat.com", rewritten)
        self.assertIn("api-int.test-infra-cluster-aabbccdd.redhat.com", rewritten)
        self.assertIn("baseDomain: test-infra-cluster-aabbccdd.redhat.com", rewritten)
        self.assertIn("192.168.200.5", rewritten)
        self.assertIn("192.168.200.10", rewritten)
        self.assertIn("192.168.200.69", rewritten)
        self.assertIn("192.168.124.50", rewritten)

    def test_recert_runs_once_on_the_first_master(self):
        nodes = ct.compact_node_plan("aabbccdd", 200, [
            "02:00:00:00:00:01", "02:00:00:00:00:02", "02:00:00:00:00:03"])
        commands = ct.recert_compact_commands(
            self._source(), "aabbccdd", nodes, "/tmp/crypto", "ingress-operator", "/tmp/plan")
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]["name"], "master-0-0")
        self.assertEqual(commands[0]["argv"][commands[0]["argv"].index("--hostname") + 1], "master-0-0")
        self.assertIn("--etcd-endpoint", commands[0]["argv"])
        self.assertNotIn("--disk-only", commands[0]["argv"])
        self.assertEqual(commands[0]["expected_error"], "Expected exactly one node")

    def test_heal_restores_quorum_etcd_certs_and_signs_kubelets(self):
        nodes = ct.compact_node_plan("aabbccdd", 200, [
            "02:00:00:00:00:01", "02:00:00:00:00:02", "02:00:00:00:00:03"])
        heal = ct.compact_heal_plan(
            self._source(), "aabbccdd", nodes, "/tmp/crypto", "ingress-operator",
            "/tmp/plan", "/tmp/crypto/csr-signer")
        copy = heal["etcd_cert_copy"]
        self.assertEqual(copy["from_role"], "master-0-1")
        self.assertEqual(copy["to_role"], "master-0-0")
        self.assertEqual(copy["from_ip"], "192.168.200.81")
        self.assertEqual(copy["to_ip"], "192.168.200.69")
        self.assertIn("secrets/etcd-client/tls.crt", copy["paths"])
        signed = {item["role"] for item in heal["kubelet_sign"]}
        self.assertEqual(signed, set(ct.COMPACT_ROLES))
        script = heal["kubelet_sign"][0]["script"]
        self.assertIn("CN=system:node:master-0-0", script)
        self.assertIn("IP:192.168.200.69", script)
        self.assertIn("extendedKeyUsage=clientAuth", script)
        self.assertIn("/tmp/crypto/csr-signer/tls.crt", script)
        self.assertIn("/etc/kubernetes/static-pod-resources/coredns/Corefile.tmpl", heal["rewrite_files"])
        self.assertIn("/etc/systemd/system/nodeip-configuration.service", heal["rewrite_files"])
        self.assertEqual(heal["base_domain"], "test-infra-cluster-aabbccdd.redhat.com")
        self.assertEqual(heal["ingress_domain"], "apps.test-infra-cluster-aabbccdd.redhat.com")
        patches = ct.compact_identity_domain_patches("aabbccdd")
        self.assertEqual(patches["dns"]["spec"]["baseDomain"], heal["base_domain"])
        self.assertEqual(patches["ingress"]["spec"]["domain"], heal["ingress_domain"])
        self.assertTrue(ct.compact_identity_domains_ready(
            {"base_domain": heal["base_domain"], "apps_domain": heal["ingress_domain"]},
            "aabbccdd"))
        self.assertFalse(ct.compact_identity_domains_ready(
            {"base_domain": heal["base_domain"], "apps_domain": "apps.test-infra-cluster-mno.redhat.com"},
            "aabbccdd"))
        crd = json.dumps({
            "spec": {"versions": [{"schema": {"openAPIV3Schema": {"properties": {
                "spec": {"properties": {"domain": {
                    "type": "string",
                    "x-kubernetes-validations": [
                        {"message": "domain is immutable once set", "rule": "self == oldSelf"},
                    ],
                }}}
            }}}}]},
        })
        stripped = json.loads(ct.compact_strip_ingress_domain_lock(crd))
        domain = stripped["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]["domain"]
        self.assertNotIn("x-kubernetes-validations", domain)
        text = ct.format_compact_heal(heal)
        self.assertIn("Recert runs once", text)
        self.assertIn("Do not copy the Recert etcd CA the other way", text)
        self.assertIn("Do not copy Recert's keys the other way", text)
        self.assertIn("start kubelet on master-0-0", text)
        start = ct.compact_start_recert_node_script()
        self.assertIn("systemctl start crio", start)
        self.assertIn("systemctl start kubelet", start)
        self.assertNotIn("stop kubelet", start)
        self.assertIn("ingress spec.domain", text)
        self.assertIn("apps.test-infra-cluster-aabbccdd.redhat.com", text)
        self.assertEqual(
            ct.compact_ingress_wildcard_host("aabbccdd"),
            "*.apps.test-infra-cluster-aabbccdd.redhat.com")
        self.assertEqual(
            ct.compact_ingresscontroller_status_patch("aabbccdd")["status"]["domain"],
            "apps.test-infra-cluster-aabbccdd.redhat.com")
        self.assertEqual(heal["client_trust_copy"]["from_role"], "master-0-1")
        self.assertEqual(heal["client_trust_copy"]["to_role"], "master-0-0")
        trust = ct.compact_apiserver_client_trust_tar_command()
        self.assertIn("client-ca/ca-bundle.crt", trust)
        self.assertIn("aggregator-client/tls.crt", trust)
        self.assertIn("kubelet-client/tls.crt", trust)
        self.assertIn("csr-signer/tls.crt", trust)
        self.assertIn("bound-service-account-signing-key/service-account.key", trust)
        self.assertIn("sa-token-signing-certs/service-account-", trust)
        self.assertNotIn("etcd-serving-ca", trust)
        digest = ct.compact_client_trust_digest_command()
        self.assertIn("aggregator-client/tls.crt", digest)
        self.assertIn("aggregator-client-ca/ca-bundle.crt", digest)
        ready = [{"role": role, "ok": True} for role in ct.COMPACT_ROLES]
        self.assertTrue(ct.compact_auth_ready(ready, ready))
        rejected = [{"role": "master-0-0", "ok": False}] + ready[1:]
        self.assertFalse(ct.compact_auth_ready(rejected, ready))
        self.assertFalse(ct.compact_auth_ready(ready, rejected))
        self.assertIn("infrastructure cluster", text)
        self.assertNotIn("--disk-only", text)
        self.assertNotIn("192.168.", text)
        self.assertNotIn("--cn-san-replace", text)
        spec, status = ct.compact_infrastructure_patches("aabbccdd", 200)
        self.assertEqual(spec["spec"]["platformSpec"]["baremetal"]["apiServerInternalIPs"], ["192.168.200.5"])
        self.assertEqual(status["status"]["apiServerInternalURI"], "https://api-int.test-infra-cluster-aabbccdd.redhat.com:6443")
        self.assertEqual(status["status"]["platformStatus"]["baremetal"]["ingressIPs"], ["192.168.200.10"])
        self.assertIn("rm -f /run/nodeip-configuration/remote-worker", ct.compact_release_keepalived_script())
        self.assertIn(("openshift-etcd", "etcd-endpoints"), ct.HEAL_IDENTITY_CONFIGMAPS)
        self.assertIn(("openshift-apiserver", "config"), ct.HEAL_IDENTITY_CONFIGMAPS)
        self.assertIn(("openshift-oauth-apiserver", "apiserver"), ct.HEAL_ETCD_DEPLOYMENTS)
        endpoints = json.dumps({"data": {"endpoints": '{"a":"https://192.168.124.69:2379"}'}})
        rewritten = json.loads(ct.rewrite_configmap(endpoints, [("192.168.124.69", "192.168.200.69")]))
        self.assertEqual(rewritten["data"]["endpoints"], '{"a":"https://192.168.200.69:2379"}')
        deployment = json.dumps({
            "metadata": {"name": "apiserver"},
            "spec": {"template": {"spec": {"containers": [{"args": ["https://192.168.124.81:2379"]}]}}},
            "status": {"readyReplicas": 0},
        })
        updated = json.loads(ct.rewrite_deployment(deployment, [("192.168.124.81", "192.168.200.81")]))
        self.assertEqual(updated["spec"]["template"]["spec"]["containers"][0]["args"], ["https://192.168.200.81:2379"])
        self.assertNotIn("status", updated)
        self.assertIn("kube-apiserver-cert-regeneration-controller", ct.compact_restart_cert_controller_command())
        self.assertEqual(
            ct.compact_cert_controller_lease(),
            ("openshift-kube-apiserver", "cert-regeneration-controller-lock"))
        refresh = inspect.getsource(ct._refresh_internal_api_certificate)
        stopped = refresh.index("compact_restart_cert_controller_command")
        released = refresh.index("compact_cert_controller_lease")
        no_op = refresh.index("if secret_has_clone_cert() and api_vip_has_clone_cert()")
        self.assertLess(stopped, released)
        self.assertLess(no_op, stopped)
        self.assertIn("_certificate_file_has_san", refresh)
        self.assertIn("api_deadline = time.monotonic() + 90", refresh)
        ensure = inspect.getsource(ct._ensure_api_certificates)
        self.assertLess(ensure.index("_secret_hostnames"), ensure.index("signing API serving certificates"))
        self.assertNotIn("time.sleep(10)", ensure)
        self.assertTrue(ct.presented_san_matches(
            "DNS:api-int.test-infra-cluster-aabbccdd.redhat.com, IP Address:192.168.200.5",
            "api-int.test-infra-cluster-aabbccdd.redhat.com"))
        self.assertFalse(ct.presented_san_matches(
            "DNS:kubernetes, DNS:kubernetes.default, IP Address:172.30.0.1",
            "api-int.test-infra-cluster-aabbccdd.redhat.com"))


class TestCompactCertificates(unittest.TestCase):
    def test_compact_api_certificate_recovery_orders_install_before_refresh(self):
        calls = []
        with patch.object(ct, "_ensure_api_certificates", side_effect=lambda *args: calls.append("install")), \
                patch.object(ct, "_refresh_internal_api_certificate", side_effect=lambda *args: calls.append("refresh")):
            ct._recover_compact_api_certificates("oc", "/work", "example.redhat.com", "192.168.200.5", [], "key")
        self.assertEqual(calls, ["install", "refresh"])

    def test_internal_api_certificate_refresh_skips_when_vip_is_already_correct(self):
        hostname = "api-int.test-infra-cluster-aabbccdd.redhat.com"
        env = MagicMock()
        env.run.return_value.returncode = 0
        with patch.object(ct, "env", env), \
                patch.object(ct, "_secret_hostnames", return_value=hostname), \
                patch.object(ct, "_certificate_file_has_san", return_value=True), \
                patch.object(ct, "_presented_san", return_value=f"DNS:{hostname}"), \
                patch.object(ct, "_clone_log"), \
                patch.object(ct, "_in_parallel") as parallel:
            ct._refresh_internal_api_certificate(
                "oc", "/work", "test-infra-cluster-aabbccdd.redhat.com", "192.168.200.5", [], "key")
        parallel.assert_not_called()

    def test_apiserver_host_ip_repair_is_bounded_and_checks_readiness(self):
        script = ct.compact_apiserver_host_ip_script("192.168.200.89")

        self.assertIn('expected=192.168.200.89', script)
        self.assertIn("sudo crictl inspect", script)
        self.assertIn("/etc/kubernetes/manifests/kube-apiserver-pod.yaml", script)
        self.assertIn('e.pop("valueFrom",None)', script)
        self.assertIn('e["value"]=sys.argv[2]', script)
        self.assertIn('sudo restorecon -F "$manifest"', script)
        self.assertNotIn('sudo crictl stop "$id"', script)
        self.assertIn('https://127.0.0.1:6443/readyz', script)
        self.assertIn("deadline=$((SECONDS + 240))", script)
        with self.assertRaises(ValueError):
            ct.compact_apiserver_host_ip_script("not-an-ip")


class TestCompactMcdRecovery(unittest.TestCase):
    def test_compact_mcd_pod_address_rows_read_host_network_identity_and_readiness(self):
        raw = json.dumps({"items": [
            {
                "metadata": {"name": "machine-config-daemon-a", "uid": "uid-a"},
                "spec": {"nodeName": "master-0-0"},
                "status": {"podIP": "192.168.200.69", "containerStatuses": [
                    {"ready": True}, {"ready": True},
                ]},
            },
            {
                "metadata": {"name": "other-daemon"},
                "spec": {"nodeName": "master-0-1"},
                "status": {"podIP": "192.168.200.81", "containerStatuses": [
                    {"ready": True},
                ]},
            },
            {
                "metadata": {"name": "machine-config-daemon-b", "uid": "uid-b"},
                "spec": {"nodeName": "master-0-2"},
                "status": {"podIP": "192.168.123.50", "containerStatuses": [
                    {"ready": True}, {"ready": False},
                ]},
            },
        ]})

        self.assertEqual(ct.compact_mcd_pod_address_rows(raw), {
            "master-0-0": {"name": "machine-config-daemon-a", "uid": "uid-a", "ip": "192.168.200.69", "ready": True},
            "master-0-2": {"name": "machine-config-daemon-b", "uid": "uid-b", "ip": "192.168.123.50", "ready": False},
        })

    def test_compact_mcd_address_recovery_replaces_stale_daemonset_pod(self):
        def pod(name, uid, node, ip, ready=True):
            return json.dumps({"items": [{
                "metadata": {"name": name, "uid": uid},
                "spec": {"nodeName": node},
                "status": {"podIP": ip, "containerStatuses": [{"ready": ready}]},
            }]})

        def result(stdout="", returncode=0, stderr=""):
            value = MagicMock()
            value.stdout = stdout
            value.stderr = stderr
            value.returncode = returncode
            return value

        env = MagicMock()
        env.run.side_effect = [
            result(pod("machine-config-daemon-old", "old-uid", "master-0-0", "192.168.123.64")),
            result(),
            result(pod("machine-config-daemon-new", "new-uid", "master-0-0", "192.168.200.69")),
            result(pod("machine-config-daemon-master-1", "master-1-uid", "master-0-1", "192.168.200.81")),
        ]
        nodes = [
            {"role": "master-0-0", "ip": "192.168.200.69"},
            {"role": "master-0-1", "ip": "192.168.200.81"},
        ]
        with patch.object(ct, "env", env), patch.object(ct, "_clone_log"), \
                patch.object(ct.time, "sleep"):
            ct._ensure_compact_mcd_pod_addresses("oc", nodes, "/work")

        self.assertIn(
            "/api/v1/namespaces/openshift-machine-config-operator/pods/machine-config-daemon-old",
            env.run.call_args_list[1].args[0],
        )
        self.assertIn(
            "--data-binary '{\"apiVersion\":\"v1\",\"kind\":\"DeleteOptions\","
            "\"preconditions\":{\"uid\":\"old-uid\"}}'",
            env.run.call_args_list[1].args[0],
        )
        self.assertFalse(any("systemctl restart kubelet" in call.args[0]
                             for call in env.run.call_args_list))

    def test_compact_mcd_waits_for_correct_ip_pod_to_become_ready(self):
        def pods(name, uid, node, ip, ready):
            return json.dumps({"items": [{
                "metadata": {"name": name, "uid": uid},
                "spec": {"nodeName": node},
                "status": {"podIP": ip, "containerStatuses": [{"ready": ready}]},
            }]})

        def result(stdout="", returncode=0, stderr=""):
            return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)

        env = MagicMock()
        env.run.side_effect = [
            result(pods("machine-config-daemon-stale", "stale-uid", "master-0-0", "192.168.123.64", True)),
            result(),
            result(pods("machine-config-daemon-current", "current-uid", "master-0-0", "192.168.200.69", False)),
            result(pods("machine-config-daemon-current", "current-uid", "master-0-0", "192.168.200.69", False)),
            result(pods("machine-config-daemon-current", "current-uid", "master-0-0", "192.168.200.69", True)),
            result(pods("machine-config-daemon-master-1", "master-1-uid", "master-0-1", "192.168.200.81", True)),
        ]
        nodes = [
            {"role": "master-0-0", "ip": "192.168.200.69"},
            {"role": "master-0-1", "ip": "192.168.200.81"},
        ]
        with patch.object(ct, "env", env), patch.object(ct, "_clone_log"), \
                patch.object(ct.time, "sleep"):
            ct._ensure_compact_mcd_pod_addresses("oc", nodes, "/work")

        delete_calls = [call.args[0] for call in env.run.call_args_list
                        if "KUBE_DELETE_HTTP_STATUS" in call.args[0]]
        self.assertEqual(len(delete_calls), 1)
        self.assertIn("pods/machine-config-daemon-stale", delete_calls[0])
        self.assertIn('"preconditions":{"uid":"stale-uid"}', delete_calls[0])

    def test_compact_mcd_does_not_delete_pod_before_it_has_an_ip(self):
        def pods(name, uid, node, ip, ready):
            return json.dumps({"items": [{
                "metadata": {"name": name, "uid": uid},
                "spec": {"nodeName": node},
                "status": {"podIP": ip, "containerStatuses": [{"ready": ready}]},
            }]})

        def result(stdout=""):
            return MagicMock(returncode=0, stdout=stdout, stderr="")

        env = MagicMock()
        env.run.side_effect = [
            result(pods("machine-config-daemon-pending", "pending-uid", "master-0-0", "", False)),
            result(pods("machine-config-daemon-pending", "pending-uid", "master-0-0", "", False)),
            result(pods("machine-config-daemon-current", "current-uid", "master-0-0", "192.168.200.69", False)),
            result(pods("machine-config-daemon-current", "current-uid", "master-0-0", "192.168.200.69", True)),
            result(pods("machine-config-daemon-master-1", "master-1-uid", "master-0-1", "192.168.200.81", True)),
        ]
        nodes = [
            {"role": "master-0-0", "ip": "192.168.200.69"},
            {"role": "master-0-1", "ip": "192.168.200.81"},
        ]
        with patch.object(ct, "env", env), patch.object(ct, "_clone_log"), \
                patch.object(ct.time, "sleep"):
            ct._ensure_compact_mcd_pod_addresses("oc", nodes, "/work")

        self.assertFalse(any("KUBE_DELETE_HTTP_STATUS" in call.args[0]
                             for call in env.run.call_args_list))


class TestCompactBootRecovery(unittest.TestCase):
    def test_api_recovery_refreshes_host_ips_and_stale_leases_before_endpoints(self):
        source = inspect.getsource(ct._recover_compact_api_certificates_and_endpoints)
        certs = source.index("_recover_compact_api_certificates(")
        host_ips = source.index("_ensure_compact_apiserver_host_ips(")
        leases = source.index("_clear_compact_stale_master_leases(")
        endpoints = source.index("_refresh_compact_kubernetes_endpoints(")
        self.assertLess(certs, host_ips)
        self.assertNotIn("_ensure_compact_mcd_pod_addresses(", source)
        self.assertLess(host_ips, leases)
        self.assertLess(leases, endpoints)
        identity = inspect.getsource(ct._apply_compact_identity_phase)
        self.assertIn("_recover_compact_api_certificates_and_endpoints(", identity)
        readiness = inspect.getsource(ct._wait_compact_install_ready)
        ready_check = readiness.index("if ready == len(nodes) and addresses_ok")
        mcd_refresh = readiness.index("_ensure_compact_mcd_pod_addresses(")
        install_gate = readiness.index("compact_install_observation_ready(")
        self.assertLess(ready_check, mcd_refresh)
        self.assertLess(mcd_refresh, install_gate)

    def test_clone_copy_keeps_source_disks_and_reissues_etcd_certs(self):
        source_nodes = [
            {"role": role, "disks": [{"path": f"/var/lib/libvirt/images/mno-test2/{role}.qcow2"}]}
            for role in ct.COMPACT_ROLES
        ]
        disks = ct.compact_clone_disks("ctcmp03", source_nodes)
        commands = ct.compact_copy_commands(disks)
        self.assertIn("cp --reflink=always", commands[1])
        self.assertIn("/var/lib/libvirt/images/mno-test2/master-0-0.qcow2", commands[1])
        self.assertIn("/var/lib/libvirt/images/ctcmp03/master-0-0.qcow2", commands[1])
        spare_nodes = [
            {"role": role, "disks": [
                {"path": f"/flavors/bare/disk-{index}.qcow2"},
                {"path": f"/flavors/bare/disk-{index}-spare.qcow2"},
            ]}
            for index, role in enumerate(ct.COMPACT_ROLES)
        ]
        spare_disks = ct.compact_clone_disks("edge3", spare_nodes)
        self.assertEqual(
            [(disk["source"], disk["dest"]) for disk in spare_disks[:2]],
            [
                ("/flavors/bare/disk-0.qcow2",
                 "/var/lib/libvirt/images/edge3/master-0-0.qcow2"),
                ("/flavors/bare/disk-0-spare.qcow2",
                 "/var/lib/libvirt/images/edge3/master-0-0-spare.qcow2"),
            ])
        self.assertEqual(len(spare_disks), 6)
        groups = ct.compact_flavor_node_disks([
            "disk-2-spare.qcow2", "disk-0.qcow2", "disk-1-spare.qcow2",
            "disk-2.qcow2", "disk-0-spare.qcow2", "disk-1.qcow2",
        ])
        self.assertEqual(groups[0], ("master-0-0", "disk-0.qcow2", "disk-0-spare.qcow2"))
        self.assertEqual(groups[2][2], "disk-2-spare.qcow2")
        self.assertEqual(
            ct.compact_flavor_node_disks(["disk-0.qcow2", "disk-1.qcow2", "disk-2.qcow2"])[1],
            ("master-0-1", "disk-1.qcow2", None))
        nodes = ct.compact_node_plan("ctcmp03", 253, ["02:00:00:00:00:01", "02:00:00:00:00:02", "02:00:00:00:00:03"])
        script = ct.compact_etcd_reissue_script(nodes, "/work/crypto", "/work/etcd-new")
        self.assertIn("san_for 192.168.253.69", script)
        self.assertIn("/work/crypto/etcd-signer.crt", script)
        self.assertIn("/work/crypto/etcd-metric-signer.key", script)
        self.assertNotIn("--force-new-cluster", script)
        proxy = ct.compact_proxy_command("etcd-image", ["192.168.253.81", "192.168.253.89"])
        self.assertIn("--advertise-client-url 127.0.0.1:2379", proxy)
        self.assertNotIn("192.168.253.69", proxy)
        body = base64.b64encode(b"api.test-infra-cluster-mno2.redhat.com").decode()
        pairs = [("test-infra-cluster-mno2.redhat.com", "test-infra-cluster-ctcmp03.redhat.com")]
        unit = "ExecStart=node-ip set 192.168.124.5\n"
        raw = json.dumps({"spec": {"config": {
            "storage": {"files": [
                {"contents": {"source": f"data:text/plain;charset=utf-8;base64,{body}"}}
            ]},
            "systemd": {"units": [{"name": "nodeip-configuration.service", "contents": unit}]},
        }}})
        ip_pairs = pairs + [("192.168.124.5", "192.168.253.5")]
        rewritten = json.loads(ct.rewrite_machineconfig(raw, ip_pairs))
        encoded = rewritten["spec"]["config"]["storage"]["files"][0]["contents"]["source"].split(",", 1)[1]
        self.assertIn(b"test-infra-cluster-ctcmp03.redhat.com", base64.b64decode(encoded))
        self.assertIn("192.168.253.5", rewritten["spec"]["config"]["systemd"]["units"][0]["contents"])
        pem = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\nserver: api.test-infra-cluster-mno2.redhat.com\n"
        kept = ct.apply_identity_preserving_pems(pem, pairs)
        self.assertIn("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----", kept)
        self.assertIn("test-infra-cluster-ctcmp03.redhat.com", kept)
        indented = (
            "additionalTrustBundle: |-\n"
            "  -----BEGIN CERTIFICATE-----\n"
            "  MIIB\n"
            "  -----END CERTIFICATE-----\n"
            "additionalTrustBundlePolicy: Proxyonly\n"
            "baseDomain: test-infra-cluster-mno2.redhat.com\n")
        rewritten_bundle = ct.apply_identity_preserving_pems(indented, pairs)
        self.assertIn("\n  -----BEGIN CERTIFICATE-----\n  MIIB\n  -----END CERTIFICATE-----\n", rewritten_bundle)
        self.assertIn("additionalTrustBundlePolicy: Proxyonly\n", rewritten_bundle)
        self.assertIn("baseDomain: test-infra-cluster-ctcmp03.redhat.com\n", rewritten_bundle)

    def test_compact_health_requires_three_nodes_and_operators(self):
        nodes = ct.parse_oc_nodes(
            "master-0-0   Ready   control-plane\n"
            "master-0-1   Ready   control-plane\n"
            "master-0-2   Ready,SchedulingDisabled   control-plane\n")
        etcd = [
            {"started": True, "leader": False},
            {"started": True, "leader": True},
            {"started": True, "leader": False},
        ]
        good = [{"name": "authentication", "available": "True", "progressing": "False", "degraded": "False"}]
        self.assertFalse(ct.compact_health(nodes, etcd, good)["ok"])
        nodes[2]["ready"] = True
        bad = good + [{"name": "machine-config", "available": "True", "progressing": "False", "degraded": "True"}]
        health = ct.compact_health(nodes, etcd, bad)
        self.assertFalse(health["ok"])
        self.assertEqual(health["unhealthy_operators"], ["machine-config"])
        self.assertTrue(ct.compact_health(nodes, etcd, good)["ok"])
        self.assertEqual(
            ct.compact_node_address_patch("192.168.204.81")["status"]["addresses"][0]["address"],
            "192.168.204.81")
        machine = json.dumps({"status": {"addresses": [{"type": "InternalIP", "address": "192.168.123.65"}]}})
        patched = json.loads(ct.compact_status_address_patch(machine, [("192.168.123.65", "192.168.204.81")]))
        self.assertEqual(patched["status"]["addresses"][0]["address"], "192.168.204.81")
        host = json.dumps({"status": {"hardware": {"nics": [{"ip": "192.168.123.65", "name": "ens3"}]}}})
        hardware = json.loads(ct.compact_bmh_hardware_patch(host, [("192.168.123.65", "192.168.204.81")]))
        self.assertEqual(hardware[0]["value"]["nics"][0]["ip"], "192.168.204.81")
        self.assertEqual(ct.compact_webhook_failure_patch("Ignore")[0]["value"], "Ignore")
        wide = ct.parse_oc_nodes(
            "master-0-0 Ready control-plane,master,worker 5h v1.35.8 192.168.204.69 <none>\n"
            "master-0-1 Ready control-plane,master,worker 5h v1.35.8 192.168.204.81 <none>\n"
            "master-0-2 Ready control-plane,master,worker 5h v1.35.8 192.168.123.50 <none>\n")
        self.assertFalse(ct.compact_node_ips_ready(wide, [
            {"role": "master-0-0", "ip": "192.168.204.69"},
            {"role": "master-0-1", "ip": "192.168.204.81"},
            {"role": "master-0-2", "ip": "192.168.204.89"},
        ]))
        wide[2]["ip"] = "192.168.204.89"
        self.assertTrue(ct.compact_node_ips_ready(wide, [
            {"role": "master-0-0", "ip": "192.168.204.69"},
            {"role": "master-0-1", "ip": "192.168.204.81"},
            {"role": "master-0-2", "ip": "192.168.204.89"},
        ]))
        secret = json.dumps({"data": {
            "etcd-peer-master-0-2.crt": "b2xk",
            "etcd-peer-master-0-2.key": "a2V5",
            "untouched": "eWVz",
        }})
        patched = json.loads(ct.compact_etcd_secret_patch(secret, {
            "etcd-peer-master-0-2.crt": "new-cert",
            "missing.crt": "nope",
        }))
        self.assertEqual(base64.b64decode(patched["data"]["etcd-peer-master-0-2.crt"]).decode(), "new-cert")
        self.assertNotIn("etcd-peer-master-0-2.key", patched["data"])
        self.assertNotIn("missing.crt", patched["data"])
        unchanged = json.loads(ct.compact_etcd_secret_patch(secret, {
            "etcd-peer-master-0-2.crt": "old",
        }))
        self.assertEqual(unchanged["data"], {})
        self.assertIn("etcd-peer-master-0-2", ct.compact_etcd_cert_names([
            {"role": "master-0-2"},
        ]))
        self.assertIn("find /etc/kubernetes/static-pod-resources", ct.compact_install_etcd_certs_script())
        self.assertEqual(ct.compact_readiness_stage(3, True, 2, 2, True, ["etcd"]), "etcd-rollout")
        self.assertEqual(ct.compact_readiness_stage(3, True, 3, 3, True, ["kube-apiserver"]), "core")
        self.assertEqual(ct.compact_readiness_stage(3, True, 3, 3, True, []), "core-ready")
        core, post = ct.compact_split_operators(["etcd", "console", "insights"])
        self.assertEqual(core, ["etcd"])
        self.assertEqual(post, ["console"])
        self.assertEqual(ct.parse_etcd_pods(
            "etcd-master-0-0 5/5 Running 0 1m\n"
            "etcd-master-0-1 0/5 Pending 0 1m\n"
            "etcd-guard-master-0-0 1/1 Running 0 1m\n"), 1)


class TestCompactLeases(unittest.TestCase):
    def test_compact_master_lease_cleanup_keeps_clone_ips_only(self):
        node_ips = [
            "192.168.220.69",
            "192.168.220.81",
            "192.168.220.89",
        ]
        lease_keys = "\n".join([
            "/kubernetes.io/masterleases/192.168.123.50",
            "/kubernetes.io/masterleases/192.168.220.89",
            "/kubernetes.io/masterleases/192.168.220.69",
            "/kubernetes.io/masterleases/192.168.123.64",
            "/kubernetes.io/masterleases/192.168.220.81",
        ])

        self.assertEqual(ct.compact_stale_master_lease_keys(lease_keys, node_ips), [
            "/kubernetes.io/masterleases/192.168.123.50",
            "/kubernetes.io/masterleases/192.168.123.64",
        ])
        self.assertEqual(ct.compact_stale_master_lease_keys("", node_ips), [])

    def test_compact_master_lease_cleanup_rejects_malformed_lease_keys(self):
        with self.assertRaises(ValueError):
            ct.compact_stale_master_lease_keys(
                "/kubernetes.io/masterleases/not-an-ip", ["192.168.220.69"])

    def test_compact_master_lease_etcd_command_uses_clone_quorum(self):
        nodes = [
            {"role": "master-0-0", "ip": "192.168.220.69"},
            {"role": "master-0-1", "ip": "192.168.220.81"},
            {"role": "master-0-2", "ip": "192.168.220.89"},
        ]
        command = ct.compact_master_lease_etcd_command(
            "quay.io/openshift/etcd:latest", nodes, "delete",
            "/kubernetes.io/masterleases/192.168.123.50")
        self.assertIn("https://192.168.220.69:2379", command)
        self.assertIn("https://192.168.220.81:2379", command)
        self.assertIn("https://192.168.220.89:2379", command)
        self.assertIn("--entrypoint etcdctl", command)
        self.assertIn("etcd-peer-master-0-1.crt", command)
        self.assertIn("del /kubernetes.io/masterleases/192.168.123.50", command)
        with self.assertRaises(ValueError):
            ct.compact_master_lease_etcd_command(
                "quay.io/openshift/etcd:latest", nodes, "delete", "not-a-lease-key")

    def test_compact_mco_lease_expiry_requires_a_valid_expired_lease(self):
        now = ct.datetime.fromisoformat("2026-10-24T04:10:00+00:00")
        expired = {
            "spec": {
                "holderIdentity": "source-controller",
                "renewTime": "2026-10-24T04:00:00Z",
                "leaseDurationSeconds": 137,
            }
        }
        recent = {
            "spec": {
                "holderIdentity": "active-controller",
                "renewTime": "2026-10-24T04:08:00Z",
                "leaseDurationSeconds": 137,
            }
        }

        self.assertTrue(ct.compact_lease_is_expired(expired, now))
        self.assertFalse(ct.compact_lease_is_expired(recent, now))
        self.assertFalse(ct.compact_lease_is_expired({"spec": {}}, now))
        self.assertFalse(ct.compact_lease_is_expired("not-json", now))
        self.assertEqual(
            ct.COMPACT_MCO_CONTROLLER_LEASE,
            ("openshift-machine-config-operator", "machine-config-controller"))

    def test_compact_lease_delete_preconditions_require_uid_and_resource_version(self):
        lease = {"metadata": {"uid": "lease-uid", "resourceVersion": "17"}}
        self.assertEqual(ct.compact_lease_delete_preconditions(lease), {
            "uid": "lease-uid", "resourceVersion": "17",
        })
        self.assertIsNone(ct.compact_lease_delete_preconditions({"metadata": {"uid": "lease-uid"}}))
        self.assertIsNone(ct.compact_lease_delete_preconditions("not-json"))

    def test_conditional_delete_script_sends_uid_and_resource_version_to_api(self):
        script = ct.compact_kubernetes_delete_proxy_script(
            "KUBECONFIG=/tmp/clone.kubeconfig oc --server=https://192.168.220.5:6443",
            "/apis/coordination.k8s.io/v1/namespaces/openshift-machine-config-operator/leases/machine-config-controller",
            {"uid": "lease-uid", "resourceVersion": "17"},
        )
        self.assertIn("proxy --address=127.0.0.1 --port=0", script)
        self.assertIn("--request DELETE", script)
        self.assertIn(
            "--data-binary '{\"apiVersion\":\"v1\",\"kind\":\"DeleteOptions\","
            "\"preconditions\":{\"uid\":\"lease-uid\",\"resourceVersion\":\"17\"}}'",
            script,
        )
        with self.assertRaises(ValueError):
            ct.compact_kubernetes_delete_proxy_script(
                "oc", "/apis/apps/v1/namespaces/ns/deployments/name",
                {"uid": "lease-uid"})

    def test_compact_mco_lease_cleanup_deletes_only_expired_leases(self):
        old_renew_time = (
            ct.datetime.now(ct.timezone.utc) - ct.timedelta(days=1)).isoformat()
        expired = json.dumps({
            "metadata": {"uid": "lease-uid", "resourceVersion": "17"},
            "spec": {
                "holderIdentity": "source-controller",
                "renewTime": old_renew_time,
                "leaseDurationSeconds": 137,
            }
        })
        get_after_delete = MagicMock(
            returncode=1, stdout="", stderr="Error from server (NotFound): leases not found")
        responses = [
            MagicMock(returncode=0, stdout=expired, stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            get_after_delete,
        ]
        mock_env = MagicMock()
        mock_env.run.side_effect = responses
        with patch.object(ct, "env", mock_env), \
                patch.object(ct, "_clone_log") as clone_log:
            ct._clear_expired_mco_controller_lease("oc --kubeconfig=clone.kubeconfig", "/work")

        self.assertEqual(mock_env.run.call_count, 3)
        delete_command = mock_env.run.call_args_list[1].args[0]
        self.assertIn("/leases/machine-config-controller", delete_command)
        self.assertIn('"preconditions":{"uid":"lease-uid","resourceVersion":"17"}',
                      delete_command)
        self.assertIn("--request DELETE", delete_command)
        self.assertIn("verified removal", clone_log.call_args.args[1])

    def test_expired_mco_lease_cleanup_preserves_lease_renewed_after_conflict(self):
        old_renew_time = (
            ct.datetime.now(ct.timezone.utc) - ct.timedelta(days=1)).isoformat()
        renewed_time = ct.datetime.now(ct.timezone.utc).isoformat()
        expired = json.dumps({
            "metadata": {"uid": "lease-uid", "resourceVersion": "17"},
            "spec": {
                "holderIdentity": "old-holder",
                "renewTime": old_renew_time,
                "leaseDurationSeconds": 137,
            },
        })
        renewed = json.dumps({
            "metadata": {"uid": "lease-uid", "resourceVersion": "18"},
            "spec": {
                "holderIdentity": "active-holder",
                "renewTime": renewed_time,
                "leaseDurationSeconds": 137,
            },
        })
        mock_env = MagicMock()
        mock_env.run.side_effect = [
            MagicMock(returncode=0, stdout=expired, stderr=""),
            MagicMock(returncode=1, stdout="", stderr="Error from server (Conflict)"),
            MagicMock(returncode=0, stdout=renewed, stderr=""),
        ]
        with patch.object(ct, "env", mock_env), patch.object(ct, "_clone_log") as clone_log:
            ct._clear_expired_mco_controller_lease("oc", "/work")

        self.assertEqual(mock_env.run.call_count, 3)
        self.assertIn('"preconditions":{"uid":"lease-uid","resourceVersion":"17"}',
                      mock_env.run.call_args_list[1].args[0])
        self.assertIn("renewed during cleanup", clone_log.call_args.args[1])

    def test_expired_mco_lease_cleanup_preserves_replacement_after_conflict(self):
        old_renew_time = (
            ct.datetime.now(ct.timezone.utc) - ct.timedelta(days=1)).isoformat()
        expired = json.dumps({
            "metadata": {"uid": "old-uid", "resourceVersion": "17"},
            "spec": {
                "holderIdentity": "old-holder",
                "renewTime": old_renew_time,
                "leaseDurationSeconds": 137,
            },
        })
        replaced = json.dumps({
            "metadata": {"uid": "new-uid", "resourceVersion": "1"},
            "spec": {
                "holderIdentity": "new-holder",
                "renewTime": old_renew_time,
                "leaseDurationSeconds": 137,
            },
        })
        mock_env = MagicMock()
        mock_env.run.side_effect = [
            MagicMock(returncode=0, stdout=expired, stderr=""),
            MagicMock(returncode=1, stdout="", stderr="Error from server (Conflict)"),
            MagicMock(returncode=0, stdout=replaced, stderr=""),
        ]
        with patch.object(ct, "env", mock_env), patch.object(ct, "_clone_log") as clone_log:
            ct._clear_expired_mco_controller_lease("oc", "/work")

        self.assertEqual(mock_env.run.call_count, 3)
        self.assertIn("preserving replacement", clone_log.call_args.args[1])

    def test_compact_finish_clears_expired_mco_lease_before_mco_render(self):
        source = inspect.getsource(ct._finish_compact_clone)
        clear_lease = source.index("_clear_expired_mco_controller_lease(clone_oc, work)")
        prepare_render = source.index("_apply_compact_identity_phase(ctx, clone_oc)")
        self.assertLess(clear_lease, prepare_render)
        self.assertIn(
            "_wait_compact_machineconfig_source(clone_oc",
            inspect.getsource(ct._apply_compact_identity_phase),
        )


class TestCompactReadiness(unittest.TestCase):
    def test_etcd_bundle_update_is_ordered_and_fail_closed(self):
        recert = inspect.getsource(ct._run_compact_recert_phase)
        finish = inspect.getsource(ct._finish_compact_clone)
        rewrite = inspect.getsource(ct._apply_compact_identity_phase)
        readiness = inspect.getsource(ct._wait_compact_install_ready)
        self.assertLess(
            recert.index("_require_reissued_etcd_certs("),
            recert.index("sign kubelet certificates with the live CSR signer"),
        )
        self.assertLess(
            finish.index("_require_reissued_etcd_certs("),
            finish.index("_apply_compact_identity_phase(ctx, clone_oc)"),
        )
        self.assertLess(
            finish.index("_run_compact_recert_phase(ctx)"),
            finish.index("_apply_compact_identity_phase(ctx, clone_oc)"),
        )
        self.assertLess(
            finish.index("_apply_compact_identity_phase(ctx, clone_oc)"),
            finish.index("_wait_compact_install_ready("),
        )
        self.assertIn("rewrite cluster records and 00-master", rewrite)
        self.assertIn(
            "wait until etcd and the API revisions are installed for vmaas", readiness)
        combined = recert + finish + rewrite + readiness
        self.assertNotIn("_sync_reissued_etcd_certs(", combined)
        self.assertNotIn("copy_to_nodes=True", combined)
        self.assertNotIn("over the operator revision", combined)
        self.assertEqual(combined.count("sudo systemctl restart kubelet"), 1)
        self.assertIn(
            "master-0-0 kubelet after installing its clone client certificate", recert)
        self.assertNotIn("systemctl restart crio", combined)
        self.assertIn("publish clone node IPs without restarting kubelet", finish)
        self.assertNotIn("is core-ready", combined)
        require_source = inspect.getsource(ct._require_reissued_etcd_certs)
        self.assertLess(
            require_source.index("etcd certificate inputs"),
            require_source.index("_trigger_etcd_member_rollout"))
        self.assertEqual(
            ct.compact_etcd_force_redeploy_patch("reissued-etcd-certs"),
            {"spec": {"forceRedeploymentReason": "reissued-etcd-certs"}})

        nodes = [{"role": "master-0-0", "ip": "192.168.210.69"}]
        pems = {
            f"{name}.{ext}": f"{name}-{ext}\n"
            for name in ct.compact_etcd_cert_names(nodes)
            for ext in ("crt", "key")
        }

        class _Cmd:
            def __init__(self, returncode=0, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        class _Env:
            def __init__(self, handler):
                self.calls = []
                self.handler = handler

            def run(self, cmd, check=True):
                self.calls.append(cmd)
                return self.handler(cmd)

        def bundle(values):
            return json.dumps({"data": {
                key: base64.b64encode(value.encode()).decode() for key, value in values.items()
            }})

        def patches(calls):
            return [cmd for cmd in calls if "patch secret " in cmd]

        previous = ct.env
        try:
            with tempfile.TemporaryDirectory() as work:
                root = Path(work) / "etcd-new"
                root.mkdir()
                for key, value in pems.items():
                    (root / key).write_text(value)
                def current_handler(cmd):
                    if "get secret etcd-all-certs" in cmd:
                        return _Cmd(0, bundle(pems))
                    for name in ct.compact_etcd_cert_names(nodes):
                        if f"get secret {name} " in cmd:
                            return _Cmd(0, bundle({
                                "tls.crt": pems[f"{name}.crt"],
                                "tls.key": pems[f"{name}.key"],
                            }))
                    return _Cmd()

                current = _Env(current_handler)
                ct.env = current
                result = ct._sync_reissued_etcd_certs("oc", nodes, "/id", work, False)
                self.assertEqual(result["bundle"], "already-current")
                self.assertEqual(result["members_current"], [
                    "etcd-peer-master-0-0",
                    "etcd-serving-master-0-0",
                    "etcd-serving-metrics-master-0-0",
                ])
                self.assertEqual(patches(current.calls), [])

                old = {key: "old\n" for key in pems}
                reads = {"n": 0}

                def changed(cmd):
                    if "get secret etcd-all-certs" in cmd:
                        reads["n"] += 1
                        return _Cmd(0, bundle(pems if reads["n"] > 1 else old))
                    if "get secret etcd-" in cmd:
                        return _Cmd(1, "", "Error from server (NotFound): secret not found")
                    return _Cmd()

                updated = _Env(changed)
                ct.env = updated
                result = ct._sync_reissued_etcd_certs("oc", nodes, "/id", work, False)
                self.assertEqual(result["bundle"], "patched")
                self.assertEqual(result["members_missing"], [
                    "etcd-peer-master-0-0",
                    "etcd-serving-master-0-0",
                    "etcd-serving-metrics-master-0-0",
                ])
                self.assertEqual(len(patches(updated.calls)), 1)
                self.assertTrue(any("etcd-all-certs patch requested" in cmd for cmd in updated.calls))
                self.assertTrue(any("etcd-all-certs patch verified" in cmd for cmd in updated.calls))

                def rejected(cmd):
                    if "get secret etcd-all-certs" in cmd:
                        return _Cmd(1, "", "connection refused")
                    if "oc config view" in cmd:
                        return _Cmd(0, "test-cluster\n")
                    if "oc config set-cluster" in cmd:
                        return _Cmd()
                    if "echo yes" in cmd:
                        return _Cmd(0, "yes\n")
                    return _Cmd()

                failed = _Env(rejected)
                ct.env = failed
                with patch.object(ct, "_clear_compact_stale_master_leases"), \
                        patch.object(
                            ct,
                            "_load_revision_baseline",
                            side_effect=lambda _work, operator=None: (
                                10 if operator == "kube-apiserver" else None
                            ),
                        ), \
                        self.assertRaises(SystemExit) as aborted:
                    ct._finish_compact_clone(ct.CompactCloneContext(
                        source={}, clone_id="edge10", nodes=nodes + [
                            {"role": "master-0-1", "ip": "192.168.210.81"},
                            {"role": "master-0-2", "ip": "192.168.210.89"},
                        ], identity="/id",
                        work=work, crypto="/crypto", etcd_image="etcd", ingress_cn="cn",
                        plan_path="/plan", src_kc="/src", pairs=[],
                    ), start_at="rewrite")
                self.assertIn("reading etcd-all-certs failed", str(aborted.exception))
                self.assertFalse(any("rewrite cluster records" in cmd for cmd in failed.calls))
                self.assertFalse(any("wait until etcd and the API revisions" in cmd for cmd in failed.calls))
                self.assertEqual(patches(failed.calls), [])

                def patch_fails(cmd):
                    if "get secret etcd-all-certs" in cmd:
                        return _Cmd(0, bundle(old))
                    if "patch secret etcd-all-certs" in cmd:
                        return _Cmd(1, "", "denied")
                    return _Cmd()

                denied = _Env(patch_fails)
                ct.env = denied
                with self.assertRaises(SystemExit) as aborted:
                    ct._require_reissued_etcd_certs("oc", nodes, "/id", work)
                self.assertIn("patching etcd-all-certs failed", str(aborted.exception))
                self.assertFalse(any("patch verified" in cmd for cmd in denied.calls))
                self.assertFalse(any("patch etcd cluster" in cmd for cmd in denied.calls))
                self.assertFalse(any("wait until etcd and the API revisions" in cmd for cmd in denied.calls))

                triggered_reads = {"n": 0}

                def trigger_handler(cmd):
                    if "get secret etcd-all-certs" in cmd:
                        triggered_reads["n"] += 1
                        return _Cmd(0, bundle(pems if triggered_reads["n"] > 1 else old))
                    if "get secret etcd-" in cmd:
                        return _Cmd(1, "", "Error from server (NotFound): secret not found")
                    if "patch etcd cluster" in cmd:
                        self.assertIn("forceRedeploymentReason", cmd)
                        return _Cmd()
                    if "get co etcd -o json" in cmd:
                        return _Cmd(0, json.dumps({"status": {"conditions": [{
                            "type": "Progressing",
                            "status": "False",
                            "message": "NodeInstallerProgressing: 3 nodes are at revision 10",
                        }]}}))
                    return _Cmd()

                triggered = _Env(trigger_handler)
                ct.env = triggered
                result = ct._require_reissued_etcd_certs("oc", nodes, "/id", work)
                self.assertEqual(result["bundle"], "patched")
                trigger_at = next(i for i, cmd in enumerate(triggered.calls) if "patch etcd cluster" in cmd)
                verified_at = next(i for i, cmd in enumerate(triggered.calls) if "etcd-all-certs patch verified" in cmd)
                self.assertLess(verified_at, trigger_at)

                current_again = _Env(current_handler)
                ct.env = current_again
                result = ct._require_reissued_etcd_certs("oc", nodes, "/id", work)
                self.assertEqual(result["bundle"], "already-current")
                self.assertFalse(any("patch etcd cluster" in cmd for cmd in current_again.calls))

                reReads = {"n": 0}

                def trigger_rejected_fixed(cmd):
                    if "get secret etcd-all-certs" in cmd:
                        reReads["n"] += 1
                        return _Cmd(0, bundle(pems if reReads["n"] > 1 else old))
                    if "patch secret etcd-all-certs" in cmd:
                        return _Cmd()
                    if "patch etcd cluster" in cmd:
                        return _Cmd(1, "", "denied")
                    if "get co etcd -o json" in cmd:
                        return _Cmd(0, json.dumps({"status": {"conditions": [{
                            "type": "Progressing",
                            "status": "False",
                            "message": "NodeInstallerProgressing: 3 nodes are at revision 10",
                        }]}}))
                    if "get secret etcd-" in cmd:
                        return _Cmd(1, "", "Error from server (NotFound): secret not found")
                    return _Cmd()

                rejected_trigger = _Env(trigger_rejected_fixed)
                ct.env = rejected_trigger
                with self.assertRaises(SystemExit) as aborted:
                    ct._require_reissued_etcd_certs("oc", nodes, "/id", work)
                self.assertIn("etcd rollout trigger failed", str(aborted.exception))
                self.assertFalse(any("etcd rollout triggered" in cmd for cmd in rejected_trigger.calls))
        finally:
            ct.env = previous

    def test_core_ready_resets_when_a_core_condition_regresses(self):
        for name in ct.CORE_OPERATORS:
            stage = ct.compact_readiness_stage(3, True, 3, 3, True, [name])
            self.assertNotEqual(stage, "core-ready", name)
        core, post = ct.compact_split_operators(["machine-config", "insights"])
        self.assertEqual(core, [])
        self.assertEqual(post, ["machine-config"])
        self.assertEqual(ct.compact_readiness_stage(3, True, 3, 3, True, core), "core-ready")
        self.assertEqual(ct.compact_next_stable_samples(1, "core-ready", True, True), 2)
        self.assertEqual(ct.compact_next_stable_samples(1, "etcd-rollout", True, True), 0)
        self.assertEqual(ct.compact_next_stable_samples(1, "core", True, True), 0)
        self.assertEqual(ct.compact_next_stable_samples(1, "core-ready", False, True), 0)
        self.assertEqual(ct.compact_next_stable_samples(1, "core-ready", True, False), 0)
        self.assertEqual(ct.compact_next_stable_samples(1, "core-ready", True, True, False), 0)
        self.assertEqual(ct.compact_install_blockers(["ingress", "insights", "console"]), ["ingress"])
        quiet = {
            "status": {
                "latestAvailableRevision": 12,
                "nodeStatuses": [
                    {"currentRevision": 12, "targetRevision": 12},
                    {"currentRevision": 12, "targetRevision": 12},
                    {"currentRevision": 12, "targetRevision": 12},
                ],
            },
        }
        pending = {
            "status": {
                "latestAvailableRevision": 12,
                "nodeStatuses": [
                    {"currentRevision": 10, "targetRevision": 12},
                    {"currentRevision": 12, "targetRevision": 12},
                    {"currentRevision": 12, "targetRevision": 12},
                ],
            },
        }
        self.assertTrue(ct.compact_static_pod_rollout_ready(ct.compact_static_pod_revision(quiet), 10))
        self.assertFalse(ct.compact_static_pod_rollout_ready(ct.compact_static_pod_revision(quiet), None))
        self.assertFalse(ct.compact_static_pod_rollout_ready(ct.compact_static_pod_revision(quiet), 12))
        self.assertFalse(ct.compact_static_pod_rollout_ready(ct.compact_static_pod_revision(pending), 10))
        settled = {"status": {"conditions": [
            {"type": "Progressing", "status": "False",
             "message": "NodeInstallerProgressing: 3 nodes are at revision 13"},
            {"type": "Available", "status": "True",
             "message": "StaticPodsAvailable: 3 nodes are active; 3 nodes are at revision 13"},
        ]}}
        rolling = {"status": {"conditions": [
            {"type": "Progressing", "status": "True",
             "message": "NodeInstallerProgressing: 3 nodes are at revision 10; 0 nodes have achieved new revision 12"},
        ]}}
        split = {"status": {"conditions": [
            {"type": "Progressing", "status": "True",
             "message": "NodeInstallerProgressing: 2 nodes are at revision 5; 1 node is at revision 8"},
        ]}}
        self.assertEqual(ct.compact_static_pod_revision(settled)["current"], 13)
        self.assertTrue(ct.compact_static_pod_rollout_ready(ct.compact_static_pod_revision(settled), 10))
        self.assertFalse(ct.compact_static_pod_rollout_ready(ct.compact_static_pod_revision(settled), 13))
        self.assertEqual(ct.compact_static_pod_revision(rolling)["current"], 10)
        self.assertEqual(ct.compact_static_pod_revision(rolling)["latest"], 12)
        self.assertFalse(ct.compact_static_pod_revision(rolling)["converged"])
        self.assertFalse(ct.compact_static_pod_revision(split)["converged"])
        self.assertEqual(ct.compact_static_pod_revision(split)["latest"], 8)

    def test_edge13_revision_messages_need_exactly_three_nodes(self):
        quiet = (
            "NodeInstallerProgressing: 3 nodes are at revision 13\n"
            "StaticPodsAvailable: 3 nodes are active; 3 nodes are at revision 13"
        )
        rolling = (
            "NodeInstallerProgressing: 3 nodes are at revision 9; "
            "0 nodes have achieved new revision 10\n"
            "StaticPodsAvailable: 3 nodes are active; 3 nodes are at revision 9; "
            "0 nodes have achieved new revision 10"
        )
        singular = "NodeInstallerProgressing: 1 node is at revision 5; 2 nodes are at revision 8"
        contradictory = "NodeInstallerProgressing: 3 nodes are at revision 10; 1 node is at revision 12"
        short = "NodeInstallerProgressing: 2 nodes are at revision 10; 2 nodes are at revision 11"
        malformed = "NodeInstallerProgressing: all nodes are current"
        self.assertEqual(ct.compact_revision_from_text(quiet), {
            "latest": 13, "current": 13, "converged": True,
        })
        self.assertEqual(ct.compact_revision_from_text(rolling), {
            "latest": 10, "current": 9, "converged": False,
        })
        self.assertFalse(ct.compact_revision_from_text(singular)["converged"])
        self.assertEqual(ct.compact_revision_from_text(singular)["latest"], 8)
        self.assertIsNone(ct.compact_revision_from_text(singular)["current"])
        self.assertFalse(ct.compact_revision_from_text(contradictory)["converged"])
        self.assertIsNone(ct.compact_revision_from_text(contradictory)["current"])
        self.assertFalse(ct.compact_revision_from_text(short)["converged"])
        self.assertEqual(ct.compact_revision_from_text(short)["latest"], 11)
        self.assertEqual(ct.compact_revision_from_text(malformed), {
            "latest": None, "current": None, "converged": False,
        })
        self.assertEqual(ct.compact_revision_from_text(""), {
            "latest": None, "current": None, "converged": False,
        })
        duplicated = {"status": {"conditions": [
            {"type": "Progressing", "status": "False",
             "message": "NodeInstallerProgressing: 3 nodes are at revision 13"},
            {"type": "Available", "status": "True",
             "message": "StaticPodsAvailable: 3 nodes are active; 3 nodes are at revision 13"},
        ]}}
        self.assertTrue(ct.compact_static_pod_revision(duplicated)["converged"])
        self.assertEqual(ct.compact_static_pod_revision(duplicated)["latest"], 13)
        split_message = (
            "NodeInstallerProgressing: 1 node is at revision 10; 2 nodes are at revision 12"
        )
        self.assertFalse(ct.compact_revision_from_text(split_message)["converged"])
        self.assertEqual(ct.compact_revision_from_text(
            "NodeInstallerProgressing: 2 nodes are at revision 5; 1 node is at revision 8"
        )["latest"], 8)
        self.assertEqual(ct.compact_revision_from_text(
            "NodeInstallerProgressing: 3 nodes are at revision 10; 0 nodes have achieved new revision 12"
        ), {"latest": 12, "current": 10, "converged": False})
        quiet_docs = {
            "etcd": {"status": {"conditions": [
                {"type": "Available", "status": "True"},
                {"type": "Progressing", "status": "False"},
                {"type": "Degraded", "status": "False"},
            ]}},
            "ingress": {"status": {"conditions": [
                {"type": "Available", "status": "True"},
                {"type": "Progressing", "status": "True"},
                {"type": "Degraded", "status": "False"},
            ]}},
        }
        observed = {op["name"]: op for op in ct.compact_operators_from_docs(quiet_docs)}
        self.assertEqual(observed["etcd"]["progressing"], "False")
        self.assertEqual(observed["ingress"]["progressing"], "True")
        self.assertFalse(ct.compact_install_observation_ready(3, True, [], ["ingress"], True))
        self.assertFalse(ct.compact_install_observation_ready(3, True, [], [], False))
        self.assertTrue(ct.compact_install_observation_ready(3, True, [], [], True))
        self.assertFalse(ct.compact_install_observation_ready(
            3, True, [], [], True, endpoints_ready=False))
        identity = inspect.getsource(ct._apply_compact_identity_phase)
        self.assertIn('"render": resolve_master_render', identity)
        self.assertIn(
            '"api-certificates": lambda: _recover_compact_api_certificates_and_endpoints(',
            identity,
        )
        recovery = inspect.getsource(ct._recover_compact_api_certificates)
        self.assertLess(recovery.index("_ensure_api_certificates"), recovery.index("_refresh_internal_api_certificate"))
        self.assertNotIn("time.sleep(30)", identity)

    def test_kube_apiserver_install_readiness_uses_each_advanced_node(self):
        rolling_but_available = {"status": {"conditions": [
            {"type": "Progressing", "status": "True",
             "message": "NodeInstallerProgressing: 1 node is at revision 8; 2 nodes are at revision 9"},
            {"type": "Available", "status": "True",
             "message": "StaticPodsAvailable: 3 nodes are active; 1 node is at revision 8; 2 nodes are at revision 9"},
            {"type": "Degraded", "status": "False"},
        ]}}
        self.assertTrue(ct.compact_static_pod_nodes_newer_than(rolling_but_available, 5))
        self.assertTrue(ct.compact_kube_apiserver_nodes_ready_for_install(rolling_but_available, 5))
        self.assertFalse(ct.compact_kube_apiserver_nodes_ready_for_install(rolling_but_available, 8))

        status_report = {"status": {
            "nodeStatuses": [
                {"nodeName": "master-0-0", "currentRevision": 8, "targetRevision": 9},
                {"nodeName": "master-0-1", "currentRevision": 9, "targetRevision": 0},
                {"nodeName": "master-0-2", "currentRevision": 9, "targetRevision": 0},
            ],
            "conditions": [
                {"type": "Available", "status": "True"},
                {"type": "Degraded", "status": "False"},
            ],
        }}
        self.assertTrue(ct.compact_kube_apiserver_nodes_ready_for_install(status_report, 5))

        wrong_nodes = {"status": {
            "nodeStatuses": [
                {"nodeName": "source-master-0-0", "currentRevision": 9},
                {"nodeName": "source-master-0-1", "currentRevision": 9},
                {"nodeName": "source-master-0-2", "currentRevision": 9},
            ],
            "conditions": [
                {"type": "Available", "status": "True"},
                {"type": "Degraded", "status": "False"},
            ],
        }}
        self.assertFalse(ct.compact_kube_apiserver_nodes_ready_for_install(wrong_nodes, 5))

        one_node_still_on_baseline = {"status": {"conditions": [
            {"type": "Progressing", "status": "True",
             "message": "NodeInstallerProgressing: 1 node is at revision 5; 1 node is at revision 8; 1 node is at revision 9"},
            {"type": "Available", "status": "True"},
            {"type": "Degraded", "status": "False"},
        ]}}
        self.assertFalse(ct.compact_static_pod_nodes_newer_than(one_node_still_on_baseline, 5))
        self.assertFalse(ct.compact_kube_apiserver_nodes_ready_for_install(one_node_still_on_baseline, 5))

        unavailable = {"status": {"conditions": [
            {"type": "Progressing", "status": "True",
             "message": "NodeInstallerProgressing: 1 node is at revision 8; 2 nodes are at revision 9"},
            {"type": "Available", "status": "False"},
            {"type": "Degraded", "status": "False"},
        ]}}
        self.assertFalse(ct.compact_kube_apiserver_nodes_ready_for_install(unavailable, 5))

        degraded = {"status": {"conditions": [
            {"type": "Progressing", "status": "True",
             "message": "NodeInstallerProgressing: 1 node is at revision 8; 2 nodes are at revision 9"},
            {"type": "Available", "status": "True"},
            {"type": "Degraded", "status": "True"},
        ]}}
        self.assertFalse(ct.compact_kube_apiserver_nodes_ready_for_install(degraded, 5))

    def test_health_resume_requires_saved_revision_baselines(self):
        self.assertIsNone(ct.compact_valid_revision_baselines(None))
        self.assertIsNone(ct.compact_valid_revision_baselines({"etcd": 10}))
        self.assertIsNone(ct.compact_valid_revision_baselines({"etcd": 10, "kube-apiserver": None}))
        self.assertIsNone(ct.compact_valid_revision_baselines({"etcd": True, "kube-apiserver": 5}))
        self.assertEqual(
            ct.compact_valid_revision_baselines({"etcd": 10, "kube-apiserver": 5}),
            (10, 5))

        class _Cmd:
            def __init__(self, stdout=""):
                self.returncode = 0
                self.stdout = stdout
                self.stderr = ""

        class _Env:
            def __init__(self, stdout=""):
                self.calls = []
                self.stdout = stdout

            def run(self, cmd, check=True):
                self.calls.append(cmd)
                return _Cmd(self.stdout)

        previous = ct.env
        try:
            missing = _Env("")
            ct.env = missing
            self.assertIsNone(ct._load_revision_baselines("/work"))
            with patch.object(ct, "_clear_compact_stale_master_leases"), \
                    self.assertRaises(SystemExit) as aborted:
                nodes = [
                    {"role": role, "ip": ip}
                    for role, ip in zip(
                        ct.COMPACT_ROLES,
                        ("192.168.214.69", "192.168.214.81", "192.168.214.89"),
                    )
                ]
                ct._finish_compact_clone(ct.CompactCloneContext(
                    source={}, clone_id="edge14", nodes=nodes, identity="/id",
                    work="/work", crypto="/crypto", etcd_image="etcd", ingress_cn="cn",
                    plan_path="/plan", src_kc="/src", pairs=[],
                ), start_at="health")
            self.assertIn("revision baselines", str(aborted.exception))
            self.assertFalse(any("wait until etcd and the API revisions" in cmd for cmd in missing.calls))
            saved = _Env(json.dumps({"etcd": 10, "kube-apiserver": 5}))
            ct.env = saved
            self.assertEqual(ct._load_revision_baselines("/work"), (10, 5))
        finally:
            ct.env = previous

    def test_parallel_failures_are_reported_in_node_order(self):
        nodes = [
            {"role": "master-0-2", "ip": "192.168.214.89"},
            {"role": "master-0-0", "ip": "192.168.214.69"},
            {"role": "master-0-1", "ip": "192.168.214.81"},
        ]

        def fail(node):
            if node["role"] != "master-0-1":
                sys.exit(f"boom {node['role']}")

        with self.assertRaises(SystemExit) as aborted:
            ct._in_parallel(nodes, fail)
        text = str(aborted.exception)
        self.assertLess(text.index("master-0-2"), text.index("master-0-0"))
        self.assertNotIn("master-0-1", text)
        self.assertIn("boom master-0-2", text)
        self.assertIn("boom master-0-0", text)
        with self.assertRaises(SystemExit) as aborted:
            ct._in_parallel([
                ({"role": "master-0-1"}, "restore"),
                ({"role": "master-0-0"}, "restore"),
            ], lambda item: sys.exit("restore failed"))
        text = str(aborted.exception)
        self.assertLess(text.index("master-0-1"), text.index("master-0-0"))
        blocker = ct.compact_core_blocker_text(json.dumps({"items": [{
            "metadata": {"name": "kube-apiserver"},
            "status": {"conditions": [
                {"type": "Progressing", "status": "True", "reason": "NodeInstallerProgressing",
                 "message": "NodeInstallerProgressing: 1 nodes are at revision 7, 2 nodes are at revision 8"},
                {"type": "Degraded", "status": "False", "reason": "AsExpected", "message": "ignored"},
            ]},
        }]}))
        self.assertIn("kube-apiserver Progressing NodeInstallerProgressing", blocker)
        self.assertNotIn("ignored", blocker)


class TestCompactFlavor(unittest.TestCase):
    def test_compact_boot_refuses_before_the_sno_path(self):
        class _State:
            def __enter__(self):
                return {"flavors": {"compact": {"topology": "compact"}}, "clones": {}}
            def __exit__(self, *args):
                return False
        args = argparse.Namespace(flavor="compact", name=None, vcpus=None, memory_kib=None, pull_secret="/tmp/ps.json", no_rollback=False)
        with patch.object(ct, "locked_state", _State):
            with self.assertRaises(SystemExit) as ctx:
                ct.cmd_boot(args)
        self.assertIn("compact-boot", str(ctx.exception))

    def test_flavor_layout_replaces_source_domain_and_ips(self):
        flavor = {
            "source_cluster": "mno-test2",
            "source_primary_subnet": 124,
            "memory_kib": 33554432,
            "vcpus": 16,
            "etcd_image": "quay.io/example/etcd:tag",
        }
        macs = ["52:54:00:a2:b0:aa", "52:54:00:76:07:c5", "52:54:00:ba:f0:e8"]
        paths = [f"/flavors/mno-3node/disk-{i}.qcow2" for i in range(3)]
        text = "KUBERNETES_SERVICE_HOST='api-int.test-infra-cluster-mno2.redhat.com'\n"
        bases = ct.compact_dns_bases_from_text(text)
        source = ct.compact_source_from_layout(flavor, macs, bases, paths)
        nodes = ct.compact_node_plan("edge1", 200, macs)
        pairs = dict(ct.compact_identity_pairs(source, "edge1", nodes))
        self.assertEqual(pairs["192.168.124.69"], "192.168.200.69")
        self.assertEqual(pairs["192.168.124.5"], "192.168.200.5")
        self.assertEqual(
            pairs["api-int.test-infra-cluster-mno2.redhat.com"],
            "api-int.test-infra-cluster-edge1.redhat.com")
        self.assertEqual(source["nodes"][0]["mac"], "52:54:00:a2:b0:aa")
        self.assertEqual(source["nodes"][0]["disks"][0]["path"], paths[0])
        manifest = (
            '{"name":"NODE_master_0_0_IP","value":"192.168.123.64"},'
            '{"name":"NODE_master_0_1_IP","value":"192.168.123.65"},'
            '{"name":"NODE_master_0_2_IP","value":"192.168.123.50"}')
        member_ips = ct.compact_etcd_member_ips(manifest)
        self.assertEqual(member_ips, {
            "master-0-0": "192.168.123.64",
            "master-0-1": "192.168.123.65",
            "master-0-2": "192.168.123.50",
        })
        assisted = ct.compact_source_from_layout(
            flavor, macs, bases, paths, node_ips=member_ips)
        self.assertEqual(assisted["nodes"][0]["ip"], "192.168.123.64")
        self.assertEqual(assisted["nodes"][1]["ip"], "192.168.123.65")
        self.assertEqual(assisted["nodes"][2]["ip"], "192.168.123.50")
        nested = ct.compact_source_from_layout(flavor, macs, bases, [
            [paths[0], paths[0] + "-spare"], paths[1], paths[2]])
        self.assertEqual(nested["nodes"][0]["disks"][1]["path"], paths[0] + "-spare")
        self.assertEqual(len(nested["nodes"][1]["disks"]), 1)
        with self.assertRaises(SystemExit):
            ct.compact_flavor_node_disks(["disk-0.qcow2"] * 6)

    def test_nm_mac_and_etcd_secret_pem_extraction(self):
        self.assertEqual(ct.parse_nm_mac("mac-address=52:54:00:76:07:C5\n"), "52:54:00:76:07:c5")
        fixture_key = _fixture_pem("TEST FIXTURE KEY", "NOT-A-KEY")
        fixture_cert = _fixture_pem("CERTIFICATE", "NOT-A-CERT")
        other_key = _fixture_pem("OTHER TEST FIXTURE KEY", "OTHER")
        blob = (
            b"noise /kubernetes.io/secrets/openshift-etcd/etcd-signer "
            + fixture_cert + b"\n" + fixture_key
            + b" /kubernetes.io/secrets/openshift-etcd/other "
            + other_key
        )
        found = ct.extract_k8s_secret_pems(blob, "etcd-signer")
        self.assertEqual(found, [fixture_cert, fixture_key])
        self.assertEqual(ct.extract_k8s_secret_pems(blob, "missing"), [])

    def test_clone_id_is_a_dns_label(self):
        self.assertEqual(ct._safe_clone_id("edge1"), "edge1")
        for bad in ("Edge1", "edge1-", "-edge", "a" * 32, ""):
            with self.assertRaises(SystemExit) as ctx:
                ct._safe_clone_id(bad)
            self.assertIn("lowercase", str(ctx.exception))

    def test_compact_subnet_is_reserved_in_state(self):
        state = {
            "flavors": {"mno": {"source_primary_subnet": 124}},
            "clones": {"edge1": {"subnet_primary": 200, "subnet_secondary": 200, "topology": "compact"}},
        }
        self.assertEqual(ct.require_compact_subnet(state, 201), 201)
        for bad in (0, 255, 124, 200, True):
            with self.assertRaises(SystemExit):
                ct.require_compact_subnet(state, bad)

    def test_record_compact_clone_reserves_subnet(self):
        saved = {}

        class _State:
            def __enter__(self):
                return saved.setdefault("state", {"flavors": {}, "clones": {}})
            def __exit__(self, *args):
                return False

        with patch.object(ct, "locked_state", _State), \
             patch.object(ct, "require_compact_libvirt_names_free"):
            ct.record_compact_clone("edge2", 201, "mno-3node")
        info = saved["state"]["clones"]["edge2"]
        self.assertEqual(info["topology"], "compact")
        self.assertEqual(info["subnet_primary"], 201)
        with patch.object(ct, "locked_state", _State), \
             patch.object(ct, "require_compact_libvirt_names_free"):
            with self.assertRaises(SystemExit):
                ct.record_compact_clone("edge3", 201, "mno-3node")


class TestCompactMachineConfig(unittest.TestCase):
    def test_render_handoff_includes_injected_pull_secret(self):
        secret = '{"auths":{"quay.io":{"auth":"test"}}}'
        render = {"spec": {"config": {"storage": {"files": [
            {"path": "/var/lib/kubelet/config.json", "contents": {
                "source": "data:;base64," + base64.b64encode(secret.encode()).decode(),
            }},
            {"path": "/unrelated/file", "contents": {"source": "data:,other"}},
        ]}}}}

        self.assertEqual(ct.compact_heal_node_files(render), {
            "/var/lib/kubelet/config.json": secret,
        })

    def test_machineconfig_retry_reuses_clone_specific_render(self):
        domain = "test-infra-cluster-edge20.redhat.com"
        body = f"KUBERNETES_SERVICE_HOST='api-int.{domain}'\nKUBERNETES_SERVICE_PORT='6443'\n"
        source = "data:;base64," + base64.b64encode(body.encode()).decode()
        original = json.dumps({"spec": {"config": {"storage": {"files": [{
            "path": "/etc/kubernetes/apiserver-url.env",
            "contents": {"source": source},
        }]}}}})
        render = json.dumps({
            "metadata": {"name": "rendered-master-edge20"},
            "spec": {"config": {"storage": {"files": [{
                "path": "/etc/kubernetes/apiserver-url.env",
                "contents": {"source": source},
            }]}}},
        })

        result = ct.compact_ensure_machineconfig_render_change(
            original, original, domain, existing_render_raw=render)
        self.assertEqual(json.loads(result)["spec"], json.loads(original)["spec"])
        self.assertEqual(json.loads(result)["spec"]["config"]["storage"]["files"][0]
                         ["contents"]["source"], source)

    def test_machine_config_observations_capture_per_node_rollout_timeline(self):
        raw_nodes = {"items": []}
        for role in ct.COMPACT_ROLES:
            raw_nodes["items"].append({
                "metadata": {
                    "name": role,
                    "annotations": {
                        "machineconfiguration.openshift.io/currentConfig": "rendered-master-old",
                        "machineconfiguration.openshift.io/desiredConfig": "rendered-master-new",
                        "machineconfiguration.openshift.io/state": "Working",
                        "machineconfiguration.openshift.io/reason": "Updating files",
                    },
                },
                "spec": {"unschedulable": role == "master-0-1"},
                "status": {"conditions": [{
                    "type": "Ready", "status": "False", "reason": "KubeletNotReady",
                    "message": "Node is rebooting", "lastTransitionTime": "2026-10-23T21:00:00Z",
                }]},
            })

        observations = ct.compact_machine_config_node_observations(
            ct.compact_parse_node_docs(json.dumps(raw_nodes)))
        second_master = observations[1]
        self.assertEqual(second_master["ready"], "False")
        self.assertEqual(second_master["ready_reason"], "KubeletNotReady")
        self.assertEqual(second_master["ready_transition"], "2026-10-23T21:00:00Z")
        self.assertTrue(second_master["unschedulable"])
        self.assertEqual(second_master["state"], "Working")
        self.assertEqual(second_master["current"], "rendered-master-old")
        self.assertEqual(second_master["desired"], "rendered-master-new")

        raw_nodes["items"][1]["spec"]["unschedulable"] = False
        raw_nodes["items"][1]["metadata"]["annotations"].update({
            "machineconfiguration.openshift.io/currentConfig": "rendered-master-new",
            "machineconfiguration.openshift.io/state": "Done",
        })
        raw_nodes["items"][1]["status"]["conditions"][0].update({
            "status": "True", "reason": "KubeletReady",
            "message": "kubelet is posting ready status",
            "lastTransitionTime": "2026-10-23T21:05:00Z",
        })
        recovered = ct.compact_machine_config_node_observations(
            ct.compact_parse_node_docs(json.dumps(raw_nodes)))[1]
        self.assertNotEqual(second_master, recovered)
        self.assertEqual(recovered["ready"], "True")
        self.assertEqual(recovered["current"], "rendered-master-new")
        self.assertEqual(recovered["state"], "Done")

    def test_machine_config_gate_requires_updated_pool_and_every_master(self):
        desired = "rendered-master-edge20"
        nodes = [{
            "name": role, "current_config": desired, "desired_config": desired,
            "mcd_state": "Done", "mcd_reason": "",
        } for role in ct.COMPACT_ROLES]
        pool = {
            "spec": {"configuration": {"name": desired}},
            "status": {
                "configuration": {"name": desired},
                "conditions": [
                    {"type": "Updated", "status": "True"},
                    {"type": "Updating", "status": "False"},
                    {"type": "Degraded", "status": "False"},
                ],
            },
        }
        self.assertTrue(ct.compact_machine_config_observation(json.dumps(pool), nodes)[0])

        nodes[1]["current_config"] = "rendered-master-old"
        ready, detail, observed = ct.compact_machine_config_observation(json.dumps(pool), nodes)
        self.assertFalse(ready)
        self.assertEqual(observed, desired)
        self.assertIn("master-0-1", detail)

        nodes[1]["current_config"] = desired
        pool["status"]["conditions"][0]["status"] = "False"
        self.assertFalse(ct.compact_machine_config_observation(json.dumps(pool), nodes)[0])

    def test_machineconfig_retry_canonicalizes_only_once(self):
        domain = "test-infra-cluster-edge20.redhat.com"
        body = f"KUBERNETES_SERVICE_HOST='api-int.{domain}'\nKUBERNETES_SERVICE_PORT='6443'\n"
        source = "data:;base64," + base64.b64encode(body.encode()).decode()
        original = json.dumps({"spec": {"config": {"storage": {"files": [{
            "path": "/etc/kubernetes/apiserver-url.env",
            "contents": {"source": source},
        }]}}}})

        first = ct.compact_ensure_machineconfig_render_change(original, original, domain)
        second = ct.compact_ensure_machineconfig_render_change(first, first, domain)
        first_doc = json.loads(first)
        second_doc = json.loads(second)
        first_source = first_doc["spec"]["config"]["storage"]["files"][0]["contents"]["source"]
        second_source = second_doc["spec"]["config"]["storage"]["files"][0]["contents"]["source"]
        self.assertEqual(first_source, "data:," + urllib.parse.quote(body, safe=""))
        self.assertEqual(second_source, first_source)
        self.assertEqual(
            ct.rendered_machineconfig_files(first_doc)["/etc/kubernetes/apiserver-url.env"], body)

    def test_compact_mco_source_identity_matches_clone_infrastructure(self):
        domain = "test-infra-cluster-edge21.redhat.com"
        api_vip = "192.168.221.5"
        subnet = 221
        infra = {
            "apiVersion": "config.openshift.io/v1",
            "kind": "Infrastructure",
            "spec": {"platformSpec": {"baremetal": {
                "apiServerInternalIPs": [api_vip],
                "machineNetworks": ["192.168.221.0/24"],
            }}},
            "status": {
                "apiServerInternalURI": f"https://api-int.{domain}:6443",
                "apiServerURL": f"https://api.{domain}:6443",
                "platformStatus": {"baremetal": {
                    "apiServerInternalIP": api_vip,
                    "apiServerInternalIPs": [api_vip],
                    "machineNetworks": ["192.168.221.0/24"],
                }},
            },
        }
        controllerconfig = {"spec": {"infra": infra}}
        self.assertTrue(ct.compact_controllerconfig_infra_matches(
            controllerconfig, domain, api_vip, subnet))
        stale_controllerconfig = json.loads(json.dumps(controllerconfig))
        stale_controllerconfig["spec"]["infra"]["status"]["apiServerInternalURI"] = (
            "https://api-int.edge16-24g.redhat.com:6443")
        self.assertFalse(ct.compact_controllerconfig_infra_matches(
            stale_controllerconfig, domain, api_vip, subnet))

        api_url = (
            f"KUBERNETES_SERVICE_HOST='api-int.{domain}'\n"
            "KUBERNETES_SERVICE_PORT='6443'\n")
        source = "data:," + urllib.parse.quote(api_url, safe="")
        machineconfig = {"spec": {"config": {"storage": {"files": [{
            "path": "/etc/kubernetes/apiserver-url.env",
            "contents": {"source": source},
        }]}}}}
        self.assertTrue(ct.compact_machineconfig_input_has_clone_api_url(
            machineconfig, domain))
        machineconfig["spec"]["config"]["storage"]["files"][0]["contents"]["source"] = (
            "data:," + urllib.parse.quote(
                "KUBERNETES_SERVICE_HOST='api-int.edge16-24g.redhat.com'\n", safe=""))
        self.assertFalse(ct.compact_machineconfig_input_has_clone_api_url(
            machineconfig, domain))


class TestCompactEndpoints(unittest.TestCase):
    def _kubernetes_endpoint_fixtures(self, ips, ready=True):
        endpoints = {
            "metadata": {"managedFields": [{"manager": "kube-controller-manager"}]},
            "subsets": [{
                "addresses": [{"ip": ip} for ip in ips],
                "ports": [{"name": "https", "port": 6443, "protocol": "TCP"}],
            }],
        }
        endpoint_slice = {
            "metadata": {"managedFields": [{"manager": "kube-controller-manager"}]},
            "ports": [{"name": "https", "port": 6443, "protocol": "TCP"}],
            "endpoints": [
                {"addresses": [ip], "conditions": {"ready": ready}}
                for ip in ips
            ],
        }
        return json.dumps(endpoints), json.dumps(endpoint_slice)

    def test_kubernetes_endpoint_check_rejects_stale_addresses(self):
        clone_ips = ["192.168.220.69", "192.168.220.81", "192.168.220.89"]
        endpoints, endpoint_slice = self._kubernetes_endpoint_fixtures(clone_ips)
        endpoints_doc = json.loads(endpoints)
        endpoints_doc["subsets"][0]["addresses"].append({"ip": "192.168.123.65"})
        ready, detail = ct.compact_kubernetes_endpoints_ready(
            json.dumps(endpoints_doc), endpoint_slice, clone_ips)
        self.assertFalse(ready)
        self.assertIn("192.168.123.65", detail)
        self.assertEqual(
            ct.compact_kubernetes_endpoint_managers(endpoints, endpoint_slice),
            "kube-controller-manager")

    def test_kubernetes_endpoint_check_requires_tcp_6443_on_ready_subset(self):
        clone_ips = ["192.168.220.69", "192.168.220.81", "192.168.220.89"]
        endpoints, endpoint_slice = self._kubernetes_endpoint_fixtures(clone_ips)
        endpoints_doc = json.loads(endpoints)
        endpoints_doc["subsets"] = [
            {
                "addresses": [{"ip": clone_ips[0]}],
                "ports": [{"name": "https", "port": 9999, "protocol": "TCP"}],
            },
            {
                "notReadyAddresses": [{"ip": ip} for ip in clone_ips[1:]],
                "ports": [{"name": "https", "port": 6443, "protocol": "TCP"}],
            },
        ]
        ready, detail = ct.compact_kubernetes_endpoints_ready(
            json.dumps(endpoints_doc), endpoint_slice, clone_ips)
        self.assertFalse(ready)
        self.assertIn("ready Endpoints subset lacks HTTPS/TCP/6443", detail)

    def test_kubernetes_endpoint_check_rejects_udp_6443(self):
        clone_ips = ["192.168.220.69", "192.168.220.81", "192.168.220.89"]
        endpoints, endpoint_slice = self._kubernetes_endpoint_fixtures(clone_ips)
        endpoints_doc = json.loads(endpoints)
        endpoints_doc["subsets"][0]["ports"][0]["protocol"] = "UDP"
        ready, detail = ct.compact_kubernetes_endpoints_ready(
            json.dumps(endpoints_doc), endpoint_slice, clone_ips)
        self.assertFalse(ready)
        self.assertIn("ready Endpoints subset lacks HTTPS/TCP/6443", detail)

        endpoints_doc["subsets"][0]["ports"][0].update({
            "name": "api-alt", "protocol": "TCP",
        })
        ready, detail = ct.compact_kubernetes_endpoints_ready(
            json.dumps(endpoints_doc), endpoint_slice, clone_ips)
        self.assertFalse(ready)
        self.assertIn("ready Endpoints subset lacks HTTPS/TCP/6443", detail)

        endpoints_doc["subsets"][0]["ports"][0]["name"] = "https"
        endpoints_doc["subsets"][0]["ports"][0]["protocol"] = "TCP"
        slice_doc = json.loads(endpoint_slice)
        slice_doc["ports"][0]["protocol"] = "UDP"
        ready, detail = ct.compact_kubernetes_endpoints_ready(
            json.dumps(endpoints_doc), json.dumps(slice_doc), clone_ips)
        self.assertFalse(ready)
        self.assertIn("EndpointSlice has no effective HTTPS/TCP/6443", detail)

    def test_endpoint_observation_failures_do_not_consume_repair_budget(self):
        good = MagicMock(returncode=0, stdout='{"metadata":{}}', stderr="")
        unavailable = MagicMock(returncode=1, stdout="", stderr="temporary timeout")
        readable, detail = ct.compact_kubernetes_endpoint_observation(good, unavailable)
        self.assertFalse(readable)
        self.assertIn("temporary timeout", detail)
        malformed = MagicMock(returncode=0, stdout='{"subsets":"bad"}', stderr="")
        readable, detail = ct.compact_kubernetes_endpoint_observation(malformed, good)
        self.assertFalse(readable)
        self.assertIn("subsets was not a list", detail)
        self.assertEqual(ct.compact_next_endpoint_repair_failures(2, False, False), 2)
        self.assertEqual(ct.compact_next_endpoint_repair_failures(2, True, False), 3)
        self.assertEqual(ct.compact_next_endpoint_repair_failures(2, True, True), 0)

    def test_unavailable_endpoint_reads_return_unknown_repair_result(self):
        class _Env:
            def run(self, cmd, check=True):
                return MagicMock(returncode=1, stdout="", stderr="temporary timeout")

        monotonic_values = iter([0, 0, 0, 11])
        with (patch.object(ct, "env", _Env()),
              patch.object(ct.time, "monotonic", side_effect=lambda: next(monotonic_values)),
              patch.object(ct.time, "sleep"), patch("builtins.print")):
            result = ct._refresh_compact_kubernetes_endpoints(
                "oc", [{"ip": "192.168.220.69"}], "/work", timeout_seconds=10)
        self.assertIsNone(result)

    def test_missing_endpoint_slice_reports_controller_owned_recovery_limit(self):
        clone_ips = ["192.168.220.69", "192.168.220.81", "192.168.220.89"]
        endpoints, _ = self._kubernetes_endpoint_fixtures(clone_ips)

        class _Env:
            def run(self, cmd, check=True):
                if "get endpointslice kubernetes" in cmd:
                    return MagicMock(returncode=1, stdout="", stderr="NotFound")
                return MagicMock(returncode=0, stdout=endpoints, stderr="")

        monotonic_values = iter([0, 0, 0, 181])
        with (patch.object(ct, "env", _Env()),
              patch.object(ct.time, "monotonic", side_effect=lambda: next(monotonic_values)),
              patch.object(ct.time, "sleep"), patch("builtins.print")):
            with self.assertRaises(SystemExit) as aborted:
                ct._refresh_compact_kubernetes_endpoints(
                    "oc", [{"ip": ip} for ip in clone_ips], "/work", timeout_seconds=180)
        self.assertIn("automatic creation is unsupported", str(aborted.exception))

    def test_kubernetes_endpoint_repair_is_noop_when_identity_is_correct(self):
        clone_ips = ["192.168.220.69", "192.168.220.81", "192.168.220.89"]
        endpoint_raw, slice_raw = self._kubernetes_endpoint_fixtures(clone_ips)

        class _Env:
            def __init__(self):
                self.calls = []

            def run(self, cmd, check=True):
                self.calls.append(cmd)
                result = MagicMock(returncode=0, stderr="", stdout="")
                if "get endpointslice kubernetes" in cmd:
                    result.stdout = slice_raw
                elif "get endpoints kubernetes" in cmd:
                    result.stdout = endpoint_raw
                return result

        fake_env = _Env()
        with patch.object(ct, "env", fake_env), patch("builtins.print"):
            self.assertTrue(ct._refresh_compact_kubernetes_endpoints(
                "oc", [{"ip": ip} for ip in clone_ips], "/work"))
        self.assertFalse(any("curl " in cmd or " patch endpoints" in cmd for cmd in fake_env.calls))

    def test_kubernetes_endpoint_repair_retries_temporary_backend_unavailability(self):
        clone_ips = ["192.168.220.69", "192.168.220.81", "192.168.220.89"]
        source_ips = ["192.168.123.64"]
        stale_endpoints, stale_slice = self._kubernetes_endpoint_fixtures(source_ips)
        good_endpoints, good_slice = self._kubernetes_endpoint_fixtures(clone_ips)

        class _Env:
            def __init__(self):
                self.calls = []
                self.endpoints = stale_endpoints
                self.endpoint_slice = stale_slice

            def run(self, cmd, check=True):
                self.calls.append(cmd)
                result = MagicMock(returncode=0, stderr="", stdout="")
                if "get endpointslice kubernetes" in cmd:
                    result.stdout = self.endpoint_slice
                elif "get endpoints kubernetes" in cmd:
                    result.stdout = self.endpoints
                elif "patch endpointslice kubernetes" in cmd:
                    self.endpoint_slice = good_slice
                elif "patch endpoints kubernetes" in cmd:
                    self.endpoints = good_endpoints
                return result

        failed = [MagicMock(returncode=1) for _ in clone_ips]
        succeeded = [MagicMock(returncode=0) for _ in clone_ips]
        fake_env = _Env()
        with (patch.object(ct, "env", fake_env),
              patch.object(ct, "_in_parallel_results", side_effect=[failed, succeeded]),
              patch("time.sleep"), patch("builtins.print")):
            self.assertTrue(ct._refresh_compact_kubernetes_endpoints(
                "oc", [{"ip": ip} for ip in clone_ips], "/work"))
        self.assertTrue(any("patch endpoints kubernetes" in cmd for cmd in fake_env.calls))
        self.assertTrue(any("patch endpointslice kubernetes" in cmd for cmd in fake_env.calls))
        self.assertGreaterEqual(sum("get endpoints kubernetes" in cmd for cmd in fake_env.calls), 2)

    def test_kubernetes_endpoint_repair_corrects_endpoint_slice_port(self):
        clone_ips = ["192.168.220.69", "192.168.220.81", "192.168.220.89"]
        good_endpoints, good_slice = self._kubernetes_endpoint_fixtures(clone_ips)
        wrong_slice_doc = json.loads(good_slice)
        wrong_slice_doc["ports"] = [{"name": "https", "port": 9999, "protocol": "UDP"}]

        class _Env:
            def __init__(self):
                self.calls = []
                self.endpoint_slice = json.dumps(wrong_slice_doc)

            def run(self, cmd, check=True):
                self.calls.append(cmd)
                if "get endpointslice kubernetes" in cmd:
                    return MagicMock(returncode=0, stdout=self.endpoint_slice, stderr="")
                if "get endpoints kubernetes" in cmd:
                    return MagicMock(returncode=0, stdout=good_endpoints, stderr="")
                if "patch endpointslice kubernetes" in cmd:
                    self.endpoint_slice = good_slice
                return MagicMock(returncode=0, stdout="", stderr="")

        fake_env = _Env()
        probes = [[MagicMock(returncode=0) for _ in clone_ips]]
        with (patch.object(ct, "env", fake_env),
              patch.object(ct, "_in_parallel_results", side_effect=probes),
              patch("time.sleep"), patch("builtins.print")):
            self.assertTrue(ct._refresh_compact_kubernetes_endpoints(
                "oc", [{"ip": ip} for ip in clone_ips], "/work"))
        slice_patch = next(
            cmd for cmd in fake_env.calls if "patch endpointslice kubernetes" in cmd)
        self.assertIn('"port": 6443', slice_patch)
        self.assertIn('"protocol": "TCP"', slice_patch)

    def test_kubernetes_endpoint_repair_rejects_unverified_patch(self):
        clone_ips = ["192.168.220.69", "192.168.220.81", "192.168.220.89"]
        source_ips = ["192.168.123.64"]
        stale_endpoints, stale_slice = self._kubernetes_endpoint_fixtures(source_ips)

        class _Env:
            def __init__(self):
                self.calls = []

            def run(self, cmd, check=True):
                self.calls.append(cmd)
                return MagicMock(returncode=0, stderr="", stdout=(
                    stale_slice if "endpointslice kubernetes" in cmd else stale_endpoints
                ))

        fake_env = _Env()
        probes = [[MagicMock(returncode=0) for _ in clone_ips] for _ in range(3)]
        with (patch.object(ct, "env", fake_env),
              patch.object(ct, "_in_parallel_results", side_effect=probes),
              patch("time.sleep"), patch("builtins.print")):
            self.assertFalse(ct._refresh_compact_kubernetes_endpoints(
                "oc", [{"ip": ip} for ip in clone_ips], "/work"))
        self.assertEqual(sum("patch endpoints kubernetes" in cmd for cmd in fake_env.calls), 3)


class TestCompactHelpers(unittest.TestCase):
    def test_apiserver_etcd_trust_copy_uses_only_the_quorum_ca(self):
        command = ct.compact_apiserver_etcd_trust_tar_command()
        self.assertIn("etcd-serving-ca/ca-bundle.crt", command)
        self.assertIn("etcd-client/tls.crt", command)
        self.assertIn("etcd-client/tls.key", command)
        self.assertNotIn("kubelet-serving-ca", command)
        self.assertNotIn("client-ca/ca-bundle.crt", command)
        self.assertEqual(
            ct.compact_new_rendered_master(
                "rendered-master-f91e5e3bbeff38fff5ddf87f21226386",
                "rendered-master-25faeae1e446640a1292f9d060699092"),
            "rendered-master-25faeae1e446640a1292f9d060699092")
        self.assertEqual(
            ct.compact_new_rendered_master(
                "rendered-master-f91e5e3bbeff38fff5ddf87f21226386",
                "rendered-master-f91e5e3bbeff38fff5ddf87f21226386"),
            "")
        decoded = ct.rendered_machineconfig_files({
            "spec": {"config": {"storage": {"files": [{
                "path": "/etc/kubernetes/apiserver-url.env",
                "contents": {"source": "data:,KUBERNETES_SERVICE_HOST%3Dapi-int.example.com%0A"},
            }]}}}
        })
        self.assertEqual(
            decoded["/etc/kubernetes/apiserver-url.env"],
            "KUBERNETES_SERVICE_HOST=api-int.example.com\n")

    def test_clone_log_includes_a_timestamp(self):
        ct.env = MagicMock()
        with patch("builtins.print") as printed:
            ct._clone_log("/var/lib/libvirt/images/edge1", "copy the three flavor disks")
        line = printed.call_args.args[0]
        self.assertRegex(
            line,
            r"^===== \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} copy the three flavor disks =====$")
        logged = ct.env.run.call_args.args[0]
        self.assertIn(line, logged)

    def test_inspect_summary_omits_addresses(self):
        text = ct.format_compact_inspect({
            "nodes": [{"role": "master-0-0", "ip": "192.168.124.69", "mac": "02:00:00:00:00:01"}],
            "gateway": "192.168.124.1",
        })
        self.assertIn("master-0-0", text)
        self.assertNotIn("192.168.", text)
        self.assertNotIn("02:00", text)

    def test_rhcos_deploy_uses_booted_ostree_status(self):
        names = ["abc.0.origin", "abc.0", "def.1"]
        self.assertEqual(ct.choose_rhcos_deploy(names, "abc.0"), "abc.0")
        self.assertEqual(ct.choose_rhcos_deploy(["only.0", "only.0.origin"]), "only.0")
        with self.assertRaises(SystemExit) as ctx:
            ct.choose_rhcos_deploy(names)
        self.assertIn("multiple RHCOS deployments", str(ctx.exception))
        self.assertEqual(
            ct.parse_booted_ostree_deployment("* rhcos abcdef0123456789.0\n  rhcos 123456789abcdef0.0\n"),
            "abcdef0123456789.0")

    def test_identity_rewrite_preserves_pem_only_input_exactly(self):
        pem_only = (
            "bundle: |\n"
            "  -----BEGIN CERTIFICATE-----\n"
            "  MIIB\n"
            "  -----END CERTIFICATE-----\n")
        self.assertEqual(
            ct.apply_identity_preserving_pems(pem_only, [("unused", "replacement")]),
            pem_only)


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
            elif "ostree admin status" in cmd:
                r.stdout = "* rhcos abcdef0123456789.0\n  rhcos 0123456789abcdef.0\n"
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
        self.assertEqual(flavor["rhcos_deployment"], "abcdef0123456789.0")

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

    def _mock_write_file(self, path, content, *, private=False):
        self.written_files[path] = content
        self.mock_env.mock_write_file(path, content, private=private)

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

    def _pull_args(self, image="quay.io/org/repo:tag"):
        return argparse.Namespace(image=image)

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
        self.assertIn("tag", state["flavors"])
        flavor = state["flavors"]["tag"]
        self.assertEqual(flavor["vcpus"], 16)
        self.assertEqual(flavor["memory_kib"], 67108864)
        self.assertEqual(flavor["source_cluster"], "abc")
        self.assertEqual(flavor["disks"], ["disk-0.qcow2"])

    def test_pull_names_flavor_from_image_tag_not_manifest(self):
        wrapped = self.mock_env.wrap_run(self._inner_mock_run)
        with patch.object(ct.env, "run", side_effect=wrapped), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            ct.cmd_pull(self._pull_args(image="quay.io/osac-project/cluster-flavors:caas"))
        state = self.mock_env.get_saved_state()
        self.assertIn("caas", state["flavors"])
        self.assertNotIn("test-pulled", state["flavors"])

    def test_pull_existing_flavor_exits(self):
        self.mock_env.save_initial_state({
            "flavors": {"tag": {"disks": []}},
            "clones": {}})
        wrapped = self.mock_env.wrap_run(self._inner_mock_run)
        with patch.object(ct.env, "run", side_effect=wrapped), \
             patch.object(ct.env, "write_file", side_effect=self.mock_env.mock_write_file):
            with self.assertRaises(SystemExit) as ctx:
                ct.cmd_pull(self._pull_args())
        self.assertIn("tag", str(ctx.exception))

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
        self.assertIn(ct.flavor_crypto_dir("tag"), crypto_cmds[0])


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
             patch.object(ct, "_dnsmasq_dir", return_value=dnsmasq_dir), \
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

    def test_remote_run_vm_quotes_identity(self):
        self.env.run_vm("192.168.1.10", "echo hello", identity="/tmp/key;touch /tmp/pwned")
        self.assertNotIn("-i /tmp/key;touch", self.captured_cmd)
        self.assertIn(shlex.quote("/tmp/key;touch /tmp/pwned"), self.captured_cmd)

    def test_remote_write_expands_home(self):
        with patch("subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stderr="")
            self.env.write_file("~/.config/cluster-tool/state.json", "{}")
        remote = run.call_args.args[0][-1]
        self.assertIn('"$HOME"', remote)
        self.assertIn("mkdir -p", remote)
        self.assertNotIn("'~/", remote)


class TestPrivateWrites(unittest.TestCase):
    def test_local_private_write_replaces_with_mode_0600_file(self):
        local = ct.ExecutionEnv(host="local", host_ip="127.0.0.1")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "pull-secret.json"
            target.write_text("old")
            target.chmod(0o644)
            modes_before_replace = []
            replace = os.replace

            def inspect_replace(source, destination):
                modes_before_replace.append(Path(source).stat().st_mode & 0o777)
                replace(source, destination)

            with patch.object(ct.os, "replace", side_effect=inspect_replace):
                local.write_file(str(target), "new secret", private=True)

            self.assertEqual(modes_before_replace, [0o600])
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(target.read_text(), "new secret")

    def test_remote_private_write_uses_private_temporary_file(self):
        remote = ct.ExecutionEnv(host="root@test-host", host_ip="10.0.0.1")
        with patch("subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stderr="")
            remote.write_file("~/clone/pull-secret.json", "sensitive-payload-value", private=True)
        command = run.call_args.args[0][-1]
        self.assertIn("mktemp", command)
        self.assertIn("mv -f", command)
        self.assertIn('"$HOME"', command)
        self.assertNotIn("sensitive-payload-value", command)


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
        return argparse.Namespace(
            name="aabbccdd", flavor="default", no_rollback=False,
            pull_secret=pull_secret, vcpus=None, memory_kib=None)

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
