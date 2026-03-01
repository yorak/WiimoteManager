#!/usr/bin/env python3
"""
wiimote_tray.py — GTK3 system tray for Wiimote Bluetooth management.

Features
--------
- Auto-pairs / connects any Wiimote discovered while scanning
  (press 1+2 or the red Sync button to put the Wiimote in pairing mode)
- Tray label shows how many Wiimotes are connected
- Fully non-blocking: all BlueZ calls are async
- Per-device status: Pairing / Connecting / Connected / Disconnected
- Disconnect All action
- Desktop notifications on connect / error

Requirements
------------
    python3-dbus
    gir1.2-gtk-3.0
    gir1.2-appindicator3-0.1
    gir1.2-notify-0.7
    gnome-shell-extension-appindicator  (or equivalent)
"""

from __future__ import annotations

import sys
import time
import logging

import dbus
import dbus.mainloop.glib

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('AppIndicator3', '0.1')
gi.require_version('Notify', '0.7')
from gi.repository import Gtk, GLib, AppIndicator3, Notify

from wiimote_manager import (
    WiimoteManager,
    read_battery,
    ST_IDLE, ST_PAIRING, ST_CONNECTING, ST_CONNECTED, ST_DISCONNECTING,
)

APP_ID = 'wiimote-tray'

log = logging.getLogger(__name__)


