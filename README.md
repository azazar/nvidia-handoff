# NVIDIA handoff

Share a displayless secondary NVIDIA GPU between CUDA applications, host games, and libvirt virtual machines. The primary GPU runs the desktop.

**Disclosure:** This project and its documentation were written with Codex.

## Features

- **GPU destinations:** idle, CUDA without Xorg, Xorg with NVIDIA PRIME, or a virtual machine.
- **Optional VM passthrough:** start a VM with or without the GPU; ordinary VM starts are GPU-free after setup.
- **Live attachment:** connect the GPU and its associated PCI functions to a prepared running VM without restarting it.
- **Per-run assignment:** a VM keeps the GPU until it powers off; its next ordinary start is GPU-free.
- **Stateless discovery:** detect the secondary GPU, PCI functions, native drivers, IOMMU groups, VM definitions, live ownership, Xorg policy, and display manager without a hardware/status configuration file.
- **Desktop GUI:** view the current destination, VM state, and complete or partial attachments; supports desktop fonts and high DPI.
- **Safety checks:** detect busy devices and active displays, roll back partial host detach, and retain recovery state after incomplete operations.
- **Cleanup:** preview or remove the installed application and its integration from the GUI or CLI.

## Requirements

- Debian with systemd/logind, system Python 3, and the proprietary NVIDIA driver. CUDA mode requires NVIDIA UVM.
- A secondary, non-boot NVIDIA GPU with no connected display, plus a separate primary GPU for the desktop.
- An X11 desktop and login screen. X.Org and XLibre are supported with compatible NVIDIA drivers; Wayland and Xwayland are unsupported.
- Tkinter for the GUI; `lspci`, kmod tools, `udevadm`, systemd tools, and `pkexec` with a desktop authentication agent.
- For VM passthrough: libvirt, `virsh`, `qemu:///system`, isolated IOMMU groups, and suitable guest PCIe ports. Live attachment also requires guest OS and driver hotplug support.

Each operation manages one selected GPU and its associated NVIDIA PCI functions, including HDMI audio and any GPU USB controllers. The display manager is detected automatically, with a per-command override. GPU and guest compatibility varies by configuration.

## Installation

Keep `nvidia-handoff` and `nvidia_handoff_core.py` together in the source directory. They provide the GUI/CLI and shared GPU control logic respectively. No third-party Python packages are required.

Inspect hardware and preview installation:

```sh
/usr/bin/python3 -I ./nvidia-handoff --probe
sudo /usr/bin/python3 -I ./nvidia-handoff --install --dry-run
```

Install:

```sh
sudo /usr/bin/python3 -I ./nvidia-handoff --install
```

A fresh installation places only the two program files in `/opt`; it creates no hardware/status configuration file or runtime directory. Installation preserves running VM hardware, GPU bindings, and graphical sessions. Use the installed copy for normal operation.

For multiple eligible GPUs, use `--probe BDF` to inspect one and `--install --gpu BDF` to preview/install for that hardware. Supply `--gpu BDF` before the operation on subsequent commands; selection is not saved. Use `--display-manager UNIT.service` before an operation to override display-manager detection for that command and its workers.

```sh
/opt/nvidia-handoff --gpu 0000:02:00.0 --gui
sudo /opt/nvidia-handoff --gpu 0000:02:00.0 --display-manager example.service connect --restart-session
```

To migrate an existing installation, run the same `--install --dry-run` and `--install` commands from the updated source. The installer validates `/etc/nvidia-handoff.json` against live hardware, transfers file ownership and original Xorg policy records into comments in existing integration files, and removes the JSON only after successful installation. It preserves existing recovery transactions and lock locations. Review the preview and authorize the listed system paths before applying it.

## GUI

Open from your normal X11 session:

```sh
/opt/nvidia-handoff --gui
```

| Destination | Operation |
| --- | --- |
| None | Park the GPU idle, normally on VFIO when isolation permits |
| CUDA without Xorg | Bind NVIDIA and load UVM for compute |
| Xorg | Enable NVIDIA PRIME for host games |
| A prepared VM | Start it with the GPU or attach to the running guest |

Select a destination and click **Connect**. The button is disabled for an already active destination or while an operation is in progress. Partial VM attachments can be completed through the same action.

**Start without GPU** starts a selected shut-off VM with GPU-free startup settings. **Refresh** updates ownership and VM status. **Full cleanup…** opens application removal.

The GUI shows the current GPU destination and distinguishes VMs running with the GPU, without it, or with only some PCI functions attached. Xorg transitions and graceful shutdown of another GPU-owning VM require confirmation.

## VM setup

