# Project rules

These rules preserve the user's requested behavior and lessons from previous fixes. Keep changes small, reviewable, and consistent with the actual implementation.

## Filesystem scope and minimal footprint

- Work in this repository. Its specific exception permits access through `~/src/nvidia-handoff` to `~/Private/Projects/nvidia-handoff`; it does not permit reading or changing any other location under `~/Private/`.
- Before creating, modifying, moving, or deleting anything outside the user's `$HOME` (`~`) and `/opt`, obtain separate, explicit user approval for the concrete paths and actions. This includes `/tmp`, `/run`, `/etc`, `/usr`, and `/var`. A task request, sudo access, or OS authentication dialog is not blanket approval. Apply this to indirect changes made by commands, installers, scripts, and services, and check symlink destinations.
- Create files only when the requested action actually needs them. Keep preparation and test artifacts in an allowed location and remove them when no longer needed. Do not create unsolicited backup versions, source duplicates, archives, compatibility links, exports, launchers, or distribution directories.
- Install only the two required program files in `/opt`. Do not add `/usr/local/bin` links or duplicate the source checkout as an installation side effect. Documentation changes and source-copy requests do not authorize deployment or host configuration changes.
- Opening the GUI, refreshing it, and inspecting status must be read-only. Do not create runtime state merely to display ownership. Create integration files only for the relevant setup/action, retain only ownership/restoration receipts in the relevant integration files, and reuse existing helpers instead of duplicating logic.
- Do not add dependencies without authorization. Do not add speculative `.gitignore` entries. Keep temporary and unrequested files out of commits; commit only the requested scope.

## GPU and host safety

- Detect hardware, bindings, VM ownership, Xorg policy, and the display manager live. Do not restore a permanent hardware/status JSON or cache the last selected destination as current attachment. Read the old JSON only during explicit installer migration; remove it only after successful validation and migration. Preserve transient locks and rollback state needed for an active handoff.
- The GPU is secondary and displayless. Discover PCI addresses, functions, native drivers, IOMMU groups, display ownership, and libvirt definitions. Do not hardcode a GPU model, BDF, group number, VM name/UUID, desktop GPU vendor, desktop environment, or display manager.
- Refuse active display/framebuffer use and busy GPU/audio device descriptors or memory mappings before changing bindings. Idle-looking HDMI audio can still be held by WirePlumber; match the selected PCI function rather than assuming a fixed ALSA card number.
- Never bypass a busy check by force-unbinding Xorg's NVIDIA device, forcing module removal, resetting/removing/rescanning PCI devices, killing workloads, or disabling checks. A previous busy Xorg unbind caused a kernel oops.
- Treat all selected GPU PCI functions as a transaction. Roll back partial host detach; preserve recovery state if rollback cannot complete. Never erase interlocks or state just to make an operation proceed.
- Do not globally stop audio services or disable unrelated sound cards to free HDMI audio. Limit any audio rule to this displayless GPU, handle the installed WirePlumber format, and explain when a session restart is required to apply it.
- Support X.Org and XLibre through X11, with compatible drivers. Detect and refuse Wayland/Xwayland and active Wayland greeters for GPU switching. Discover the active login manager; do not assume LightDM, GDM, SDDM, or XFCE.
- Xorg mode changes can end graphical sessions: require explicit permission for that transition. Keep the primary GPU responsible for the desktop. Never interrupt running VMs or rebind their GPU while installing or updating the program.

## Libvirt, locks, and VM lifecycle

- Never call virsh or other libvirt APIs from a libvirt hook. Never hold the shared GPU lock while making libvirt calls; synchronous hooks would deadlock waiting for the same lock.
- Keep hook work narrow: pre-start preparation and post-stop release. Reconnect and unrelated events must be harmless no-ops; a reconnect failure can jeopardize a running guest.
- Ordinary VM starts must be GPU-free, including when the GPU is busy elsewhere. Keep persistent startup definitions free of the selected GPU host devices after optional setup. Preserve the guest's other devices, PCIe ports, and unrelated metadata.
- Use one-time boot XML in memory with the existing VM identity. Do not persistently attach the GPU before starting and then try to undo the definition afterward: that creates a crash window and unnecessary disk changes. Do not create temporary VM XML files or whole-definition backup trees.
- Adding the GPU to a running prepared VM uses live-only hotplug and must not restart that guest. Do not use persistent attachment flags. If attachment is partial, retain ownership and an actionable error rather than force-unplugging a function already assigned to the guest.
- Starting without the GPU must not claim it, check whether a CUDA workload can be displaced, change drivers, or restart Xorg. Use the caller's libvirt permissions rather than elevating the entire GUI unnecessarily.
- Do not silently discard or restore saved guest memory with changed hardware. Do not force shutdowns; any planned graceful shutdown of another GPU-owning VM must be explicit and confirmed.
- Accept names and UUIDs. A UUID supplied by the GUI is already an identifier: normalize and use it directly, rather than passing it to `virsh domuuid` as a name. Recheck live state before acting on inventory that may be stale.
- Keep libvirt calls outside hooks and locks even in failure handling, rollback, cleanup, and tests. Log useful errors without exposing secrets or adding unnecessary persistent log files.

## GUI behavior

- Keep all application text in English. Show the actual current destination and distinguish a running owner, a running VM without the GPU, and partial attachment. Row selection is not evidence of GPU ownership.
- Disable Connect while busy, for an unavailable target, and when the selected destination is already fully active. Apply the same no-op guard before confirmation/dispatch. Keep partial attachments actionable so they can be completed.
- Enable Start without GPU only for an eligible shut-off selection with GPU-free persistent startup. Keep it separate from the GPU Connect action, and restore control state after both success and failure.
- Respect desktop fonts and DPI; size table rows from font metrics. Check 96, 144, and 192 DPI when changing layout. Check real widget bounds after mapping, not only requested sizes in an invisible/unmapped window.
- Reserve height for action buttons. A long wrapped error or a refreshed multi-line status must shrink the table instead of clipping the footer. Keep all labels and buttons usable at the supported minimum window size.

## Cleanup and verification

- Preserve both the GUI Full cleanup action and CLI `--cleanup` / `--cleanup --dry-run`. Preview must not create or change files. Refuse a busy GPU or active transition before changing anything.
- Remove only verified tool-owned files, expected artifacts, and metadata. Validate paths, permissions, symlinks, and recorded content hashes. Preserve administrator edits and unexpected files; never recursively delete an unverified directory or restore a stale full VM definition.
- Full removal leaves VM startup GPU-free, restores owned host policy safely, and removes runtime and abandoned progress files. Do not remove the source repository or system journal history. Prevent stale workers from recreating state after uninstall.
- Test each code change using focused checks. Cover busy refusal, rollback, both boot modes, live attachment failures, UUID/name dispatch, current-target button state, asynchronous success/failure, and long-error layout as relevant.
- Mock or intercept VM creation, hotplug, service restarts, and driver mutations in tests. Do not start/stop real VMs or switch real GPU bindings merely to test a patch without explicit authorization. Read-only integration checks should clearly state which mutation was intercepted.
- Verify deployment changes only the intended `/opt` files and preserves VM XML, power states, PIDs, GPU bindings, configuration, and active recovery state. Remove preparation files afterward.
- Keep README commands aligned with actual CLI options and behavior. Distinguish verified behavior from untested GPU/guest combinations; a guest reboot keeps its live hardware, whereas a power-off followed by a fresh ordinary start uses the GPU-free persistent definition.
