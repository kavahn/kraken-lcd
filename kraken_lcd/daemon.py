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
SNAP = 0.005          # ring changes smaller than this are just drawn, not animated


def _ease_out(x):
    return 1 - (1 - x) ** 3


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
    temps, sampled_at, rings_now = None, 0.0, None
    gif_phase = None                 # (animation ms, monotonic time) at the last switch

    def phase_at(t):
        if gif_phase is None:
            return 0
        p0, t0 = gif_phase
        return (p0 + (t - t0) * 1000) % (renderer.gif_length_ms or 1)

    def plain_push(animated):
        nonlocal gif_phase
        if animated:
            # The cooler pauses GIF playback while it receives data, so the new GIF
            # starts at the frame that was showing when the upload began: the
            # animation pauses and resumes instead of skipping ahead.
            data = renderer.gif(temps, start_fn=lambda: phase_at(time.monotonic()),
                                rotation=screen.orientation if screen.fast else 0)
            ok = screen.push_gif(data, rotated=screen.fast)
            if ok:
                gif_phase = (renderer.last_gif_start, time.monotonic())
            return ok
        gif_phase = None
        return screen.push_still(renderer.still(temps))

    def glide(frm, to):
        """Pictures only. Stage the full-colour resting image while the screen is
        still static (an upload pause can't be seen then), send the movement as
        one GIF the cooler plays by itself, and switch to the resting image the
        moment it ends -- nothing is uploaded while anything moves."""
        staged = screen.push_still(renderer.still(temps), stage=True)
        data, length_ms = renderer.glide_gif(temps, frm, to, GLIDE_SECONDS, _ease_out,
                                             rotation=screen.orientation)
        if not screen.push_gif(data, rotated=True):
            return plain_push(False)
        end = time.monotonic() + length_ms / 1000
        _sleep(max(0.0, end - time.monotonic()), stop)
        ok = screen.commit() if staged else plain_push(False)
        LOG.debug("glide %d ms", length_ms)
        return ok

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
            last_key, temps, gif_phase, rings_now = None, None, None, None

        now = time.monotonic()
        animated = renderer.animated and screen.gif_supported
        # On GIF backgrounds every change costs a whole GIF upload, so temps are
        # only re-read every gif_refresh seconds.
        if temps is None or not animated or now - sampled_at >= cfg["gif_refresh"]:
            temps, sampled_at = sensors.read_all(cfg, liquid_fallback=screen.liquid_temp), now
        targets = renderer.ring_targets(temps)
        shown = renderer.texts(temps)
        key = (tuple(shown[s] for s in cfg["show"]), tuple(round(v, 3) for v in targets.values()),
               renderer._bg_key, animated)
        keepalive = max(300.0, cfg["keepalive"]) if animated else cfg["keepalive"]

        if key != last_key or now - last_push >= keepalive:
            moved = (rings_now is not None and rings_now.keys() == targets.keys()
                     and max(abs(targets[k] - rings_now[k]) for k in targets) >= SNAP)
            # GIF backgrounds don't glide: every upload pauses their animation, and a
            # glide needs two GIF-sized uploads instead of one.
            if (cfg["smooth"] and screen.fast and not animated and moved
                    and last_key is not None and key[2:] == last_key[2:]):
                ok = glide(rings_now, targets)
            else:
                ok = plain_push(animated)
            if ok:
                LOG.debug("screen updated in %.2fs", time.monotonic() - now)
                last_key, last_push, backoff, rings_now = key, now, 2.0, dict(targets)
            elif screen.dev is None:
                LOG.info("retrying in %.0fs", backoff)
                _sleep(backoff, stop)
                backoff = min(backoff * 2, 60)
                continue
        _sleep(float(cfg["interval"]), stop)

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
