"""
Webcam Studio - USB webcam viewer / recorder / snapshot tool for Windows.

Layout:
  +--------------------------------------------------+
  |  Toolbar: device, resolution, snapshot, record   |
  +---------------------------+----------------------+
  |                           |                      |
  |      LIVE PREVIEW         |    LAST CAPTURE      |
  |                           |                      |
  +---------------------------+----------------------+
  |  DOCK: all manual camera image controls          |
  |  (brightness, exposure, focus, WB, gain, ...)    |
  +--------------------------------------------------+

Camera image controls are read directly from the DirectShow driver via
IAMVideoProcAmp / IAMCameraControl (comtypes), so every property the
camera actually supports shows up with its real min/max/step/default,
plus an Auto checkbox where the driver supports auto mode. Property
changes are device-global on UVC cameras, so they apply live to the
OpenCV preview stream.

Requirements (Windows):
    pip install PySide6 opencv-python numpy comtypes

Run:
    python webcam_studio.py

Output files go to  ~/Pictures/WebcamStudio/  (snapshots .png, videos .mp4).
"""

import sys
import time
import math
import threading
from datetime import datetime
from pathlib import Path

import numpy as np
import cv2

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QSize
from PySide6.QtGui import QImage, QPixmap, QAction, QKeySequence, QPainter
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QDockWidget, QScrollArea,
    QGridLayout, QHBoxLayout, QVBoxLayout, QSlider, QCheckBox, QComboBox,
    QPushButton, QToolBar, QSizePolicy, QFrame, QMessageBox, QStyle,
)

# --------------------------------------------------------------------------
# DirectShow camera property access (Windows only, via comtypes)
# --------------------------------------------------------------------------

DSHOW_AVAILABLE = False
try:
    import comtypes
    from comtypes import GUID, IUnknown, COMMETHOD, HRESULT
    from comtypes.automation import VARIANT
    from ctypes import POINTER, c_long, c_ulong, c_void_p, c_wchar_p, c_int
    DSHOW_AVAILABLE = sys.platform == "win32"
except ImportError:
    pass