Configure all selected GPU PCI functions in each intended VM's inactive definition, with the PCIe ports needed for passthrough and hotplug. Enable optional GPU support:

```sh
sudo /opt/nvidia-handoff enable-optional
```

This configures all matching persistent VMs for GPU-free ordinary starts and records their GPU device templates. Running VM hardware remains unchanged.

For a new installation, optional VM setup can be included in installation:

```sh
sudo /usr/bin/python3 -I ./nvidia-handoff --install --optional-gpu
```

PCIe ports needed for live attachment must exist before the guest starts. See [libvirt PCI topology and hotplug](https://www.libvirt.org/pci-hotplug.html).

## Starting and connecting VMs

```sh
# Start with a one-time GPU assignment.
sudo /opt/nvidia-handoff start-vm VM_NAME_OR_UUID

# Start without the selected GPU.
/opt/nvidia-handoff start-vm VM_NAME_OR_UUID --without-gpu
```

The GPU boot command can fall back to GPU-free startup when the GPU cannot be claimed safely. Starting without the GPU uses the caller's libvirt permissions and leaves GPU ownership unchanged.

Ordinary starts from virt-manager or `virsh start` use the persistent GPU-free definition. An explicit GPU boot uses a configuration for that run only. The GPU remains connected until the VM powers off; a reboot inside the guest retains the same hardware.

To attach to a running prepared VM, select it and click **Connect**. The attachment affects the running guest only. Successful NVIDIA driver initialization after hotplug depends on the guest configuration. See [libvirt attach-device](https://www.libvirt.org/manpages/virsh.html#attach-device).

Fresh boot requires any managed save state to be resumed or removed explicitly in virt-manager first. Both VM names and UUIDs are accepted.

## Host gaming and status

```sh
/opt/nvidia-handoff status
/opt/nvidia-handoff connect --restart-session
/opt/nvidia-handoff disconnect --restart-session
/opt/nvidia-handoff run -- YOUR_GAME_COMMAND
```

`connect` enables the Xorg gaming mode; `disconnect` returns Xorg to the primary GPU. These transitions restart the detected display manager and end graphical sessions. Save your work before using `--restart-session`, then sign in again and launch games as your normal user.

## Installed files

| Location | Purpose |
| --- | --- |
| `/opt/nvidia-handoff`, `/opt/nvidia_handoff_core.py` | Program files |
| `/etc/libvirt/hooks/qemu.d/50-nvidia-handoff` | VM lifecycle integration |
| `/etc/X11/xorg.conf.d/90-nvidia-handoff.conf` | Xorg GPU policy |
| `/etc/wireplumber/wireplumber.conf.d/51-nvidia-handoff-*.conf` | Selected HDMI audio policy on WirePlumber 0.5+ |
| `/etc/wireplumber/main.lua.d/51-nvidia-handoff-*.lua` | Selected HDMI audio policy on WirePlumber 0.4.7+ |
| `/run/nvidia-handoff/<BDF>/` | Locking, recovery state, and destination policy |
| `/run/user/<UID>/nvidia-handoff-<JOB_UUID>.json` | GUI operation progress |

Integration files are created for the relevant setup or switching action. These are default paths; existing Xorg policies, owned hooks, and migrated recovery directories are discovered in place. Integration comments contain content hashes and the original Xorg setting for safe cleanup; they do not cache hardware or ownership status. GUI refresh and status inspection are read-only. Current attachment comes from live VM definitions, device bindings, and X11 state. Temporary locks, rollback records, and destination policy under `/run` protect handoffs; they are not a replacement hardware/status database. Switching jobs run in a transient systemd unit.

The WirePlumber policy excludes the selected displayless GPU's HDMI audio from host audio monitoring. Apply a new policy by logging in again or restarting WirePlumber in the affected session; the latter can briefly interrupt sound.

## Cleanup

```sh
sudo /opt/nvidia-handoff --cleanup --dry-run
sudo /opt/nvidia-handoff --cleanup
```

The GUI provides the same operation through **Full cleanup…**. Interactive CLI removal asks for `REMOVE`; `--yes` confirms noninteractive removal.

Cleanup requires an idle GPU and no active transition. It restores host GPU bindings and the original Xorg policy, removes owned integration and runtime files, and leaves VM startup GPU-free. Administrator-modified integration files block removal for review. The source checkout and journal history remain. Log in again after cleanup to restore HDMI audio monitoring.

## Logs

```sh
sudo journalctl -t nvidia-handoff
sudo journalctl -u nvidia-handoff.service
```

## Verification

Run isolated regression checks from the source directory:

```sh
/usr/bin/python3 -I ./test_stateless.py
```

Tests use synthetic devices and integration files inside the checkout. They do not change host drivers, services, or VMs.
