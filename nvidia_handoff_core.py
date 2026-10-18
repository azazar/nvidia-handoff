#!/usr/bin/python3 -I
"""Portable, configuration-driven NVIDIA handoff. No libvirt calls in hooks."""
import base64
import errno
import fcntl
import json
import os
import re
from pathlib import Path
import signal
import stat
import struct
import subprocess
import sys
import syslog
import time
import tempfile
import uuid
import xml.etree.ElementTree as ET

SETTINGS = {}
VM = UUID = GPU = AUDIO = None
DEVICES = ()
HOST = IDS = MODULE_ALIASES = {}
MODULES = ()
SYS, PROC, DEV = Path('/sys'), Path('/proc'), Path('/dev')
STATE_DIR = Path('/run/nvidia-handoff')
SERVICE = 'nvidia-persistenced.service'
XORG_CONFIG = Path('/etc/X11/xorg.conf.d/90-nvidia-handoff.conf')


class Refuse(RuntimeError):
    pass


def log(message, error=False):
    syslog.openlog('nvidia-handoff', syslog.LOG_PID, syslog.LOG_DAEMON)
    syslog.syslog(syslog.LOG_ERR if error else syslog.LOG_INFO, message)
    print('nvidia-handoff: ' + message, file=sys.stderr)


def run(*args):
    result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, timeout=30, env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
    if result.returncode:
        raise Refuse(f'{args[0]} failed ({result.returncode}): {result.stderr.strip()}')
    return result.stdout.strip()


def pci(bdf):
    return SYS / 'bus/pci/devices' / bdf


def driver(bdf):
    path = pci(bdf) / 'driver'
    return path.resolve(strict=True).name if path.is_symlink() else None


def override(bdf):
    value = (pci(bdf) / 'driver_override').read_text().strip()
    return '' if value == '(null)' else value


def write(path, value):
    path.write_text(value + '\n')


def module_loaded(name):
    return (SYS / 'module' / MODULE_ALIASES.get(name, name)).is_dir()


def persistence():
    try:
        return run('/usr/bin/systemctl', 'show', SERVICE, '-p', 'ActiveState', '--value')
    except Refuse:
        return 'inactive'


def topology():
    for d in SETTINGS['devices']:
        p = pci(d['bdf'])
        if any(read_attr(p / key) != d[key] for key in ('vendor', 'device', 'class')):
            raise Refuse('Configured PCI identity changed: ' + d['bdf'])
        if driver(d['bdf']) not in (None, HOST[d['bdf']], 'vfio-pci'):
            raise Refuse('Unexpected driver on ' + d['bdf'])
    if read_attr(pci(GPU) / 'boot_vga', '0') == '1':
        raise Refuse('Selected NVIDIA is the boot/primary VGA device')
    if attached_display(GPU):
        raise Refuse('A display is connected/enabled on the selected NVIDIA GPU')


def validate_xml(xml):
    root = ET.fromstring(xml)
    if root.findtext('name') != VM or root.findtext('uuid') != UUID:
        raise Refuse('Domain identity does not match hook context')
    found = []
    for h in root.findall('./devices/hostdev'):
        a = h.find('./source/address')
        if h.get('type') != 'pci' or a is None:
            continue
        bdf = '%04x:%02x:%02x.%x' % tuple(int(a.get(k, '0'), 0) for k in ('domain', 'bus', 'slot', 'function'))
        if bdf in DEVICES:
            if h.get('managed', 'no') not in ('no', 'yes'):
                raise Refuse('Unsupported hostdev managed attribute')
            found.append(bdf)
    if sorted(found) != sorted(DEVICES):
        raise Refuse('Domain XML must contain all selected GPU PCI functions exactly once')


def beneath(path, ancestor):
    return path.resolve().is_relative_to(ancestor.resolve())


def host_nodes():
    nodes = (set(DEV.glob('nvidia*')) | set((DEV / 'nvidia-caps').glob('*'))) if driver(GPU) == 'nvidia' else set()
    for kind, folder in (('drm', 'dri'), ('sound', 'snd')):
        for p in (SYS / 'class' / kind).glob('*'):
            if any(beneath(p, pci(b)) for b in DEVICES):
                nodes.add(DEV / folder / p.name)
    nodes.update(DEV / p.name for p in nvidia_framebuffers())
    return {p for p in nodes if p.is_char_device()}