if DSHOW_AVAILABLE:

    CLSID_SystemDeviceEnum = GUID('{62BE5D10-60EB-11D0-BD3B-00A0C911CE86}')
    CLSID_VideoInputDeviceCategory = GUID('{860BB310-5D01-11D0-BD3B-00A0C911CE86}')
    IID_IBaseFilter = GUID('{56A86895-0AD4-11CE-B03A-0020AF0BA770}')

    class IBaseFilter(IUnknown):
        _iid_ = IID_IBaseFilter
        # No methods needed; we only QueryInterface off of it.

    class IPropertyBag(IUnknown):
        _iid_ = GUID('{55272A00-42CB-11CE-8135-00AA004BB851}')
        _methods_ = [
            COMMETHOD([], HRESULT, 'Read',
                      (['in'], c_wchar_p, 'pszPropName'),
                      (['out'], POINTER(VARIANT), 'pVar'),
                      (['in'], c_void_p, 'pErrorLog')),
            COMMETHOD([], HRESULT, 'Write',
                      (['in'], c_wchar_p, 'pszPropName'),
                      (['in'], POINTER(VARIANT), 'pVar')),
        ]

    class IMoniker(IUnknown):
        """IMoniker with full vtable order. Only BindToObject/BindToStorage
        are actually called; the rest are correctly-ordered placeholders."""
        _iid_ = GUID('{0000000F-0000-0000-C000-000000000046}')
        _methods_ = [
            # --- IPersist ---
            COMMETHOD([], HRESULT, 'GetClassID',
                      (['out'], POINTER(GUID), 'pClassID')),
            # --- IPersistStream ---
            COMMETHOD([], HRESULT, 'IsDirty'),
            COMMETHOD([], HRESULT, 'Load', (['in'], c_void_p, 'pStm')),
            COMMETHOD([], HRESULT, 'Save',
                      (['in'], c_void_p, 'pStm'),
                      (['in'], c_int, 'fClearDirty')),
            COMMETHOD([], HRESULT, 'GetSizeMax',
                      (['in'], c_void_p, 'pcbSize')),
            # --- IMoniker ---
            COMMETHOD([], HRESULT, 'BindToObject',
                      (['in'], c_void_p, 'pbc'),
                      (['in'], c_void_p, 'pmkToLeft'),
                      (['in'], POINTER(GUID), 'riidResult'),
                      (['out'], POINTER(POINTER(IUnknown)), 'ppvResult')),
            COMMETHOD([], HRESULT, 'BindToStorage',
                      (['in'], c_void_p, 'pbc'),
                      (['in'], c_void_p, 'pmkToLeft'),
                      (['in'], POINTER(GUID), 'riid'),
                      (['out'], POINTER(POINTER(IUnknown)), 'ppvObj')),
            COMMETHOD([], HRESULT, 'Reduce',
                      (['in'], c_void_p, 'pbc'),
                      (['in'], c_ulong, 'dwReduceHowFar'),
                      (['in'], c_void_p, 'ppmkToLeft'),
                      (['in'], c_void_p, 'ppmkReduced')),
            COMMETHOD([], HRESULT, 'ComposeWith',
                      (['in'], c_void_p, 'pmkRight'),
                      (['in'], c_int, 'fOnlyIfNotGeneric'),
                      (['in'], c_void_p, 'ppmkComposite')),
            COMMETHOD([], HRESULT, 'Enum',
                      (['in'], c_int, 'fForward'),
                      (['in'], c_void_p, 'ppenumMoniker')),
            COMMETHOD([], HRESULT, 'IsEqual',
                      (['in'], c_void_p, 'pmkOtherMoniker')),
            COMMETHOD([], HRESULT, 'Hash',
                      (['in'], c_void_p, 'pdwHash')),
            COMMETHOD([], HRESULT, 'IsRunning',
                      (['in'], c_void_p, 'pbc'),
                      (['in'], c_void_p, 'pmkToLeft'),
                      (['in'], c_void_p, 'pmkNewlyRunning')),
            COMMETHOD([], HRESULT, 'GetTimeOfLastChange',
                      (['in'], c_void_p, 'pbc'),
                      (['in'], c_void_p, 'pmkToLeft'),
                      (['in'], c_void_p, 'pFileTime')),
            COMMETHOD([], HRESULT, 'Inverse',
                      (['in'], c_void_p, 'ppmk')),
            COMMETHOD([], HRESULT, 'CommonPrefixWith',
                      (['in'], c_void_p, 'pmkOther'),
                      (['in'], c_void_p, 'ppmkPrefix')),
            COMMETHOD([], HRESULT, 'RelativePathTo',
                      (['in'], c_void_p, 'pmkOther'),
                      (['in'], c_void_p, 'ppmkRelPath')),
            COMMETHOD([], HRESULT, 'GetDisplayName',
                      (['in'], c_void_p, 'pbc'),
                      (['in'], c_void_p, 'pmkToLeft'),
                      (['in'], c_void_p, 'ppszDisplayName')),
            COMMETHOD([], HRESULT, 'ParseDisplayName',
                      (['in'], c_void_p, 'pbc'),
                      (['in'], c_void_p, 'pmkToLeft'),
                      (['in'], c_wchar_p, 'pszDisplayName'),
                      (['in'], c_void_p, 'pchEaten'),
                      (['in'], c_void_p, 'ppmkOut')),
            COMMETHOD([], HRESULT, 'IsSystemMoniker',
                      (['in'], c_void_p, 'pdwMksys')),
        ]

    class IEnumMoniker(IUnknown):
        _iid_ = GUID('{00000102-0000-0000-C000-000000000046}')
        _methods_ = [
            COMMETHOD([], HRESULT, 'Next',
                      (['in'], c_ulong, 'celt'),
                      (['out'], POINTER(POINTER(IMoniker)), 'rgelt'),
                      (['out'], POINTER(c_ulong), 'pceltFetched')),
            COMMETHOD([], HRESULT, 'Skip', (['in'], c_ulong, 'celt')),
            COMMETHOD([], HRESULT, 'Reset'),
            COMMETHOD([], HRESULT, 'Clone', (['in'], c_void_p, 'ppenum')),
        ]

    class ICreateDevEnum(IUnknown):
        _iid_ = GUID('{29840822-5B84-11D0-BD3B-00A0C911CE86}')
        _methods_ = [
            COMMETHOD([], HRESULT, 'CreateClassEnumerator',
                      (['in'], POINTER(GUID), 'clsidDeviceClass'),
                      (['out'], POINTER(POINTER(IEnumMoniker)), 'ppEnumMoniker'),
                      (['in'], c_ulong, 'dwFlags')),
        ]

    class IAMVideoProcAmp(IUnknown):
        _iid_ = GUID('{C6E13360-30AC-11D0-A18C-00A0C9118956}')
        _methods_ = [
            COMMETHOD([], HRESULT, 'GetRange',
                      (['in'], c_long, 'Property'),
                      (['out'], POINTER(c_long), 'pMin'),
                      (['out'], POINTER(c_long), 'pMax'),
                      (['out'], POINTER(c_long), 'pSteppingDelta'),
                      (['out'], POINTER(c_long), 'pDefault'),
                      (['out'], POINTER(c_long), 'pCapsFlags')),
            COMMETHOD([], HRESULT, 'Set',
                      (['in'], c_long, 'Property'),
                      (['in'], c_long, 'lValue'),
                      (['in'], c_long, 'Flags')),
            COMMETHOD([], HRESULT, 'Get',
                      (['in'], c_long, 'Property'),
                      (['out'], POINTER(c_long), 'lValue'),
                      (['out'], POINTER(c_long), 'Flags')),
        ]

    class IAMCameraControl(IUnknown):
        _iid_ = GUID('{C6E13370-30AC-11D0-A18C-00A0C9118956}')
        _methods_ = [
            COMMETHOD([], HRESULT, 'GetRange',
                      (['in'], c_long, 'Property'),
                      (['out'], POINTER(c_long), 'pMin'),
                      (['out'], POINTER(c_long), 'pMax'),
                      (['out'], POINTER(c_long), 'pSteppingDelta'),
                      (['out'], POINTER(c_long), 'pDefault'),
                      (['out'], POINTER(c_long), 'pCapsFlags')),
            COMMETHOD([], HRESULT, 'Set',
                      (['in'], c_long, 'Property'),
                      (['in'], c_long, 'lValue'),
                      (['in'], c_long, 'Flags')),
            COMMETHOD([], HRESULT, 'Get',
                      (['in'], c_long, 'Property'),
                      (['out'], POINTER(c_long), 'lValue'),
                      (['out'], POINTER(c_long), 'Flags')),
        ]

    # Property tables: (id, display name)
    VIDEOPROCAMP_PROPS = [
        (0, "Brightness"),
        (1, "Contrast"),
        (2, "Hue"),
        (3, "Saturation"),
        (4, "Sharpness"),
        (5, "Gamma"),
        (6, "Color Enable"),
        (7, "White Balance"),
        (8, "Backlight Comp"),
        (9, "Gain"),
    ]
    CAMERACONTROL_PROPS = [
        (0, "Pan"),
        (1, "Tilt"),
        (2, "Roll"),
        (3, "Zoom"),
        (4, "Exposure"),
        (5, "Iris"),
        (6, "Focus"),
    ]
    FLAG_AUTO = 0x0001
    FLAG_MANUAL = 0x0002


