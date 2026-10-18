# NVIDIA handoff

Share a displayless secondary NVIDIA GPU between CUDA applications, host games, and libvirt virtual machines. The primary GPU runs the desktop.

**Disclosure:** This project and its documentation were written with Codex.

## Features

- **GPU destinations:** idle, CUDA without Xorg, Xorg with NVIDIA PRIME, or a virtual machine.
- **Automatic Debian VM setup:** Connect prepares missing passthrough hardware and installs a hardware-matched NVIDIA driver through the QEMU guest agent. Prepared VMs appear first; unprepared VMs are listed too.
- **Docker GPU support in Debian VMs:** guest software setup configures the NVIDIA runtime when Docker Engine is installed, preserving Docker settings and reloading without a daemon restart.
- **Optional VM passthrough:** start a VM with or without the GPU; ordinary VM starts are GPU-free after setup.
- **Live attachment:** connect the GPU and its associated PCI functions to a prepared running VM without restarting it.
- **Per-run assignment:** a VM keeps the GPU until it powers off; its next ordinary start is GPU-free.
- **Stateless discovery:** detect the secondary GPU, PCI functions, native drivers, IOMMU groups, VM definitions, live ownership, Xorg policy, and display manager without a hardware/status configuration file.
- **Desktop GUI:** view the current destination, VM state, complete or partial attachments, and verified-driver tags; supports desktop fonts and high DPI.
- **Quick connections:** ready VMs with verified drivers skip the ordinary Connect confirmation. Disruptive actions still require confirmation.
- **Safety checks:** detect busy devices and active displays, roll back partial host detach, and retain recovery state after incomplete operations.
- **Cleanup:** preview or remove the installed application and its integration from the GUI or CLI.

## Requirements

- Debian with systemd/logind, system Python 3, and the proprietary NVIDIA driver. CUDA mode requires NVIDIA UVM.
- A secondary, non-boot NVIDIA GPU with no connected display, plus a separate primary GPU for the desktop.
- An X11 desktop and login screen. X.Org and XLibre are supported with compatible NVIDIA drivers; Wayland and Xwayland are unsupported.
- Tkinter for the GUI; `lspci`, kmod tools, `udevadm`, systemd tools, and `pkexec` with a desktop authentication agent.
- For automatic VM setup: libvirt, `virsh`, `qemu:///system`, isolated IOMMU groups, and a persistent x86_64 QEMU/KVM q35 or i440fx guest running Debian 12 or 13 (amd64). The guest must already have `qemu-guest-agent` installed, enabled, and permitted to run guest commands. Missing guest hardware/channel configuration is prepared automatically; the guest agent itself cannot be installed without an existing access method.
- Guest Docker setup also requires system Python 3 and an active rootful `docker.service` using `/etc/docker/daemon.json` and `/run/docker.sock`. Rootless Docker, custom configuration paths and runtime flags require manual setup.
- Guest driver installation requires network access and APT, plus headers for the running guest kernel. Secure Boot signing-key enrollment, if required, must be completed manually. Live attachment also requires guest OS and driver hotplug support.

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
| A VM | Prepare and connect the GPU; install missing Debian drivers |

Prepared VMs are listed before unprepared VMs. Unsupported VM architectures or transient definitions remain visible with an explanation. Select a destination and click **Connect**. Ready VMs with a successful driver-verification tag, or a successful live driver check, skip the ordinary connection confirmation. Other ready VMs get a short confirmation without preparation warnings. A conflicting live driver check, missing preparation, planned VM shutdown, or desktop restart keeps confirmation enabled. The resizable confirmation wraps and scrolls its text while keeping action buttons visible. Debian GPU software setup is a separate checkbox, off by default for prepared VMs; it verifies drivers and configures an installed Docker Engine. **VM software…** opens this setup confirmation for the selected VM, including one already using the GPU. The button is disabled for an already active destination with a working driver, or while an operation is in progress. If a connected guest’s NVIDIA driver is unavailable or cannot be verified, Connect remains available to finish Debian setup. Partial VM attachments can be completed through the same action.

**Start without GPU** starts a selected shut-off VM with GPU-free startup settings. **Refresh** updates ownership and VM status. **Full cleanup…** opens application removal.

The GUI shows the current GPU destination and distinguishes VMs running with the GPU, without it, or with only some PCI functions attached. Its confirmation describes VM definition changes, guest package/repository installation, and any graceful shutdown or guest reboot needed. Xorg transitions and shutdown of another GPU-owning VM also require confirmation.

## VM setup

No manual PCI passthrough preparation is required for automatic setup. Select the VM and click **Connect**. After confirmation, the script creates missing GPU function templates, adds PCI capacity and a guest-agent channel when necessary, and preserves GPU-free ordinary startup. Existing unrelated VM devices and metadata are preserved.

A running VM with available PCI capacity receives the GPU live. If it needs new controllers or a guest-agent channel, Connect requests permission for a graceful shutdown and fresh start. It never forces a shutdown or removes saved guest memory.

The guest agent identifies the actual operating system before package changes. Automatic driver setup supports Debian 12 and 13 amd64 only. A working driver for the selected GPU is left untouched. Otherwise, the script installs matching kernel headers, adds NVIDIA's signed APT repository using its official keyring package when needed, and uses NVIDIA's driver assistant to select the appropriate driver packages. Missing Debian contrib/non-free components are enabled in a separate APT source without overwriting existing source files. Package changes are simulated first and package removal is refused.

