#!/bin/sh
# Install kraken-lcd for the current user (no root needed).
#   ./install.sh            install / update
#   ./install.sh uninstall  remove it again (your config.toml is kept)
set -eu

PREFIX="${PREFIX:-$HOME/.local}"
LIBDIR="$PREFIX/share/kraken-lcd"
BINDIR="$PREFIX/bin"
APPDIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
UNITDIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SRC="$(cd "$(dirname "$0")" && pwd)"

if [ "${1:-}" = "uninstall" ]; then
    systemctl --user disable --now kraken-lcd.service 2>/dev/null || true
    rm -rf "$LIBDIR"
    rm -f "$BINDIR/kraken-lcd" "$BINDIR/kraken-lcd-settings" \
          "$APPDIR/kraken-lcd-settings.desktop" "$UNITDIR/kraken-lcd.service"
    systemctl --user daemon-reload 2>/dev/null || true
    echo "kraken-lcd removed. Your settings are still in ~/.config/kraken-lcd/."
    exit 0
fi

missing=""
python3 -c 'import liquidctl' 2>/dev/null || missing="$missing liquidctl"
python3 -c 'import PIL' 2>/dev/null || missing="$missing Pillow"
python3 -c 'import PyQt6' 2>/dev/null || missing="$missing PyQt6(settings-window-only)"
if [ -n "$missing" ]; then
    echo "Missing Python packages:$missing"
    echo "  Arch/CachyOS:   sudo pacman -S liquidctl python-pillow python-pyqt6"
    echo "  Fedora:         sudo dnf install liquidctl python3-pillow python3-pyqt6"
    echo "  Debian/Ubuntu:  sudo apt install liquidctl python3-pil python3-pyqt6"
    case "$missing" in *liquidctl*|*Pillow*) exit 1;; esac
fi

mkdir -p "$LIBDIR" "$BINDIR" "$APPDIR" "$UNITDIR"
rm -rf "$LIBDIR/kraken_lcd"
cp -r "$SRC/kraken_lcd" "$LIBDIR/"
find "$LIBDIR" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

for cmd in kraken-lcd:daemon kraken-lcd-settings:gui; do
    name="${cmd%%:*}"; mod="${cmd##*:}"
    cat > "$BINDIR/$name" <<WRAP
#!/bin/sh
exec python3 -P -c 'import sys; sys.path.insert(0, "$LIBDIR"); from kraken_lcd.$mod import main; sys.exit(main())' "\$@"
WRAP
    chmod +x "$BINDIR/$name"
done

sed "s|@BINDIR@|$BINDIR|" "$SRC/data/kraken-lcd.service" > "$UNITDIR/kraken-lcd.service"
sed "s|^Exec=kraken-lcd-settings|Exec=$BINDIR/kraken-lcd-settings|" \
    "$SRC/data/kraken-lcd-settings.desktop" > "$APPDIR/kraken-lcd-settings.desktop"

systemctl --user daemon-reload
if systemctl --user is-active --quiet kraken-lcd.service; then
    systemctl --user restart kraken-lcd.service
    echo "kraken-lcd updated and restarted."
else
    echo "kraken-lcd installed."
    echo "Start it now and at every login with:"
    echo "  systemctl --user enable --now kraken-lcd"
fi
echo "Open \"Kraken LCD Settings\" from your app menu to pick a picture, GIF and colours."
