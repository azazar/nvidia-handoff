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



if __name__ == '__main__':
    unittest.main(verbosity=2)
