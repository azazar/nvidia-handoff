# NVIDIA handoff

Safely share one displayless secondary NVIDIA GPU between host compute, host gaming, and libvirt virtual machines. The primary GPU continues to run the desktop.

## Requirements and scope

- Debian with systemd/logind and an installed proprietary NVIDIA driver, including UVM for CUDA.
- An X11 desktop and X11 login screen. X.Org and XLibre use the same X11 path; Wayland and Xwayland sessions are refused. XLibre still needs a compatible NVIDIA driver.
- A secondary, non-boot NVIDIA GPU with no connected display. Device discovery includes its NVIDIA PCI functions, such as HDMI audio and USB controllers.
- Isolated IOMMU groups for VM passthrough. Unrelated devices in the group are not detached.
- System Python 3; Tkinter for the GUI; `lspci`, kmod tools, `udevadm`, and systemd tools. GUI GPU switching uses `pkexec` and a desktop authentication agent.
- For VMs: libvirt's `virsh`, `qemu:///system`, and suitable guest PCIe topology. Hotplug also depends on the guest OS and NVIDIA driver.

The display manager is detected through systemd and the system X11 configuration; it is not tied to XFCE or a particular login manager. There is one selected GPU per installation. Support for arbitrary Debian, GPU, and guest combinations is not a claim that all combinations have been tested.

## Files

| File | Purpose |
| --- | --- |
| `nvidia-handoff` | GUI, CLI, installer, optional VM startup, and cleanup |
| `nvidia_handoff_core.py` | Shared busy checks, PCI driver transactions, rollback, and hook handling |
| `AGENTS.md` | Rules for maintaining this project |

Both program files are required and must stay together. The core module is not a backup or generated file. There are no third-party Python package requirements.

## Inspect and install

From the repository directory, inspect discovery and preview installation:

```sh
/usr/bin/python3 -I ./nvidia-handoff --probe
sudo /usr/bin/python3 -I ./nvidia-handoff --install --dry-run
```

If multiple GPUs qualify, use `--probe BDF` for discovery or `--gpu BDF` with `--install`; replace `BDF` with the selected PCI address. If login-manager detection needs an override, pass `--display-manager UNIT.service` during installation.

Install after reviewing the system locations below:

```sh
sudo /usr/bin/python3 -I ./nvidia-handoff --install
```

The base installation places the two program files in `/opt` and writes `/etc/nvidia-handoff.json`. It does not rebind a GPU, stop a VM, or restart the desktop. Integration files are created when the relevant setup or switching action needs them. Existing VM integration is retained on upgrades.

Use the installed, root-owned copy for normal operation. The installer can read the user-owned source checkout; the installed application deliberately validates ownership and permissions before loading its core module and configuration.

## Prepare optional VM passthrough

Each intended VM must first have all selected GPU PCI functions configured in its **inactive** definition, with suitable PCIe ports. Then enable optional passthrough:

```sh
sudo /opt/nvidia-handoff enable-optional
```

This processes all persistent VMs whose definitions contain the selected GPU. It moves those device definitions into the VM's handoff metadata and removes the actual GPU host devices from persistent startup configuration. Running VM hardware remains unchanged. This is a one-time definition change, not something repeated before every boot.

For a new installation, setup can also be requested with:

```sh
sudo /usr/bin/python3 -I ./nvidia-handoff --install --optional-gpu
```