def vfio_nodes():
    groups = iommu_groups() or ()
    nodes = {DEV / 'vfio' / group.name for group in groups} | {DEV / 'iommu'}
    for bdf in DEVICES:
        nodes.update(DEV / 'vfio/devices' / p.name for p in (pci(bdf) / 'vfio-dev').glob('*'))
    return {p for p in nodes if p.is_char_device()}


def nvidia_framebuffers():
    for p in (SYS / 'class/graphics').glob('fb[0-9]*'):
        if beneath(p, pci(GPU)):
            yield p


def framebuffer_idle(p):
    # FBIOGET_CON2FBMAP from linux/fb.h reads console ownership without changing it.
    try:
        fd = os.open(DEV / p.name, os.O_RDONLY | os.O_CLOEXEC)
        try:
            for console in range(1, 64):
                mapping = bytearray(struct.pack('=II', console, 0))
                fcntl.ioctl(fd, 0x460F, mapping, True)
                if struct.unpack('=II', mapping)[1] == int(p.name[2:]):
                    raise Refuse('NVIDIA framebuffer owns console ' + str(console))
        finally:
            os.close(fd)
    except OSError as exc:
        raise Refuse('Cannot inspect NVIDIA framebuffer; refusing detach') from exc


def display_idle():
    for p in nvidia_framebuffers():
        framebuffer_idle(p)
    for p in (SYS / 'class/drm').glob('card*-*'):
        if beneath(p, pci(GPU)) and (p / 'enabled').exists() and (p / 'enabled').read_text().strip() == 'enabled':
            raise Refuse('NVIDIA display connector is active: ' + p.name)


def idle(nodes, ignored_pids=()):
    """Fail closed on unreadable processes. Check open FDs and closed-FD mappings."""
    rdevs = {p.stat().st_rdev for p in nodes}
    names = {str(p) for p in nodes}
    for p in PROC.iterdir():
        if not p.name.isdigit() or int(p.name) == os.getpid() or int(p.name) in ignored_pids:
            continue
        try:
            cmd = (p / 'cmdline').read_bytes().split(b'\0')
            if cmd and b'qemu-system' in cmd[0] and any(b.encode() in x for b in DEVICES for x in cmd):
                raise Refuse('QEMU still owns the selected GPU: pid ' + p.name)
            for fd in (p / 'fd').iterdir():
                try:
                    target = os.readlink(fd)
                    # Legacy VFIO device FDs are anonymous; ownership cannot be inferred safely.
                    if 'vfio-device' in target or 'iommufd' in target:
                        raise Refuse('VFIO device FD is still open: pid ' + p.name)
                    s = fd.stat()
                    if stat.S_ISCHR(s.st_mode) and s.st_rdev in rdevs:
                        raise Refuse(f'Device busy: pid {p.name}, fd {fd.name}, {target}')
                except (FileNotFoundError, ProcessLookupError):
                    continue
            for line in (p / 'maps').read_text().splitlines():
                fields = line.split(None, 5)
                if len(fields) == 6:
                    name = fields[5].removesuffix(' (deleted)')
                    if 'vfio-device' in name or 'iommufd' in name:
                        raise Refuse('VFIO device mapping is still active: pid ' + p.name)
                    if name in names:
                        raise Refuse('Device mapping is still active: pid ' + p.name)
                    if name.startswith('/dev/'):
                        try:
                            st = (p / 'map_files' / fields[0]).stat()
                            if stat.S_ISCHR(st.st_mode) and st.st_rdev in rdevs:
                                raise Refuse('Aliased device mapping is active: pid ' + p.name)
                        except (FileNotFoundError, ProcessLookupError):
                            continue
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError as exc:
            raise Refuse('Cannot inspect process ' + p.name + '; refusing handoff') from exc


def guard():
    topology()
    display_idle()
    idle(host_nodes() | vfio_nodes())


def state_path():
    return STATE_DIR / 'state.json'


def atomic_json(path, value, mode=0o600):
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='.' + path.name, delete=False) as f:
        tmp = Path(f.name)
        try:
            os.fchmod(f.fileno(), mode)
            json.dump(value, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)


def save(state):
    atomic_json(state_path(), state)


def policy_target():
    path = STATE_DIR / 'destination.json'
    if not path.exists():
        return None
    target = json.loads(path.read_text())['target']
    if target not in ('none', 'xorg', 'cuda', 'blocked'):
        if str(uuid.UUID(target)) != target:
            raise Refuse('Invalid destination policy')
    return target


