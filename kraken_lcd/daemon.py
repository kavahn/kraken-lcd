"""The background service: keeps the Kraken screen up to date.

  kraken-lcd                    run (normally started by the systemd user service)
  kraken-lcd --preview OUT.png  draw one frame to a file (OUT.gif for GIF backgrounds); cooler untouched
  kraken-lcd --once             draw + upload one frame, then exit
  kraken-lcd --liquid           put the screen back to the cooler's own liquid-temperature screen
  kraken-lcd --list             show detected coolers
"""

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
import tomllib

from . import __version__, config, sensors
from .render import Renderer

LOG = logging.getLogger("kraken-lcd")


def _warn_about_conflicts():
    """Two programs talking liquidctl to the same cooler garble each other's replies."""
    try:
        with open("/etc/coolercontrol/config.toml", "rb") as f:
            cc = tomllib.load(f)
        if cc.get("settings", {}).get("liquidctl_integration", True):
            if subprocess.run(["systemctl", "is-active", "--quiet", "coolercontrold"]).returncode == 0:
                LOG.warning("CoolerControl has liquidctl integration ON. It will fight kraken-lcd over the "
                            "cooler. Turn it off in CoolerControl > Settings (fans keep working through the "
                            "kernel driver). See the README.")
    except (OSError, tomllib.TOMLDecodeError):
        pass
    try:
        if subprocess.run(["pgrep", "-f", "openkraken"], capture_output=True).returncode == 0:
            LOG.warning("OpenKraken is running; it also drives the screen. Close it to avoid conflicts.")
    except OSError:
        pass


GLIDE_SECONDS = 1.6   # how long a ring takes to move to a new value
SMOOTH_STEP = 0.05    # pause between in-between frames (uploads take ~0.6 s on their own)
SNAP = 0.005          # changes smaller than this don't start a new glide


def _ease_out(x):
    return 1 - (1 - x) ** 3


class Glide:
    """Fixed-length, gently easing glide from where each ring is to where it should be.

    In-between frames upload in ~0.15 s on the Elite V2 (whole GIFs take ~0.9 s),
    so a still-picture glide gets 6-8 frames. A fixed duration with an ease-out
    keeps the steps even-ish, without the long tail of tiny steps an exponential
    ease would spend uploads on. A new target mid-glide starts a fresh glide
    from the current spot."""

    def __init__(self):
        self.tweens = {}        # key -> (start, end, t0)

    def snap(self, targets, now):
        self.tweens = {k: (v, v, now - GLIDE_SECONDS) for k, v in targets.items()}

    def _at(self, key, now):
        a, b, t0 = self.tweens[key]
        return a + (b - a) * _ease_out(min(1.0, max(0.0, (now - t0) / GLIDE_SECONDS)))

    def update(self, targets, now):
        if self.tweens.keys() != targets.keys():
            self.snap(targets, now)
        for key, t in targets.items():
            if abs(self.tweens[key][1] - t) >= SNAP:
                self.tweens[key] = (self._at(key, now), t, now)
        return {key: self._at(key, now) for key in targets}

    def moving(self, now):
        return any(now - t0 < GLIDE_SECONDS and a != b for a, b, t0 in self.tweens.values())


def _sleep(seconds, stop):
    end = time.monotonic() + seconds
    while not stop() and time.monotonic() < end:
        time.sleep(0.1)


