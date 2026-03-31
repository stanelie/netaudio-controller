#!/usr/bin/env python3
"""Dante Patch Bay — PyQt6 GUI for managing Dante audio subscriptions."""

import sys
import asyncio
import time
import logging

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QCheckBox, QLabel, QLineEdit, QStatusBar,
    QAbstractItemView, QStyledItemDelegate,
    QTableWidget, QTableWidgetItem, QTabWidget,
)
from PyQt6.QtCore import Qt, QRect, QThread, pyqtSignal, QSize, QTimer
from PyQt6.QtGui import QColor, QFont, QBrush, QPainter, QPen

from netaudio.daemon.client import get_devices_from_daemon, device_request_via_daemon
from netaudio.dante.application import DanteApplication
from netaudio.dante.device_commands import DanteDeviceCommands
from netaudio.dante.const import SUBSCRIPTION_STATUS_INFO
from netaudio.dante.events import EventType
from netaudio.dante.services.notification import (
    NOTIFICATION_TX_CHANNEL_CHANGE,
    NOTIFICATION_RX_CHANNEL_CHANGE,
    NOTIFICATION_TX_FLOW_CHANGE,
    NOTIFICATION_RX_FLOW_CHANGE,
    NOTIFICATION_ROUTING_DEVICE_CHANGE,
    NOTIFICATION_ROUTING_READY,
)
from netaudio.common.app_config import settings as netaudio_settings

log = logging.getLogger("patchbay")

# Notification IDs that signal a routing/subscription change on a device.
_ROUTING_NOTIFICATION_IDS = {
    NOTIFICATION_TX_CHANNEL_CHANGE,    # 257 — TX channel assigned/changed
    NOTIFICATION_RX_CHANNEL_CHANGE,    # 258 — RX subscription changed
    NOTIFICATION_TX_FLOW_CHANGE,       # 260 — TX flow changed
    NOTIFICATION_RX_FLOW_CHANGE,       # 261 — RX flow changed
    NOTIFICATION_ROUTING_DEVICE_CHANGE,# 288 — routing-level device change
    NOTIFICATION_ROUTING_READY,        # 256 — routing table ready (e.g. after reboot)
}

# ── Palette ────────────────────────────────────────────────────────────────────
C_TX_HDR    = QColor(40,  80, 130)   # TX device header (dark blue)
C_RX_HDR    = QColor(90,  40, 110)   # RX device header (dark purple)
C_TX_CH     = QColor(205, 220, 245)  # TX channel label
C_RX_CH     = QColor(230, 210, 242)  # RX channel label
C_HDR_FG    = QColor(255, 255, 255)  # white text on coloured headers
C_CH_FG     = QColor(25,  25,  25)   # near-black text on channel label cells
C_CONN      = QColor(50,  175,  70)  # connected dot (green)
C_ERROR     = QColor(200,  50,  50)  # error dot (red — connected but faulted)
C_EMPTY_DOT = QColor(195, 195, 195)  # disconnected dot (grey)
C_EMPTY_BG  = QColor(252, 252, 252)  # connection cell background
C_GRAY_CELL = QColor(218, 220, 224)  # separator cell (device×device, etc.)
C_PENDING   = QColor(220, 175,  45)  # pending dot (amber)
C_CORNER    = QColor(50,  50,   55)  # top-left corner


# ── Helpers ────────────────────────────────────────────────────────────────────
def _arc_port(device) -> int:
    from netaudio.dante.const import SERVICE_ARC
    if device.services:
        for svc in device.services.values():
            if svc.get("type") == SERVICE_ARC:
                return svc.get("port", 4440)
    return 4440


def _bold_font(size: int | None = None) -> QFont:
    f = QFont()
    f.setBold(True)
    if size:
        f.setPointSize(size)
    return f


def _small_font() -> QFont:
    f = QFont()
    f.setPointSize(8)
    return f


# ── Delegate: draws dots in connection cells ───────────────────────────────────
class DotDelegate(QStyledItemDelegate):
    def paint(self, painter: QPainter, option, index):
        # ── Rotated TX header row ──────────────────────────────────────────────
        if index.row() == 0 and index.column() > 0:
            painter.save()
            r = option.rect
            bg = index.data(Qt.ItemDataRole.BackgroundRole)
            if bg:
                painter.fillRect(r, bg)
            painter.setClipRect(r)
            painter.translate(r.center().x(), r.center().y())
            painter.rotate(-90)
            # In rotated frame the cell is (original-height wide × original-width tall)
            rotated = QRect(-r.height() // 2, -r.width() // 2, r.height(), r.width())
            fg = index.data(Qt.ItemDataRole.ForegroundRole)
            painter.setPen(fg.color() if fg else option.palette.text().color())
            font = index.data(Qt.ItemDataRole.FontRole)
            if font:
                painter.setFont(font)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            painter.drawText(
                rotated,
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextSingleLine,
                index.data(Qt.ItemDataRole.DisplayRole) or "",
            )
            painter.restore()
            return

        data = index.data(Qt.ItemDataRole.UserRole)
        if isinstance(data, dict) and data.get("kind") == "rx_ch":
            super().paint(painter, option, index)
            status = data.get("status")
            if status is not None:
                painter.save()
                r = option.rect
                size = min(r.width(), r.height()) * 0.28
                margin = 4
                cx = r.right() - margin - size
                cy = r.y() + r.height() / 2
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                if status == "ok":
                    painter.setBrush(QBrush(C_CONN))
                    painter.setPen(QPen(QColor(30, 130, 45), 1.5))
                else:
                    painter.setBrush(QBrush(C_ERROR))
                    painter.setPen(QPen(QColor(150, 20, 20), 1.5))
                painter.drawEllipse(
                    int(cx - size), int(cy - size),
                    int(size * 2), int(size * 2),
                )
                painter.restore()
            return

        if not (isinstance(data, dict) and data.get("kind") == "conn"):
            super().paint(painter, option, index)
            return

        painter.save()
        painter.fillRect(option.rect, QBrush(C_EMPTY_BG))

        pending   = data.get("pending", False)
        connected = data.get("connected", False)
        r = option.rect
        size = min(r.width(), r.height()) * 0.33
        cx = r.x() + r.width()  / 2
        cy = r.y() + r.height() / 2

        error     = data.get("error", False)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if pending:
            painter.setBrush(QBrush(C_PENDING))
            painter.setPen(QPen(QColor(170, 130, 20), 1.5))
        elif error:
            painter.setBrush(QBrush(C_ERROR))
            painter.setPen(QPen(QColor(150, 20, 20), 1.5))
        elif connected:
            painter.setBrush(QBrush(C_CONN))
            painter.setPen(QPen(QColor(30, 130, 45), 1.5))
        else:
            painter.setBrush(QBrush(C_EMPTY_DOT))
            painter.setPen(QPen(QColor(155, 155, 155), 1.0))

        painter.drawEllipse(int(cx - size), int(cy - size),
                            int(size * 2),  int(size * 2))
        painter.restore()

    def sizeHint(self, option, index):
        return QSize(32, 26)


# ── Persistent async engine ────────────────────────────────────────────────────
# A single DanteApplication is kept alive for the lifetime of the GUI process.
# It holds the multicast notification listener, so external routing changes
# (e.g. from Dante Controller on another machine) are received immediately
# rather than being discovered only on the next 5-second poll.
#
# All async work runs on a dedicated event loop in its own thread so that
# asyncio never blocks the Qt main thread.
# ──────────────────────────────────────────────────────────────────────────────
import threading

class _AsyncEngine:
    """Owns a persistent DanteApplication and a background asyncio event loop."""

    def __init__(self):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._app: DanteApplication | None = None
        self._ready = threading.Event()   # set once the loop + app are up

    # ── Lifecycle ──────────────────────────────────────────────────────────────
    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="dante-async")
        self._thread.start()
        self._ready.wait(timeout=10)

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._main())

    async def _main(self):
        self._app = DanteApplication()
        await self._app.startup()
        self._ready.set()
        # Keep the loop alive indefinitely; tasks submitted via submit() run here.
        await asyncio.get_event_loop().create_future()   # never resolves

    def stop(self):
        if self._loop and self._app:
            asyncio.run_coroutine_threadsafe(self._app.shutdown(), self._loop)

    # ── Submit a coroutine from any thread ────────────────────────────────────
    def submit(self, coro) -> "asyncio.Future":
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    @property
    def app(self) -> DanteApplication:
        return self._app


# Module-level singleton — created once when the GUI starts.
_engine = _AsyncEngine()


