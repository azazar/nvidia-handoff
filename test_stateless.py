"""Isolated discovery and safety regression tests; no host/device mutations."""
import ast
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent


def module(filename):
    result = types.ModuleType(filename)
    result.__file__ = str(ROOT / filename)
    exec(compile((ROOT / filename).read_bytes(), result.__file__, 'exec'), result.__dict__)
    return result


class StatelessTests(unittest.TestCase):
    def setUp(self):
        self.core = module('nvidia_handoff_core.py')
        self.app = module('nvidia-handoff')
        self.tmp = tempfile.TemporaryDirectory(prefix='.test-', dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.core.SYS = self.root / 'sys'
        (self.core.SYS / 'bus/pci/devices').mkdir(parents=True)
        self.core.run = lambda *args: 'nvidia' if 'modinfo' in args[0] else ''
        self.gpu = '0000:02:00.0'

    def device(self, bdf=None, kind='0x030000', driver=None):
        path = self.core.pci(bdf or self.gpu)
        path.mkdir()
        for name, value in dict(vendor='0x10de', device='0x1234', boot_vga='0', **{'class': kind}).items():
            (path / name).write_text(value)
        if driver:
            drivers = self.root / 'drivers' / driver
            drivers.mkdir(parents=True, exist_ok=True)
            (path / 'driver').symlink_to(drivers)
        return path

    def mapped_path(self, value):
        path = Path(value)
        if str(path).startswith(('/etc/', '/run', '/opt/', '/var/')):
            return self.root / str(path).lstrip('/')
        return path

    @contextlib.contextmanager
    def owned_paths(self):
        lstat, fstat = Path.lstat, os.fstat
        def owned(st):
            values = list(st)
            values[4] = 0
            return os.stat_result(values)
        with patch.object(Path, 'lstat', lambda path: owned(lstat(path))), \
             patch.object(os, 'fstat', lambda fd: owned(fstat(fd))):
            yield

    def discovery(self):
        self.core.Path = self.mapped_path
        self.core.xorg_path = lambda: '/etc/X11/xorg.conf.d/90-nvidia-handoff.conf'
        self.core.runtime_path = lambda gpu, devices: '/run/nvidia-handoff/' + gpu
        return self.core.discover()

    def test_discovery_native_and_vfio_use_actual_identity(self):
        for driver in ('nvidia', 'vfio-pci'):
            with self.subTest(driver=driver):
                p = self.device(driver=driver)
                c = self.discovery()
                self.assertEqual(c['gpu'], self.gpu)
                self.assertEqual(c['devices'][0]['host_driver'], 'nvidia')
                self.assertEqual(c['devices'][0]['device'], '0x1234')
                (p / 'driver').unlink()
                for child in p.iterdir(): child.unlink()
                p.rmdir()

    def test_xorg_policy_is_discovered_without_creating_files(self):
        self.core.Path = self.mapped_path
        folder = self.root / 'etc/X11/xorg.conf.d'
        folder.mkdir(parents=True)
        p = folder / '10-user-policy.conf'
        p.write_text('Section "ServerFlags"\n Option "AutoAddGPU" "off"\nEndSection\n')
        p.chmod(0o644)
        before = p.read_bytes()
        with self.owned_paths():
            self.assertEqual(self.core.xorg_path(), str(p))
        self.assertEqual(p.read_bytes(), before)

    def test_conflicting_xorg_policies_refused(self):
        self.core.Path = self.mapped_path
        folder = self.root / 'etc/X11/xorg.conf.d'
        folder.mkdir(parents=True)
        for name in ('10-first.conf', '90-second.conf'):
            (folder / name).write_text('Option "AutoAddGPU" "off"\n')
            (folder / name).chmod(0o644)
        with self.owned_paths(), self.assertRaises(self.core.Refuse):
            self.core.xorg_path()

    def test_runtime_directory_discovered_from_transaction(self):
        self.core.Path = self.mapped_path
        directory = self.root / 'run/legacy-gpu-handoff'
        directory.mkdir(parents=True, mode=0o700)
        p = directory / 'state.json'
        p.write_text(json.dumps({'host': {'drivers': {self.gpu: 'nvidia'}}}))
        p.chmod(0o600)
        with self.owned_paths():
            self.assertEqual(self.core.runtime_path(self.gpu, [self.gpu]), str(directory))

    def test_runtime_directory_remains_discoverable_without_transaction(self):
        self.core.Path = self.mapped_path
        directory = self.root / 'run/legacy-gpu-handoff'
        directory.mkdir(parents=True, mode=0o700)
        p = directory / 'lock'
        p.write_text('nvidia-handoff ' + self.gpu + '\n')
        p.chmod(0o600)
        with self.owned_paths():
            self.assertEqual(self.core.runtime_path(self.gpu, [self.gpu]), str(directory))

    def test_discovery_without_runtime_does_not_create_it(self):
        self.core.Path = self.mapped_path
        with self.owned_paths():
            result = self.core.runtime_path(self.gpu, [self.gpu])
        self.assertEqual(result, str(self.root / 'run/nvidia-handoff' / self.gpu))
        self.assertFalse((self.root / 'run').exists())

    def test_stale_process_refuses_before_mkdir_without_settings_file(self):
        p = self.root / 'installed'
        p.write_text('v1')
        self.core.SETTINGS = {'program_identity': {str(p): [1, 2, 3]}}
        self.core.STATE_DIR = self.root / 'state'
        with patch.object(os, 'geteuid', return_value=0), self.assertRaises(self.core.Refuse):
            self.core.lock()
        self.assertFalse(self.core.STATE_DIR.exists())

    def test_uninstalled_process_refuses_before_mkdir(self):
        self.core.SETTINGS = {'program_identity': {str(self.root / 'removed'): [1, 2, 3]}}
        self.core.STATE_DIR = self.root / 'state'
        with patch.object(os, 'geteuid', return_value=0), self.assertRaises(self.core.Refuse):
            self.core.lock()
        self.assertFalse(self.core.STATE_DIR.exists())

    def test_receipts_preserve_shebang_and_both_comment_formats(self):
        for body in (self.app.HOOK, self.app.XORG_OFF, b'-- audio policy\\nreturn true\\n'):
            with self.subTest(body=body):
                p = self.root / 'policy'
                p.write_bytes(self.app.marked(body))
                p.chmod(0o644)
                with self.owned_paths():
                    self.assertEqual(self.app.receipt(p), (body, 'absent'))
                    self.app.check_managed(p, body)
                if body.startswith(b'#!'):
                    self.assertEqual(p.read_bytes().splitlines()[0], b'#!/bin/sh')
                if body.startswith(b'--'):
                    self.assertTrue(p.read_bytes().startswith(b'-- nvidia-handoff'))

    def test_receipt_rejects_administrator_edit(self):
        p = self.root / 'policy'
        p.write_bytes(self.app.marked(self.app.XORG_OFF) + b'# edited\\n')
        p.chmod(0o644)
        with self.owned_paths(), self.assertRaises(self.app.Error):
            self.app.receipt(p)

    def test_xorg_toggle_preserves_original_and_refreshes_hash(self):
        p = self.root / 'policy'
        p.write_bytes(self.app.XORG_OFF)
        p.chmod(0o644)
        self.app.CONFIG = p
        self.app.write_file = lambda path, data, mode: path.write_bytes(data)
        with self.owned_paths():
            self.app.atomic_config(p.read_bytes().replace(b'"off"', b'"on"'))
            body, original = self.app.receipt(p)
            self.assertEqual(original, 'off')
            self.assertIn(b'"on"', body)
            self.app.atomic_config(p.read_bytes().replace(b'"on"', b'"off"'))
            self.assertEqual(self.app.receipt(p), (self.app.XORG_OFF, 'off'))

    def test_initialize_never_reads_legacy_json_and_reprobes(self):
        self.app.LEGACY_CONFIG = Mock()
        h = Mock()
        c = dict(gpu=self.gpu, label='live label', devices=[], state_dir='unused')
        h.discover.side_effect = [dict(c), {**c, 'label': 'new live label'}]
        h.DEVICES = (self.gpu,)
        h.GPU = self.gpu
        h.XORG_CONFIG = self.root / 'policy'
        self.app.load_core = lambda: h
        self.app.hook_path = lambda: self.root / 'hook'
        self.app.initialize()
        self.assertEqual(self.app.LABEL, 'live label')
        self.app.initialize(self.gpu)
        self.assertEqual(self.app.LABEL, 'new live label')
        self.app.LEGACY_CONFIG.assert_not_called()
        self.assertFalse(self.app.LEGACY_CONFIG.mock_calls)

    def test_hook_reconnect_does_not_discover_or_read_stdin(self):
        self.app.load_core = Mock(side_effect=AssertionError('must not load core'))
        with patch.object(self.app.sys, 'stdin', Mock()):
            self.assertEqual(self.app.main(['--libvirt-hook', 'guest', 'reconnect', 'begin', '-']), 0)
            self.app.sys.stdin.read.assert_not_called()

    def test_hook_ignores_unrelated_guest_without_initialization(self):
        self.app.load_core = lambda: self.core
        self.app.initialize = Mock(side_effect=AssertionError('must not initialize'))
        self.core.pci_gpu_devices = lambda: []
        with patch.object(self.app.sys, 'stdin', io.StringIO('<domain><devices/></domain>')):
            self.assertEqual(self.app.main(['--libvirt-hook', 'guest', 'prepare', 'begin', '-']), 0)

    def test_hook_selects_gpu_from_audio_function_without_libvirt(self):
        self.device()
        self.core.pci_gpu_devices = lambda: [self.core.pci(self.gpu)]
        xml = '<domain><devices><hostdev type="pci"><source><address domain="0x0000" bus="0x02" slot="0x00" function="0x1"/></source></hostdev></devices></domain>'
        self.assertEqual(self.app.hook_gpu(self.core, xml), self.gpu)
        self.app.initialize = Mock()
        hook = Mock()
        self.app.load_core = lambda: self.core
        self.app.load_hook = lambda: hook
        self.app.virsh = Mock(side_effect=AssertionError('libvirt forbidden'))
        with patch.object(self.app.sys, 'stdin', io.StringIO(xml)):
            self.app.main(['--libvirt-hook', 'guest', 'prepare', 'begin', '-'])
        self.app.initialize.assert_called_once_with(self.gpu)
        hook.main.assert_called_once_with(['guest', 'prepare', 'begin', '-'], xml)

    def test_gpu_choice_propagates_to_child_commands(self):
        self.app.GPU = self.gpu
        self.app.SETTINGS = {'display_manager': 'example.service'}
        command = self.app.program_command('start-vm', 'guest', '--without-gpu')
        self.assertEqual(command[-7:], ['--gpu', self.gpu, '--display-manager', 'example.service', 'start-vm', 'guest', '--without-gpu'])

    def test_uuid_dispatch_does_not_treat_uuid_as_name(self):
        ident = '48b2b9fb-782a-4661-a1de-5ab22219beb2'
        self.app.initialize = Mock()
        self.app.virsh = Mock(side_effect=AssertionError('UUID must not be looked up as a name'))
        self.app.start_optional = Mock()
        self.app.main(['start-vm', ident, '--without-gpu'])
        self.app.start_optional.assert_called_once_with(ident, without_gpu=True)


    def test_malformed_receipt_is_not_adopted_as_user_policy(self):
        p = self.root / 'policy'
        p.write_bytes(b'# nvidia-handoff sha256=broken original=off\n' + self.app.XORG_OFF)
        p.chmod(0o644)
        with self.owned_paths(), self.assertRaises(self.app.Error):
            self.app.receipt(p)

    def test_private_runtime_is_ignored_for_unprivileged_inventory(self):
        self.core.Path = self.mapped_path
        exists = Path.exists
        def private(path):
            if '/run/' in str(path):
                raise PermissionError('private')
            return exists(path)
        with patch.object(Path, 'exists', private):
            self.assertEqual(self.core.runtime_path(self.gpu, [self.gpu]),
                             str(self.root / 'run/nvidia-handoff' / self.gpu))

    def migration_fixture(self):
        (self.root / 'run/systemd/system').mkdir(parents=True)
        runtime = self.root / 'run/legacy-gpu-handoff'
        runtime.mkdir(mode=0o700)
        (runtime / 'state.json').write_text('active transaction')
        hook = self.root / 'etc/libvirt/hooks/qemu.d/50-old-gpu'
        hook.parent.mkdir(parents=True)
        hook.write_bytes(self.app.HOOK); hook.chmod(0o755)
        xorg = self.root / 'etc/X11/xorg.conf.d/10-user.conf'
        xorg.parent.mkdir(parents=True)
        xorg.write_bytes(self.app.XORG_OFF); xorg.chmod(0o644)
        self.app.LEGACY_CONFIG = self.root / 'etc/nvidia-handoff.json'
        config = dict(version=2, gpu=self.gpu, devices=[dict(bdf=self.gpu, vendor='0x10de', device='0x1234')],
                      xorg_config=str(xorg), hook_path=str(hook), state_dir='/run/legacy-gpu-handoff')
        previous = {**config, 'managed_files': {str(hook): self.app.digest(self.app.HOOK)},
                    'xorg_original_mode': 'off', 'xorg_applied_mode': 'off'}
        self.app.LEGACY_CONFIG.write_text(json.dumps(previous)); self.app.LEGACY_CONFIG.chmod(0o644)
        for directory in (self.root / 'etc', hook.parent, xorg.parent):
            directory.chmod(0o755)
        h = Mock()
        h.assert_x11 = Mock()
        h.discover.return_value = dict(config)
        h.STATE_DIR = runtime
        h.lock.side_effect = lambda **kw: os.open(runtime / 'lock', os.O_CREAT | os.O_RDWR, 0o600)
        self.app.load_core = lambda **kw: h
        self.app.Path = self.mapped_path
        self.app.hook_path = lambda: hook
        def write(path, data, mode=0o644):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.parent.chmod(0o755)
            path.write_bytes(data); path.chmod(mode)
        self.app.write_file = write
        return h, runtime, hook, xorg

    def test_explicit_migration_preserves_active_transaction_and_restoration(self):
        h, runtime, hook, xorg = self.migration_fixture()
        before = (runtime / 'state.json').read_bytes()
        with self.owned_paths(), patch.object(os, 'geteuid', return_value=0), \
             patch.object(self.app.shutil, 'which', return_value='/usr/bin/tool'), contextlib.redirect_stdout(io.StringIO()):
            self.app.install([])
            self.assertEqual(self.app.receipt(hook), (self.app.HOOK, 'absent'))
            self.assertEqual(self.app.receipt(xorg), (self.app.XORG_OFF, 'off'))
        self.assertFalse(self.app.LEGACY_CONFIG.exists())
        self.assertEqual((runtime / 'state.json').read_bytes(), before)
        h.topology.assert_called_once()
        h.load.assert_called_once()
        h.prepare.assert_not_called()
        h.restore.assert_not_called()
        self.assertEqual((self.root / 'opt/nvidia-handoff').read_bytes(), (ROOT / 'nvidia-handoff').read_bytes())

    def test_migration_write_failure_restores_files_and_retains_manifest(self):
        h, runtime, hook, xorg = self.migration_fixture()
        write = self.app.write_file
        def fail(path, data, mode=0o644):
            if path == xorg and self.app.RECEIPT.search(data):
                raise OSError('injected write failure')
            write(path, data, mode)
        self.app.write_file = fail
        before = self.app.LEGACY_CONFIG.read_bytes()
        with self.owned_paths(), patch.object(os, 'geteuid', return_value=0), \
             patch.object(self.app.shutil, 'which', return_value='/usr/bin/tool'), \
             contextlib.redirect_stdout(io.StringIO()), self.assertRaises(OSError):
            self.app.install([])
        self.assertEqual(self.app.LEGACY_CONFIG.read_bytes(), before)
        self.assertEqual(hook.read_bytes(), self.app.HOOK)
        self.assertEqual(xorg.read_bytes(), self.app.XORG_OFF)
        self.assertFalse((self.root / 'opt/nvidia-handoff').exists())
        self.assertFalse((self.root / 'opt/nvidia_handoff_core.py').exists())

    def test_migration_refuses_edited_legacy_hook_before_writes(self):
        h, runtime, hook, xorg = self.migration_fixture()
        hook.write_bytes(self.app.HOOK + b'# administrator edit\n')
        self.app.write_file = Mock(side_effect=AssertionError('must not write'))
        with self.owned_paths(), patch.object(os, 'geteuid', return_value=0), \
             patch.object(self.app.shutil, 'which', return_value='/usr/bin/tool'), self.assertRaises(self.app.Error):
            self.app.install([])
        h.lock.assert_not_called()

    def test_installer_preview_is_read_only(self):
        h, runtime, hook, xorg = self.migration_fixture()
        self.app.write_file = Mock(side_effect=AssertionError('must not write'))
        with self.owned_paths(), patch.object(self.app.shutil, 'which', return_value='/usr/bin/tool'), \
             contextlib.redirect_stdout(io.StringIO()):
            self.app.install(['--dry-run'])
        h.lock.assert_not_called()
        self.assertTrue(self.app.LEGACY_CONFIG.exists())


    def cleanup_fixture(self):
        h, runtime, hook, xorg = self.migration_fixture()
        with self.owned_paths(), patch.object(os, 'geteuid', return_value=0), \
             patch.object(self.app.shutil, 'which', return_value='/usr/bin/tool'), contextlib.redirect_stdout(io.StringIO()):
            self.app.install([])
        (runtime / 'state.json').unlink()
        self.app.GPU, self.app.CONFIG = self.gpu, xorg
        self.app.SETTINGS = {'hook_path': str(hook)}
        self.app.load_hook = lambda: h
        self.app.redundant_files = lambda: ([], [])
        self.app.leftover_progress = lambda: []
        self.app.cleanup_blockers = Mock()
        self.app.cleanup_devices = Mock()
        self.app.restore_host = Mock()
        h.lock.reset_mock()
        h.atomic_json.side_effect = lambda p, data: (p.write_text(json.dumps(data)), p.chmod(0o600))
        return h, runtime, hook, xorg

    def test_full_cleanup_restores_user_policy_without_configuration_manifest(self):
        h, runtime, hook, xorg = self.cleanup_fixture()
        args = types.SimpleNamespace(dry_run=False, yes=True)
        with self.owned_paths(), patch.object(os, 'geteuid', return_value=0), \
             patch.object(self.app.shutil, 'which', return_value=None), contextlib.redirect_stdout(io.StringIO()):
            self.app.cleanup(args)
        self.assertEqual(xorg.read_bytes(), self.app.XORG_OFF)
        self.assertFalse(hook.exists())
        self.assertFalse(runtime.exists())
        self.assertFalse((self.root / 'opt/nvidia-handoff').exists())
        self.app.restore_host.assert_called_once_with(h)

    def test_cleanup_preview_creates_no_lock_or_files(self):
        h, runtime, hook, xorg = self.cleanup_fixture()
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with self.owned_paths(), patch.object(os, 'geteuid', return_value=0), \
             patch.object(self.app.shutil, 'which', return_value=None), contextlib.redirect_stdout(io.StringIO()):
            self.app.cleanup(types.SimpleNamespace(dry_run=True, yes=False))
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        h.lock.assert_not_called()
        self.app.restore_host.assert_not_called()

    def test_cleanup_busy_refuses_before_lock_and_preserves_files(self):
        h, runtime, hook, xorg = self.cleanup_fixture()
        self.app.cleanup_blockers.side_effect = self.app.Error('Device busy')
        with self.owned_paths(), patch.object(os, 'geteuid', return_value=0), \
             patch.object(self.app.shutil, 'which', return_value=None), \
             contextlib.redirect_stdout(io.StringIO()), self.assertRaises(self.app.Error):
            self.app.cleanup(types.SimpleNamespace(dry_run=False, yes=True))
        h.lock.assert_not_called()
        self.assertTrue(hook.exists())
        self.app.cleanup_blockers.assert_called_once_with(h)
        self.app.restore_host.assert_not_called()


    def guest_xml(self, machine='pc-q35-9.2'):
        return ('<domain type="kvm"><name>example</name><os><type arch="x86_64" machine="' +
                machine + '">hvm</type></os><devices><controller type="pci" index="0" model="' +
                ('pcie-root' if 'q35' in machine else 'pci-root') + '"/></devices></domain>')

    def configure_guest_devices(self):
        self.core.GPU = self.gpu
        self.core.DEVICES = (self.gpu, self.gpu[:-1]+'1')
        self.app.BDFS = self.core.DEVICES

    def test_unprepared_vm_gets_complete_optional_templates_and_ports(self):
        self.configure_guest_devices()
        root = self.app.ET.fromstring(self.core.prepare_guest_xml(self.guest_xml()))
        self.assertEqual(len(self.core.optional_devices(root)), 2)
        self.assertEqual(root.findall('./devices/hostdev'), [])
        self.assertTrue(self.core.agent_channel(root))
        self.assertGreaterEqual(self.core.hotplug_capacity(root), 3)
        self.assertEqual(self.core.prepare_guest_xml(self.app.ET.tostring(root, encoding='unicode')),
                         self.app.ET.tostring(root, encoding='unicode'))

    def test_partial_manual_configuration_is_completed(self):
        self.configure_guest_devices()
        root = self.app.ET.fromstring(self.guest_xml())
        hostdev = self.app.ET.SubElement(root.find('devices'), 'hostdev', {'type':'pci','managed':'no'})
        source = self.app.ET.SubElement(hostdev, 'source')
        self.app.ET.SubElement(source, 'address', {'domain':'0x0000','bus':'0x02','slot':'0x00','function':'0x0'})
        changed = self.core.prepare_guest_xml(self.app.ET.tostring(root, encoding='unicode'))
        self.assertEqual(len(self.core.optional_devices(self.app.ET.fromstring(changed))), 2)

    def test_preparation_preserves_unrelated_vm_devices_and_metadata(self):
        self.configure_guest_devices()
        root = self.app.ET.fromstring(self.guest_xml())
        self.app.ET.SubElement(root.find('devices'), 'disk', {'type':'file'})
        meta = self.app.ET.SubElement(root,'metadata')
        self.app.ET.SubElement(meta,'{urn:user}note').text = 'keep'
        changed = self.core.prepare_guest_xml(self.app.ET.tostring(root, encoding='unicode'))
        result = self.app.ET.fromstring(changed)
        self.assertIsNotNone(result.find('./devices/disk'))
        self.assertEqual(result.findtext('./metadata/{urn:user}note'), 'keep')

    def test_unprepared_i440fx_uses_existing_pci_bus(self):
        self.configure_guest_devices()
        root = self.app.ET.fromstring(self.core.prepare_guest_xml(self.guest_xml('pc-i440fx-9.2')))
        self.assertFalse(root.findall('./devices/controller[@model="pcie-root-port"]'))
        self.assertEqual(len(self.core.optional_devices(root)), 2)

    def test_unsupported_architecture_refuses_preparation(self):
        self.configure_guest_devices()
        xml = self.guest_xml().replace('x86_64','aarch64')
        with self.assertRaises(self.core.Refuse):
            self.core.prepare_guest_xml(xml)

    def test_cleanup_removes_only_unused_owned_preparation_devices(self):
        self.configure_guest_devices()
        changed = self.core.prepare_guest_xml(self.guest_xml())
        result = self.app.ET.fromstring(self.core.disable_optional_xml(changed))
        self.assertFalse(self.core.agent_channel(result))
        self.assertEqual(len(result.findall('./devices/controller')), 1)
        root = self.app.ET.fromstring(changed)
        disk = self.app.ET.SubElement(root.find('devices'),'disk')
        self.app.ET.SubElement(disk,'address',{'type':'pci','bus':'0x01','slot':'0x0','function':'0x0'})
        result = self.app.ET.fromstring(self.core.disable_optional_xml(self.app.ET.tostring(root,encoding='unicode')))
        self.assertIsNotNone(result.find('./devices/controller[@index="1"]'))

    def test_rows_include_unprepared_and_rank_prepared_first(self):
        self.configure_guest_devices()
        def record(name, prepared):
            return dict(uuid=name,name=name,state='shut off',pci=False,optional=prepared,
                        prepared=prepared,eligible=True,live_gpu=[])
        rows=self.app.destination_rows([record('Alpha',False),record('Zeta',True),record('Beta',False)])
        self.assertEqual([row[0] for row in rows[3:]], ['Zeta','Alpha','Beta'])
        self.assertTrue(all(row[4] for row in rows))

    def test_operation_plan_requires_shutdown_only_for_missing_live_capacity(self):
        self.configure_guest_devices()
        d=dict(uuid='vm',name='vm',state='running',eligible=True,optional=False,live_gpu=[],needs_restart=False)
        self.assertEqual(self.app.operation_plan('vm',[d],False),([],False))
        d['needs_restart']=True
        self.assertEqual(self.app.operation_plan('vm',[d],False),([d],False))

    def test_debian_detection_rejects_other_os_without_guest_exec(self):
        self.app.guest_agent=Mock(return_value={'id':'ubuntu','version-id':'24.04','pretty-name':'Ubuntu'})
        self.app.guest_exec=Mock(side_effect=AssertionError('must not execute'))
        with self.assertRaises(self.app.Error):
            self.app.debian_guest('vm')
        self.app.guest_exec.assert_not_called()

    def test_debian_detection_accepts_supported_guest(self):
        self.app.guest_agent=Mock(return_value={'id':'debian','version-id':'13','pretty-name':'Debian'})
        self.app.guest_exec=Mock(return_value=(0,'amd64\n'))
        self.assertEqual(self.app.debian_guest('vm'),'13')

    def test_missing_agent_has_actionable_error(self):
        self.app.guest_agent=Mock(side_effect=self.app.Error('offline'))
        with self.assertRaisesRegex(self.app.Error,'qemu-guest-agent'):
            self.app.debian_guest('vm')

    def test_guest_exec_decodes_result_without_running_a_command(self):
        self.app.guest_agent=Mock(side_effect=[{'pid':42},{'exited':True,'exitcode':0,
            'out-data':'aGVsbG8K'}])
        self.assertEqual(self.app.guest_exec('vm','/bin/test'),(0,'hello\n'))
        self.assertEqual(self.app.guest_agent.call_args_list[-1].args[1],'guest-exec-status')

    def test_driver_recommendation_rejects_shell_and_options(self):
        self.assertEqual(self.app.recommended_driver_packages('sudo apt install -y nvidia-open'),['nvidia-open'])
        self.assertEqual(self.app.recommended_driver_packages('  sudo apt-get install -Vy nvidia-open'),['nvidia-open'])
        self.assertEqual(self.app.recommended_driver_packages('sudo apt -V install -y nvidia-open'),['nvidia-open'])
        for text in ('sudo apt install nvidia-open; reboot','sudo apt install --allow-remove-essential nvidia-open','curl bad | sh'):
            with self.subTest(text=text), self.assertRaises(self.app.Error):
                self.app.recommended_driver_packages(text)

    def test_ready_guest_skips_all_package_changes(self):
        self.app.load_hook=Mock()
        self.app.debian_guest=Mock(return_value='13')
        self.app.guest_driver_ready=Mock(return_value=True)
        self.app.guest_exec=Mock(side_effect=AssertionError('no packages needed'))
        self.assertIn('ready', self.app.setup_guest_driver('vm','job','vm'))
        self.app.guest_exec.assert_not_called()

    def test_failed_package_bootstrap_keeps_gpu_and_does_not_reboot(self):
        self.app.load_hook=Mock()
        self.app.operation_status=Mock()
        self.app.debian_guest=Mock(return_value='13')
        self.app.guest_driver_ready=Mock(return_value=False)
        self.app.guest_exec=Mock(return_value=(1,'no network'))
        self.app.virsh=Mock(side_effect=AssertionError('must not reboot or unplug'))
        with self.assertRaisesRegex(self.app.Error,'GPU remains attached'):
            self.app.setup_guest_driver('vm','job','vm')
        self.app.virsh.assert_not_called()

    def test_driver_packages_are_simulated_and_cannot_remove_packages(self):
        self.app.load_hook=Mock()
        self.app.operation_status=Mock()
        self.app.debian_guest=Mock(return_value='13')
        self.app.guest_driver_ready=Mock(side_effect=[False,True])
        self.app.guest_exec=Mock(side_effect=[(0,''),(0,'sudo apt install -y nvidia-open'),(0,''),(0,'')])
        self.assertIn('ready',self.app.setup_guest_driver('vm','job','vm'))
        install=self.app.guest_exec.call_args_list[2:]
        self.assertIn('--simulate',install[0].args[2])
        self.assertTrue(all('--no-remove' in call.args[2] for call in install))


    def test_preparation_defines_only_inactive_xml_and_rechecks_edits(self):
        self.configure_guest_devices()
        self.app.load_hook=lambda:self.core
        xml=self.guest_xml()
        self.app.virsh=Mock(side_effect=['Persistent: yes',xml,xml])
        self.app.virsh_input=Mock()
        self.app.prepare_vm_definition('vm')
        self.assertEqual(self.app.virsh_input.call_args.args[1:],('define','/dev/stdin','--validate'))
        self.app.virsh=Mock(side_effect=['Persistent: yes',xml,xml.replace('example','edited')])
        self.app.virsh_input.reset_mock()
        with self.assertRaisesRegex(self.app.Error,'changed during preparation'):
            self.app.prepare_vm_definition('vm')
        self.app.virsh_input.assert_not_called()

    def test_new_vm_pipeline_prepares_before_boot_then_installs_driver(self):
        self.configure_guest_devices()
        d=dict(uuid='vm',name='vm',state='shut off',eligible=True,optional=False,live_gpu=[])
        self.app.inventory=lambda:[d]
        self.app.xorg_owners=lambda h:set()
        self.app.operation_status=Mock()
        self.app.worker=Mock()
        h=Mock()
        h.assert_x11=Mock()
        h.xorg_mode.return_value='off'
        h.load.return_value=None
        locked=[False]
        def acquire():
            self.assertFalse(locked[0])
            locked[0]=True
            return os.open(self.root/'lock',os.O_CREAT|os.O_RDWR,0o600)
        close=os.close
        def release(fd):
            locked[0]=False
            close(fd)
        h.lock=acquire
        self.app.load_hook=lambda:h
        events=[]
        def prepare(ident):
            self.assertFalse(locked[0])
            events.append('prepare')
            d['optional']=True
        def start(ident):
            self.assertFalse(locked[0])
            events.append('start')
            d.update(state='running',live_gpu=list(self.app.BDFS))
        def driver(*args):
            self.assertFalse(locked[0])
            events.append('driver')
            return 'ready'
        self.app.prepare_vm_definition=prepare
        self.app.start_optional=start
        self.app.setup_guest_driver=driver
        with patch.object(os,'geteuid',return_value=0),patch.object(os,'close',release):
            self.app.selection_worker('vm','job',False,False,True)
        self.assertEqual(events,['prepare','start','driver'])

    def test_guest_exec_timeout_reports_pid_without_killing_process(self):
        self.app.guest_agent=Mock(side_effect=[{'pid':42},{'exited':False}])
        with patch.object(self.app.time,'monotonic',side_effect=[0,2]), \
             self.assertRaisesRegex(self.app.Error,'process 42 may still be running'):
            self.app.guest_exec('vm','/bin/test',timeout=1)
        self.assertEqual(self.app.guest_agent.call_count,2)

    def test_guest_driver_readiness_matches_selected_device(self):
        self.app.GPU=self.gpu
        self.app.SETTINGS={'devices':[{'bdf':self.gpu,'device':'0x2d04'}]}
        self.app.guest_exec=Mock(return_value=(0,'0x2D0410DE\n'))
        self.assertTrue(self.app.guest_driver_ready('vm'))
        self.app.guest_exec=Mock(return_value=(0,'0x123410DE\n'))
        self.assertFalse(self.app.guest_driver_ready('vm'))

    def test_package_failure_never_requests_reboot(self):
        self.app.load_hook=Mock()
        self.app.operation_status=Mock()
        self.app.debian_guest=Mock(return_value='13')
        self.app.guest_driver_ready=Mock(return_value=False)
        self.app.guest_exec=Mock(side_effect=[(0,''),(0,'sudo apt install -y nvidia-open'),(1,'removal required')])
        self.app.virsh=Mock(side_effect=AssertionError('must not reboot'))
        with self.assertRaisesRegex(self.app.Error,'package operation failed'):
            self.app.setup_guest_driver('vm','job','vm')
        self.app.virsh.assert_not_called()

    def test_driver_setup_reboots_once_only_after_confirmed_install(self):
        self.app.load_hook=Mock()
        self.app.operation_status=Mock()
        self.app.debian_guest=Mock(return_value='13')
        self.app.guest_driver_ready=Mock(side_effect=[False,False,True])
        old='11111111-1111-1111-1111-111111111111\n'
        new='22222222-2222-2222-2222-222222222222\n'
        self.app.guest_exec=Mock(side_effect=[(0,''),(0,'sudo apt install -y nvidia-open'),(0,''),(0,''),(0,old),(0,new)])
        self.app.virsh=Mock()
        with patch.object(self.app.time,'sleep'):
            self.assertIn('after guest reboot',self.app.setup_guest_driver('vm','job','vm'))
        self.app.virsh.assert_called_once_with('reboot','vm','--mode','agent')


    def test_connected_guest_can_retry_driver_setup_without_gpu_switch(self):
        self.configure_guest_devices()
        d=dict(uuid='vm',name='vm',state='running',eligible=True,optional=True,live_gpu=list(self.app.BDFS))
        self.app.inventory=lambda:[d]
        self.app.xorg_owners=lambda h:set()
        self.app.debian_guest=Mock(return_value='13')
        self.app.operation_status=Mock()
        self.app.worker=Mock(side_effect=AssertionError('must not switch host mode'))
        h=Mock()
        h.assert_x11=Mock()
        h.driver.return_value='vfio-pci'
        self.app.load_hook=lambda:h
        locked=[False]
        def acquire():
            locked[0]=True
            return os.open(self.root/'lock',os.O_CREAT|os.O_RDWR,0o600)
        close=os.close
        def release(fd):
            locked[0]=False
            close(fd)
        h.lock=acquire
        def driver(*args):
            self.assertFalse(locked[0])
            return 'ready'
        self.app.setup_guest_driver=Mock(side_effect=driver)
        with patch.object(os,'geteuid',return_value=0),patch.object(os,'close',release):
            self.app.selection_worker('vm','job',False,False,True)
        self.app.setup_guest_driver.assert_called_once()
        self.app.worker.assert_not_called()

    def test_current_target_guard_accounts_for_live_guest_driver_readiness(self):
        self.configure_guest_devices()
        d=dict(uuid='vm',name='vm',state='running',eligible=True,prepared=True,
               optional=True,pci=True,live_gpu=list(self.app.BDFS),driver_ready=False)
        window=types.SimpleNamespace(current=Mock(),tree=Mock(),update_button=Mock())
        window.tree.selection.return_value=('vm',)
        window.tree.get_children.return_value=()
        self.app.GPUWindow.show_inventory(window,[d],False,'vfio-pci')
        self.assertIsNone(window.current_target)
        d['driver_ready']=True
        self.app.GPUWindow.show_inventory(window,[d],False,'vfio-pci')
        self.assertEqual(window.current_target,'vm')



if __name__ == '__main__':
    unittest.main(verbosity=2)
