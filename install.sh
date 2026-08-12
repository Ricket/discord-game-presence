#!/bin/bash
set -euo pipefail
root=$(cd -- "$(dirname -- "$0")" && pwd)
install -Dm755 "$root/discord_game_presence.py" "$HOME/.local/bin/discord-game-presence"
install -Dm644 "$root/discord-game-presence.service" "$HOME/.config/systemd/user/discord-game-presence.service"
if [[ ! -e "$HOME/.config/discord-game-presence/config.toml" ]]; then
  install -Dm600 "$root/config.toml" "$HOME/.config/discord-game-presence/config.toml"
fi
systemctl --user daemon-reload
systemctl --user enable discord-game-presence.service
systemctl --user restart discord-game-presence.service
echo "Installed and started discord-game-presence.service"
