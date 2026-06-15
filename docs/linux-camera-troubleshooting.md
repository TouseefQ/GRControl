# Getting the IDS USB3 Camera Working on Linux

## 1. Summary

The IDS **U3-34LxXCP-C** USB3 Vision camera worked correctly on Windows (live
image in IDS peak Cockpit) but produced **no image** on the Ubuntu machine —
neither in IDS's own Cockpit application nor in the GRControl software.
Investigation revealed that the failure was not a single fault but **three
independent problems stacked on top of one another**, each hiding the next. Once
all three were resolved at the operating-system level, two further bugs in the
GRControl application code were found and fixed. The camera now streams live
video in GRControl.

## 2. Hardware / software context

- **Camera:** IDS uEye+ **U3-34LxXCP-C**, a 12-megapixel (3504 × 3536) USB3
  Vision camera, USB ID `1409:8000`.
- **Host:** Ubuntu (Linux 6.8), Intel Comet Lake xHCI USB 3.1 controller.
- **Driver stack:** IDS peak SDK 2.21 (tar-archive install), using the USB3
  Vision (U3V) GenTL transport layer.
- **Application:** GRControl (FastAPI backend + IDS peak Python API).

## 3. Symptom

The camera was invisible to all software. In the earliest state it did not even
appear in `lsusb`; later it enumerated correctly but still showed no image in any
application, including IDS's own Cockpit. Because Cockpit is IDS's native tool and
sits directly on the SDK, the fact that *it* failed proved the problem was below
the application layer — in the USB/driver/permissions stack — not in GRControl.

## 4. Diagnosis and root causes

A bottom-up diagnosis was performed, fixing each layer and re-testing before
moving up.

### 4.1 USB enumeration (cabling/port)

Initially the camera did not appear in `lsusb` at all. Watching the kernel log
(`sudo dmesg -w`) while reconnecting showed that, on a correct USB 3 port, the
device enumerated cleanly:

```
usb 2-1: new SuperSpeed USB device number ... using xhci_hcd
usb 2-1: Product: U3-34LxXCP-C
usb 2-1: Manufacturer: IDS Imaging Development Systems GmbH
```

So the cable/port were fine; this was simply a connection issue. The device
negotiated SuperSpeed (5 Gb/s) with no errors.

### 4.2 Missing udev permissions (camera could not be opened)

The camera now enumerated but still showed nothing. The cause: the IDS peak SDK
ships a **udev rule** that grants user-level access to the camera, but the
tar-archive installer does **not activate it**. The rule file was present only at
`/usr/local/lib/udev/rules.d/99-ids-usb-access.rules` — a path that udev does
**not** read. Because the rule was inactive, the USB device node was owned by
`root`, so any normal-user program (Cockpit or GRControl) was denied access. The
device showed in `lsusb` (which only reads sysfs) but could not be opened.

**Fix:** run the IDS-provided installer, which copies the rule into the active
`/etc/udev/rules.d/` directory and reloads udev:

```bash
sudo /usr/local/share/ids-peak/scripts/ids_install_udev_rule.sh
```

After replugging, the device node became world-accessible (`crwxrwxrwx`),
confirming the rule was active.

### 4.3 Ruling out wrong software / missing libraries

Two further hypotheses were checked and **eliminated**:

