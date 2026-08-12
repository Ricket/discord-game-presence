# Discord Game Presence

A tiny, privacy-preserving fallback game detector for Flatpak Discord. It scans
the host process list itself and sends only the selected activity to Discord.
It yields whenever another application (such as a Minecraft presence mod) uses
Discord RPC.

Requires Linux, Python 3.11 or newer, a systemd user session, and Discord's
Unix RPC socket. The included log-arbitration path targets Discord's Flatpak
package (`com.discordapp.Discord`).

## Install

```bash
./install.sh
```

The installer enables a systemd user service under `default.target`, so it
starts automatically when the user session starts after login and survives
normal reboots. It does not require Discord to be running first; the monitor
waits and retries until Discord becomes available.

Configuration lives at `~/.config/discord-game-presence/config.toml`. Add a
`[[games]]` block with a display label, public Discord Application ID, and one
or more exact executable basenames. Entries earlier in the file have priority.
Changes reload automatically.

`poll_interval_seconds` controls game discovery and priority-switch latency and
defaults to 60 seconds. While a game is active, its individual process is
checked every 15 seconds without rescanning the full process list.

See [FINDING_APPLICATION_IDS.md](FINDING_APPLICATION_IDS.md) for the safe
discovery, static-analysis, and validation workflow used to recover Rocket
League's genuine Discord Application ID.

```toml
[[games]]
name = "Rocket League"
application_id = "356877880938070016"
process_names = ["RocketLeague.exe"]
```

## Operations

```bash
systemctl --user status discord-game-presence.service
journalctl --user -u discord-game-presence.service -f
discord-game-presence --check-config
systemctl --user restart discord-game-presence.service
./scripts/test-discord-presence.py --application-id APPLICATION_ID
./uninstall.sh             # preserve config
./uninstall.sh --purge     # also remove config
```

If Discord is absent, the service waits and retries. If it cannot safely read
Discord's RPC log, it suppresses fallback presence and sends one notification.
An RPC client that merely opens a socket does not suppress the fallback; the
monitor yields after that client actually sends a command. This accommodates
games such as Tabletop Simulator whose native Linux Discord SDK opens an idle
connection without publishing an activity.
