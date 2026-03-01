#!/usr/bin/env python3
"""
wiimote_manager.py — Wiimote Bluetooth management.

Handles device discovery, pairing, connection, player-LED assignment,
and battery reading via BlueZ DBus and Linux sysfs.

Can be imported as a module or used standalone (see WiimoteManager).
"""

from __future__ import annotations

import logging
import pathlib

import dbus
import dbus.service
import gi
from gi.repository import GLib

# ---------------------------------------------------------------------------
# BlueZ / DBus constants
# ---------------------------------------------------------------------------

BLUEZ_SERVICE       = 'org.bluez'
ADAPTER_IFACE       = 'org.bluez.Adapter1'
DEVICE_IFACE        = 'org.bluez.Device1'
AGENT_MANAGER_IFACE = 'org.bluez.AgentManager1'
AGENT_IFACE         = 'org.bluez.Agent1'
PROPS_IFACE         = 'org.freedesktop.DBus.Properties'
OM_IFACE            = 'org.freedesktop.DBus.ObjectManager'

_AGENT_PATH       = '/org/wiimote_manager/agent'
_AGENT_CAPABILITY = 'NoInputNoOutput'

WIIMOTE_NAME_PREFIXES = (
    'Nintendo RVL-CNT',    # RVL-CNT-01 (Gen1), RVL-CNT-01-TR (Gen2)
    'Nintendo Wii Remote', # older kernel name
)

# Per-device operation states
ST_IDLE          = 'idle'
ST_PAIRING       = 'pairing'
ST_CONNECTING    = 'connecting'
ST_CONNECTED     = 'connected'
ST_DISCONNECTING = 'disconnecting'