# ── Discovery worker ───────────────────────────────────────────────────────────
class DiscoveryWorker(QThread):
    """Full device discovery + channel population via the shared async engine.

    If *force_full* is True the daemon cache is bypassed and
    discover_and_populate() is always called (full ARC re-query of every
    device on the network).  Use this for the manual Refresh button and the
    periodic deep-sync timer.
    """
    done   = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, force_full: bool = False):
        super().__init__()
        self._force_full = force_full

    def run(self):
        future = _engine.submit(self._discover())
        try:
            self.done.emit(future.result(timeout=30))
        except Exception as exc:
            self.failed.emit(str(exc))

    async def _discover(self):
        app = _engine.app
        devices = None if self._force_full else await get_devices_from_daemon()
        source = "daemon-cache"
        if devices is None:
            source = "ARC full-query"
            devices = await app.discover_and_populate(
                timeout=netaudio_settings.mdns_timeout
            )
        else:
            # Register daemon devices in app.devices so _device_by_ip can
            # find them when routing notifications arrive.  We intentionally
            # do NOT call populate_controls here: doing so on every 5-second
            # refresh would race with a running DeviceRefreshWorker on the
            # same UDP socket (both use transaction_id=0 for command_receivers,
            # corrupting each other's asyncio.Future in _pending).
            for sn, d in devices.items():
                if sn not in app.devices:
                    app.devices[sn] = d
                else:
                    # Merge fresh subscription and channel data from the daemon
                    # into the existing device object.  This is safe (no UDP
                    # traffic) and ensures crosspoint changes made externally
                    # (e.g. via Dante Controller) are reflected after a device
                    # restart, when the old app.devices entry would otherwise
                    # stay stale indefinitely.
                    existing = app.devices[sn]
                    existing.subscriptions = d.subscriptions
                    if d.rx_channels:
                        existing.rx_channels = d.rx_channels
                    if d.tx_channels:
                        existing.tx_channels = d.tx_channels
            # Return app.devices so sync() also sees any ARC-fresh data
            # already written by a running DeviceRefreshWorker.
            devices = app.devices

        devices = devices or {}
        log.debug("[discover] source=%s  devices=%d", source, len(devices))
        for sn, d in devices.items():
            tx_chs = list(d.tx_channels.keys()) if d.tx_channels else []
            rx_chs = list(d.rx_channels.keys()) if d.rx_channels else []
            log.debug(
                "  device sn=%s name=%r ip=%s  tx_channels=%s  rx_channels=%s  subscriptions=%d",
                sn, d.name, getattr(d, 'ipv4', '?'),
                tx_chs, rx_chs, len(d.subscriptions),
            )
            for sub in d.subscriptions:
                log.debug(
                    "    sub: rx_dev=%r rx_ch=%r  tx_dev=%r tx_ch=%r  "
                    "status=0x%04x rx_ch_status=0x%04x",
                    sub.rx_device_name, sub.rx_channel_name,
                    sub.tx_device_name, sub.tx_channel_name,
                    sub.status_code or 0,
                    sub.rx_channel_status_code or 0,
                )
        return devices


# ── Single-device refresh worker ──────────────────────────────────────────────
class DeviceRefreshWorker(QThread):
    """Re-queries rx/tx channels for ONE device after a notification fires."""
    done = pyqtSignal(dict)   # emits the full (updated) devices dict

    def __init__(self, server_name: str):
        super().__init__()
        self._server_name = server_name

    def run(self):
        future = _engine.submit(self._refresh())
        try:
            self.done.emit(future.result(timeout=10))
        except Exception:
            pass   # silent — the 5 s fallback will catch it

    async def _refresh(self):
        # Dante devices broadcast the notification *before* their ARC state is
        # committed.  A short pause lets the device finish writing so the ARC
        # response is fresh.
        await asyncio.sleep(0.35)
        app = _engine.app
        device = app.devices.get(self._server_name)
        if device is None:
            log.debug("[device-refresh] server_name=%r not in app.devices — skipping", self._server_name)
            return app.devices
        arc_port = app.get_arc_port(device)
        log.debug("[device-refresh] refreshing %r  ip=%s  arc_port=%s", self._server_name, getattr(device, 'ipv4', '?'), arc_port)
        if arc_port:
            try:
                await app.arc.get_controls(device, arc_port)
                log.debug("[device-refresh] get_controls OK for %r  subscriptions=%d", self._server_name, len(device.subscriptions))
                for sub in device.subscriptions:
                    log.debug(
                        "  sub: rx_dev=%r rx_ch=%r  tx_dev=%r tx_ch=%r  "
                        "status=0x%04x rx_ch_status=0x%04x",
                        sub.rx_device_name, sub.rx_channel_name,
                        sub.tx_device_name, sub.tx_channel_name,
                        sub.status_code or 0,
                        sub.rx_channel_status_code or 0,
                    )
            except Exception as exc:
                log.warning("[device-refresh] get_controls FAILED for %r: %s", self._server_name, exc)
        else:
            log.warning("[device-refresh] no ARC port for %r — cannot refresh", self._server_name)
        return app.devices


# ── Subscription command worker ────────────────────────────────────────────────
class CmdWorker(QThread):
    done = pyqtSignal(bool, str)

    def __init__(self, action: str, rx_dev, rx_ch, tx_dev=None, tx_ch=None):
        super().__init__()
        self._action = action   # "add" | "remove"
        self._rx_dev = rx_dev
        self._rx_ch  = rx_ch
        self._tx_dev = tx_dev
        self._tx_ch  = tx_ch

    def run(self):
        future = _engine.submit(self._send())
        try:
            future.result(timeout=10)
            self.done.emit(True, "")
        except Exception as exc:
            self.done.emit(False, str(exc))

    async def _send(self):
        cmds = DanteDeviceCommands()
        port = _arc_port(self._rx_dev)
        ip   = str(self._rx_dev.ipv4)

        if self._action == "add":
            tx_name = self._tx_ch.friendly_name or self._tx_ch.name
            packet, _ = cmds.command_add_subscription(
                self._rx_ch.number, tx_name, self._tx_dev.name
            )
        else:
            packet, _ = cmds.command_remove_subscription(self._rx_ch.number)

        result = await device_request_via_daemon(packet, ip, port)
        if result is None:
            await _engine.app.arc.request(
                packet, ip, port, logical_command_name="patch_bay"
            )


# ── Clock leader command worker ────────────────────────────────────────────────
class ClockSetLeaderWorker(QThread):
    """Sends a preferred-master (clock leader) command to a single device."""
    done = pyqtSignal(bool, str)

    def __init__(self, device, value: bool):
        super().__init__()
        self._device = device
        self._value  = value

    def run(self):
        future = _engine.submit(self._send())
        try:
            future.result(timeout=10)
            self.done.emit(True, "")
        except Exception as exc:
            self.done.emit(False, str(exc))

    async def _send(self):
        cmds = DanteDeviceCommands()
        ip   = str(self._device.ipv4)

        packet, _, port = cmds.command_set_preferred_leader(self._value)
        result = await device_request_via_daemon(packet, ip, port)
        if result is None:
            await _engine.app.settings.request(
                packet, ip, port, logical_command_name="clock_leader"
            )


# ── Channel rename worker ─────────────────────────────────────────────────────
class ChannelRenameWorker(QThread):
    """Sends a set- or reset-channel-name command to a device."""
    done = pyqtSignal(bool, str)

    def __init__(self, device, channel_type: str, channel_number: int, new_name: str):
        super().__init__()
        self._device         = device
        self._channel_type   = channel_type
        self._channel_number = channel_number
        self._new_name       = new_name

    def run(self):
        future = _engine.submit(self._rename())
        try:
            future.result(timeout=10)
            self.done.emit(True, "")
        except Exception as exc:
            self.done.emit(False, str(exc))

    async def _rename(self):
        cmds = DanteDeviceCommands()
        ip   = str(self._device.ipv4)
        port = _arc_port(self._device)
        if self._new_name:
            packet, _ = cmds.command_set_channel_name(
                self._channel_type, self._channel_number, self._new_name)
        else:
            packet, _ = cmds.command_reset_channel_name(
                self._channel_type, self._channel_number)
        result = await device_request_via_daemon(packet, ip, port)
        if result is None:
            await _engine.app.arc.request(
                packet, ip, port, logical_command_name="rename_channel")


