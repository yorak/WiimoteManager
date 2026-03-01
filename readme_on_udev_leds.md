# Wiimote LED Permissions on Linux

When working with Wiimote LEDs in Linux, users find that writing to the brightness
nodes under `/sys/class/leds/` requires `sudo` by default. This document explains
why common fixes fail and what actually works.

---

## Why standard udev rules fail

LED devices live in the sysfs virtual filesystem (`/sys`), not in `/dev`. This
causes two common approaches to silently do nothing:

**`MODE="0666"` in a udev rule** — `MODE` sets the permissions of a `/dev` device
node. LED class devices have no `/dev` node, so this key is ignored entirely.

**`chmod -R 666 /sys%p`** — This removes the execute (traverse) bit from the LED
device directory. Linux requires execute permission on every directory component
of a path to open a file inside it. Setting a directory to `666` makes it
untraversable, breaking access even though the `brightness` file is now writable.

There is also a **timing race**: BlueZ signals `Connected=True` the moment the
L2CAP connection is established. The kernel HID driver creates the sysfs LED
entries a few milliseconds later. A udev `add` rule that fires on the LED device
appearing (after the driver is done) sidesteps this entirely.

---

## The correct udev rule

Create `/etc/udev/rules.d/99-wiimote-leds.rules`:

```
# Wiimote LED permissions — original (0306) and MotionPlus/TR (0330) variants
SUBSYSTEM=="leds", KERNELS=="0005:057E:0306.*", RUN+="/bin/chmod a+x /sys%p", RUN+="/bin/chmod a+w /sys%p/brightness"
SUBSYSTEM=="leds", KERNELS=="0005:057E:0330.*", RUN+="/bin/chmod a+x /sys%p", RUN+="/bin/chmod a+w /sys%p/brightness"
```

**Why this works:**

- `KERNELS=="0005:057E:03??.* "` matches the HID device parent (Nintendo VID `057E`,
  Wiimote PIDs `0306`/`0330`) without relying on fragile driver name strings.
- `RUN+="/bin/chmod a+x /sys%p"` adds the execute/traverse bit to the LED device
  directory so the path can be entered.
- `RUN+="/bin/chmod a+w /sys%p/brightness"` makes the brightness file writable.
- Two separate `RUN+` lines are used because udev does not run commands through a
  shell, so `&&` is not available.

Apply immediately without rebooting:

```bash
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=leds --action=add
```

---

## BlueZ 5.73+ requirement

Since BlueZ 5.73 the input plugin defaults to `ClassicBondedOnly=true`, which
rejects connections from classic Bluetooth HID devices that are not already
bonded at the kernel level. Wiimotes use a non-standard pairing flow and are
blocked by this default.

Edit `/etc/bluetooth/input.conf` (create it if absent):

```ini
[General]
ClassicBondedOnly=false
```

Then restart the Bluetooth service:

```bash
sudo systemctl restart bluetooth
```

Without this change, Wiimotes will fail to connect on BlueZ 5.73 and later.