def _battery_block(pct: int) -> tuple[str, str]:
    """Return (block_char, pango_color) for the given battery percentage.

    Block characters range from ▏(1/8 full) to █(full), mapped linearly.
    Color: green ≥67%, orange 34–66%, red <34%.
    """
    char = '▏▎▍▌▋▊▉█'[min(pct * 8 // 100, 7)]
    if pct >= 67:
        color = '#44bb44'
    elif pct >= 34:
        color = '#ffaa00'
    else:
        color = '#ee3333'
    return char, color


# ---------------------------------------------------------------------------
# Tray Application
# ---------------------------------------------------------------------------

class WiimoteTrayApp:
    """
    GTK3 AppIndicator3 system tray for Wiimote connection management.

    Menu structure
    --------------
    Scan for Wiimotes  (toggles scan; label changes to "Stop Scanning…")
    Disconnect All     (sensitive only when ≥1 connected)
    ──────────────
    [device items…]    (one per known Wiimote, rebuilt on every state change)
    ──────────────
    Quit
    """

    def __init__(self):
        Notify.init(APP_ID)

        self._bus = dbus.SystemBus()
        self._wm = WiimoteManager(
            self._bus,
            on_state_changed=self._refresh,
            on_notify=self._desktop_notify,
        )

        self._indicator = AppIndicator3.Indicator.new(
            APP_ID,
            'input-gaming',
            AppIndicator3.IndicatorCategory.HARDWARE,
        )
        self._indicator.set_title('Wiimote Tray')
        self._indicator.set_attention_icon_full('bluetooth-active', 'Scanning…')

        self._menu = Gtk.Menu()
        self._item_scan = None
        self._item_disconnect_all = None
        self._sep_top = None
        self._sep_bottom = None
        self._device_items: list[Gtk.MenuItem] = []
        self._scan_start: float = 0.0
        self._scan_label_timer: int | None = None

        self._build_menu_skeleton()
        self._indicator.set_menu(self._menu)
        self._indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self._refresh()

    # ---- Menu skeleton (built once) ----------------------------------------

    def _build_menu_skeleton(self):
        self._item_scan = Gtk.MenuItem(label='Scan for Wiimotes')
        self._item_scan.connect('activate', self._on_scan_clicked)
        self._menu.append(self._item_scan)

        self._item_hint = Gtk.MenuItem(
            label='  Tip: press Sync on all Wiimotes at once'
        )
        self._item_hint.set_sensitive(False)
        self._menu.append(self._item_hint)

        self._item_disconnect_all = Gtk.MenuItem(label='Disconnect All')
        self._item_disconnect_all.connect(
            'activate', lambda _: self._wm.disconnect_all()
        )
        self._menu.append(self._item_disconnect_all)

        self._sep_top = Gtk.SeparatorMenuItem()
        self._menu.append(self._sep_top)

        self._sep_bottom = Gtk.SeparatorMenuItem()
        self._menu.append(self._sep_bottom)

        item_quit = Gtk.MenuItem(label='Quit')
        item_quit.connect('activate', self._on_quit_clicked)
        self._menu.append(item_quit)

        self._menu.show_all()

    # ---- Dynamic device section --------------------------------------------

    def _refresh(self):
        """
        Rebuild the device section and update the tray label/icon.

        Called from WiimoteManager on every state change. Since BlueZ signals
        are dispatched on the GLib main thread (same as GTK), this is safe
        to call without GLib.idle_add().
        """
        for item in self._device_items:
            self._menu.remove(item)
            item.destroy()
        self._device_items.clear()

        devices = self._wm.get_devices()
        n_connected = self._wm.connected_count()
        scanning = self._wm.is_scanning

        if scanning and self._scan_label_timer is None:
            self._scan_start = time.monotonic()
            self._scan_label_timer = GLib.timeout_add(1000, self._tick_scan_label)
        elif not scanning and self._scan_label_timer is not None:
            GLib.source_remove(self._scan_label_timer)
            self._scan_label_timer = None

        if scanning:
            elapsed = int(time.monotonic() - self._scan_start)
            self._item_scan.set_label(f'Stop Scanning  ({elapsed}s)')
        else:
            self._item_scan.set_label('Scan for Wiimotes')

        self._item_hint.set_visible(scanning)
        self._item_disconnect_all.set_sensitive(self._wm.has_connected())

        self._indicator.set_status(
            AppIndicator3.IndicatorStatus.ATTENTION
            if scanning else
            AppIndicator3.IndicatorStatus.ACTIVE
        )

        if n_connected:
            label = f'{n_connected} connected'
        elif scanning:
            label = 'scanning…'
        else:
            label = ''
        self._indicator.set_label(label, '00 connected')

        if not devices:
            item = Gtk.MenuItem(label='No Wiimotes found')
            item.set_sensitive(False)
            self._insert_device_item(item)
        else:
            for path, props, state in devices:
                self._insert_device_item(
                    self._make_device_item(path, props, state)
                )

        self._menu.show_all()

    def _tick_scan_label(self) -> bool:
        elapsed = int(time.monotonic() - self._scan_start)
        self._item_scan.set_label(f'Stop Scanning  ({elapsed}s)')
        return True

    def _insert_device_item(self, item: Gtk.MenuItem):
        pos = self._menu.get_children().index(self._sep_bottom)
        self._menu.insert(item, pos)
        self._device_items.append(item)

    def _make_device_item(self, path: str, props: dict,
                          state: str) -> Gtk.MenuItem:
        name = str(props.get('Name', 'Unknown Wiimote'))
        addr = str(props.get('Address', '??:??:??:??:??:??'))
        short_addr = ':'.join(addr.split(':')[-2:])

        state_str = {
            ST_PAIRING:       '  ⟳ Pairing…',
            ST_CONNECTING:    '  ⟳ Connecting…',
            ST_CONNECTED:     '  ● Connected',
            ST_DISCONNECTING: '  ⟳ Disconnecting…',
            ST_IDLE:          '  ○',
        }.get(state, '')

        mac = str(props.get('Address', ''))
        batt = read_battery(mac)
        plain = GLib.markup_escape_text(f'{name} [{short_addr}]{state_str}')
        if batt is not None:
            char, color = _battery_block(batt)
            markup = f'{plain}  <span foreground="{color}">{char}</span>'
        else:
            markup = plain
        item = Gtk.MenuItem()
        lbl = Gtk.Label(xalign=0.0)
        lbl.set_markup(markup)
        item.add(lbl)

        busy = state in (ST_PAIRING, ST_CONNECTING, ST_DISCONNECTING)
        item.set_sensitive(not busy)

        if not busy:
            if state == ST_CONNECTED:
                item.connect(
                    'activate', lambda _, p=path: self._wm.disconnect(p)
                )
            else:
                item.connect(
                    'activate', lambda _, p=path: self._wm.pair_trust_connect(p)
                )
        return item

    # ---- Event handlers ----------------------------------------------------

    def _on_scan_clicked(self, _item):
        if self._wm.is_scanning:
            self._wm.stop_scan()
        else:
            try:
                self._wm.start_scan()
            except dbus.DBusException as e:
                self._desktop_notify(
                    f'Scan failed: {e.get_dbus_message()}', 'dialog-error'
                )

    def _on_quit_clicked(self, _item):
        self._wm.shutdown()
        Notify.uninit()
        Gtk.main_quit()

    # ---- Desktop notifications ---------------------------------------------

    def _desktop_notify(self, message: str, icon: str = 'input-gaming'):
        urgency = (Notify.Urgency.CRITICAL if icon == 'dialog-error'
                   else Notify.Urgency.NORMAL)
        n = Notify.Notification.new('Wiimote Tray', message, icon)
        n.set_urgency(urgency)
        n.set_timeout(4000)
        try:
            n.show()
        except Exception as e:
            log.debug('Notify.show() failed: %s', e)
        log.info('Notification: %s', message)

    # ---- Main loop ---------------------------------------------------------

    def run(self):
        Gtk.main()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    )

    # MUST set up the GLib mainloop as DBus default before any SystemBus() call.
    # This routes all DBus signal callbacks through the GLib/GTK event loop,
    # keeping everything on one thread with no locking needed.
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

    try:
        app = WiimoteTrayApp()
        app.run()
    except RuntimeError as e:
        log.critical('%s', e)
        sys.exit(1)


if __name__ == '__main__':
    main()