# ── Clock status fetch worker ─────────────────────────────────────────────────
class ClockRefreshWorker(QThread):
    """Calls get_clocking_status() on every device to populate clock fields.

    Operates on the device objects already held by ClockStatusTab (the same
    dict reference) so that written fields are immediately visible to the tab.
    """
    done = pyqtSignal(dict)

    def __init__(self, devices: dict):
        super().__init__()
        self._devices = devices   # same objects the tab holds — no key mismatch

    def run(self):
        future = _engine.submit(self._fetch())
        try:
            self.done.emit(future.result(timeout=30))
        except Exception as exc:
            print(f"[clock-refresh] worker top-level exception: {exc!r}")

    async def _fetch(self):
        app = _engine.app

        async def _probe_one(sn, ip):
            try:
                await app.probe_preferred_leader_state(ip, timeout=1.5)
            except Exception as exc:
                log.debug("[clock-refresh] probe failed for %r (%s): %s", sn, ip, exc)

        ips = [(sn, str(device.ipv4))
               for sn, device in self._devices.items()
               if getattr(device, 'ipv4', None)]
        # Probe all devices concurrently so total time ≈ one round-trip, not N×timeout.
        await asyncio.gather(*(_probe_one(sn, ip) for sn, ip in ips))
        return self._devices