def enumerate_dshow_devices():
    """Return list of (index, friendly_name, filter_IUnknown).

    Enumeration order matches OpenCV's CAP_DSHOW index order, so index i
    here corresponds to cv2.VideoCapture(i, cv2.CAP_DSHOW).
    """
    devices = []
    if not DSHOW_AVAILABLE:
        return devices
    try:
        dev_enum = comtypes.CoCreateInstance(
            CLSID_SystemDeviceEnum,
            interface=ICreateDevEnum,
            clsctx=comtypes.CLSCTX_INPROC_SERVER,
        )
        enum_moniker = dev_enum.CreateClassEnumerator(
            CLSID_VideoInputDeviceCategory, 0)
        if not enum_moniker:
            return devices
        idx = 0
        while True:
            try:
                moniker, fetched = enum_moniker.Next(1)
            except Exception:
                break
            if not fetched or not moniker:
                break
            name = f"Camera {idx}"
            filt = None
            try:
                bag_unk = moniker.BindToStorage(None, None, IPropertyBag._iid_)
                bag = bag_unk.QueryInterface(IPropertyBag)
                var = bag.Read('FriendlyName', None)
                if var and var.value:
                    name = str(var.value)
            except Exception:
                pass
            try:
                filt = moniker.BindToObject(None, None, IID_IBaseFilter)
            except Exception:
                filt = None
            devices.append((idx, name, filt))
            idx += 1
    except Exception:
        pass
    return devices