def choose_target(target):
    if target is None:
        (STATE_DIR / 'destination.json').unlink(missing_ok=True)
    else:
        if target not in ('none', 'xorg', 'cuda', 'blocked') and str(uuid.UUID(target)) != target:
            raise Refuse('Invalid destination policy')
        atomic_json(STATE_DIR / 'destination.json', {'target': target})


def load():
    if not state_path().exists():
        return None
    s = json.loads(state_path().read_text())
    if s.get('version') != 1 or (UUID is not None and s.get('phase') != 'parked' and s.get('uuid') != UUID):
        raise Refuse('Invalid transaction state')
    return s


def snapshot():
    return {'drivers': {b: driver(b) for b in DEVICES},
            'overrides': {b: override(b) for b in DEVICES},
            'modules': loaded_nvidia_modules()}


def transaction(before):
    host = {'drivers': dict(HOST),
            'overrides': {b: before['overrides'][b] if before['drivers'][b] == HOST[b] else '' for b in DEVICES},
            'modules': before['modules'] or list(MODULES)}
    return {'version': 1, 'uuid': UUID, 'domain': VM, 'phase': 'preparing', 'before': before,
            'host': host, 'persistence': persistence() == 'active', 'gates': []}


def gate_nodes(s):
    # Mask normal users' access, including extended ACLs, before the second scan.
    for p in sorted(host_nodes()):
        st = p.stat()
        try:
            acl = base64.b64encode(os.getxattr(p, 'system.posix_acl_access')).decode()
        except OSError as exc:
            if exc.errno not in (errno.ENODATA, errno.ENOTSUP):
                raise
            acl = None
        s['gates'].append({'path': str(p), 'dev': st.st_dev, 'ino': st.st_ino,
                           'rdev': st.st_rdev, 'mode': stat.S_IMODE(st.st_mode), 'acl': acl})
        save(s)  # Recovery information precedes each permission change.
        os.chmod(p, 0)


def ungate(s):
    for g in s['gates']:
        p = Path(g['path'])
        try:
            st = p.stat()
        except FileNotFoundError:
            continue
        if (st.st_dev, st.st_ino, st.st_rdev) != (g['dev'], g['ino'], g['rdev']):
            continue  # A new node is owned/configured by udev.
        os.chmod(p, g['mode'])
        if g['acl'] is not None:
            os.setxattr(p, 'system.posix_acl_access', base64.b64decode(g['acl']))


def bind(bdf, target):
    current = driver(bdf)
    if current == target:
        return
    if current:
        # NVIDIA must ONLY be removed through normal module unload (EBUSY-safe).
        if current not in ({'vfio-pci'} | (set(HOST.values()) - {'nvidia'})):
            raise Refuse('Refusing direct unbind of ' + str(current))
        write(pci(bdf) / 'driver/unbind', bdf)
    if target:
        write(SYS / 'bus/pci/drivers' / target / 'bind', bdf)
    if driver(bdf) != target:
        raise Refuse('Driver verification failed for ' + bdf)