# ── Patch bay table ────────────────────────────────────────────────────────────
#
# Table layout:
#
#   row 0, col 0          : corner label
#   row 0, col 1..N_tx    : TX entries  (device header OR channel label)
#   row 1..N_rx, col 0    : RX entries  (device header OR channel label)
#   row 1..N_rx, col 1..  : connection cells (dots) or separator cells
#
# _tx_struct / _rx_struct items:
#   {'kind': 'dev',  'name': str,   'device': DanteDevice}
#   {'kind': 'chan', 'dname': str,  'device': DanteDevice, 'channel': DanteChannel}
#
# Device entries show a ▼/▶ toggle and are always visible.
# Channel entries are shown/hidden by expanding/collapsing their device.
# ──────────────────────────────────────────────────────────────────────────────
class PatchBayTable(QTableWidget):
    status  = pyqtSignal(str)
    patched = pyqtSignal()   # emitted after any successful subscription change

    def __init__(self):
        super().__init__()
        self._tx_struct    = []
        self._rx_struct    = []
        self._conns          = {}  # (rx_dev_name, rx_ch_name) -> (tx_dev_name, tx_ch_name)
        self._user_set_conns = {}  # same key -> (intended value, expiry_time); takes priority over stale daemon data
        self._USER_CONN_TTL  = 12  # seconds before an unconfirmed override expires
        self._tx_exp         = {}  # device_name -> bool  (True = expanded)
        self._rx_exp         = {}
        self._last_devices   = {}
        self._workers        = []  # keep refs so GC doesn't kill running threads

        self.setItemDelegate(DotDelegate())
        self.horizontalHeader().setVisible(False)
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setShowGrid(True)
        self.setGridStyle(Qt.PenStyle.SolidLine)
        self.cellClicked.connect(self._cell_clicked)
        self.cellDoubleClicked.connect(self._cell_double_clicked)

    # ── Public ─────────────────────────────────────────────────────────────────
    def load(self, devices: dict):
        self._last_devices = devices

        # Build connection map from each device's subscription list
        self._conns = {}
        for sn, device in devices.items():
            for sub in device.subscriptions:
                if sub.rx_channel_name:
                    key = (sub.rx_device_name, sub.rx_channel_name)
                    if sub.tx_channel_name and sub.tx_device_name:
                        self._conns[key] = (
                            sub.tx_device_name, sub.tx_channel_name,
                            sub.status_code, sub.rx_channel_status_code or 0,
                        )
                    else:
                        log.debug("[load] sub skipped (no tx): rx_dev=%r rx_ch=%r  tx_dev=%r tx_ch=%r",
                                  sub.rx_device_name, sub.rx_channel_name,
                                  sub.tx_device_name, sub.tx_channel_name)

        log.debug("[load] _conns built: %d connection(s)", len(self._conns))
        for k, v in self._conns.items():
            log.debug("  conn: rx_dev=%r rx_ch=%r  ->  tx_dev=%r tx_ch=%r  status=0x%04x rx_ch_status=0x%04x",
                      k[0], k[1], v[0], v[1], v[2] or 0, v[3])

        self._rebuild()

    # ── Build / rebuild grid ───────────────────────────────────────────────────
    def _rebuild(self):
        devices = self._last_devices

        tx_devs = sorted(
            [(sn, d) for sn, d in devices.items() if d.tx_channels],
            key=lambda x: (x[1].name or x[0]).lower()
        )
        rx_devs = sorted(
            [(sn, d) for sn, d in devices.items() if d.rx_channels],
            key=lambda x: (x[1].name or x[0]).lower()
        )

        self._tx_struct = []
        for sn, dev in tx_devs:
            name = dev.name or sn
            self._tx_struct.append({'kind': 'dev', 'name': name, 'device': dev})
            if self._tx_exp.get(name, False):
                for ch in sorted(dev.tx_channels.values(), key=lambda c: c.number):
                    self._tx_struct.append(
                        {'kind': 'chan', 'dname': name, 'device': dev, 'channel': ch}
                    )

        self._rx_struct = []
        for sn, dev in rx_devs:
            name = dev.name or sn
            self._rx_struct.append({'kind': 'dev', 'name': name, 'device': dev})
            if self._rx_exp.get(name, False):
                for ch in sorted(dev.rx_channels.values(), key=lambda c: c.number):
                    self._rx_struct.append(
                        {'kind': 'chan', 'dname': name, 'device': dev, 'channel': ch}
                    )

        self.setRowCount(1 + len(self._rx_struct))
        self.setColumnCount(1 + len(self._tx_struct))

        self._fill_corner()
        self._fill_tx_headers()
        self._fill_rx_headers()
        self._fill_cells()
        self._resize_table()

    # ── Cell factories ─────────────────────────────────────────────────────────
    def _make_item(self, text="", bg=None, fg=None, font=None,
                   align=Qt.AlignmentFlag.AlignCenter,
                   flags=Qt.ItemFlag.ItemIsEnabled) -> QTableWidgetItem:
        it = QTableWidgetItem(text)
        it.setTextAlignment(align)
        it.setFlags(flags)
        if bg:
            it.setBackground(QBrush(bg))
        if fg:
            it.setForeground(QBrush(fg))
        if font:
            it.setFont(font)
        return it

    def _fill_corner(self):
        it = self._make_item(
            "RX ↓  /  TX →",
            bg=C_CORNER, fg=C_HDR_FG,
            font=_small_font(),
            flags=Qt.ItemFlag.NoItemFlags,
        )
        self.setItem(0, 0, it)

    def _fill_tx_headers(self):
        for ci, entry in enumerate(self._tx_struct):
            col = ci + 1
            if entry['kind'] == 'dev':
                name = entry['name']
                exp  = self._tx_exp.get(name, False)
                it = self._make_item(
                    f"{'▼' if exp else '▶'}  {name}",
                    bg=C_TX_HDR, fg=C_HDR_FG, font=_bold_font(),
                    flags=Qt.ItemFlag.ItemIsEnabled,
                )
                it.setData(Qt.ItemDataRole.UserRole, {'kind': 'tx_dev', 'name': name})
                it.setToolTip(f"{'Collapse' if exp else 'Expand'} {name}")
            else:
                ch  = entry['channel']
                lbl = ch.friendly_name or ch.name
                it = self._make_item(
                    lbl, bg=C_TX_CH, fg=C_CH_FG, font=_small_font(),
                    flags=Qt.ItemFlag.ItemIsEnabled,
                )
                it.setToolTip(f"{entry['dname']}  /  {lbl}  (double-click to rename)")
            self.setItem(0, col, it)

    def _fill_rx_headers(self):
        left = Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
        for ri, entry in enumerate(self._rx_struct):
            row = ri + 1
            if entry['kind'] == 'dev':
                name = entry['name']
                exp  = self._rx_exp.get(name, False)
                it = self._make_item(
                    f"  {'▼' if exp else '▶'}  {name}",
                    bg=C_RX_HDR, fg=C_HDR_FG, font=_bold_font(),
                    align=left, flags=Qt.ItemFlag.ItemIsEnabled,
                )
                it.setData(Qt.ItemDataRole.UserRole, {'kind': 'rx_dev', 'name': name})
                it.setToolTip(f"{'Collapse' if exp else 'Expand'} {name}")
            else:
                ch  = entry['channel']
                lbl = ch.friendly_name or ch.name
                it = self._make_item(
                    f"  {lbl}", bg=C_RX_CH, fg=C_CH_FG, font=_small_font(),
                    align=left, flags=Qt.ItemFlag.ItemIsEnabled,
                )
                it.setToolTip(f"{entry['dname']}  /  {lbl}  (double-click to rename)")
                it.setData(Qt.ItemDataRole.UserRole, {
                    'kind': 'rx_ch',
                    'status': self._get_rx_status(entry['dname'], ch.name),
                })
            self.setItem(row, 0, it)

    def _get_rx_status(self, rx_dev_name: str, rx_ch_name: str):
        """Returns None (unpatched), 'ok', or 'error' for the given RX channel."""
        key = (rx_dev_name, rx_ch_name)
        if key in self._user_set_conns:
            expected, expiry = self._user_set_conns[key]
            if time.monotonic() <= expiry:
                return None if expected is None else 'ok'
        conn_data = self._conns.get(key)
        if conn_data is None:
            return None
        rx_ch_status = conn_data[3] or 0
        return 'ok' if (rx_ch_status & 0x0001) else 'error'

    def _update_rx_header_status(self) -> bool:
        """Refresh the status dot stored in every RX channel header cell (col 0).
        Returns True if any cell changed."""
        changed = False
        for ri, rx_e in enumerate(self._rx_struct):
            if rx_e['kind'] != 'chan':
                continue
            it = self.item(ri + 1, 0)
            if it is None:
                continue
            d = it.data(Qt.ItemDataRole.UserRole)
            if not isinstance(d, dict) or d.get('kind') != 'rx_ch':
                continue
            new_status = self._get_rx_status(rx_e['dname'], rx_e['channel'].name)
            if d.get('status') != new_status:
                d['status'] = new_status
                it.setData(Qt.ItemDataRole.UserRole, d)
                changed = True
        return changed

    def _fill_cells(self):
        no_flags = Qt.ItemFlag.NoItemFlags
        for ri, rx_e in enumerate(self._rx_struct):
            for ci, tx_e in enumerate(self._tx_struct):
                row = ri + 1
                col = ci + 1
                same_device = (
                    rx_e['kind'] == 'chan' and tx_e['kind'] == 'chan'
                    and rx_e['device'].name == tx_e['device'].name
                )
                if rx_e['kind'] == 'chan' and tx_e['kind'] == 'chan' and not same_device:
                    self._make_conn_cell(row, col, rx_e, tx_e)
                else:
                    it = QTableWidgetItem()
                    it.setBackground(QBrush(C_GRAY_CELL))
                    it.setFlags(no_flags)
                    self.setItem(row, col, it)

    def _make_conn_cell(self, row: int, col: int, rx_e: dict, tx_e: dict):
        rx_dev  = rx_e['device']
        rx_ch   = rx_e['channel']
        tx_dev  = tx_e['device']
        tx_ch   = tx_e['channel']
        tx_name = tx_ch.friendly_name or tx_ch.name
        key = (rx_dev.name, rx_ch.name)

        status_code = None
        rx_ch_status = 0
        if key in self._user_set_conns:
            expected, _expiry = self._user_set_conns[key]
            connected = (expected == (tx_dev.name, tx_name)) if expected else False
        else:
            conn_data = self._conns.get(key)
            if conn_data is not None:
                connected = conn_data[:2] == (tx_dev.name, tx_name)
                if connected:
                    status_code = conn_data[2]
                    rx_ch_status = conn_data[3]
            else:
                connected = False

        # rx_ch_status LSB: 1 = audio flowing (healthy), 0 = not flowing (error).
        # 0x0000 = self/loopback subscription (no error); 0x0101 = healthy; 0x0100 = broken.
        error = connected and bool(rx_ch_status) and not (rx_ch_status & 0x0001)

        it = QTableWidgetItem()
        it.setFlags(Qt.ItemFlag.ItemIsEnabled)
        it.setData(Qt.ItemDataRole.UserRole, {
            'kind': 'conn', 'connected': connected, 'error': error, 'pending': False,
            'rx_dev': rx_dev, 'rx_ch': rx_ch,
            'tx_dev': tx_dev, 'tx_ch': tx_ch,
        })
        rx_lbl = rx_ch.friendly_name or rx_ch.name
        tooltip = f"{'Connected: ' if connected else ''}{rx_lbl}@{rx_dev.name}  ←  {tx_name}@{tx_dev.name}"
        if error:
            if status_code in SUBSCRIPTION_STATUS_INFO:
                _state, label, detail = SUBSCRIPTION_STATUS_INFO[status_code]
                tooltip += f"\nStatus: {label}"
                if detail:
                    tooltip += f" — {detail}"
            else:
                tooltip += f"\nStatus: 0x{status_code:04x} (rx_ch: 0x{rx_ch_status:04x})"
        it.setToolTip(tooltip)
        self.setItem(row, col, it)

    def _resize_table(self):
        self.setColumnWidth(0, 160)
        self.setRowHeight(0, 140)          # tall enough for rotated names
        for ci, e in enumerate(self._tx_struct):
            self.setColumnWidth(ci + 1, 22 if e['kind'] == 'chan' else 28)
        for ri, e in enumerate(self._rx_struct):
            self.setRowHeight(ri + 1, 26 if e['kind'] == 'chan' else 30)

    # ── Background sync (auto-refresh) ────────────────────────────────────────
    def sync(self, devices: dict):
        """Merge fresh discovery data without interrupting in-progress patches.

        If any cell is pending (command in flight), the update is skipped
        entirely — it will be applied on the next auto-refresh cycle instead.
        If the device / channel set has changed, the grid is fully rebuilt
        (preserving expand/collapse state).  Otherwise only the connection
        dots are repainted.
        """
        if self._has_pending():
            return

        new_conns: dict = {}
        for sn, device in devices.items():
            for sub in device.subscriptions:
                if sub.rx_channel_name:
                    key = (sub.rx_device_name, sub.rx_channel_name)
                    if sub.tx_channel_name and sub.tx_device_name:
                        new_conns[key] = (
                            sub.tx_device_name, sub.tx_channel_name,
                            sub.status_code, sub.rx_channel_status_code or 0,
                        )

        # Compare current channel fingerprint to detect structural changes.
        # Both sides must be derived from the full device set (not from
        # _rx_struct/_tx_struct which only contain *expanded* rows) so that
        # collapsed devices don't create a spurious mismatch on every cycle.
        old_tx = frozenset(
            (d.name, ch.number)
            for d in self._last_devices.values()
            for ch in d.tx_channels.values()
        )
        old_rx = frozenset(
            (d.name, ch.number)
            for d in self._last_devices.values()
            for ch in d.rx_channels.values()
        )
        new_tx = frozenset(
            (d.name, ch.number)
            for d in devices.values()
            for ch in d.tx_channels.values()
        )
        new_rx = frozenset(
            (d.name, ch.number)
            for d in devices.values()
            for ch in d.rx_channels.values()
        )

        log.debug("[sync] new_conns: %d connection(s)", len(new_conns))
        for k, v in new_conns.items():
            log.debug("  conn: rx_dev=%r rx_ch=%r  ->  tx_dev=%r tx_ch=%r  status=0x%04x rx_ch_status=0x%04x",
                      k[0], k[1], v[0], v[1], v[2] or 0, v[3])

        self._last_devices = devices
        self._conns = new_conns

        if old_tx != new_tx or old_rx != new_rx:
            log.debug("[sync] structural change detected — full rebuild")
            log.debug("  old_tx=%s  new_tx=%s", sorted(old_tx), sorted(new_tx))
            log.debug("  old_rx=%s  new_rx=%s", sorted(old_rx), sorted(new_rx))
            self._rebuild()                  # device set changed — full grid rebuild
        else:
            self._sync_cells(new_conns)      # same devices — repaint dots only

    def _has_pending(self) -> bool:
        for ri in range(1, self.rowCount()):
            for ci in range(1, self.columnCount()):
                it = self.item(ri, ci)
                if it:
                    d = it.data(Qt.ItemDataRole.UserRole)
                    if isinstance(d, dict) and d.get('pending'):
                        return True
        return False

    def _sync_cells(self, fresh_conns: dict):
        """Repaint connection dots using fresh discovery data.

        For any RX channel the user recently patched, we trust their intended
        state over the (potentially stale) daemon cache, and only stop doing so
        once the device confirms the change in fresh_conns.
        """
        changed = False
        for ri, rx_e in enumerate(self._rx_struct):
            if rx_e['kind'] != 'chan':
                continue
            rx_dev = rx_e['device']
            rx_ch  = rx_e['channel']
            row    = ri + 1
            key    = (rx_dev.name, rx_ch.name)

            for ci, tx_e in enumerate(self._tx_struct):
                if tx_e['kind'] != 'chan':
                    continue
                col = ci + 1
                it  = self.item(row, col)
                if it is None:
                    continue
                data = it.data(Qt.ItemDataRole.UserRole)
                if not isinstance(data, dict) or data.get('kind') != 'conn':
                    continue
                if data.get('pending'):
                    continue

                tx_dev  = tx_e['device']
                tx_ch   = tx_e['channel']
                tx_name = tx_ch.friendly_name or tx_ch.name

                now_rx_ch_status = 0
                if key in self._user_set_conns:
                    # User patched this channel: use their intended state.
                    # Clear the override once the device confirms it, or when the TTL expires.
                    expected, expiry = self._user_set_conns[key]
                    fresh = fresh_conns.get(key)
                    confirmed = ((fresh[:2] if fresh else None) == expected)
                    expired   = (time.monotonic() > expiry)
                    if confirmed or expired:
                        del self._user_set_conns[key]   # device confirmed or TTL elapsed
                        now_connected = (fresh is not None and fresh[:2] == (tx_dev.name, tx_name))
                        if now_connected and fresh:
                            now_rx_ch_status = fresh[3]
                    else:
                        now_connected = (expected == (tx_dev.name, tx_name)) if expected else False
                else:
                    target = fresh_conns.get(key)
                    now_connected = (target is not None and target[:2] == (tx_dev.name, tx_name))
                    if now_connected and target:
                        now_rx_ch_status = target[3]

                now_error = now_connected and bool(now_rx_ch_status) and not (now_rx_ch_status & 0x0001)

                if data['connected'] != now_connected or data.get('error') != now_error:
                    log.debug(
                        "[sync-cells] key=%r tx=%r  connected: %s->%s  error: %s->%s  "
                        "rx_ch_status=0x%04x",
                        key, (tx_dev.name, tx_name),
                        data['connected'], now_connected,
                        data.get('error'), now_error,
                        now_rx_ch_status,
                    )
                    data['connected'] = now_connected
                    data['error'] = now_error
                    it.setData(Qt.ItemDataRole.UserRole, data)
                    changed = True

        if self._update_rx_header_status():
            changed = True
        if changed:
            self.viewport().update()

    # ── Click handling ─────────────────────────────────────────────────────────
    def _cell_clicked(self, row: int, col: int):
        it = self.item(row, col)
        if it is None:
            return
        data = it.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict):
            return

        kind = data.get('kind')

        if kind == 'tx_dev':
            name = data['name']
            self._tx_exp[name] = not self._tx_exp.get(name, False)
            self._rebuild()

        elif kind == 'rx_dev':
            name = data['name']
            self._rx_exp[name] = not self._rx_exp.get(name, False)
            self._rebuild()

        elif kind == 'conn' and not data.get('pending'):
            self._toggle_connection(row, col, data)

    def _cell_double_clicked(self, row: int, col: int):
        """Open an inline editor when the user double-clicks a channel name cell."""
        if row == 0 and 1 <= col <= len(self._tx_struct):
            entry = self._tx_struct[col - 1]
            if entry['kind'] == 'chan':
                self._start_inline_edit(row, col, entry['device'], entry['channel'])
        elif col == 0 and 1 <= row <= len(self._rx_struct):
            entry = self._rx_struct[row - 1]
            if entry['kind'] == 'chan':
                self._start_inline_edit(row, col, entry['device'], entry['channel'])

    def _start_inline_edit(self, row: int, col: int, device, channel):
        current = channel.friendly_name or channel.name or ""

        table = self   # capture for closures

        class _Editor(QLineEdit):
            def keyPressEvent(self_, event):
                if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    _commit()
                elif event.key() == Qt.Key.Key_Escape:
                    _cancel()
                else:
                    super().keyPressEvent(event)
            def focusOutEvent(self_, event):
                _commit()
                super().focusOutEvent(event)

        _done = [False]

        def _commit():
            if _done[0]:
                return
            _done[0] = True
            new_name = editor.text().strip()
            table.setCellWidget(row, col, None)
            if new_name != current:
                table._apply_rename(row, col, device, channel, new_name)

        def _cancel():
            if _done[0]:
                return
            _done[0] = True
            table.setCellWidget(row, col, None)

        editor = _Editor(current)
        editor.setMaxLength(31)
        editor.selectAll()
        editor.setStyleSheet(
            "background: white; color: #191919; border: 2px solid #2860A0; padding: 1px;")
        self.setCellWidget(row, col, editor)
        editor.setFocus()

    def _apply_rename(self, row: int, col: int, device, channel, new_name: str):
        if len(new_name) > 31:
            self.status.emit("Channel name too long (max 31 characters)")
            return

        # Update local display immediately for responsiveness
        channel.friendly_name = new_name or None
        lbl = channel.friendly_name or channel.name or ""
        item = self.item(row, col)
        if item:
            item.setText(f"  {lbl}" if col == 0 else lbl)
            item.setToolTip(f"{device.name}  /  {lbl}")

        w = ChannelRenameWorker(device, channel.channel_type, channel.number, new_name)
        w.done.connect(lambda ok, msg, n=new_name: self._on_rename_done(ok, msg, n))
        self._workers.append(w)
        w.start()

    def _on_rename_done(self, ok: bool, msg: str, new_name: str):
        if ok:
            label = f"'{new_name}'" if new_name else "default"
            self.status.emit(f"Channel renamed to {label}")
        else:
            self.status.emit(f"Channel rename failed: {msg}")

    def _clear_row_connections(self, row: int, except_col: int, rx_dev, rx_ch):
        """Optimistically uncheck any other TX already connected to this RX channel."""
        key = (rx_dev.name, rx_ch.name)
        self._conns.pop(key, None)
        for ci, tx_e in enumerate(self._tx_struct):
            col = ci + 1
            if col == except_col or tx_e['kind'] != 'chan':
                continue
            it = self.item(row, col)
            if it is None:
                continue
            d = it.data(Qt.ItemDataRole.UserRole)
            if isinstance(d, dict) and d.get('kind') == 'conn' \
                    and d.get('connected') and not d.get('pending'):
                d['connected'] = False
                it.setData(Qt.ItemDataRole.UserRole, d)

    def _toggle_connection(self, row: int, col: int, data: dict):
        # Drop references to workers whose OS thread has fully exited.
        # isFinished() is only True after QThreadPrivate::finish() has run,
        # which sets d->running = False — so this is the safe point to GC them.
        self._workers = [w for w in self._workers if not w.isFinished()]

        connected = data['connected']
        rx_dev    = data['rx_dev']
        rx_ch     = data['rx_ch']
        tx_dev    = data['tx_dev']
        tx_ch     = data['tx_ch']
        action    = 'remove' if connected else 'add'

        # Dante only allows one TX per RX channel: uncheck any existing connection
        # in this row before marking the new cell as pending.
        if action == 'add':
            self._clear_row_connections(row, col, rx_dev, rx_ch)

        # Mark cell as pending (amber dot)
        data['pending'] = True
        self.item(row, col).setData(Qt.ItemDataRole.UserRole, data)
        self.viewport().update()

        rx_lbl = rx_ch.friendly_name or rx_ch.name
        tx_lbl = tx_ch.friendly_name or tx_ch.name
        if connected:
            self.status.emit(f"Disconnecting  {rx_lbl}@{rx_dev.name}…")
        else:
            self.status.emit(f"Connecting  {rx_lbl}@{rx_dev.name}  ←  {tx_lbl}@{tx_dev.name}…")

        w = CmdWorker(
            action, rx_dev, rx_ch,
            tx_dev if action == 'add' else None,
            tx_ch  if action == 'add' else None,
        )
        w.done.connect(
            lambda ok, msg, r=row, c=col, new=not connected:
            self._cmd_done(ok, msg, r, c, new)
        )
        self._workers.append(w)
        w.start()

    def _cmd_done(self, ok: bool, msg: str, row: int, col: int, new_state: bool):
        it = self.item(row, col)
        if it is None:
            return
        data = it.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict) or data.get('kind') != 'conn':
            return

        data['pending'] = False
        if ok:
            data['connected'] = new_state
            rx_dev  = data['rx_dev']
            rx_ch   = data['rx_ch']
            tx_dev  = data['tx_dev']
            tx_ch   = data['tx_ch']
            tx_name = tx_ch.friendly_name or tx_ch.name
            key = (rx_dev.name, rx_ch.name)
            if new_state:
                self._conns[key] = (tx_dev.name, tx_name, None, 0)  # status unknown until next refresh
                self._user_set_conns[key] = ((tx_dev.name, tx_name), time.monotonic() + self._USER_CONN_TTL)
            else:
                self._conns.pop(key, None)
                self._user_set_conns[key] = (None, time.monotonic() + self._USER_CONN_TTL)
            self.status.emit("Done.")
            self.patched.emit()
        else:
            self.status.emit(f"Error: {msg}")

        it.setData(Qt.ItemDataRole.UserRole, data)
        self._update_rx_header_status()
        self.viewport().update()


