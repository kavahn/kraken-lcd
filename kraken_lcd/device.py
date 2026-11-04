"""Uploading frames to the Kraken LCD.

Only the screen is touched; pump and fan control is left to whatever else you
use (CoolerControl on the kernel driver, the cooler's firmware, ...).

On the Kraken 2024 Elite (1e71:3012) frames go through our own uploader:
two fixed memory slots used in turn, and every command's reply matched by
its prefix. liquidctl's generic path reads "the next report" as the reply,
and with the kernel driver bound that is often an unrelated status report,
so uploads look failed when they weren't; it also scans all 16 slots per
upload and wipes the memory (briefly showing the liquid screen) when it
fills up. Other models use liquidctl's path with the safeguards adapted
from OpenKraken by David Boulay (MIT License,
https://github.com/davidboulay/OpenKraken).
"""

import logging
import math
import os
import tempfile
import time
from pathlib import Path

from PIL import Image

LOG = logging.getLogger("kraken-lcd")

NZXT = 0x1E71
PLAIN_RETRIES = 2
CLEAR_RETRIES = 2
RECONNECT_AFTER = 4
DESYNC_MARKER = "missing messages"
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir()))

FAST_PRODUCTS = {0x3012}          # tested with the two-slot uploader
LCD_MEMORY_KB = 24320
SLOTS = 3                         # on screen, staged resting image, glide
SLOT_KB = LCD_MEMORY_KB // SLOTS
BULK_HEADER = [0x12, 0xFA, 0x01, 0xE8, 0xAB, 0xCD, 0xEF, 0x98, 0x76, 0x54, 0x32, 0x10]
KIND_STILL, KIND_GIF = 0x02, 0x01