def process_start(pid):
    try:
        return (PROC / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


def assert_claim(state):
    pid = state.get('claim_pid')
    if (pid and pid != os.getpid() and state.get('claim_uuid') != UUID
            and process_start(pid) == state.get('claim_start')):
        raise Refuse('Another GPU attachment/start is in progress')


def restore(s, rollback=False):
    assert_claim(s)
    target = s['before'] if rollback else s['host']
    # This also rejects a manual recovery command while omm is running.
    topology()
    assert_x11()
    if all(driver(b) == target['drivers'][b] for b in DEVICES):
        for b in DEVICES:
            write(pci(b) / 'driver_override', target['overrides'][b])
        ungate(s)
        if s['persistence']:
            run('/usr/bin/systemctl', 'start', SERVICE)
        state_path().unlink()
        return
    guard()
    log('Rollback to original bindings' if rollback else 'Returning selected GPU PCI functions to host')
    for b in DEVICES:
        write(pci(b) / 'driver_override', target['drivers'][b] or 'none')
    # Unbind VFIO first. No reset/remove/rescan, forced module removal or Xorg stop.
    for b in reversed(DEVICES):
        if driver(b) != target['drivers'][b] and driver(b):
            bind(b, None)
    for mod in target['modules']:
        run('/usr/sbin/modprobe', mod)
    for b in DEVICES:
        if target['drivers'][b]:
            run('/usr/sbin/modprobe', next(d['host_module'] for d in SETTINGS['devices'] if d['bdf'] == b) if target['drivers'][b] == HOST[b] else target['drivers'][b])
        bind(b, target['drivers'][b])
        write(pci(b) / 'driver_override', target['overrides'][b])
    run('/usr/bin/udevadm', 'settle', '--timeout=10')
    ungate(s)
    if s['persistence']:
        run('/usr/bin/systemctl', 'start', SERVICE)
    state_path().unlink()
    log('Rollback complete' if rollback else 'Host bindings restored')


def xorg_mode():
    if not XORG_CONFIG.exists():
        return "off"  # Integration is created only when first needed.
    values = re.findall(r'^\s*Option\s+"AutoAddGPU"\s+"(off|on)"\s*$',
                        XORG_CONFIG.read_text(), re.IGNORECASE | re.MULTILINE)
    if len(values) != 1:
        raise Refuse('Expected one explicit AutoAddGPU off/on option')
    return values[0].lower()


def prepare(parking=False):
    assert_x11()
    if (STATE_DIR / 'uninstalling').exists():
        raise Refuse('Full cleanup is in progress')
    if not parking and not isolated():
        raise Refuse('PCI passthrough requires isolated IOMMU groups containing only selected GPU functions')
    parking_driver = 'vfio-pci' if isolated() else None
    if not parking and policy_target() not in (None, UUID):
        raise Refuse('GPU destination is locked by nvidia-handoff; select this VM in its GUI first')
    if (STATE_DIR / 'xorg-transition').exists() or xorg_mode() != 'off':
        raise Refuse('Xorg gaming mode/transition is active; run nvidia-handoff disconnect --restart-session first')
    existing = load()
    if existing is not None:
        assert_claim(existing)
        if existing['phase'] != 'parked':
            raise Refuse('Outstanding transaction; release/recovery must complete first')
        guard()
        if any(driver(b) not in (None, 'vfio-pci') for b in DEVICES):
            raise Refuse('Parked GPU binding changed')
        if not parking:
            run('/usr/sbin/modprobe', 'vfio-pci')
            for b in DEVICES:
                write(pci(b) / 'driver_override', 'vfio-pci')
                bind(b, 'vfio-pci')
        existing.update(uuid=UUID, domain=VM, phase='vfio')
        save(existing)
        log('Adopted idle GPU for ' + str(VM))
        return
    topology()
    display_idle()
    idle(vfio_nodes())  # Reject live QEMU before even stopping persistence.
    assert_auxiliary_idle()
    if driver(GPU) == 'nvidia' and shared_nvidia_driver():
        raise Refuse('Safe detach requires unloading NVIDIA, but another host GPU shares that driver. Refusing to disrupt the primary GPU.')
    before = snapshot()
    if any(before['drivers'][b] not in (None, HOST[b], 'vfio-pci') for b in DEVICES):
        raise Refuse('Unbound PCI function has no known original driver')
    s = transaction(before)
    if persistence() not in ('active', 'inactive', 'failed'):
        raise Refuse('Persistence daemon is transitioning')
    # No driver mutation has occurred; mark owned state before stopping persistence.
    save(s)
    try:
        if s['persistence']:
            run('/usr/bin/systemctl', 'stop', SERVICE)
        guard()
        gate_nodes(s)
        guard()
        if parking_driver:
            run('/usr/sbin/modprobe', parking_driver)
        for b in DEVICES:
            write(pci(b) / 'driver_override', parking_driver or 'none')
        if before['drivers'][GPU] == 'nvidia':
            for mod in reversed(before['modules']):
                if module_loaded(mod):
                    run('/usr/sbin/modprobe', '-r', mod)
            if driver(GPU) is not None:
                raise Refuse('NVIDIA did not detach after normal module unload')
        guard()
        for b in reversed(DEVICES):
            bind(b, parking_driver)
        s['phase'] = 'vfio'
        save(s)
        log('All selected PCI functions ready: ' + str(parking_driver or 'unbound'))
    except BaseException:
        s['phase'] = 'rollback'
        save(s)
        try:
            # If no binding changed, undo harmless overrides/gates without unbinding
            # an application which caused the busy check to fail.
            if all(driver(b) == before['drivers'][b] for b in DEVICES):
                for b in DEVICES:
                    write(pci(b) / 'driver_override', before['overrides'][b])
                for mod in before['modules']:
                    if not module_loaded(mod):
                        run('/usr/sbin/modprobe', mod)
                ungate(s)
                if s['persistence']:
                    run('/usr/bin/systemctl', 'start', SERVICE)
                state_path().unlink()
                log('Pre-start changes rolled back; bindings unchanged')
            else:
                restore(s, rollback=True)
        except BaseException as exc:
            log('Rollback incomplete; state retained; no forced detach: ' + str(exc), True)
        raise


def park():
    existing = load()
    if existing is not None and existing['phase'] == 'parked':
        assert_claim(existing)
        guard()
        if any(driver(b) not in (None, 'vfio-pci') for b in DEVICES):
            raise Refuse('Parked GPU binding changed')
        return
    prepare(parking=True)
    state = load()
    state['phase'] = 'parked'
    save(state)
    log('GPU parked without a guest owner')


def release():
    s = load()
    if s is None:
        log('No owned transaction; release is a no-op')
        return
    restore(s, rollback=s['phase'] == 'rollback')


def lock(installing=False, cleaning=False):
    if os.geteuid() != 0:
        raise Refuse('Root required for full /proc visibility and device control')
    def installed():
        if installing:
            return
        for name, expected in SETTINGS.get('program_identity', {}).items():
            try:
                st = Path(name).lstat()
            except FileNotFoundError:
                raise Refuse('The application has been removed; close this process')
            if (st.st_dev, st.st_ino, st.st_mtime_ns) != tuple(expected):
                raise Refuse('Installation changed; close this process and retry')
    installed()  # Never recreate runtime files after an uninstall.
    os.umask(0o077)
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = STATE_DIR.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0 or stat.S_IMODE(st.st_mode) != 0o700:
        raise Refuse('Unsafe state directory ownership/permissions')
    fd = os.open(STATE_DIR / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    deadline = time.monotonic() + 10
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                installed()
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o077:
                    raise Refuse('Unsafe runtime lock ownership/permissions')
                if st.st_nlink == 0:
                    raise Refuse('Handoff lock was removed; retry with the installed application')
                if not cleaning and (STATE_DIR / 'uninstalling').exists():
                    raise Refuse('Full cleanup is in progress')
                tag = ('nvidia-handoff ' + GPU + '\n').encode()
                current = os.pread(fd, 128, 0)
                if current not in (b'', tag):
                    raise Refuse('Runtime lock belongs to another GPU')
                if not current:
                    os.pwrite(fd, tag, 0)
                    os.fsync(fd)
                return fd
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Refuse('Another handoff owns the lock')
                time.sleep(0.1)
    except BaseException:
        os.close(fd)
        raise


OPTIONAL_NS = 'urn:nvidia-handoff:optional:1'


def optional_devices(root):
    group = root.find('./metadata/{' + OPTIONAL_NS + '}gpu')
    if group is None or group.get('bdf') != GPU:
        return []
    devices = list(group.findall('hostdev'))
    found = []
    for dev in devices:
        a = dev.find('./source/address')
        if dev.get('type') != 'pci' or a is None:
            raise Refuse('Invalid optional GPU metadata')
        found.append('%04x:%02x:%02x.%x' % tuple(int(a.get(k, '0'), 0) for k in ('domain', 'bus', 'slot', 'function')))
    if sorted(found) != sorted(DEVICES):
        raise Refuse('Optional GPU metadata must contain all selected PCI functions exactly once')
    return devices


def enable_optional_xml(xml):
    root = ET.fromstring(xml)
    existing = optional_devices(root)
    if existing:
        return None
    selected = []
    devices = root.find('devices')
    for dev in devices.findall('hostdev'):
        a = dev.find('./source/address')
        if dev.get('type') == 'pci' and a is not None:
            bdf = '%04x:%02x:%02x.%x' % tuple(int(a.get(k, '0'), 0) for k in ('domain', 'bus', 'slot', 'function'))
            if bdf in DEVICES:
                selected.append(dev)
    if not selected:
        return None
    meta = root.find('metadata')
    if meta is None:
        meta = ET.SubElement(root, 'metadata')
    group = ET.SubElement(meta, '{' + OPTIONAL_NS + '}gpu', {'bdf': GPU})
    for dev in selected:
        devices.remove(dev)
        # User aliases are allowed; generated aliases must not be persisted as templates.
        alias = dev.find('alias')
        if alias is not None and not alias.get('name', '').startswith('ua-'):
            dev.remove(alias)
        group.append(dev)
    optional_devices(root)  # Validate the entire group before returning a changed definition.
    return ET.tostring(root, encoding='unicode')


def disable_optional_xml(xml):
    """Remove our metadata; keep GPU-free defaults and all later user edits."""
    root = ET.fromstring(xml)
    templates = optional_devices(root)
    if not templates:
        return None
    meta = root.find('metadata')
    meta.remove(meta.find('{' + OPTIONAL_NS + '}gpu'))
    if not len(meta) and not meta.attrib:
        root.remove(meta)
    return ET.tostring(root, encoding='unicode')


def boot_xml(xml, with_gpu):
    root = ET.fromstring(xml)
    templates = optional_devices(root)
    if not templates:
        raise Refuse('Guest is not configured for optional GPU passthrough')
    devices = root.find('devices')
    for dev in list(devices.findall('hostdev')):
        a = dev.find('./source/address')
        if dev.get('type') == 'pci' and a is not None:
            bdf = '%04x:%02x:%02x.%x' % tuple(int(a.get(k, '0'), 0) for k in ('domain', 'bus', 'slot', 'function'))
            if bdf in DEVICES:
                devices.remove(dev)
    if with_gpu:
        import copy
        for dev in templates:
            devices.append(copy.deepcopy(dev))
    return ET.tostring(root, encoding='unicode')




def main(argv, xml=None):
    global VM, UUID
    # Ignore reconnect unconditionally: failure there can cause libvirt to kill QEMU.
    if len(argv) >= 4:
        if tuple(argv[1:3]) not in (('prepare', 'begin'), ('release', 'end')):
            return 0
        xml = sys.stdin.read() if xml is None else xml
        root = ET.fromstring(xml)
        addresses = root.findall('./devices/hostdev[@type="pci"]/source/address')
        related = any('%04x:%02x:%02x.%x' % tuple(int(a.get(k, '0'), 0) for k in ('domain', 'bus', 'slot', 'function')) in DEVICES for a in addresses)
        if not related:
            return 0
        VM = root.findtext('name')
        UUID = root.findtext('uuid')
        if VM != argv[0] or str(uuid.UUID(UUID)) != UUID:
            raise Refuse('Domain identity does not match hook arguments')
        if argv[1] == 'prepare':
            validate_xml(xml)
        action = argv[1]
    elif argv == ['--check']:
        action = 'check'
    elif argv == ['--recover']:
        if state_path().exists():
            state = json.loads(state_path().read_text())
            UUID = str(uuid.UUID(state['uuid'])) if state.get('uuid') else None
            VM = state.get('domain', VM)
        action = 'release'
    else:
        raise Refuse('Use libvirt hook arguments, --check (read-only), or --recover')
    if action == 'check':
        if os.geteuid() != 0:
            raise Refuse('Root required for complete ownership checks')
        guard()
        log('Read-only handoff checks passed')
        return 0
    fd = lock()
    try:
        prepare() if action == 'prepare' else release()
    finally:
        os.close(fd)
    return 0




def read_attr(path, default=None):
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return default


def pci_gpu_devices():
    return [p for p in (SYS / 'bus/pci/devices').iterdir()
            if read_attr(p / 'class', '').startswith('0x03')]


def connectors(bdf):
    return [p for p in (SYS / 'class/drm').glob('card*-*') if beneath(p, pci(bdf))]


def attached_display(bdf):
    return any(read_attr(p / 'status') == 'connected' or read_attr(p / 'enabled') == 'enabled'
               for p in connectors(bdf))


def module_name(alias):
    try:
        value = run('/usr/sbin/modinfo', '-F', 'name', alias)
        return value.splitlines()[0] if value else alias.replace('-', '_')
    except Refuse:
        return alias.replace('-', '_')


def native_driver(p):
    cls = read_attr(p / 'class', '')
    current = driver(p.name)
    if cls.startswith('0x03'):
        if current not in (None, 'nvidia', 'vfio-pci'):
            raise Refuse('Install/bind the proprietary NVIDIA driver before setup: ' + p.name)
        return 'nvidia', module_name('nvidia')
    if current and current != 'vfio-pci':
        module = p / 'driver/module'
        return current, module.resolve().name if module.is_symlink() else current
    known = {'0x040300': ('snd_hda_intel', 'snd_hda_intel'),
             '0x0c0330': ('xhci_hcd', 'xhci_pci'), '0x0c8000': ('i2c_nvidia_gpu', 'i2c_nvidia_gpu')}
    if cls in known:
        return known[cls]
    raise Refuse('Cannot infer native driver for ' + p.name + '; configure it while host-bound')


def xorg_path():
    candidates = []
    for path in [Path('/etc/X11/xorg.conf'), *Path('/etc/X11/xorg.conf.d').glob('*.conf')]:
        if not path.exists():
            continue
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
            raise Refuse('Unsafe Xorg configuration: ' + str(path))
        data = path.read_bytes()
        if re.search(rb'(?im)^\s*Option\s+"AutoAddGPU"', data) or b'nvidia-handoff sha256=' in data:
            candidates.append(path)
    if len(candidates) > 1:
        raise Refuse('Multiple AutoAddGPU policies found; resolve them before switching')
    return str(candidates[0] if candidates else Path('/etc/X11/xorg.conf.d/90-nvidia-handoff.conf'))


def runtime_path(gpu, devices):
    default = Path('/run/nvidia-handoff') / gpu
    candidates = []
    for directory in [default, *Path('/run').glob('*-gpu-handoff')]:
        try:
            if not directory.exists():
                continue
            st = directory.lstat()
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0 or stat.S_IMODE(st.st_mode) != 0o700:
                raise Refuse('Unsafe runtime directory: ' + str(directory))
            lockfile = directory / 'lock'
            tag = b''
            if lockfile.exists():
                fd = os.open(lockfile, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                try:
                    st = os.fstat(fd)
                    if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o077:
                        raise Refuse('Unsafe runtime lock: ' + str(lockfile))
                    tag = os.read(fd, 128)
                finally:
                    os.close(fd)
            state = directory / 'state.json'
            matches = False
            if state.exists():
                fd = os.open(state, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd) as source:
                    st = os.fstat(source.fileno())
                    if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o077:
                        raise Refuse('Unsafe recovery state: ' + str(state))
                    value = json.load(source)
                matches = set(value.get('host', {}).get('drivers', {})) == set(devices)
            if tag == ('nvidia-handoff ' + gpu + '\n').encode() or matches:
                candidates.append(directory)
        except PermissionError:
            continue  # Unprivileged inventory does not need private transaction state.
    if len(candidates) > 1:
        raise Refuse('Multiple recovery directories match this GPU; refusing parallel locks')
    return str(candidates[0] if candidates else default)


def discover(gpu=None):
    candidates = [p for p in pci_gpu_devices() if read_attr(p / 'vendor') == '0x10de'
                  and read_attr(p / 'boot_vga', '0') != '1' and not attached_display(p.name)]
    if gpu:
        candidates = [p for p in candidates if p.name == gpu]
    if len(candidates) != 1:
        names = ', '.join(p.name for p in candidates) or 'none'
        raise Refuse('Select one non-boot, displayless NVIDIA GPU with --gpu BDF. Candidates: ' + names)
    selected = candidates[0]
    functions = sorted(p for p in (SYS / 'bus/pci/devices').iterdir()
                       if p.name.rsplit('.', 1)[0] == selected.name.rsplit('.', 1)[0]
                       and read_attr(p / 'vendor') == '0x10de')
    devices = []
    for p in functions:
        host, module = native_driver(p)
        devices.append({'bdf': p.name, 'vendor': read_attr(p / 'vendor'),
                        'device': read_attr(p / 'device'), 'class': read_attr(p / 'class'),
                        'host_driver': host, 'host_module': module})
    label = 'NVIDIA GPU @ ' + selected.name
    try:
        import shlex
        values = shlex.split(run('/usr/bin/lspci', '-Dmm', '-s', selected.name))
        if len(values) >= 4:
            label = values[3][:100]
    except (Refuse, OSError, ValueError):
        pass
    return {'version': 2, 'gpu': selected.name, 'devices': devices, 'label': label,
            'state_dir': runtime_path(selected.name, [d['bdf'] for d in devices]),
            'xorg_config': xorg_path(),
            'display_manager': None, 'hook_path': '/etc/libvirt/hooks/qemu.d/50-nvidia-handoff'}


def configure(config):
    global SETTINGS, GPU, AUDIO, DEVICES, HOST, IDS, MODULES, MODULE_ALIASES, STATE_DIR, XORG_CONFIG, VM, UUID
    if config.get('version') != 2:
        raise Refuse('Unsupported installation configuration version')
    devices = config['devices']
    if not devices or any(not re.fullmatch(r'[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]', d['bdf']) for d in devices):
        raise Refuse('Invalid configured PCI addresses')
    SETTINGS = config
    GPU = config['gpu']
    DEVICES = tuple(d['bdf'] for d in devices)
    if GPU not in DEVICES or len(set(DEVICES)) != len(DEVICES):
        raise Refuse('Invalid GPU/device scope')
    AUDIO = next((d['bdf'] for d in devices if d['class'].startswith('0x04')), None)
    HOST = {d['bdf']: d['host_driver'] for d in devices}
    IDS = {d['bdf']: d['device'] for d in devices}
    MODULE_ALIASES = {a: module_name(a) for a in ('nvidia', 'nvidia_modeset', 'nvidia_uvm', 'nvidia_drm')}
    MODULES = tuple(dict.fromkeys(MODULE_ALIASES.values()))
    STATE_DIR = Path(config['state_dir'])
    XORG_CONFIG = Path(config['xorg_config'])
    VM = UUID = None


def iommu_groups():
    groups = set()
    for bdf in DEVICES:
        p = pci(bdf) / 'iommu_group'
        if not p.is_symlink():
            return None
        groups.add(p.resolve(strict=True))
    return groups


def isolated():
    groups = iommu_groups()
    return bool(groups) and all({p.name for p in (group / 'devices').iterdir()} <= set(DEVICES) for group in groups)


def shared_nvidia_driver():
    return any(p.name not in DEVICES and driver(p.name) == 'nvidia' for p in pci_gpu_devices())


def loaded_nvidia_modules():
    pending = {p.name for p in (SYS / 'module').iterdir() if p.name.startswith('nvidia')}
    order = []
    while pending:
        leaves = sorted(m for m in pending if not ({p.name for p in (SYS / 'module' / m / 'holders').glob('*')} & pending))
        if not leaves:
            raise Refuse('NVIDIA module dependency cycle; refusing unload')
        for m in leaves:
            pending.remove(m)
            order.append(m)
    return list(reversed(order))  # Load order; reverse it for normal removal.


def assert_auxiliary_idle():
    for bdf in DEVICES:
        if driver(bdf) in (None, 'vfio-pci'):
            continue
        for child in (SYS / 'bus/usb/devices').glob('*'):
            if not child.name.startswith('usb') and beneath(child, pci(bdf)):
                raise Refuse('USB device attached to GPU controller: ' + child.name)
        for partner in (SYS / 'class/typec').glob('*partner*'):
            if beneath(partner, pci(bdf)):
                raise Refuse('USB-C partner attached to GPU controller')


def session_records():
    result = []
    for line in run('/usr/bin/loginctl', 'list-sessions', '--no-legend', '--no-pager').splitlines():
        ident = line.split()[0]
        fields = run('/usr/bin/loginctl', 'show-session', ident, '-p', 'Type', '-p', 'Class', '-p', 'State', '-p', 'Active')
        result.append(dict(line.split('=', 1) for line in fields.splitlines() if '=' in line))
    return result


def assert_x11(need_display=False):
    if os.environ.get('XDG_SESSION_TYPE', '').lower() == 'wayland' or os.environ.get('WAYLAND_DISPLAY'):
        raise Refuse('Wayland/Xwayland is unsupported. Use an Xorg session and Xorg greeter.')
    for session in session_records():
        if session.get('Type') == 'wayland' and session.get('State') != 'closing':
            raise Refuse('A Wayland user/greeter session is present. Configure the login manager for Xorg first.')
    if need_display and not os.environ.get('DISPLAY'):
        raise Refuse('Open the GUI from an X11 desktop session')


def display_manager():
    explicit = SETTINGS.get('display_manager')
    candidates = [explicit] if explicit else ['display-manager.service']
    if not explicit:
        binary = read_attr(Path('/etc/X11/default-display-manager'))
        if binary:
            candidates.append(Path(binary).name + '.service')
    for unit in candidates:
        if not unit or not re.fullmatch(r'[A-Za-z0-9_.@-]+\.service', unit):
            raise Refuse('Invalid display-manager unit')
        props = run('/usr/bin/systemctl', 'show', unit, '-p', 'Id', '-p', 'LoadState', '-p', 'ActiveState')
        values = dict(line.split('=', 1) for line in props.splitlines() if '=' in line)
        if values.get('LoadState') == 'loaded' and values.get('ActiveState') == 'active':
            return values.get('Id', unit)
    raise Refuse('No active display manager. Set --display-manager UNIT during installation, or restart a manual Xorg session yourself.')