# ── Clock status tab ───────────────────────────────────────────────────────────
C_PENDING_ROW = QColor(255, 248, 215)   # pale amber  — change in flight
C_CONFIRM_ROW = QColor(220, 245, 220)   # pale green  — just confirmed
C_ERROR_ROW   = QColor(255, 220, 220)   # pale red    — could not confirm
C_CLOCK_FG    = QColor(25,  25,  25)    # near-black text for clock tab cells

import tempfile as _tempfile, os as _os

def _write_tick_svg():
    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
        b'<polyline points="1,5 4,8.5 9,1.5" fill="none" stroke="#191919"'
        b' stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
        b'</svg>'
    )
    fd, path = _tempfile.mkstemp(suffix='.svg')
    _os.write(fd, svg)
    _os.close(fd)
    return path

_TICK_SVG_PATH = _write_tick_svg()
_CHECKBOX_STYLE = f"""
    QCheckBox::indicator {{
        width: 13px; height: 13px;
        border: 1.5px solid #191919;
        border-radius: 2px;
        background-color: white;
    }}
    QCheckBox::indicator:checked {{
        background-color: white;
        border: 1.5px solid #191919;
        image: url("{_TICK_SVG_PATH}");
    }}
"""


class ClockStatusTab(QWidget):
    """Tab listing devices with their PTP clock role and a 'Preferred Leader' checkbox.

    Checkbox behaviour
    ──────────────────
    • Reflects the network state at all times (via ``sync()``).
    • When the user toggles a checkbox the command is sent immediately and the
      row turns amber to signal a pending change.
    • A background worker re-queries the device after ``_VERIFY_DELAY_MS`` ms.
      – If the device confirms the new state the row flashes green, then clears.
      – If the TTL expires without confirmation the row flashes red, the checkbox
        reverts to the previous value, and the pending state is cleared.
      – If still not confirmed but within the TTL the verification is rescheduled.
    """

    status = pyqtSignal(str)

    _VERIFY_DELAY_MS = 4_000   # ms between re-query attempts
    _CHANGE_TTL      = 15      # s  — give up and revert after this long

    COL_NAME   = 0
    COL_IP     = 1
    COL_LEADER = 2
    COL_ROLE   = 3
    _HEADERS   = ["Device", "IP Address", "Preferred Leader", "Clock Role"]

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        self._table = QTableWidget()
        self._table.setColumnCount(len(self._HEADERS))
        self._table.setHorizontalHeaderLabels(self._HEADERS)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(True)
        self._table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self._table)

        self._devices: dict = {}              # sn → device
        # sn → (intended_bool, expiry_monotonic, flash_colour | None)
        self._pending: dict = {}
        self._workers: list = []
        self._verify_timers: dict[str, QTimer] = {}

    # ── Public interface (called by PatchBayWindow) ────────────────────────────

    @property
    def devices(self) -> dict:
        """The device dict currently held by this tab."""
        return self._devices

    def load(self, devices: dict):
        """Full rebuild from a fresh discovery result."""
        self._devices = devices
        self._rebuild()

    def sync(self, devices: dict):
        """Merge fresh data without full rebuild; validates pending changes."""
        now = time.monotonic()
        for sn in list(self._pending.keys()):
            intended, expiry, flash = self._pending[sn]
            device = devices.get(sn)
            if device is None:
                continue
            actual = getattr(device, 'preferred_leader', None)
            if actual is not None and bool(actual) == intended:
                # Confirmed by network — flash green, then clear
                self._pending[sn] = (intended, expiry, C_CONFIRM_ROW)
                QTimer.singleShot(1500, lambda s=sn: self._clear_pending(s))

        # If this is the same dict object (e.g. emitted by ClockRefreshWorker
        # after in-place mutation), just repaint — don't replace or compare keys.
        if devices is not self._devices:
            old_sns = set(self._devices.keys())
            self._devices = devices
            new_sns = set(devices.keys())
            if old_sns != new_sns:
                self._rebuild()
                return

        self._update_rows()

    # ── Build / update table ───────────────────────────────────────────────────

    def _sorted_items(self) -> list:
        return sorted(
            self._devices.items(),
            key=lambda kv: (kv[1].name or kv[0]).lower()
        )

    def _rebuild(self):
        self._table.setRowCount(0)
        items = self._sorted_items()
        self._table.setRowCount(len(items))
        for row, (sn, device) in enumerate(items):
            self._fill_row(row, sn, device)
        self._table.resizeColumnsToContents()
        self._table.setColumnWidth(self.COL_LEADER, 130)

    def _fill_row(self, row: int, sn: str, device):
        dev_name     = device.name or sn
        ip_str       = str(device.ipv4) if getattr(device, 'ipv4', None) else "—"
        pm           = getattr(device, 'preferred_leader', None)
        configurable = getattr(device, 'preferred_leader_configurable', None)

        # Col 0: device name — stores sn in UserRole for later lookups
        it = QTableWidgetItem(dev_name)
        it.setFlags(Qt.ItemFlag.ItemIsEnabled)
        it.setData(Qt.ItemDataRole.UserRole, sn)
        it.setForeground(QBrush(C_CLOCK_FG))
        self._table.setItem(row, self.COL_NAME, it)

        # Col 1: IP address
        it = QTableWidgetItem(ip_str)
        it.setFlags(Qt.ItemFlag.ItemIsEnabled)
        it.setForeground(QBrush(C_CLOCK_FG))
        self._table.setItem(row, self.COL_IP, it)

        # Col 2: Preferred Leader checkbox (widget-in-cell, centred)
        self._set_leader_checkbox(row, sn, pm, configurable)

        # Col 3: Clock role
        it = QTableWidgetItem(self._clock_role(device))
        it.setFlags(Qt.ItemFlag.ItemIsEnabled)
        it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        it.setForeground(QBrush(C_CLOCK_FG))
        self._table.setItem(row, self.COL_ROLE, it)

        self._apply_row_colour(row, sn)

    def _set_leader_checkbox(self, row: int, sn: str, pm, configurable=None):
        """Create (or recreate) the Preferred Leader cell widget.

        Shows 'Follower only' when pm is None (probe not yet returned) or when
        configurable is explicitly False (PTP priority1=0xFF, device is hardcoded
        as follower-only, e.g. BLN).  Shows a checkbox otherwise.
        """
        container = QWidget()
        hbox = QHBoxLayout(container)
        hbox.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hbox.setContentsMargins(0, 0, 0, 0)

        if pm is None or configurable is False:
            lbl = QLabel("Follower only")
            lbl.setStyleSheet(f"color: rgb({C_CLOCK_FG.red()},{C_CLOCK_FG.green()},{C_CLOCK_FG.blue()});")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hbox.addWidget(lbl)
        else:
            chk = QCheckBox()
            chk.setStyleSheet(_CHECKBOX_STYLE)
            chk.setChecked(bool(pm))
            chk.setToolTip(
                "This device is marked as the preferred network clock leader."
                if pm else
                "Tick to designate this device as the preferred clock leader."
            )
            chk.stateChanged.connect(lambda state, s=sn: self._on_leader_toggled(s, state))
            hbox.addWidget(chk)

        self._table.setCellWidget(row, self.COL_LEADER, container)

    def _update_rows(self):
        """Repaint all rows from current ``_devices`` and ``_pending`` state."""
        for row in range(self._table.rowCount()):
            name_it = self._table.item(row, self.COL_NAME)
            if name_it is None:
                continue
            sn     = name_it.data(Qt.ItemDataRole.UserRole)
            device = self._devices.get(sn)
            if device is None:
                continue

            # Update checkbox only when not waiting for confirmation
            if sn not in self._pending:
                pm           = getattr(device, 'preferred_leader', None)
                configurable = getattr(device, 'preferred_leader_configurable', None)

                should_show_label = pm is None or configurable is False
                container = self._table.cellWidget(row, self.COL_LEADER)
                has_checkbox = container is not None and container.findChild(QCheckBox) is not None

                if should_show_label and has_checkbox:
                    self._set_leader_checkbox(row, sn, pm, configurable)
                elif not should_show_label and not has_checkbox:
                    self._set_leader_checkbox(row, sn, pm, configurable)
                elif not should_show_label and has_checkbox:
                    chk = container.findChild(QCheckBox)
                    chk.blockSignals(True)
                    chk.setChecked(bool(pm))
                    chk.blockSignals(False)

            # Clock role text
            role_it = self._table.item(row, self.COL_ROLE)
            if role_it:
                role_it.setText(self._clock_role(device))

            self._apply_row_colour(row, sn)

    def _clock_role(self, device) -> str:
        """Best-effort clock role label from whatever attribute the library exposes."""
        for attr in ('is_clock_master', 'clock_master', 'is_master'):
            v = getattr(device, attr, None)
            if v is True:
                return "Leader"
            if v is False:
                return "Follower"
        pm = getattr(device, 'preferred_leader', None)
        if pm is True:
            return "Preferred"
        return "—"

    def _apply_row_colour(self, row: int, sn: str):
        if sn in self._pending:
            _, _, flash = self._pending[sn]
            colour = flash or C_PENDING_ROW
        else:
            colour = None
        for col in (self.COL_NAME, self.COL_IP, self.COL_ROLE):
            it = self._table.item(row, col)
            if it:
                if colour:
                    it.setBackground(QBrush(colour))
                else:
                    it.setBackground(QBrush(QColor(Qt.GlobalColor.white)))
                it.setForeground(QBrush(C_CLOCK_FG))
        # The checkbox cell uses setCellWidget, so the item background has no
        # effect — set the container widget's stylesheet instead.
        container = self._table.cellWidget(row, self.COL_LEADER)
        if container:
            bg = colour.name() if colour else "#ffffff"
            container.setStyleSheet(f"background-color: {bg};")

    def _clear_pending(self, sn: str):
        """Remove the pending entry and repaint the row to clear any flash colour."""
        self._pending.pop(sn, None)
        self._update_rows()

    # ── Checkbox interaction ───────────────────────────────────────────────────

    def _on_leader_toggled(self, sn: str, state: int):
        if sn in self._pending:
            return   # signal fired by our own blockSignals=False update — ignore
        device = self._devices.get(sn)
        if device is None:
            return

        new_val  = (state == Qt.CheckState.Checked.value)
        dev_name = device.name or sn

        print(f"[clock-toggle] device={dev_name!r}  new_val={new_val!r}  "
              f"preferred_leader_before={getattr(device, 'preferred_leader', None)!r}")

        # Record optimistic intent
        self._pending[sn] = (new_val, time.monotonic() + self._CHANGE_TTL, None)
        self._repaint_row_for(sn)

        self.status.emit(
            f"{'Enabling' if new_val else 'Disabling'} preferred leader on {dev_name}…"
        )

        # Fire the ARC command
        w = ClockSetLeaderWorker(device, new_val)
        w.done.connect(lambda ok, msg, s=sn, nv=new_val: self._cmd_done(ok, msg, s, nv))
        self._workers = [x for x in self._workers if not x.isFinished()]
        self._workers.append(w)
        w.start()

        # Schedule first verification pass
        self._schedule_verify(sn)

    def _cmd_done(self, ok: bool, msg: str, sn: str, new_val: bool):
        device   = self._devices.get(sn)
        dev_name = (device.name or sn) if device else sn
        if ok:
            self.status.emit(f"Clock leader command sent to {dev_name}.")
        else:
            # Hard failure — revert immediately without waiting for TTL
            self.status.emit(f"Clock leader command failed for {dev_name}: {msg}")
            self._pending.pop(sn, None)
            self._revert_checkbox(sn)
            self._repaint_row_for(sn)

    # ── Verification ──────────────────────────────────────────────────────────

    def _schedule_verify(self, sn: str):
        """(Re-)arm the single-shot timer that triggers a device re-query."""
        if sn in self._verify_timers:
            t = self._verify_timers[sn]
            if t.isActive():
                t.stop()
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda s=sn: self._run_verify(s))
        timer.start(self._VERIFY_DELAY_MS)
        self._verify_timers[sn] = timer

    def _run_verify(self, sn: str):
        if sn not in self._pending:
            return
        w = DeviceRefreshWorker(sn)
        w.done.connect(lambda devices, s=sn: self._on_verify_done(devices, s))
        self._workers = [x for x in self._workers if not x.isFinished()]
        self._workers.append(w)
        w.start()

    def _on_verify_done(self, devices: dict, sn: str):
        if sn not in self._pending:
            return
        intended, expiry, _ = self._pending[sn]
        device = devices.get(sn)
        now    = time.monotonic()

        actual = getattr(device, 'preferred_leader', None) if device else None
        print(f"[clock-verify] sn={sn!r}  intended={intended!r}  "
              f"actual={actual!r}  ttl_remaining={expiry - now:.1f}s")

        if device is not None:
            actual = getattr(device, 'preferred_leader', None)
            if actual is not None and bool(actual) == intended:
                # ✓ Confirmed
                dev_name = device.name or sn
                self.status.emit(f"Preferred leader change confirmed for {dev_name}.")
                self._devices[sn]  = device
                self._pending[sn]  = (intended, expiry, C_CONFIRM_ROW)
                QTimer.singleShot(1500, lambda s=sn: self._clear_pending(s))
                self._update_rows()
                return

        if now > expiry:
            # ✗ TTL elapsed — give up and revert
            device   = self._devices.get(sn)
            dev_name = (device.name or sn) if device else sn
            self.status.emit(
                f"Could not confirm preferred leader change for {dev_name} — reverting."
            )
            self._pending[sn] = (intended, expiry, C_ERROR_ROW)
            self._revert_checkbox(sn)
            QTimer.singleShot(2000, lambda s=sn: self._clear_pending(s))
            return

        # Still within TTL but not yet confirmed — reschedule
        self._schedule_verify(sn)

    def _revert_checkbox(self, sn: str):
        """Set the checkbox back to the last known network value."""
        device = self._devices.get(sn)
        if device is None:
            return
        pm = getattr(device, 'preferred_leader', None)
        for row in range(self._table.rowCount()):
            it = self._table.item(row, self.COL_NAME)
            if it and it.data(Qt.ItemDataRole.UserRole) == sn:
                container = self._table.cellWidget(row, self.COL_LEADER)
                if container:
                    chk = container.findChild(QCheckBox)
                    if chk:
                        chk.blockSignals(True)
                        chk.setChecked(bool(pm) if pm is not None else False)
                        chk.blockSignals(False)
                break

    def _repaint_row_for(self, sn: str):
        for row in range(self._table.rowCount()):
            it = self._table.item(row, self.COL_NAME)
            if it and it.data(Qt.ItemDataRole.UserRole) == sn:
                self._apply_row_colour(row, sn)
                break
        self._table.viewport().update()