class CameraProperties:
    """Wraps IAMVideoProcAmp + IAMCameraControl for one device filter."""

    def __init__(self, filter_unknown):
        self.proc_amp = None
        self.cam_ctrl = None
        if filter_unknown is None:
            return
        try:
            self.proc_amp = filter_unknown.QueryInterface(IAMVideoProcAmp)
        except Exception:
            pass
        try:
            self.cam_ctrl = filter_unknown.QueryInterface(IAMCameraControl)
        except Exception:
            pass

    def list_supported(self):
        """Yield dicts describing every property the camera supports."""
        out = []
        for iface, kind, table in (
            (self.proc_amp, 'amp', VIDEOPROCAMP_PROPS if DSHOW_AVAILABLE else []),
            (self.cam_ctrl, 'cam', CAMERACONTROL_PROPS if DSHOW_AVAILABLE else []),
        ):
            if iface is None:
                continue
            for pid, name in table:
                try:
                    mn, mx, step, default, caps = iface.GetRange(pid)
                except Exception:
                    continue
                if mx <= mn:
                    continue
                try:
                    value, flags = iface.Get(pid)
                except Exception:
                    value, flags = default, FLAG_MANUAL
                out.append({
                    'kind': kind, 'id': pid, 'name': name,
                    'min': mn, 'max': mx, 'step': max(1, step),
                    'default': default, 'caps': caps,
                    'value': value, 'auto': bool(flags & FLAG_AUTO),
                    'supports_auto': bool(caps & FLAG_AUTO),
                })
        return out

    def _iface(self, kind):
        return self.proc_amp if kind == 'amp' else self.cam_ctrl

    def set_value(self, kind, pid, value, auto):
        iface = self._iface(kind)
        if iface is None:
            return
        flags = FLAG_AUTO if auto else FLAG_MANUAL
        try:
            iface.Set(pid, int(value), flags)
        except Exception:
            pass

    def get_value(self, kind, pid):
        iface = self._iface(kind)
        if iface is None:
            return None, False
        try:
            value, flags = iface.Get(pid)
            return value, bool(flags & FLAG_AUTO)
        except Exception:
            return None, False


# --------------------------------------------------------------------------
# Capture thread
# --------------------------------------------------------------------------

class CameraThread(QThread):
    frame_ready = Signal(QImage)
    fps_update = Signal(float)
    error = Signal(str)
    opened = Signal(int, int, float)   # width, height, fps

    def __init__(self, device_index, width=0, height=0, parent=None):
        super().__init__(parent)
        self.device_index = device_index
        self.req_width = width
        self.req_height = height
        self._running = False
        self._lock = threading.Lock()
        self._latest_frame = None       # BGR numpy array
        self._writer = None             # cv2.VideoWriter or None
        self.actual_size = (0, 0)
        self.actual_fps = 30.0

    # ---- external API (called from GUI thread) ----

    def latest_frame(self):
        with self._lock:
            return None if self._latest_frame is None else self._latest_frame.copy()

    def start_recording(self, path):
        w, h = self.actual_size
        if w == 0:
            return False
        fps = self.actual_fps if 1.0 <= self.actual_fps <= 120.0 else 30.0
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'mp4v'),
                                 fps, (w, h))
        if not writer.isOpened():
            return False
        with self._lock:
            self._writer = writer
        return True

    def stop_recording(self):
        with self._lock:
            writer, self._writer = self._writer, None
        if writer is not None:
            writer.release()

    def stop(self):
        self._running = False
        self.wait(3000)

    # ---- thread body ----

    def run(self):
        backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
        cap = cv2.VideoCapture(self.device_index, backend)
        if not cap.isOpened():
            self.error.emit(f"Could not open camera {self.device_index}.")
            return
        # Best-effort MJPG for higher frame rates at large resolutions.
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        if self.req_width and self.req_height:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.req_width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.req_height)

        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        self.actual_size = (w, h)
        self.actual_fps = fps
        self.opened.emit(w, h, fps)

        self._running = True
        frame_count = 0
        t0 = time.monotonic()
        while self._running:
            ok, frame = cap.read()
            if not ok:
                self.error.emit("Frame grab failed; camera disconnected?")
                break
            with self._lock:
                self._latest_frame = frame
                if self._writer is not None:
                    self._writer.write(frame)
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            qimg = QImage(rgb.data, rgb.shape[1], rgb.shape[0],
                          rgb.strides[0], QImage.Format_RGB888).copy()
            self.frame_ready.emit(qimg)
            frame_count += 1
            now = time.monotonic()
            if now - t0 >= 1.0:
                self.fps_update.emit(frame_count / (now - t0))
                frame_count = 0
                t0 = now
        self.stop_recording()
        cap.release()


# --------------------------------------------------------------------------
# Widgets
# --------------------------------------------------------------------------

class ScaledImageLabel(QLabel):
    """QLabel that scales its pixmap to fit while preserving aspect ratio."""

    def __init__(self, placeholder_text="", parent=None):
        super().__init__(parent)
        self._pixmap = None
        self.setMinimumSize(160, 120)
        self.setAlignment(Qt.AlignCenter)
        self.setText(placeholder_text)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setStyleSheet(
            "background-color:#1b1b1b; color:#777; border:1px solid #333;")

    def set_image(self, qimg_or_pixmap):
        if isinstance(qimg_or_pixmap, QImage):
            self._pixmap = QPixmap.fromImage(qimg_or_pixmap)
        else:
            self._pixmap = qimg_or_pixmap
        self.update()

    def clear_image(self, text=""):
        self._pixmap = None
        self.setText(text)
        self.update()

    def paintEvent(self, event):
        if self._pixmap is None:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        scaled = self._pixmap.scaled(self.size(), Qt.KeepAspectRatio,
                                     Qt.SmoothTransformation)
        x = (self.width() - scaled.width()) // 2
        y = (self.height() - scaled.height()) // 2
        painter.drawPixmap(x, y, scaled)


