#!/usr/bin/env bash
#
# Fix: IDS U3V camera (idVendor:idProduct = 1409:8000) drops off the USB bus
# the moment streaming starts on Linux ("usb_submit_urb returned -19" / USB
# disconnect), while the same camera/cable/port works on Windows.
#
# Cause: Linux enables USB3 Link Power Management (LPM, U1/U2 link states) for
# the camera; its firmware mishandles them under streaming load. The fix is the
# NO_LPM usb-quirk ("k") for this device.
#
# Run with sudo:  sudo ./scripts/fix_usb_lpm.sh
#
set -euo pipefail

QUIRK="1409:8000:k"          # k = USB_QUIRK_NO_LPM
GRUB=/etc/default/grub

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run as root:  sudo $0" >&2
    exit 1
fi

echo "==> 1/3  Applying quirk at runtime (no reboot needed for the test)"
echo "$QUIRK" > /sys/module/usbcore/parameters/quirks
echo "    /sys/module/usbcore/parameters/quirks = $(cat /sys/module/usbcore/parameters/quirks)"
echo "    >>> NOW UNPLUG AND REPLUG THE CAMERA so the quirk takes effect, then"
echo "        test a live image in IDS peak Cockpit (or GRControl)."

echo "==> 2/3  Making it permanent in $GRUB"
line=$(grep '^GRUB_CMDLINE_LINUX_DEFAULT=' "$GRUB" || true)
if echo "$line" | grep -q "usbcore.quirks=$QUIRK"; then
    echo "    Already present in GRUB — nothing to change."
else
    cp -a "$GRUB" "$GRUB.bak.$(date +%s 2>/dev/null || echo backup)"
    # Insert the quirk param just before the closing quote of the line.
    sed -i "s#^\(GRUB_CMDLINE_LINUX_DEFAULT=\".*\)\"#\1 usbcore.quirks=$QUIRK\"#" "$GRUB"
    echo "    Updated:"
    grep '^GRUB_CMDLINE_LINUX_DEFAULT=' "$GRUB" | sed 's/^/      /'
    echo "    (backup saved next to $GRUB)"
fi

echo "==> 3/3  Regenerating GRUB config"
if command -v update-grub >/dev/null 2>&1; then
    update-grub
else
    grub-mkconfig -o /boot/grub/grub.cfg
fi

echo
echo "Done. The runtime quirk is active now (after a replug). The GRUB change"
echo "makes it survive reboots. If 'k' alone is not enough, also try adding"
echo "'usbcore.autosuspend=-1' to the same line and re-run update-grub."
