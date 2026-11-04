"""Uploading frames to the Kraken LCD through liquidctl.

Only the screen is touched; pump and fan control is left to whatever else you
use (CoolerControl on the kernel driver, the cooler's firmware, ...).

The upload safeguards -- catching liquidctl's logged-but-swallowed bucket
failures, refusing to switch to a bucket whose setup failed, flushing HID
desyncs, clear-and-retry, reconnect -- are adapted from OpenKraken by David
Boulay (MIT License, https://github.com/davidboulay/OpenKraken).
"""

import logging
import os
import tempfile
from pathlib import Path

from PIL import Image

LOG = logging.getLogger("kraken-lcd")

NZXT = 0x1E71
PLAIN_RETRIES = 2
CLEAR_RETRIES = 2
RECONNECT_AFTER = 4
DESYNC_MARKER = "missing messages"
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir()))


class _BucketWatcher(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.failed = False

    def emit(self, record):
        msg = record.getMessage()
        if "Failed to setup bucket" in msg or "Failed to switch active bucket" in msg:
            self.failed = True


def find_screens():
    """All connected NZXT coolers liquidctl can draw on."""
    from liquidctl import find_liquidctl_devices
    return [d for d in find_liquidctl_devices(vendor=NZXT)
            if hasattr(d, "set_screen") and hasattr(d, "lcd_resolution")]


class Screen:
    def __init__(self, cfg):
        self.cfg = cfg
        self.dev = None
        self.gif_supported = True
        self.watcher = _BucketWatcher()
        logging.getLogger("liquidctl.driver.kraken3").addHandler(self.watcher)
        self._guard_active = False
        self._setup_ok = {}
        self.fails = 0
        self._applied = {}

    @property
    def resolution(self):
        return self.dev.lcd_resolution[0] if self.dev is not None else 640

    # ---------------------------------------------------------------- connection
    def connect(self):
        self.close()
        for dev in find_screens():
            dev.connect()
            try:
                for k, v, u in dev.initialize() or []:
                    if k in ("Firmware version", "LCD Orientation", "LCD Brightness"):
                        LOG.info("%s: %s%s", k, v, u)
            except Exception as exc:
                LOG.warning("initialize() failed (%s); carrying on", exc)
            self._install_guard(dev)
            self._fast_prepare(dev)
            self.dev, self.fails, self._applied = dev, 0, {}
            self.gif_supported = True
            LOG.info("connected to %s (%dx%d screen)", dev.description, *dev.lcd_resolution)
            self.apply_settings()
            return True
        LOG.warning("no NZXT cooler with a screen found")
        return False

    def close(self):
        if self.dev is not None:
            try:
                self.dev.disconnect()
            except Exception:
                pass
        self.dev = None

    def apply_settings(self):
        """Brightness / rotation, only when they change."""
        for mode in ("brightness", "orientation"):
            value = self.cfg.get(mode)
            if value is None or self._applied.get(mode) == value:
                continue
            try:
                self._call(mode, int(value))
                self._applied[mode] = value
                LOG.info("set %s to %s", mode, value)
            except Exception as exc:
                LOG.warning("couldn't set %s: %s", mode, exc)

    def liquid_temp(self):
        if self.dev is None:
            return None
        for k, v, _ in self.dev.get_status():
            if k.lower().startswith("liquid temp"):
                return float(v)
        return None

    # ---------------------------------------------------------------- driver hooks
    def _install_guard(self, dev):
        """Never switch the panel to a bucket whose setup failed (avoids black flashes)."""
        if not (hasattr(dev, "_setup_bucket") and hasattr(dev, "_switch_bucket")):
            return
        orig_setup, orig_switch = dev._setup_bucket, dev._switch_bucket

        def setup(start, end, mem_start, mem_size):
            ok = orig_setup(start, end, mem_start, mem_size)
            if self._guard_active:
                self._setup_ok[start] = bool(ok)
            return ok

        def switch(index, mode=0x4):
            # mode 0x2 (liquid) must always pass: clearing buckets needs it
            if self._guard_active and mode != 0x2 and not self._setup_ok.get(index, True):
                return False
            return orig_switch(index, mode)

        dev._setup_bucket, dev._switch_bucket = setup, switch

    @staticmethod
    def _fast_prepare(dev):
        """liquidctl converts every pixel in a Python loop; do it in C instead.
        Same output (R, G, B, 0 per pixel, rotated to the panel orientation)."""
        if not hasattr(dev, "_prepare_static_file"):
            return

        def prepare(path, rotation):
            img = Image.open(path).resize(dev.lcd_resolution).rotate(rotation * -90).convert("RGB")
            return Image.merge("RGBA", (*img.split(), Image.new("L", img.size, 0))).tobytes()

        dev._prepare_static_file = prepare

    def _flush(self):
        try:
            self.dev.device.clear_enqueued_reports()
        except Exception:
            pass

    def _call(self, mode, value):
        for attempt in range(3):
            try:
                self.dev.set_screen("lcd", mode, value)
                return
            except AssertionError as exc:
                if DESYNC_MARKER not in str(exc) or attempt == 2:
                    raise
                self._flush()

    # ---------------------------------------------------------------- uploads
    def push_still(self, img):
        path = RUNTIME_DIR / "kraken-lcd-frame.png"
        img.save(path)
        return self._push("static", path)

    def push_gif(self, data):
        path = RUNTIME_DIR / "kraken-lcd-frame.gif"
        path.write_bytes(data)
        return self._push("gif", path)

    def _push(self, mode, path):
        if self.dev is None and not self.connect():
            return False
        self._guard_active = True
        self._setup_ok.clear()
        try:
            ok = self._upload(mode, str(path))
        except (OSError, ValueError) as exc:            # USB gone: unplugged, suspend
            LOG.warning("USB error (%s); will reconnect", exc)
            self.close()
            return False
        except AssertionError as exc:
            LOG.warning("upload rejected: %s", exc)
            if DESYNC_MARKER in str(exc):
                self._flush()
            ok = False
        except Exception as exc:
            if mode == "gif" and type(exc).__name__ == "NotSupportedByDriver":
                LOG.warning("this cooler's firmware can't show GIFs; showing the first frame instead")
                self.gif_supported = False
                return False
            LOG.exception("upload failed: %s", exc)
            self.close()
            return False
        finally:
            self._guard_active = False
        if ok:
            self.fails = 0
        else:
            self.fails += 1
            if self.fails >= RECONNECT_AFTER:
                LOG.warning("%d uploads failed in a row; reconnecting", self.fails)
                self.close()
        return ok

    def _upload(self, mode, path):
        self.watcher.failed = False
        self._call(mode, path)
        if not self.watcher.failed:
            return True
        for n in range(1, PLAIN_RETRIES + 1):
            self.watcher.failed = False
            self._call(mode, path)
            if not self.watcher.failed:
                LOG.info("upload recovered on retry %d", n)
                return True
        for _ in range(CLEAR_RETRIES):
            LOG.info("LCD memory looks full; clearing it")
            self.dev._delete_all_buckets()
            self.watcher.failed = False
            self._call(mode, path)
            if not self.watcher.failed:
                LOG.info("upload recovered after clearing LCD memory")
                return True
        LOG.warning("upload failed after all retries; keeping the last good frame")
        return False

    def liquid(self):
        if self.dev is None and not self.connect():
            return
        try:
            self._call("liquid", None)
            LOG.info("screen back to the cooler's liquid temperature")
        except Exception as exc:
            LOG.warning("couldn't switch to liquid mode: %s", exc)