class FullscreenViewer(QWidget):
    """Borderless fullscreen window showing one image stream.

    Close with Esc, double-click, or by un-toggling the button that
    opened it (the `closed` signal keeps the button state in sync).
    """
    closed = Signal()

    def __init__(self, title=""):
        super().__init__(None, Qt.Window | Qt.FramelessWindowHint)
        self.setWindowTitle(title)
        self.setStyleSheet("background-color:black;")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.image_label = ScaledImageLabel("")
        self.image_label.setStyleSheet("background-color:black;")
        layout.addWidget(self.image_label)
        hint = QLabel("Esc or double-click to exit fullscreen", self)
        hint.setStyleSheet(
            "color:#666; background:transparent; padding:6px;")
        hint.move(10, 10)

    def set_image(self, img):
        self.image_label.set_image(img)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, _event):
        self.close()

    def closeEvent(self, event):
        self.closed.emit()
        event.accept()


class PropertyRow:
    """One camera property: label + slider + value + auto checkbox."""

    def __init__(self, info, props, grid, row, col_offset):
        self.info = info
        self.props = props
        kind, pid = info['kind'], info['id']

        self.name_label = QLabel(info['name'])
        self.name_label.setMinimumWidth(105)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(info['min'], info['max'])
        self.slider.setSingleStep(info['step'])
        self.slider.setPageStep(max(info['step'],
                                    (info['max'] - info['min']) // 10 or 1))
        self.slider.setValue(info['value'])
        self.slider.setMinimumWidth(140)

        self.value_label = QLabel(str(info['value']))
        self.value_label.setMinimumWidth(52)
        self.value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.auto_check = QCheckBox("Auto")
        self.auto_check.setEnabled(info['supports_auto'])
        self.auto_check.setChecked(info['auto'])
        self.slider.setEnabled(not info['auto'])

        self.slider.valueChanged.connect(self._on_slider)
        self.auto_check.toggled.connect(self._on_auto)

        grid.addWidget(self.name_label, row, col_offset + 0)
        grid.addWidget(self.slider, row, col_offset + 1)
        grid.addWidget(self.value_label, row, col_offset + 2)
        grid.addWidget(self.auto_check, row, col_offset + 3)

    def _on_slider(self, value):
        self.value_label.setText(str(value))
        if not self.auto_check.isChecked():
            self.props.set_value(self.info['kind'], self.info['id'],
                                 value, auto=False)

    def _on_auto(self, checked):
        self.slider.setEnabled(not checked)
        self.props.set_value(self.info['kind'], self.info['id'],
                             self.slider.value(), auto=checked)

    def reset_default(self):
        self.auto_check.blockSignals(True)
        self.auto_check.setChecked(False)
        self.auto_check.blockSignals(False)
        self.slider.setEnabled(True)
        self.slider.setValue(self.info['default'])  # triggers Set(manual)

    def refresh_from_device(self):
        """When in auto mode, poll the driver so the slider tracks reality."""
        if not self.auto_check.isChecked():
            return
        value, _auto = self.props.get_value(self.info['kind'], self.info['id'])
        if value is None:
            return
        self.slider.blockSignals(True)
        self.slider.setValue(value)
        self.slider.blockSignals(False)
        self.value_label.setText(str(value))


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------

RESOLUTIONS = [
    ("Default", 0, 0),
    ("640 x 480", 640, 480),
    ("1280 x 720", 1280, 720),
    ("1920 x 1080", 1920, 1080),
    ("2560 x 1440", 2560, 1440),
    ("3840 x 2160", 3840, 2160),
]


class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Webcam Studio")
        self.resize(1280, 800)

        self.output_dir = Path.home() / "Pictures" / "WebcamStudio"
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.camera_thread = None
        self.camera_props = None
        self.property_rows = []
        self.devices = []           # (index, name, filter)
        self.recording = False
        self.record_start = None
        self.live_viewer = None       # FullscreenViewer or None
        self.capture_viewer = None    # FullscreenViewer or None
        self.last_capture_qimg = None

        self._build_toolbar()
        self._build_central()
        self._build_dock()
        self.statusBar().showMessage("Ready")

        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(1500)
        self.refresh_timer.timeout.connect(self._refresh_auto_props)

        self.record_timer = QTimer(self)
        self.record_timer.setInterval(500)
        self.record_timer.timeout.connect(self._update_record_status)

        self.refresh_devices()

    # ---- UI construction ----

    def _build_toolbar(self):
        tb = QToolBar("Main")
        tb.setMovable(False)
        tb.setIconSize(QSize(20, 20))
        self.addToolBar(tb)
        style = self.style()

        tb.addWidget(QLabel(" Device: "))
        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(220)
        self.device_combo.currentIndexChanged.connect(self._on_device_changed)
        tb.addWidget(self.device_combo)

        refresh_act = QAction(style.standardIcon(QStyle.SP_BrowserReload),
                              "Rescan devices", self)
        refresh_act.triggered.connect(self.refresh_devices)
        tb.addAction(refresh_act)

        tb.addSeparator()
        tb.addWidget(QLabel(" Resolution: "))
        self.res_combo = QComboBox()
        for name, _w, _h in RESOLUTIONS:
            self.res_combo.addItem(name)
        self.res_combo.currentIndexChanged.connect(self._on_device_changed)
        tb.addWidget(self.res_combo)

        tb.addSeparator()
        self.snap_btn = QPushButton(" Snapshot (Space)")
        self.snap_btn.setIcon(style.standardIcon(QStyle.SP_DialogSaveButton))
        self.snap_btn.clicked.connect(self.take_snapshot)
        tb.addWidget(self.snap_btn)
        snap_shortcut = QAction(self)
        snap_shortcut.setShortcut(QKeySequence(Qt.Key_Space))
        snap_shortcut.setShortcutContext(Qt.ApplicationShortcut)
        snap_shortcut.triggered.connect(self.take_snapshot)
        self.addAction(snap_shortcut)

        self.record_btn = QPushButton(" Record")
        self.record_btn.setCheckable(True)
        self.record_btn.setIcon(style.standardIcon(QStyle.SP_MediaPlay))
        self.record_btn.toggled.connect(self.toggle_recording)
        tb.addWidget(self.record_btn)

        tb.addSeparator()
        folder_btn = QPushButton(" Open Output Folder")
        folder_btn.setIcon(style.standardIcon(QStyle.SP_DirOpenIcon))
        folder_btn.clicked.connect(self._open_output_folder)
        tb.addWidget(folder_btn)

    def _build_central(self):
        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # Left: live preview
        live_box = QVBoxLayout()
        live_header = QHBoxLayout()
        live_title = QLabel("LIVE PREVIEW")
        live_title.setStyleSheet("font-weight:bold; color:#aaa;")
        self.live_fs_btn = QPushButton("Fullscreen")
        self.live_fs_btn.setCheckable(True)
        self.live_fs_btn.setToolTip("Show the live preview fullscreen")
        self.live_fs_btn.toggled.connect(self.toggle_live_fullscreen)
        live_header.addWidget(live_title)
        live_header.addStretch(1)
        live_header.addWidget(self.live_fs_btn)
        self.live_label = ScaledImageLabel("No camera")
        live_box.addLayout(live_header)
        live_box.addWidget(self.live_label, 1)

        # Right: last capture
        cap_box = QVBoxLayout()
        cap_header = QHBoxLayout()
        cap_title = QLabel("LAST CAPTURE")
        cap_title.setStyleSheet("font-weight:bold; color:#aaa;")
        self.cap_fs_btn = QPushButton("Fullscreen")
        self.cap_fs_btn.setCheckable(True)
        self.cap_fs_btn.setToolTip("Show the last capture fullscreen")
        self.cap_fs_btn.toggled.connect(self.toggle_capture_fullscreen)
        cap_header.addWidget(cap_title)
        cap_header.addStretch(1)
        cap_header.addWidget(self.cap_fs_btn)
        self.capture_label = ScaledImageLabel("No capture yet")
        self.capture_filename = QLabel("")
        self.capture_filename.setStyleSheet("color:#888;")
        self.capture_filename.setAlignment(Qt.AlignCenter)
        cap_box.addLayout(cap_header)
        cap_box.addWidget(self.capture_label, 1)
        cap_box.addWidget(self.capture_filename)

        layout.addLayout(live_box, 3)
        layout.addLayout(cap_box, 2)
        self.setCentralWidget(central)

    def _build_dock(self):
        self.dock = QDockWidget("Camera Controls", self)
        self.dock.setAllowedAreas(Qt.BottomDockWidgetArea | Qt.TopDockWidgetArea)
        self.dock.setFeatures(QDockWidget.DockWidgetMovable |
                              QDockWidget.DockWidgetFloatable)

        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(6, 6, 6, 6)

        top_row = QHBoxLayout()
        self.controls_status = QLabel("")
        self.controls_status.setStyleSheet("color:#888;")
        reset_btn = QPushButton("Reset All to Defaults")
        reset_btn.clicked.connect(self._reset_all_props)
        top_row.addWidget(self.controls_status)
        top_row.addStretch(1)
        top_row.addWidget(reset_btn)
        outer.addLayout(top_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        self.controls_widget = QWidget()
        self.controls_grid = QGridLayout(self.controls_widget)
        self.controls_grid.setHorizontalSpacing(10)
        self.controls_grid.setVerticalSpacing(4)
        scroll.setWidget(self.controls_widget)
        outer.addWidget(scroll)

        self.dock.setWidget(container)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.dock)
        self.dock.setMinimumHeight(190)

    # ---- device management ----

    def refresh_devices(self):
        self._stop_camera()
        self.devices = enumerate_dshow_devices()
        if not self.devices:
            # Fallback: probe indices blindly (also covers non-Windows dev).
            for i in range(5):
                backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
                cap = cv2.VideoCapture(i, backend)
                if cap.isOpened():
                    self.devices.append((i, f"Camera {i}", None))
                cap.release()
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for _idx, name, _filt in self.devices:
            self.device_combo.addItem(name)
        self.device_combo.blockSignals(False)
        if self.devices:
            self._on_device_changed()
        else:
            self.live_label.clear_image("No cameras found")
            self.statusBar().showMessage("No cameras found")

    def _on_device_changed(self, *_):
        if not self.devices:
            return
        if self.recording:
            self.record_btn.setChecked(False)
        self._stop_camera()

        combo_i = max(0, self.device_combo.currentIndex())
        dev_index, dev_name, dev_filter = self.devices[combo_i]
        _res_name, w, h = RESOLUTIONS[max(0, self.res_combo.currentIndex())]

        self.camera_props = CameraProperties(dev_filter)
        self._rebuild_property_rows()

        self.camera_thread = CameraThread(dev_index, w, h)
        self.camera_thread.frame_ready.connect(self._on_live_frame)
        self.camera_thread.fps_update.connect(self._on_fps)
        self.camera_thread.error.connect(self._on_camera_error)
        self.camera_thread.opened.connect(self._on_camera_opened)
        self.camera_thread.start()
        self.statusBar().showMessage(f"Opening {dev_name}...")
        self.refresh_timer.start()

    def _stop_camera(self):
        self.refresh_timer.stop()
        if self.camera_thread is not None:
            self.camera_thread.stop()
            self.camera_thread = None

    def _on_camera_opened(self, w, h, fps):
        name = self.device_combo.currentText()
        self.statusBar().showMessage(f"{name}  |  {w}x{h} @ {fps:.0f} fps nominal")

    def _on_camera_error(self, msg):
        self.statusBar().showMessage(msg)
        self.live_label.clear_image(msg)

    def _on_fps(self, fps):
        if not self.recording:
            base = self.statusBar().currentMessage().split("  ||")[0]
            self.statusBar().showMessage(f"{base}  ||  {fps:.1f} fps actual")

    # ---- property dock ----

    def _rebuild_property_rows(self):
        while self.controls_grid.count():
            item = self.controls_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.property_rows = []

        if not DSHOW_AVAILABLE:
            self.controls_status.setText(
                "DirectShow controls unavailable (Windows + comtypes required).")
            return
        if self.camera_props is None:
            return

        infos = self.camera_props.list_supported()
        if not infos:
            self.controls_status.setText(
                "This camera exposes no adjustable DirectShow properties.")
            return
        self.controls_status.setText(
            f"{len(infos)} camera properties reported by driver")

        # Two-column layout of property rows.
        rows_per_col = math.ceil(len(infos) / 2)
        for i, info in enumerate(infos):
            col_block = i // rows_per_col
            row = i % rows_per_col
            col_offset = col_block * 5  # 4 widgets + 1 spacer column
            if col_block > 0:
                spacer = QLabel("  ")
                self.controls_grid.addWidget(spacer, row, col_offset - 1)
            self.property_rows.append(
                PropertyRow(info, self.camera_props,
                            self.controls_grid, row, col_offset))

    def _reset_all_props(self):
        for row in self.property_rows:
            row.reset_default()

    def _refresh_auto_props(self):
        for row in self.property_rows:
            row.refresh_from_device()

    # ---- fullscreen ----

    def _on_live_frame(self, qimg):
        self.live_label.set_image(qimg)
        if self.live_viewer is not None:
            self.live_viewer.set_image(qimg)

    def toggle_live_fullscreen(self, checked):
        if checked:
            if self.live_viewer is None:
                self.live_viewer = FullscreenViewer("Live Preview")
                self.live_viewer.closed.connect(self._live_viewer_closed)
            self.live_viewer.showFullScreen()
        elif self.live_viewer is not None:
            self.live_viewer.close()

    def _live_viewer_closed(self):
        self.live_viewer = None
        self.live_fs_btn.blockSignals(True)
        self.live_fs_btn.setChecked(False)
        self.live_fs_btn.blockSignals(False)

    def toggle_capture_fullscreen(self, checked):
        if checked:
            if self.last_capture_qimg is None:
                self.statusBar().showMessage("No capture to show yet")
                self.cap_fs_btn.blockSignals(True)
                self.cap_fs_btn.setChecked(False)
                self.cap_fs_btn.blockSignals(False)
                return
            if self.capture_viewer is None:
                self.capture_viewer = FullscreenViewer("Last Capture")
                self.capture_viewer.closed.connect(self._capture_viewer_closed)
            self.capture_viewer.set_image(self.last_capture_qimg)
            self.capture_viewer.showFullScreen()
        elif self.capture_viewer is not None:
            self.capture_viewer.close()

    def _capture_viewer_closed(self):
        self.capture_viewer = None
        self.cap_fs_btn.blockSignals(True)
        self.cap_fs_btn.setChecked(False)
        self.cap_fs_btn.blockSignals(False)

    # ---- snapshot & recording ----

    def take_snapshot(self):
        if self.camera_thread is None:
            return
        frame = self.camera_thread.latest_frame()
        if frame is None:
            self.statusBar().showMessage("No frame available yet")
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.output_dir / f"snap_{stamp}.png"
        ok = cv2.imwrite(str(path), frame)
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        qimg = QImage(rgb.data, rgb.shape[1], rgb.shape[0],
                      rgb.strides[0], QImage.Format_RGB888).copy()
        self.last_capture_qimg = qimg
        self.capture_label.set_image(qimg)
        if self.capture_viewer is not None:
            self.capture_viewer.set_image(qimg)
        if ok:
            self.capture_filename.setText(path.name)
            self.statusBar().showMessage(f"Saved {path}")
        else:
            self.capture_filename.setText("(save failed)")
            self.statusBar().showMessage(f"Could not write {path}")

    def toggle_recording(self, checked):
        style = self.style()
        if checked:
            if self.camera_thread is None:
                self.record_btn.setChecked(False)
                return
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = self.output_dir / f"video_{stamp}.mp4"
            if not self.camera_thread.start_recording(path):
                self.record_btn.setChecked(False)
                QMessageBox.warning(self, "Recording",
                                    "Could not start the video writer.")
                return
            self.recording = True
            self.record_start = time.monotonic()
            self.current_video_path = path
            self.record_btn.setText(" Stop")
            self.record_btn.setIcon(style.standardIcon(QStyle.SP_MediaStop))
            self.record_btn.setStyleSheet(
                "background-color:#8b1a1a; color:white;")
            self.device_combo.setEnabled(False)
            self.res_combo.setEnabled(False)
            self.record_timer.start()
        else:
            if self.camera_thread is not None:
                self.camera_thread.stop_recording()
            if self.recording:
                self.statusBar().showMessage(
                    f"Saved {getattr(self, 'current_video_path', '')}")
            self.recording = False
            self.record_timer.stop()
            self.record_btn.setText(" Record")
            self.record_btn.setIcon(style.standardIcon(QStyle.SP_MediaPlay))
            self.record_btn.setStyleSheet("")
            self.device_combo.setEnabled(True)
            self.res_combo.setEnabled(True)

    def _update_record_status(self):
        if self.recording and self.record_start is not None:
            elapsed = int(time.monotonic() - self.record_start)
            m, s = divmod(elapsed, 60)
            self.statusBar().showMessage(f"REC  {m:02d}:{s:02d}")

    def _open_output_folder(self):
        import subprocess
        if sys.platform == "win32":
            subprocess.Popen(["explorer", str(self.output_dir)])
        else:
            subprocess.Popen(["xdg-open", str(self.output_dir)])

    # ---- shutdown ----

    def closeEvent(self, event):
        if self.live_viewer is not None:
            self.live_viewer.close()
        if self.capture_viewer is not None:
            self.capture_viewer.close()
        self._stop_camera()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
