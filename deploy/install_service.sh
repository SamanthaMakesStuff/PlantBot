#!/usr/bin/env bash
# Makes the plant start by itself when the Raspberry Pi boots, and restart if it crashes.
# Run once from the PlantBot folder:   bash deploy/install_service.sh
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(id -un)"
PYTHON="$(command -v python3)"

if [ ! -f "$DIR/.env" ]; then
  echo "No .env file found in $DIR. Create it first (see GUIDE.md)."; exit 1
fi
if ! grep -q '^WEB_PASSWORD=.\{8,\}' "$DIR/.env"; then
  echo "WEB_PASSWORD in .env needs to be set to at least 8 characters first."; exit 1
fi
if [ ! -f "$DIR/data/persona.json" ]; then
  echo "Your plant hasn't been born yet. Run: python3 run.py   (then /quit) and try again."; exit 1
fi

sudo tee /etc/systemd/system/plant.service >/dev/null <<EOF
[Unit]
Description=Plant agent (web page and thoughts)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$DIR
ExecStart=$PYTHON $DIR/serve.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now plant.service
sleep 2
sudo systemctl --no-pager --lines=8 status plant.service || true
echo
echo "Done. Useful commands:"
echo "  sudo systemctl status plant     (is it running?)"
echo "  sudo journalctl -u plant -f     (live log)"
echo "  sudo systemctl restart plant    (after changing .env or code)"
echo "  sudo systemctl stop plant       (pause it)"