- **Wrong SDK package.** IDS offers "IDS peak" and "IDS peak with uEye Transport
  Layer." The camera's USB descriptor reports `bDeviceClass=ef,
  bDeviceSubClass=02, bDeviceProtocol=01, configuration="USB3 Vision"` — i.e. it
  self-identifies as a **USB3 Vision** device. Such cameras are driven by the U3V
  transport layer, which was already installed (`ids_u3vgentl.cti`). The uEye-TL
  package and IDS Software Suite are only for legacy UI-model cameras and were
  correctly **not** needed.
- **Missing dependencies.** The IDS readme lists `libusb`, `libatomic1` and
  `avahi-autoipd` as requirements; all three were installed, and the U3V GenTL
  producer loaded with all its shared-library dependencies resolved.

### 4.4 The real blocker — USB transfer-buffer limit

A standalone test using the IDS Python SDK (bypassing the GUI) showed the
decisive result. The camera:

- was discovered, opened with read/write access,
- reported correct settings (`TriggerMode = Off`, `AcquisitionMode = Continuous`,
  exposure ≈ 15 ms),
- started acquisition without error,
- but **never delivered a single frame** — `WaitForFinishedBuffer` timed out
  (`GC_ERR_TIMEOUT`), and crucially the camera **did not disconnect** (it stayed
  on the bus).

This "opens, arms, but zero frames, no disconnect" signature ruled out USB Link
Power Management (a NO-LPM quirk was applied with no effect) and USB autosuspend
(the device was `active`). The remaining cause was the **usbfs transfer-buffer
limit**.

The boot configuration pinned `usbcore.usbfs_memory_mb=0`. Although the kernel
documents `0` as "no limit," on this system it effectively starved the large USB3
bulk transfers needed for a 15.5 MB frame, so acquisition armed but no data was
ever transferred. Raising the limit fixed it immediately:

```bash
echo 1000 | sudo tee /sys/module/usbcore/parameters/usbfs_memory_mb
```

After this change the full frame was delivered on the first attempt:

```
FRAME RECEIVED: 3504 x 3536, 15487680 bytes  →  STREAMING WORKS
```

Made permanent via the kernel command line:

```bash
sudo sed -i 's/usbfs_memory_mb=0/usbfs_memory_mb=1000/' /etc/default/grub
sudo update-grub
```

At this point IDS peak Cockpit showed a live image.

## 5. Application-level issues (GRControl)

With the camera working at the system level, two bugs in GRControl were found and
fixed:

1. **Single-process device contention.** A USB3 Vision camera can be opened by
   only one program at a time. While Cockpit was still running it held the
   camera, so GRControl reported "Camera open failed." Operating rule
   established: **never run Cockpit and GRControl simultaneously.**

2. **Preview crash (HTTP 503).** Once GRControl could open the camera, the
   preview still failed. The cause was a bug in the preview code: it called
   `Image.Scale(factor, factor)` with two floating-point scale factors, but the
   IDS IPL API's `Scale()` accepts a `Size2D` object of **target pixel
   dimensions**, not scale factors. The resulting `TypeError` was swallowed and
   surfaced as a 503. Fixed by constructing a `Size2D` with the target
   width/height.

3. **Non-idempotent open.** Opening an already-open camera threw an error instead
   of succeeding; `open()` was made idempotent.

## 6. Live video preview (enhancement)

The original UI offered only a single-snapshot "Refresh" button. A continuous
**live preview** was added: a new backend endpoint streams MJPEG
(`multipart/x-mixed-replace`) which the browser plays natively, with a Live/Stop
toggle in the UI. The stream serves one viewer at a time and automatically pauses
during a scan so it never competes with scan capture for camera buffers.
Effective frame rate is ≈ 8–9 fps, limited by the per-frame debayering and
downscaling of the 12 MP image.

## 7. Resolution checklist (for a fresh Linux setup)

1. Connect the camera to a USB 3 port; confirm it enumerates at SuperSpeed
   (`lsusb`, `dmesg`).
2. Install the IDS udev rule:
   `sudo /usr/local/share/ids-peak/scripts/ids_install_udev_rule.sh`, then replug.
3. Set `usbcore.usbfs_memory_mb=1000` on the kernel command line and reboot.
4. Use the plain "IDS peak" (U3V) package — not the uEye-TL variant.
5. Do not run IDS Cockpit and GRControl at the same time.

## 8. Key lessons

- A camera "working on Windows but not Linux" is almost always an OS-level
  USB/permissions issue, not an application bug — verifying with the vendor's own
  tool (Cockpit) isolates the layer quickly.
- `usbcore.usbfs_memory_mb=0` does **not** behave as "unlimited" for
  high-bandwidth USB3 cameras on this system; an explicit large value (1000) is
  required.
- "No frames but no disconnect" points to the data-transfer buffer, distinct from
  "device drops off the bus," which points to power-management (LPM/autosuspend).

## See also

- [`scripts/fix_usb_lpm.sh`](../scripts/fix_usb_lpm.sh) — disables USB Link Power
  Management, needed on some hosts if the camera drops out *while streaming*.
- [README → Linux notes → Camera setup on Linux](../README.md#camera-setup-on-linux-required-for-the-ids-usb3-camera)
