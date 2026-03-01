#!/bin/bash
# check_and_fix_led_permissions.sh
# Checks Wiimote LED brightness permissions and optionally installs the udev fix.

RULE_FILE="/etc/udev/rules.d/99-wiimote-leds.rules"
INPUT_CONF="/etc/bluetooth/input.conf"

# ---- 1. Check for connected Wiimotes ----------------------------------------
# List /sys/class/leds/ directly — this never requires traversing into the LED
# device directories, so it works even when permissions are broken.

led_names=$(ls /sys/class/leds/ 2>/dev/null | grep -E '^0005:057E:03(06|30).*:blue:p0$')

if [ -z "$led_names" ]; then
    echo "No Wiimote LED nodes found in /sys/class/leds/."
    echo "Please ensure a Wiimote is connected via Bluetooth."
    exit 1
fi

echo "Found Wiimote LED devices:"
echo "$led_names"
echo "---"

# ---- 2. Check permissions ---------------------------------------------------

need_fix=false
for name in $led_names; do
    brightness="/sys/class/leds/$name/brightness"
    if [ -w "$brightness" ]; then
        echo "[OK]  writable: $brightness"
    else
        echo "[!!]  not writable: $brightness"
        need_fix=true
    fi
done

# ---- 3. Apply udev fix if needed --------------------------------------------

if [ "$need_fix" = true ]; then
    echo "---"
    echo "Permission fix needed. This will write $RULE_FILE and reload udev."
    read -p "Apply? (y/n): " confirm
    if [[ $confirm == [yY] ]]; then
        sudo bash -c "cat > $RULE_FILE" <<'EOF'
# Wiimote LED permissions — original (0306) and MotionPlus/TR (0330) variants
SUBSYSTEM=="leds", KERNELS=="0005:057E:0306.*", RUN+="/bin/chmod a+x /sys%p", RUN+="/bin/chmod a+w /sys%p/brightness"
SUBSYSTEM=="leds", KERNELS=="0005:057E:0330.*", RUN+="/bin/chmod a+x /sys%p", RUN+="/bin/chmod a+w /sys%p/brightness"
EOF
        sudo udevadm control --reload-rules
        sudo udevadm trigger --subsystem-match=leds --action=add
        echo "Done. Reconnect the Wiimote if the node is still not writable."
    else
        echo "Aborted."
    fi
else
    echo "---"
    echo "LED permissions look good."
fi

# ---- 4. Check BlueZ input.conf (required since BlueZ 5.73) -----------------

echo ""
bluez_ver=$(bluetoothctl --version 2>/dev/null | grep -oP '\d+\.\d+' | head -1)
echo "BlueZ version: ${bluez_ver:-unknown}"

bonded_only=$(grep -i "ClassicBondedOnly" "$INPUT_CONF" 2>/dev/null | grep -i "false")
if [ -n "$bonded_only" ]; then
    echo "[OK]  $INPUT_CONF: ClassicBondedOnly=false"
else
    echo "[!!]  $INPUT_CONF does not have ClassicBondedOnly=false"
    echo "      Since BlueZ 5.73 this setting is required for Wiimotes to connect."
    read -p "      Write the fix now? (y/n): " confirm
    if [[ $confirm == [yY] ]]; then
        if grep -qi "\[General\]" "$INPUT_CONF" 2>/dev/null; then
            sudo sed -i '/\[General\]/a ClassicBondedOnly=false' "$INPUT_CONF"
        else
            sudo bash -c "printf '[General]\nClassicBondedOnly=false\n' >> $INPUT_CONF"
        fi
        sudo systemctl restart bluetooth
        echo "Done."
    else
        echo "Aborted."
    fi
fi