class LCDError(Exception):
    """The cooler refused a step or didn't answer."""


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
        self.fast = False
        self.shown_slot = None
        self.orientation = 0
        self.last_upload_s = None
        self._staged = None

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
            self.fast = dev.product_id in FAST_PRODUCTS and getattr(dev, "bulk_device", None) is not None
            LOG.info("connected to %s (%dx%d screen)", dev.description, *dev.lcd_resolution)
            self.apply_settings()
            if self.fast:
                try:
                    self._read_orientation()
                    self._fast_reset()
                except (LCDError, OSError) as exc:
                    LOG.warning("fast uploader unavailable (%s); using liquidctl's", exc)
                    self.fast = False
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
                if mode == "orientation" and self.fast:
                    self._read_orientation()
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

    # ---------------------------------------------------------------- fast uploader
    def _cmd(self, data, timeout=1.0):
        """Send one HID command and wait for *its* reply (first byte + 1, same second
        byte), skipping the status reports the kernel driver keeps the cooler sending."""
        from liquidctl.error import Timeout
        hid = self.dev.device
        hid.clear_enqueued_reports()
        hid.write(list(data) + [0] * (64 - len(data)))
        want = bytes([data[0] + 1, data[1]])
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                break
            try:
                msg = hid.read(64, timeout=max(1, int(left * 1000)))
            except Timeout:
                break
            if bytes(msg[:2]) == want:
                return msg
        raise LCDError(f"no reply to {data[0]:02x} {data[1]:02x}")

    def _read_orientation(self):
        msg = self._cmd([0x30, 0x01])
        self.orientation = msg[0x1A] if msg[0x1A] in (0, 1, 2, 3) else 0
        LOG.debug("LCD orientation %d°, brightness %d%%", self.orientation * 90, msg[0x18])

    def _fast_reset(self):
        """Clean slate: liquid screen for a moment, then all 16 slots emptied."""
        self._cmd([0x38, 0x01, 0x02, 0x00])
        for b in range(16):
            self._cmd([0x32, 0x02, b])
        self.shown_slot = None
        self._staged = None

    def _fast_stage(self, payload, kind):
        """Send a frame into a slot that is neither on screen nor holding a staged
        frame. Returns that slot."""
        busy = {self.shown_slot, self._staged[0] if self._staged else None}
        slot = next(i for i in range(SLOTS) if i not in busy)
        header = BULK_HEADER + [kind, 0, 0, 0] + list(len(payload).to_bytes(4, "little"))
        size_kb = math.ceil((len(header) + len(payload)) / 1024)
        if size_kb > SLOT_KB:
            raise ValueError(f"frame is {size_kb} KB, the limit is {SLOT_KB} KB")
        offset = slot * SLOT_KB
        self._cmd([0x36, 0x03])
        self._cmd([0x32, 0x02, slot])
        if self._cmd([0x32, 0x01, slot, slot + 1, offset & 0xFF, offset >> 8,
                      size_kb & 0xFF, size_kb >> 8, 0x01])[14] != 1:
            raise LCDError("slot setup refused")
        self._cmd([0x36, 0x01, slot])
        bulk, chunk = self.dev.bulk_device, self.dev.bulk_buffer_size
        bulk.write(0x2, header)
        for i in range(0, len(payload), chunk):
            bulk.write(0x2, payload[i:i + chunk])
        self.dev.device.write([0x36, 0x02] + [0] * 62)
        return slot

    def _fast_switch(self, slot):
        if self._cmd([0x38, 0x01, 0x04, slot])[14] != 1:
            raise LCDError("switch refused")
        self.shown_slot = slot

    def _fast_push(self, payload, kind, switch=True):
        """Upload with retries. switch=False leaves the frame staged for commit()."""
        if self.dev is None and not self.connect():
            return False
        t0 = time.monotonic()
        if not switch:
            self._staged = None                     # replacing an older staged frame
        for attempt in range(3):
            try:
                slot = self._fast_stage(payload, kind)
                if switch:
                    self._fast_switch(slot)
                else:
                    self._staged = (slot, payload, kind)
                self.fails = 0
                self.last_upload_s = time.monotonic() - t0
                if attempt:
                    LOG.info("upload recovered on attempt %d", attempt + 1)
                return True
            except LCDError as exc:
                LOG.info("upload hiccup: %s", exc)
                if attempt == 1:
                    try:
                        self._fast_reset()
                        self._staged = None
                    except LCDError:
                        pass
            except ValueError as exc:
                LOG.warning("%s", exc)
                return False
            except OSError as exc:                      # USB gone: unplugged, suspend
                LOG.warning("USB error (%s); will reconnect", exc)
                self.close()
                return False
        self.fails += 1
        if self.fails >= RECONNECT_AFTER:
            LOG.warning("%d uploads failed in a row; reconnecting", self.fails)
            self.close()
        return False

    def commit(self):
        """Show the staged frame now. If that fails, upload it again the normal way."""
        if not self._staged or self.dev is None:
            return False
        slot, payload, kind = self._staged
        self._staged = None
        try:
            self._fast_switch(slot)
            return True
        except LCDError as exc:
            LOG.info("switch hiccup: %s; re-sending", exc)
            return self._fast_push(payload, kind)
        except OSError as exc:
            LOG.warning("USB error (%s); will reconnect", exc)
            self.close()
            return False

    def _still_payload(self, img):
        img = img.convert("RGB").resize(self.dev.lcd_resolution).rotate(self.orientation * -90)
        return Image.merge("RGBA", (*img.split(), Image.new("L", img.size, 0))).tobytes()

    # ---------------------------------------------------------------- uploads
    def push_still(self, img, stage=False):
        """stage=True sends the frame but waits for commit() to show it."""
        if self.dev is None and not self.connect():
            return False
        if self.fast:
            return self._fast_push(self._still_payload(img), KIND_STILL, switch=not stage)
        path = RUNTIME_DIR / "kraken-lcd-frame.png"
        img.save(path)
        return self._push("static", path)

    def push_gif(self, data, stage=False, rotated=False):
        """rotated=True: the frames were already drawn rotated to self.orientation."""
        if self.dev is None and not self.connect():
            return False
        path = RUNTIME_DIR / "kraken-lcd-frame.gif"
        path.write_bytes(data)
        if self.fast:
            if self.orientation and not rotated:
                data = self.dev._prepare_gif_file(str(path), self.orientation)
            return self._fast_push(bytes(data), KIND_GIF, switch=not stage)
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