PCIe ports must already exist before a guest starts if they are needed for later hotplug. See [libvirt PCI topology and hotplug](https://www.libvirt.org/pci-hotplug.html).

## GUI

Open from your normal X11 session, without sudo:

```sh
/opt/nvidia-handoff --gui
```

Choose a destination:

| Destination | Result |
| --- | --- |
| None | Park the GPU idle, normally on VFIO when isolation permits |
| CUDA without Xorg | Bind NVIDIA and load UVM without attaching the GPU to Xorg |
| Xorg | Enable NVIDIA PRIME for host games; keep the desktop on the primary GPU |
| A prepared VM | Start it with the GPU, or attach to an already running optional-GPU guest |

**Connect** is disabled when the selected destination is already active or an operation is in progress. A partial GPU attachment remains actionable. VM rows distinguish running with the GPU, running without it, and partial attachment.

**Start without GPU** starts the selected shut-off VM without claiming the selected GPU or changing its drivers. It uses the user's libvirt permissions. **Refresh** updates ownership and VM state. **Full cleanup…** removes the installed application and its owned integration after confirmation and safety checks.

Switching into or out of Xorg GPU mode can restart the detected display manager and end graphical sessions. The GUI asks before that transition or before gracefully stopping a different GPU-owning VM. It does not forcibly shut down guests or unplug a GPU from a running guest.

## VM startup is temporary

Ordinary starts from virt-manager or `virsh start` use the persistent GPU-free definition. They do not try to take the GPU from Xorg, CUDA, or another VM.

The script supplies a separate XML configuration for an explicit boot using `virsh create /dev/stdin --validate`. It does not add GPU devices to the persistent XML and then rewrite it after boot. No temporary VM XML file is needed, and there is no interval in which the next ordinary start is persistently configured to take the GPU.

```sh
# Explicit one-time GPU boot; may fall back to no GPU when the safe claim is refused.
sudo /opt/nvidia-handoff start-vm VM_NAME_OR_UUID

# Boot without the selected GPU; requires libvirt write permission, not a driver switch.
/opt/nvidia-handoff start-vm VM_NAME_OR_UUID --without-gpu
```

The GPU remains attached while that VM runs. After the VM powers off, its next ordinary start is GPU-free. A reboot inside the guest keeps the same running VM hardware; it is not a new GPU-free start.

For a running prepared VM, **Connect** attaches the PCI functions with `attach-device --live`, without restarting the guest or changing persistent startup settings. Whether the NVIDIA driver initializes successfully after hotplug must be checked on the particular guest. See [libvirt attach-device](https://www.libvirt.org/manpages/virsh.html#attach-device).

Saved-memory guests are not silently resumed with different hardware, and their saved memory is not automatically discarded. Resolve the managed save state explicitly in virt-manager before requesting a fresh boot. Names and UUIDs are both accepted; an existing UUID is not looked up again as a VM name.

## Host commands

```sh
/opt/nvidia-handoff status
/opt/nvidia-handoff connect --restart-session
/opt/nvidia-handoff disconnect --restart-session
/opt/nvidia-handoff run -- YOUR_GAME_COMMAND
```

`connect` and `disconnect` explicitly permit the display-manager restart and logout. Launch games as your normal user after signing in again.

## System footprint

| Location | When needed |
| --- | --- |
| `/opt/nvidia-handoff`, `/opt/nvidia_handoff_core.py` | Installed program |
| `/etc/nvidia-handoff.json` | Selected hardware, integration paths, and ownership records |
| `/etc/libvirt/hooks/qemu.d/50-nvidia-handoff` | VM integration hook |
| `/etc/X11/xorg.conf.d/90-nvidia-handoff.conf` | Xorg GPU policy when integration needs it |
| `/etc/wireplumber/wireplumber.conf.d/51-nvidia-handoff-*.conf` | Selected HDMI audio exclusion on WirePlumber 0.5+ |
| `/etc/wireplumber/main.lua.d/51-nvidia-handoff-*.lua` | Corresponding policy on WirePlumber 0.4.7+ |
| `/run/nvidia-handoff/<BDF>/` | Private locking, recovery state, and destination policy after an actual operation |
| `/run/user/<UID>/nvidia-handoff-<JOB_UUID>.json` | GUI operation progress; removed after completion is consumed |

These are new-install defaults. Migrated installations retain paths recorded in their configuration. Merely opening or refreshing the GUI does not create runtime transaction files. There is no `/usr/local/bin` launcher, permanent handoff systemd unit, distribution-copy directory, or automatic archive/backup tree. Switching jobs use a transient systemd unit.

WirePlumber can hold a GPU's HDMI audio control device even when no sound is playing. The generated policy excludes only the selected displayless GPU's HDMI audio; it does not disable host audio globally. Apply a newly created policy by restarting WirePlumber in the affected desktop session or logging in again; restarting it can briefly interrupt sound.

Hooks never invoke libvirt commands, and workers do not hold the GPU lock across libvirt calls. Busy checks include open device descriptors and mappings. Partial host detach is rolled back. If only some functions reach a running VM, the program retains recovery state instead of forcibly unplugging them.

## Cleanup and logs

Preview removal before doing it:

```sh
sudo /opt/nvidia-handoff --cleanup --dry-run
sudo /opt/nvidia-handoff --cleanup
```

The CLI asks for `REMOVE`; `--yes` is available for an explicitly confirmed noninteractive removal. Cleanup refuses while the GPU is owned by a VM, Xorg, or another workload, or a transition is active. It preserves administrator-modified integration files by refusing removal rather than overwriting or deleting them.

Successful cleanup restores safe host bindings and the original Xorg policy, removes owned integration files and runtime state, and removes handoff metadata while retaining GPU-free VM startup. It does not restore an old whole-VM XML over later user edits. The source checkout and system journal history remain. Log in again to restore HDMI audio monitoring after policy removal.

```sh
sudo journalctl -t nvidia-handoff
sudo journalctl -u nvidia-handoff.service
```

Report the error, current destination, and whether a guest has a complete or partial attachment. Do not force-unbind, reset the PCI device, or erase retained recovery state to bypass a busy check.