# ── Main window ────────────────────────────────────────────────────────────────
class PatchBayWindow(QMainWindow):
    # Signal emitted from the notification callback (async thread → Qt main thread)
    _notify_refresh = pyqtSignal(str)   # server_name of device that changed
    _clock_status_received = pyqtSignal(str)  # source_ip of device whose clock state changed

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Dante Patch Bay")
        self.resize(1050, 680)

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(4)

        # ── shared toolbar ────────────────────────────────────────────────────
        toolbar = QHBoxLayout()
        self._btn_refresh = QPushButton("⟳  Refresh")
        self._btn_refresh.setFixedWidth(110)
        self._btn_refresh.clicked.connect(self.refresh)
        toolbar.addWidget(self._btn_refresh)
        toolbar.addStretch()
        outer.addLayout(toolbar)

        # ── tab widget ────────────────────────────────────────────────────────
        self._tabs = QTabWidget()
        outer.addWidget(self._tabs)

        # Patch Bay tab
        patch_widget = QWidget()
        patch_layout = QVBoxLayout(patch_widget)
        patch_layout.setContentsMargins(0, 0, 0, 0)
        self._table = PatchBayTable()
        self._table.status.connect(self._show_status)
        self._table.patched.connect(self._auto_refresh)
        patch_layout.addWidget(self._table)
        self._tabs.addTab(patch_widget, "Patch Bay")

        # Clock Status tab
        self._clock_tab = ClockStatusTab()
        self._clock_tab.status.connect(self._show_status)
        self._tabs.addTab(self._clock_tab, "Clock Status")

        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)

        self._discovery_worker: DiscoveryWorker | None = None
        self._bg_worker: DiscoveryWorker | None = None
        self._clock_worker: ClockRefreshWorker | None = None
        self._all_workers: list = []

        # Wire the cross-thread notification signals
        self._notify_refresh.connect(self._on_device_notification)
        self._clock_status_received.connect(self._on_clock_status_received)
        self._device_refresh_workers: dict[str, DeviceRefreshWorker] = {}

        # Start the persistent async engine and register notification handlers
        _engine.start()
        self._register_notification_handlers()

        self._auto_timer = QTimer(self)
        self._auto_timer.setInterval(5000)
        self._auto_timer.timeout.connect(self._auto_refresh)
        self._auto_timer.start()

        self.refresh()

    def _register_notification_handlers(self):
        """Register callbacks on the persistent DanteApplication so any routing
        change broadcast by a device triggers an immediate single-device refresh."""
        app = _engine.app

        # 1. Notification-level: called for specific ARC notification IDs.
        async def _on_routing_notification(event):
            server_name = event.server_name or event.device_name
            log.debug("[notification] routing notification  server=%r  event=%r", server_name, event)
            if server_name:
                self._notify_refresh.emit(server_name)

        for nid in _ROUTING_NOTIFICATION_IDS:
            app.on_notification(nid, _on_routing_notification)

        # 2. Dispatcher-level fallback: netaudio's _on_packet() resolves
        #    server_name via _device_by_ip(), which can silently return None
        #    if the device isn't in app.devices yet (e.g. first notification
        #    before discovery completes).  Subscribe to the raw
        #    NOTIFICATION_RECEIVED event and do our own IP→server_name lookup
        #    so notifications are never silently dropped.
        async def _on_raw_notification(event):
            notification_id = event.data.get("notification_id")
            if notification_id not in _ROUTING_NOTIFICATION_IDS:
                return
            server_name = event.server_name or event.device_name
            source_ip = event.data.get("source_ip", "")
            log.debug("[notification] raw  id=0x%04x  server=%r  source_ip=%r",
                      notification_id or 0, server_name, source_ip)
            if not server_name:
                if source_ip:
                    for sn, d in app.devices.items():
                        if d.ipv4 and str(d.ipv4) == source_ip:
                            server_name = sn
                            log.debug("[notification] resolved ip=%r -> server=%r", source_ip, server_name)
                            break
                if not server_name:
                    log.warning("[notification] could not resolve server_name for ip=%r — refresh skipped", source_ip)
            if server_name:
                self._notify_refresh.emit(server_name)

        app.dispatcher.on(EventType.NOTIFICATION_RECEIVED, _on_raw_notification)

        # 3. DEVICE_UPDATED is fired by register_device() which the notification
        #    service calls after applying pending notifications.
        async def _on_device_updated(event):
            server_name = event.server_name or event.device_name
            log.debug("[notification] DEVICE_UPDATED  server=%r", server_name)
            if server_name:
                self._notify_refresh.emit(server_name)

        app.dispatcher.on(EventType.DEVICE_UPDATED, _on_device_updated)

        # 4. PTP clock status broadcasts: the library's notification service
        #    processes these CONMON packets (opcode 0x0020) via
        #    _handle_ptp_clock_status, which updates device.preferred_leader and
        #    device.ptp_v1_role directly but never dispatches a NOTIFICATION_RECEIVED
        #    event.  Hook _notify_preferred_leader_waiter — called at the very end of
        #    _handle_ptp_clock_status — to get an immediate signal on every broadcast
        #    or probe response without any extra polling or network traffic.
        _clock_signal = self._clock_status_received
        _notifications = app.notifications
        _original_handle = _notifications._handle_ptp_clock_status
        _original_notify = _notifications._notify_preferred_leader_waiter

        def _hooked_handle(data: bytes, source_ip: str):
            _original_handle(data, source_ip)
            # Byte at offset 0x27 (index 39) is the PTP priority1 value.
            # Follower-only devices (e.g. BLN) are hardcoded at 0xFF (= 255,
            # the absolute minimum — they can never win clock election).
            # Configurable devices report lower values (0x9d, 0x9f, etc.).
            device = _notifications._lookup_device(source_ip)
            if device is not None and len(data) > 0x27:
                priority1 = data[0x27]
                device.preferred_leader_configurable = (priority1 != 0xFF)

        _notifications._handle_ptp_clock_status = _hooked_handle

        def _hooked_notify(source_ip: str, preferred_leader):
            _original_notify(source_ip, preferred_leader)
            _clock_signal.emit(source_ip)

        _notifications._notify_preferred_leader_waiter = _hooked_notify

    def _on_clock_status_received(self, source_ip: str):
        """Called immediately (Qt main thread) when any device sends a PTP clock
        status packet — both spontaneous broadcasts and probe responses.  The
        device object is already updated by the time this fires."""
        self._clock_tab.sync(self._clock_tab.devices)

    # ── Notification-driven single-device refresh ──────────────────────────────
    def _on_device_notification(self, server_name: str):
        """Slot called (on Qt main thread) when a routing notification arrives."""
        if server_name in self._device_refresh_workers:
            w = self._device_refresh_workers[server_name]
            if w.isRunning():
                return   # already refreshing this device

        w = DeviceRefreshWorker(server_name)
        w.done.connect(self._on_bg_refresh)
        self._device_refresh_workers[server_name] = w
        self._all_workers.append(w)
        w.start()

    def refresh(self):
        """Manual refresh: full ARC re-query of all devices, then rebuilds the grid."""
        self._btn_refresh.setEnabled(False)
        self._show_status("Discovering devices…")
        w = DiscoveryWorker(force_full=True)
        w.done.connect(self._on_discovered)
        w.failed.connect(self._on_error)
        self._discovery_worker = w
        self._all_workers.append(w)
        w.start()

    def _auto_refresh(self):
        """Background refresh: silently syncs state without disrupting patching."""
        self._all_workers = [w for w in self._all_workers if not w.isFinished()]
        self._device_refresh_workers = {
            sn: w for sn, w in self._device_refresh_workers.items()
            if not w.isFinished()
        }

        if self._discovery_worker is not None and self._discovery_worker.isRunning():
            return
        if self._bg_worker is not None and self._bg_worker.isRunning():
            return
        w = DiscoveryWorker()
        w.done.connect(self._on_bg_refresh)
        self._bg_worker = w
        self._all_workers.append(w)
        w.start()

    def _on_bg_refresh(self, devices: dict):
        self._table.sync(devices)
        self._clock_tab.sync(devices)
        self._start_clock_refresh()

    def _on_discovered(self, devices: dict):
        self._table.load(devices)
        self._clock_tab.load(devices)
        self._start_clock_refresh()
        tx = sum(1 for d in devices.values() if d.tx_channels)
        rx = sum(1 for d in devices.values() if d.rx_channels)
        self._show_status(
            f"{len(devices)} device(s)  —  {tx} transmitter(s), {rx} receiver(s)"
        )
        self._btn_refresh.setEnabled(True)

    def _start_clock_refresh(self):
        """Kick off a background fetch of get_clocking_status() for all devices.

        discover_and_populate() does not fetch clock data — it must be
        requested separately per device.  This worker calls
        get_clocking_status() on every known device then feeds the result
        back to the clock tab so preferred_leader / clock_role are populated.
        """
        if self._clock_worker is not None and self._clock_worker.isRunning():
            print("[clock-refresh] skipped — worker already running")
            return
        print("[clock-refresh] launching ClockRefreshWorker")
        w = ClockRefreshWorker(self._clock_tab.devices)
        w.done.connect(self._clock_tab.sync)
        self._clock_worker = w
        self._all_workers.append(w)
        w.start()

    def _on_error(self, msg: str):
        self._show_status(f"Discovery failed: {msg}")
        self._btn_refresh.setEnabled(True)

    def _show_status(self, msg: str):
        self._status_bar.showMessage(msg)

    def closeEvent(self, event):
        _engine.stop()
        super().closeEvent(event)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Dante Patch Bay")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable debug logging to stderr")
    args, qt_args = parser.parse_known_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    app = QApplication([sys.argv[0]] + qt_args)
    app.setStyle("Fusion")
    win = PatchBayWindow()
    win.show()
    sys.exit(app.exec())