# LED patterns indexed by 0-based player number.
# Bit 0 = p0 = LED1 (leftmost), bit 3 = p3 = LED4 (rightmost).
# Visual layout shown as LED1 LED2 LED3 LED4:
#   players 1-4:  single LEDs        (1000, 0100, 0010, 0001)
#   players 5-7:  adjacent pairs     (1100, 0110, 0011)
#   players 8-9:  triplets           (1110, 0111)
#   player 10:    all four           (1111)
# Player 11+ gets the all-on pattern (no more distinct combinations).
PLAYER_LED_PATTERNS = (
    0b0001,  # 1  — LED 1
    0b0010,  # 2  — LED 2
    0b0100,  # 3  — LED 3
    0b1000,  # 4  — LED 4
    0b0011,  # 5  — LEDs 1+2
    0b0110,  # 6  — LEDs 2+3
    0b1100,  # 7  — LEDs 3+4
    0b0111,  # 8  — LEDs 1+2+3
    0b1110,  # 9  — LEDs 2+3+4
    0b1111,  # 10 — all four
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# sysfs LED / battery helpers
# ---------------------------------------------------------------------------

def led_brightness_paths(mac: str) -> list[str]:
    """Return list of up to 4 brightness file paths for the Wiimote with given MAC."""
    ps = pathlib.Path(f'/sys/class/power_supply/wiimote_battery_{mac.lower()}')
    if not ps.exists():
        return []
    hid_dir = ps.resolve().parent.parent
    return sorted(str(p / 'brightness') for p in hid_dir.glob('leds/*:blue:p?'))


def set_leds(mac: str, pattern: int) -> bool:
    """
    Set LEDs for a Wiimote. pattern is a bitmask: bit 0 = LED1 … bit 3 = LED4.
    E.g. pattern=0b0001 -> LED1 on only (player 1).
         pattern=0      -> all off.
    Returns True if at least one brightness file was written successfully.
    """
    wrote = False
    for i, path in enumerate(led_brightness_paths(mac)):
        try:
            pathlib.Path(path).write_text('1' if (pattern >> i) & 1 else '0')
            wrote = True
        except OSError:
            pass
    return wrote


def read_battery(mac: str) -> int | None:
    """Return battery % (0-100) or None if unavailable."""
    ps = pathlib.Path(f'/sys/class/power_supply/wiimote_battery_{mac.lower()}')
    try:
        return int((ps.resolve() / 'capacity').read_text().strip())
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Pairing agent (internal)
# ---------------------------------------------------------------------------

class _WiimoteAgent(dbus.service.Object):
    """
    BlueZ pairing agent implementing org.bluez.Agent1.

    Registered with capability 'NoInputNoOutput'. The hid-wiimote kernel
    driver handles the HID-level authentication internally, so this agent
    only needs to auto-approve BlueZ authorization requests.
    """

    def __init__(self, bus):
        super().__init__(bus, _AGENT_PATH)

    @dbus.service.method(AGENT_IFACE, in_signature='o', out_signature='s')
    def RequestPinCode(self, device):
        log.info('RequestPinCode for %s (returning empty)', device)
        return ''

    @dbus.service.method(AGENT_IFACE, in_signature='ou', out_signature='')
    def RequestConfirmation(self, device, passkey):
        log.info('RequestConfirmation %s passkey=%d (auto-confirm)', device, passkey)

    @dbus.service.method(AGENT_IFACE, in_signature='o', out_signature='')
    def RequestAuthorization(self, device):
        log.info('RequestAuthorization %s (auto-approve)', device)

    @dbus.service.method(AGENT_IFACE, in_signature='os', out_signature='')
    def AuthorizeService(self, device, uuid):
        log.info('AuthorizeService %s %s (auto-approve)', device, uuid)

    @dbus.service.method(AGENT_IFACE, in_signature='o', out_signature='u')
    def RequestPasskey(self, device):
        log.warning('RequestPasskey from %s (unexpected, returning 0)', device)
        return dbus.UInt32(0)

    @dbus.service.method(AGENT_IFACE, in_signature='ouq', out_signature='')
    def DisplayPasskey(self, device, passkey, entered):
        pass

    @dbus.service.method(AGENT_IFACE, in_signature='os', out_signature='')
    def DisplayPinCode(self, device, pincode):
        pass

    @dbus.service.method(AGENT_IFACE, in_signature='', out_signature='')
    def Release(self):
        log.info('Agent released by BlueZ')

    @dbus.service.method(AGENT_IFACE, in_signature='', out_signature='')
    def Cancel(self):
        log.info('Agent: pairing cancelled')


# ---------------------------------------------------------------------------
# WiimoteManager
# ---------------------------------------------------------------------------

class WiimoteManager:
    """
    Manages Wiimote pairing, connection, player-LED assignment and battery.

    Parameters
    ----------
    bus : dbus.SystemBus
        A DBus system bus with the GLib main loop set as default
        (call dbus.mainloop.glib.DBusGMainLoop(set_as_default=True) first).
    on_state_changed : callable()
        Called on the GLib main thread whenever the device list or scan
        state changes so a UI layer can refresh itself.
    on_notify : callable(message: str, icon: str)
        Called for user-visible events (connect, disconnect, errors).

    Public API
    ----------
    start_scan() / stop_scan() / is_scanning
    get_devices() -> list of (path, props_dict, state)
    connected_count() / has_connected()
    get_adapter_address()
    pair_trust_connect(path) / disconnect(path) / disconnect_all()
    shutdown()
    """

    def __init__(self, bus, on_state_changed, on_notify):
        self._bus = bus
        self._on_state_changed = on_state_changed
        self._on_notify = on_notify

        self._adapter_path: str | None = None
        self._adapter_address: str | None = None
        self._agent: _WiimoteAgent | None = None

        self._scanning = False

        # path -> Device1 property dict (local cache, updated from signals)
        self._devices: dict[str, dict] = {}
        # path -> ST_* state
        self._op_state: dict[str, str] = {}
        # connection-order list for player-number LED assignment
        self._player_order: list[str] = []

        self._find_adapter()
        self._load_known_devices()
        self._register_agent()
        self._subscribe_signals()

    # ---- LED assignment ----------------------------------------------------

    def _assign_player_leds(self, path: str, mac: str, retries: int = 6):
        """
        Assign the next player-LED pattern to this device.

        Called with retries>0 on connect events so that we survive the race
        between BlueZ's Connected=True signal and the kernel HID driver
        finishing its sysfs setup (power_supply + leds entries).
        """
        if path not in self._player_order:
            self._player_order.append(path)
        player_num = self._player_order.index(path)
        pattern = PLAYER_LED_PATTERNS[min(player_num, len(PLAYER_LED_PATTERNS) - 1)]
        log.debug('%s -> player %d, LED pattern 0b%04b', mac, player_num + 1, pattern)
        if set_leds(mac, pattern):
            log.debug('LEDs set for %s', mac)
        elif retries > 0:
            log.debug('LED write failed for %s, retrying (%d left)', mac, retries)
            GLib.timeout_add(500, self._assign_player_leds, path, mac, retries - 1)

    # ---- Adapter -----------------------------------------------------------

    def _find_adapter(self):
        obj = self._bus.get_object(BLUEZ_SERVICE, '/')
        om = dbus.Interface(obj, OM_IFACE)
        for path, ifaces in om.GetManagedObjects().items():
            if ADAPTER_IFACE in ifaces:
                self._adapter_path = str(path)
                self._adapter_address = str(ifaces[ADAPTER_IFACE]['Address'])
                log.info('Adapter: %s  MAC: %s', self._adapter_path,
                         self._adapter_address)
                return
        raise RuntimeError('No Bluetooth adapter found via BlueZ DBus')

    def get_adapter_address(self) -> str:
        return self._adapter_address

    # ---- Known device loading ----------------------------------------------

    def _load_known_devices(self):
        obj = self._bus.get_object(BLUEZ_SERVICE, '/')
        om = dbus.Interface(obj, OM_IFACE)
        for path, ifaces in om.GetManagedObjects().items():
            if DEVICE_IFACE in ifaces:
                props = dict(ifaces[DEVICE_IFACE])
                if self._is_wiimote(props):
                    path = str(path)
                    self._devices[path] = props
                    connected = bool(props.get('Connected', False))
                    self._op_state[path] = ST_CONNECTED if connected else ST_IDLE
                    log.debug('Loaded device: %s  connected=%s',
                              props.get('Name'), connected)
                    if connected:
                        mac = str(props.get('Address', ''))
                        self._assign_player_leds(path, mac)  # sysfs ready at startup

    # ---- Pairing agent -----------------------------------------------------

    def _register_agent(self):
        self._agent = _WiimoteAgent(self._bus)
        mgr = dbus.Interface(
            self._bus.get_object(BLUEZ_SERVICE, '/org/bluez'),
            AGENT_MANAGER_IFACE,
        )
        mgr.RegisterAgent(_AGENT_PATH, _AGENT_CAPABILITY)
        mgr.RequestDefaultAgent(_AGENT_PATH)
        log.info('Pairing agent registered at %s', _AGENT_PATH)

    def _unregister_agent(self):
        if not self._agent:
            return
        try:
            mgr = dbus.Interface(
                self._bus.get_object(BLUEZ_SERVICE, '/org/bluez'),
                AGENT_MANAGER_IFACE,
            )
            mgr.UnregisterAgent(_AGENT_PATH)
        except dbus.DBusException as e:
            log.debug('UnregisterAgent: %s', e)

    # ---- DBus signal subscriptions -----------------------------------------

    def _subscribe_signals(self):
        self._bus.add_signal_receiver(
            self._on_ifaces_added,
            dbus_interface=OM_IFACE,
            signal_name='InterfacesAdded',
        )
        self._bus.add_signal_receiver(
            self._on_ifaces_removed,
            dbus_interface=OM_IFACE,
            signal_name='InterfacesRemoved',
        )
        self._bus.add_signal_receiver(
            self._on_props_changed,
            dbus_interface=PROPS_IFACE,
            signal_name='PropertiesChanged',
            path_keyword='path',
            arg0=DEVICE_IFACE,
        )

    def _on_ifaces_added(self, path, interfaces):
        path = str(path)
        if DEVICE_IFACE not in interfaces:
            return
        props = dict(interfaces[DEVICE_IFACE])
        if not self._is_wiimote(props):
            return

        is_new = path not in self._devices
        self._devices[path] = props
        name = str(props.get('Name', path))

        if is_new and self._scanning:
            if bool(props.get('Connected', False)):
                self._op_state[path] = ST_CONNECTED
                self._assign_player_leds(path, str(props.get('Address', '')), retries=6)
            elif bool(props.get('Paired', False)):
                log.info('Auto-connecting known Wiimote: %s', name)
                self._op_state[path] = ST_CONNECTING
                self._on_state_changed()
                self._do_pair_trust_connect(path)
                return
            else:
                log.info('Auto-pairing new Wiimote: %s', name)
                self._op_state[path] = ST_PAIRING
                self._on_state_changed()
                self._do_pair_trust_connect(path)
                return
        elif is_new:
            connected = bool(props.get('Connected', False))
            self._op_state[path] = ST_CONNECTED if connected else ST_IDLE
            if connected:
                self._assign_player_leds(path, str(props.get('Address', '')), retries=6)

        self._on_state_changed()

    def _on_ifaces_removed(self, path, interfaces):
        path = str(path)
        if DEVICE_IFACE in interfaces and path in self._devices:
            log.info('Device removed: %s', self._devices[path].get('Name', path))
            del self._devices[path]
            self._op_state.pop(path, None)
            self._on_state_changed()

    _RELEVANT_PROPS = frozenset({'Connected', 'Paired', 'Trusted', 'Name'})

    def _on_props_changed(self, iface, changed, invalidated, path=None):
        path = str(path)
        if path not in self._devices:
            return
        for k, v in changed.items():
            self._devices[path][k] = v

        if 'Connected' in changed:
            connected = bool(changed['Connected'])
            cur_state = self._op_state.get(path)
            mac = str(self._devices[path].get('Address', ''))
            if connected:
                self._op_state[path] = ST_CONNECTED
                name = str(self._devices[path].get('Name', 'Wiimote'))
                self._on_notify(f'{name} connected', 'input-gaming')
                self._assign_player_leds(path, mac, retries=6)  # with retry for timing
            else:
                if cur_state == ST_CONNECTED:
                    name = str(self._devices[path].get('Name', 'Wiimote'))
                    self._on_notify(f'{name} disconnected', 'bluetooth')
                set_leds(mac, 0)
                if path in self._player_order:
                    self._player_order.remove(path)
                self._op_state[path] = ST_IDLE

        if self._RELEVANT_PROPS & changed.keys():
            self._on_state_changed()

    # ---- Discovery ---------------------------------------------------------

    def start_scan(self):
        if self._scanning:
            return
        adapter = self._adapter_iface()
        # BR/EDR only; no UUID filter — some Wiimotes don't advertise their
        # HID UUID until after SDP query, so filtering by UUID would miss them.
        adapter.SetDiscoveryFilter(dbus.Dictionary(
            {'Transport': dbus.String('bredr')},
            signature='sv',
        ))
        adapter.StartDiscovery()
        self._scanning = True
        log.info('Scan started')
        self._on_state_changed()

    def stop_scan(self):
        if not self._scanning:
            return
        self._scanning = False
        try:
            self._adapter_iface().StopDiscovery()
        except dbus.DBusException as e:
            if 'No discovery' not in str(e):
                log.warning('StopDiscovery: %s', e)
        log.info('Scan stopped')
        self._on_state_changed()

    @property
    def is_scanning(self) -> bool:
        return self._scanning

    # ---- Device queries ----------------------------------------------------

    @staticmethod
    def _is_wiimote(props: dict) -> bool:
        name = str(props.get('Name', ''))
        return any(name.startswith(p) for p in WIIMOTE_NAME_PREFIXES)

    def get_devices(self) -> list[tuple[str, dict, str]]:
        """Return list of (path, props, state) sorted by name then address."""
        items = [
            (p, self._devices[p], self._op_state.get(p, ST_IDLE))
            for p in self._devices
        ]
        items.sort(key=lambda x: (str(x[1].get('Name', '')),
                                  str(x[1].get('Address', ''))))
        return items

    def connected_count(self) -> int:
        return sum(1 for p in self._devices if self._devices[p].get('Connected'))

    def has_connected(self) -> bool:
        return self.connected_count() > 0

    # ---- DBus helpers ------------------------------------------------------

    def _adapter_iface(self):
        obj = self._bus.get_object(BLUEZ_SERVICE, self._adapter_path)
        return dbus.Interface(obj, ADAPTER_IFACE)

    def _device_iface(self, path: str):
        obj = self._bus.get_object(BLUEZ_SERVICE, path)
        return dbus.Interface(obj, DEVICE_IFACE)

    def _props_iface(self, path: str):
        obj = self._bus.get_object(BLUEZ_SERVICE, path)
        return dbus.Interface(obj, PROPS_IFACE)

    # ---- Pair / connect / disconnect ---------------------------------------

    def pair_trust_connect(self, path: str):
        """Initiate connect (pairing first if needed)."""
        props = self._devices.get(path, {})
        self._op_state[path] = ST_CONNECTING if props.get('Paired') else ST_PAIRING
        self._on_state_changed()
        self._do_pair_trust_connect(path)

    def _do_pair_trust_connect(self, path: str):
        props = self._devices.get(path, {})
        dev = self._device_iface(path)

        def _connect():
            self._op_state[path] = ST_CONNECTING
            self._on_state_changed()

            def _on_connect_ok():
                if self._op_state.get(path) != ST_CONNECTED:
                    self._op_state[path] = ST_CONNECTED
                    self._on_state_changed()

            dev.Connect(reply_handler=_on_connect_ok, error_handler=_on_error)

        def _on_error(e):
            ename = getattr(e, 'get_dbus_name', lambda: '')()
            if ename == 'org.bluez.Error.AlreadyConnected':
                self._op_state[path] = ST_CONNECTED
                self._on_state_changed()
                return
            name = str(self._devices.get(path, {}).get('Name', 'Wiimote'))
            msg = getattr(e, 'get_dbus_message', lambda: str(e))()
            log.error('Error on %s: %s', path, e)
            self._op_state[path] = ST_IDLE
            self._on_state_changed()
            self._on_notify(f'{name}: {msg}', 'dialog-error')

        def _on_paired():
            try:
                self._props_iface(path).Set(
                    DEVICE_IFACE, 'Trusted', dbus.Boolean(True)
                )
            except dbus.DBusException as e:
                log.warning('Set Trusted: %s', e)
            _connect()

        def _on_pair_error(e):
            ename = getattr(e, 'get_dbus_name', lambda: '')()
            if ename == 'org.bluez.Error.AlreadyExists':
                _connect()
            else:
                _on_error(e)

        if props.get('Paired'):
            if not props.get('Trusted'):
                try:
                    self._props_iface(path).Set(
                        DEVICE_IFACE, 'Trusted', dbus.Boolean(True)
                    )
                except dbus.DBusException:
                    pass
            _connect()
        else:
            dev.Pair(reply_handler=_on_paired, error_handler=_on_pair_error)

    def disconnect(self, path: str):
        """Async disconnect."""
        self._op_state[path] = ST_DISCONNECTING
        self._on_state_changed()
        dev = self._device_iface(path)

        def _on_error(e):
            ename = getattr(e, 'get_dbus_name', lambda: '')()
            if ename != 'org.bluez.Error.NotConnected':
                log.error('Disconnect error: %s', e)
            self._op_state[path] = ST_IDLE
            self._on_state_changed()

        def _on_disconnect_ok():
            if self._op_state.get(path) != ST_IDLE:
                self._op_state[path] = ST_IDLE
                self._on_state_changed()

        dev.Disconnect(reply_handler=_on_disconnect_ok, error_handler=_on_error)

    def disconnect_all(self):
        for path, props in list(self._devices.items()):
            if props.get('Connected'):
                self.disconnect(path)

    # ---- Cleanup -----------------------------------------------------------

    def shutdown(self):
        self.stop_scan()
        self._unregister_agent()
