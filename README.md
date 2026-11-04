# kraken-lcd

Put a picture or an animated GIF **with live CPU / GPU / liquid temperatures** on
the screen of an NZXT Kraken AIO, on Linux. NZXT's own app (CAM) only runs on Windows;
this fills that gap with a minimal, good-looking watch-face style screen and a small
settings window.

![Three example screens](docs/faces.png)

- **Picture or GIF + temps at the same time.** One thin ring that fills as things
  heat up, a big number, and two small readings underneath.
- **Duotone look.** Your picture is recoloured in black, your accent colour and
  white, so any photo matches your build. Or keep its original colours.
- **Settings window** to pick the picture/GIF, colours, which temps to show,
  rotation and brightness, with a live round preview. It only edits a config file
  and closes completely: nothing extra stays running.
- **Screen only.** Pump and fans are left alone, so it works next to
  CoolerControl (see [below](#using-it-with-coolercontrol)) or the cooler's
  built-in curves.
- **Self-healing uploads.** Failed screen uploads are retried, the LCD memory is
  cleared when it fills up, and it reconnects after sleep or replugging.

![The settings window](docs/settings.png)

Not affiliated with NZXT.

## Supported coolers

| Cooler | USB ID | Status |
|---|---|---|
| Kraken Elite 2024 / Elite V2 (240, 280, 360, 420) | `1e71:3012` | Tested (firmware 1.2.0) |
| Kraken 2024 Plus | `1e71:3014` | Should work, untested |
| Kraken Z53 / Z63 / Z73 | `1e71:3008` | Should work, untested |
| Kraken 2023 / 2023 Elite | `1e71:300e`, `1e71:300c` | Should work, untested. Some 2023 firmware (2.x) can't show GIFs; you get the first frame with live temps instead |

Anything liquidctl's `KrakenZ3` driver can draw on is picked up automatically.
`kraken-lcd --list` shows what it finds. If yours works (or doesn't), please open an issue.

## Install

You need Python 3.11+, [liquidctl](https://github.com/liquidctl/liquidctl) 1.15+
(1.16+ for 2024 models), Pillow, and PyQt6 for the settings window.

**Arch / CachyOS / EndeavourOS / Manjaro**

```sh
sudo pacman -S --needed liquidctl python-pillow python-pyqt6 noto-fonts
git clone https://github.com/kavahn/kraken-lcd
cd kraken-lcd/packaging/arch && makepkg -si
```

**Fedora**

```sh
sudo dnf install liquidctl python3-pillow python3-pyqt6 google-noto-sans-fonts
git clone https://github.com/kavahn/kraken-lcd && cd kraken-lcd && ./install.sh
```

**Debian / Ubuntu**

```sh
sudo apt install liquidctl python3-pil python3-pyqt6 fonts-noto-core
git clone https://github.com/kavahn/kraken-lcd && cd kraken-lcd && ./install.sh
```

`install.sh` installs into `~/.local` for your user only (no root). `./install.sh uninstall` removes it.

Then start the service (it also starts at every login):

```sh
systemctl --user enable --now kraken-lcd
```

and open **Kraken LCD Settings** from your app menu.

> **Permissions:** distro liquidctl packages ship udev rules that let your user talk
> to the cooler. If `kraken-lcd --list` says permission denied, install liquidctl's
> [`71-liquidctl.rules`](https://github.com/liquidctl/liquidctl/blob/main/extra/linux/71-liquidctl.rules)
> into `/etc/udev/rules.d/` and replug the cooler or reboot.

## Using it with CoolerControl

**Turn off CoolerControl's liquidctl integration** (Settings → *Liquidctl
Integration*). CoolerControl can't hand over just the screen: with liquidctl on,
it reads from the cooler every second over the same HID connection, and two
programs doing that garble each other's replies. You'd get screen glitches and,
worse, dropped readings in CoolerControl.

With it off, CoolerControl controls the pump and fans through the kernel's
`nzxt-kraken3` driver (the device shows up as `kraken2024elite` or similar),
which works just as well. After switching:

1. Assign your pump and fan profiles to the kernel-driver device.
2. Any profile whose temperature source was the liquidctl device's **liquid**
   sensor should point at the kernel device's **Coolant temp** (`temp1`) instead.
   It's the same physical sensor.

The service logs a warning if it sees CoolerControl's liquidctl integration
switched on. For the same reason, don't run OpenKraken, NZXT-related liquidctl
scripts, or a second copy of kraken-lcd at the same time.

## GIFs

The screen can't take frames fast enough to draw temperatures on top of a playing
animation live. So kraken-lcd draws the temps onto **every frame** and uploads the
whole GIF, and the cooler plays it on its own. When the numbers change it uploads
again, at most every *GIF temp refresh* seconds (15 by default). Each upload
restarts the animation. Long GIFs are thinned to 48 frames (keeping their overall
speed) so uploads stay around a second or two.

## Settings

Everything lives in `~/.config/kraken-lcd/config.toml`. The settings window writes
it for you, and you can also edit it by hand. Changes reach the screen within a few seconds.

| Key | Default | |
|---|---|---|
| `image` | `""` | jpg / png / webp / animated gif. Empty = plain black |
| `style` | `"duotone"` | or `"original"` |
| `dim` | `0.45` | 0 = picture untouched, 1 = black |
| `accent_color` | `"#ff5ca8"` | ring, main label, duotone tint |
| `text_color` | `"#ffffff"` | numbers |
| `show` | `["cpu", "gpu", "liquid"]` | first gets the ring + big number |
| `gpu_sensor` | `"edge"` | AMD: `edge`, `junction` or `mem` |
| `brightness` | not set | 0–100; not set = keep the cooler's setting |
| `orientation` | not set | 0 / 90 / 180 / 270 |
| `interval` | `2.0` | seconds between temperature checks |
| `keepalive` | `30.0` | re-send a still frame at least this often |
| `gif_refresh` | `15.0` | seconds between GIF re-uploads |
| `gif_max_frames` | `48` | longer GIFs are thinned |
| `[ranges]` | cpu/gpu 25–95, liquid 20–45 | temperature for an empty / full ring |

Temperatures come from hwmon: `coretemp` / `k10temp` / `zenpower` for the CPU,
`amdgpu` / `nouveau` or `nvidia-smi` for the GPU, and the Kraken's kernel driver
(or liquidctl) for the liquid.

## Commands

```sh
kraken-lcd --list               # detected coolers
kraken-lcd --preview out.png    # draw a frame to a file, cooler untouched (out.gif for GIFs)
kraken-lcd --once               # draw + upload one frame
kraken-lcd --liquid             # back to the cooler's own liquid-temperature screen
journalctl --user -u kraken-lcd -f    # service log
systemctl --user stop kraken-lcd      # stop (the screen goes back to liquid temp)
```

## Troubleshooting

- **`Failed to switch active bucket` / `Failed to setup bucket` in the log.** That
  happens now and then while the kernel driver shares the connection. It's followed
  by `upload recovered on retry 1`, and the last good frame stays on screen meanwhile.
- **Screen stuck or black:** `systemctl --user restart kraken-lcd`. If that doesn't
  help, run `kraken-lcd --liquid`, then restart the service.
- **"no NZXT cooler with a screen found":** check `liquidctl list` sees it and that
  nothing else (CAM in a VM, OpenKraken, CoolerControl with liquidctl on) holds it.

## Credits

- [liquidctl](https://github.com/liquidctl/liquidctl) does all the talking to the
  cooler.
- The upload safeguards are adapted from [OpenKraken](https://github.com/davidboulay/OpenKraken)
  by David Boulay (MIT), which worked out this panel's quirks first. See `NOTICE`.

## License

MIT. See `LICENSE`.