The setup confirmation authorizes these guest package changes, the NVIDIA driver license, and one graceful guest reboot if the new driver cannot initialize immediately. Driver readiness is checked again after reboot. Successful verification adds `driver-verified="yes"` to the existing optional-GPU metadata in the inactive VM definition. This records completed setup, not current GPU ownership; a failed live check overrides the tag. The tag appears as **drivers verified** in the VM list and is removed with the handoff metadata during cleanup. Administrator authentication remains required when applicable. Failures retain the GPU assignment and report what still needs attention. A missing agent, unsupported guest OS, network/package failure, or required Secure Boot enrollment is reported rather than treated as success.

If Docker Engine is installed inside the guest, software setup checks its live NVIDIA runtime registration. It installs the NVIDIA Container Toolkit only when the runtime is missing, using an existing APT candidate or adding NVIDIA’s signed container-toolkit repository. The new repository files, when needed, are `/etc/apt/sources.list.d/nvidia-handoff-container-toolkit.list` and `/usr/share/keyrings/nvidia-handoff-container-toolkit.gpg`. Package removal and upgrades of already-installed requested packages are disabled.

Setup validates and atomically merges the NVIDIA runtime entry in `/etc/docker/daemon.json`, preserving other runtimes, the default runtime, and unrelated settings. It signals Docker to reload and checks runtime registration; it does not restart Docker, launch containers, or download container images. A failed reload retains the validated configuration and reports the problem. An absent Docker Engine is skipped; a stopped or unsupported daemon requires manual attention. Registration is verified, but no container CUDA workload is run automatically.

This configures Docker inside Debian VMs only. It does not install Docker itself, Python libraries, PyTorch, or `libtorch-dev`, and does not configure host Docker. Toolkit packages and repositories remain under the guest package manager after application cleanup. See the [NVIDIA Container Toolkit guide](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) and [Docker configuration reload documentation](https://docs.docker.com/reference/cli/dockerd/#configuration-reload-behavior).

See [NVIDIA's Debian installation guide](https://docs.nvidia.com/datacenter/tesla/driver-installation-guide/debian.html), [driver assistant](https://docs.nvidia.com/datacenter/tesla/driver-installation-guide/driver-assistant.html), and [libvirt PCI hotplug](https://www.libvirt.org/pci-hotplug.html).

For CLI setup, use the VM UUID with explicit preparation consent:

```sh
sudo /opt/nvidia-handoff select VM_UUID --prepare-vm --setup-driver
```

`--prepare-vm` permits missing VM hardware setup; `--setup-driver` separately permits Debian GPU software setup: missing driver installation, one guest reboot if needed, and NVIDIA runtime configuration for an installed Docker Engine. Use it again for an already connected VM to set up Docker without changing GPU ownership. Omit both for a prepared VM when only connecting its GPU. Add `--shutdown-owner` only when you authorize a required graceful VM shutdown, and `--restart-session` only when you authorize an Xorg session restart. Host and guest changes outside the permitted filesystem scope must be reviewed and authorized before execution.

The existing `enable-optional` command remains available for converting manually configured passthrough definitions to GPU-free ordinary startup without guest package changes:

```sh
sudo /opt/nvidia-handoff enable-optional
```

## Starting and connecting VMs

```sh
# Start with a one-time GPU assignment.
sudo /opt/nvidia-handoff start-vm VM_NAME_OR_UUID

# Start without the selected GPU.
/opt/nvidia-handoff start-vm VM_NAME_OR_UUID --without-gpu
```

The GPU boot command can fall back to GPU-free startup when the GPU cannot be claimed safely. Starting without the GPU uses the caller's libvirt permissions and leaves GPU ownership unchanged.

Ordinary starts from virt-manager or `virsh start` use the persistent GPU-free definition. An explicit GPU boot uses a configuration for that run only. The GPU remains connected until the VM powers off; a reboot inside the guest retains the same hardware.

To attach to a running VM, select it and click **Connect**. Missing preparation is handled by the same action; new PCI controllers or a missing agent channel require a confirmed shutdown and start. The attachment affects the running guest only. Successful NVIDIA driver initialization after hotplug depends on the guest configuration. See [libvirt attach-device](https://www.libvirt.org/manpages/virsh.html#attach-device).

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

Cleanup requires an idle GPU and no active transition. It restores host GPU bindings and the original Xorg policy, removes owned integration and runtime files, and leaves VM startup GPU-free. Administrator-modified integration files block removal for review. Unused PCI controllers and guest-agent channels created by automatic preparation are removed from inactive definitions; devices subsequently used by other VM hardware are preserved. Installed guest driver packages and APT repositories remain under the guest package manager. The source checkout and journal history remain. Log in again after cleanup to restore HDMI audio monitoring.

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

For mapped dialog resize checks on an existing X11 display, run `NVIDIA_HANDOFF_GUI_TESTS=1 /usr/bin/python3 -I ./test_stateless.py`; these cover 96, 144, and 192 DPI.

Tests use synthetic devices and integration files inside the checkout. They do not change host drivers, services, or VMs.
