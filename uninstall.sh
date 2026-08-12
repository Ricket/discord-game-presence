#!/bin/bash
set -euo pipefail
systemctl --user disable --now discord-game-presence.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/discord-game-presence.service" "$HOME/.local/bin/discord-game-presence"
if [[ ${1:-} == --purge ]]; then rm -rf "$HOME/.config/discord-game-presence"; fi
systemctl --user daemon-reload
echo "Removed service and executable; configuration retained unless --purge was used."