def run():
    from .device import Screen

    _warn_about_conflicts()
    cfg = config.load()
    screen = Screen(cfg)
    screen.connect()
    renderer = Renderer(cfg, screen.resolution)

    stopping = False

    def on_signal(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    stop = lambda: stopping

    last_key, last_push, backoff = None, 0.0, 2.0
    size = screen.resolution
    glide = Glide()
    temps, sampled_at = None, 0.0
    gif_phase, gif_lead = None, 1.0     # (animation ms at last switch, when); render+upload time
    while not stopping:
        try:
            new_cfg = config.load()
        except (OSError, tomllib.TOMLDecodeError) as exc:
            LOG.warning("config.toml can't be read (%s); keeping the previous settings", exc)
            new_cfg = cfg
        if new_cfg != cfg or screen.resolution != size:
            if new_cfg != cfg:
                LOG.info("settings changed")
            cfg, size = new_cfg, screen.resolution
            renderer = Renderer(cfg, size)
            screen.cfg = cfg
            if screen.dev is not None:
                screen.apply_settings()
            last_key, temps, gif_phase = None, None, None

        now = time.monotonic()
        animated = renderer.animated and screen.gif_supported
        # On GIF backgrounds every change costs a whole GIF upload, so temps are only
        # re-read every gif_refresh seconds (and never in the middle of a glide).
        if (temps is None or not animated
                or (not glide.moving(now) and now - sampled_at >= cfg["gif_refresh"])):
            temps, sampled_at = sensors.read_all(cfg, liquid_fallback=screen.liquid_temp), now
        targets = renderer.ring_targets(temps)
        if cfg["smooth"] and last_key is not None:
            rings = glide.update(targets, now)
        else:
            glide.snap(targets, now)
            rings = dict(targets)
        moving = glide.moving(now)
        shown = renderer.texts(temps)
        key = (tuple(shown[s] for s in cfg["show"]), tuple(round(v, 3) for v in rings.values()),
               renderer._bg_key, animated)
        keepalive = max(300.0, cfg["keepalive"]) if animated else cfg["keepalive"]
        due = key != last_key or now - last_push >= keepalive

        if due:
            if animated:
                total = renderer.gif_length_ms or 1
                start = 0
                if gif_phase is not None:          # carry on from where the cooler is now
                    p0, t_switch = gif_phase
                    start = (p0 + (now + gif_lead - t_switch) * 1000) % total
                ok = screen.push_gif(renderer.gif(temps, rings=rings, start_ms=start))
                if ok:
                    t_done = time.monotonic()
                    gif_phase, gif_lead = (start, t_done), t_done - now
            else:
                gif_phase = None
                # in-between frames go as small GIFs; the frame the ring settles on is full colour
                ok = screen.push_still(renderer.still(temps, rings=rings), quick=moving)
            if ok:
                LOG.debug("uploaded %s in %.2fs", "gif" if animated else "frame", time.monotonic() - now)
                last_key, last_push, backoff = key, now, 2.0
            elif screen.dev is None:
                LOG.info("retrying in %.0fs", backoff)
                _sleep(backoff, stop)
                backoff = min(backoff * 2, 60)
                continue
        _sleep(SMOOTH_STEP if moving else float(cfg["interval"]), stop)

    screen.liquid()
    screen.close()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="kraken-lcd", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--preview", metavar="OUT")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--liquid", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=f"kraken-lcd {__version__}")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    if not args.verbose:
        logging.getLogger("liquidctl").setLevel(logging.WARNING)

    cfg = config.load()

    if args.preview:
        r = Renderer(cfg)
        temps = sensors.read_all(cfg)
        if args.preview.lower().endswith(".gif") and r.animated:
            with open(args.preview, "wb") as f:
                f.write(r.gif(temps))
        else:
            r.still(temps).save(args.preview)
        print(args.preview)
        return 0

    from .device import Screen, find_screens

    if args.list:
        found = find_screens()
        for d in found:
            print(f"{d.description}  ({d.vendor_id:04x}:{d.product_id:04x}, {d.lcd_resolution[0]}x{d.lcd_resolution[1]} screen)")
        if not found:
            print("No NZXT cooler with a screen found.")
        return 0 if found else 1

    if args.liquid:
        s = Screen(cfg)
        s.liquid()
        s.close()
        return 0

    if args.once:
        s = Screen(cfg)
        if not s.connect():
            return 1
        r = Renderer(cfg, s.resolution)
        temps = sensors.read_all(cfg, liquid_fallback=s.liquid_temp)
        t0 = time.monotonic()
        animated = r.animated and s.gif_supported
        ok = s.push_gif(r.gif(temps)) if animated else s.push_still(r.still(temps))
        LOG.info("%s upload %s in %.2fs", "GIF" if animated else "frame", "ok" if ok else "FAILED",
                 time.monotonic() - t0)
        s.close()
        return 0 if ok else 1

    return run()


if __name__ == "__main__":
    sys.exit(main())
