# Finding Discord Application IDs for Games

This guide documents practical ways to recover a game's public Discord
Application ID for use with this monitor. Start with the least invasive method
and treat every candidate ID as untrusted until Discord itself resolves it to
the expected game.

An Application ID (also called a Client ID) is a public decimal identifier. It
is not an API key, OAuth token, or client secret. Basic local Rich Presence
uses it in the first Discord IPC handshake:

```json
{"v": 1, "client_id": "356877880938070016"}
```

The ID determines the activity's registered name and available artwork.

## Recommended discovery order

1. Match the store ID against Discord's public detectable-game catalog.
2. Check whether the game or an open-source presence integration publishes the
   ID in source code or configuration.
3. Search installed game files for Discord SDKs and nearby configuration.
4. Capture a native Linux application's Discord IPC handshake.
5. Statically analyze the game executable for the value passed to
   `DiscordCreate` or an older Discord RPC initialization function.
6. Use a Wine named-pipe bridge only as a last resort, offline and without
   anti-cheat. It is unnecessary when static analysis succeeds.

Never assume the Steam App ID is the Discord Application ID. They are unrelated
numbering systems.

## Query Discord's detectable-game catalog

Discord's public detectable-game catalog includes Discord Application IDs,
registered names, executable rules, and third-party store identifiers. Matching
the `steam` SKU is stronger evidence than searching by a possibly ambiguous
title:

```bash
steam_id=286160
curl -fsS https://discord.com/api/v10/applications/detectable \
  | jq --arg id "$steam_id" \
    '.[] | select(any(.third_party_skus[]?;
      .distributor == "steam" and .id == $id))
      | {name, id, executables, third_party_skus}'
```

Verify the returned ID resolves to the expected application:

```bash
application_id=363408834095742976
curl -fsS \
  "https://discord.com/api/v10/applications/$application_id/rpc" \
  | jq '{name, id, icon, cover_image, third_party_skus}'
```

These are public Discord client endpoints, but they are not a stability
guarantee: catalog entries and metadata can change. Keep the same standalone
RPC validation requirement used for IDs recovered from binaries.

## Survey the installation

Steam manifests provide the game name, Steam ID, and installation directory:

```bash
rg '"appid"|"name"|"installdir"' \
  ~/.local/share/Steam/steamapps/appmanifest_*.acf
```

Look for Discord libraries and configuration:

```bash
find ~/.local/share/Steam/steamapps/common/GAME -type f \
  | rg -i 'discord|rpc|presence'
```

Useful indicators include:

- `discord_game_sdk.dll` or `discord_game_sdk.so`
- older `discord-rpc.dll` or `libdiscord-rpc.so`
- source/config keys such as `client_id`, `application_id`, or `DiscordCreate`

Bundling an SDK does not prove the current game build actually invokes it.

## Capture a native Linux IPC handshake

Discord on Linux listens on a Unix socket such as:

```text
$XDG_RUNTIME_DIR/discord-ipc-0
```

A transparent Unix-socket proxy can temporarily stand between a native game
and Discord to print the handshake and Application ID. This repository does
not include such a capture tool. Use only a locally audited proxy that restores
the original Discord socket or symlink on normal exit, interruption, and
failure.

This works for native Linux clients that connect to the Unix socket. It does
not normally capture Windows games under Proton: Windows Discord SDK builds
look for `\\.\pipe\discord-ipc-0`, a Windows named pipe, and Proton does not
automatically translate that to Discord's Linux Unix socket.

Flatpak games also need permission and an in-sandbox symlink. A compatible
Flatpak usually declares:

```text
--filesystem=xdg-run/app/com.discordapp.Discord:create
```

## Static analysis: the Rocket League technique

Rocket League supplied the most useful example. Its installation contained
`discord_game_sdk.dll`, and its executable imported `DiscordCreate`:

```bash
objdump -p RocketLeague.exe | rg -i -C 4 'discord|DiscordCreate'
strings -a -t x RocketLeague.exe | rg -i -C 5 'DiscordCreate|discord_game_sdk'
```

Disassembling the Discord initialization area revealed a 64-bit immediate
written into the SDK's create-parameter structure shortly before the call:

```asm
movabs rax,0x04f3e28ab9820000
mov    QWORD PTR [rbp+0x110],rax
...
call   ...DiscordCreate setup...
```

Convert the hexadecimal value to unsigned decimal:

```bash
printf '%d\n' $((0x04f3e28ab9820000))
```

That produced:

```text
356877880938070016
```

The surrounding structure initialization and subsequent `DiscordCreate` call
made this substantially stronger evidence than finding an arbitrary 17–20
digit string in a large executable.

### Practical reverse-engineering tips

- Check PE imports/exports first with `objdump -p`; an SDK function name gives
  an anchor for disassembly.
- Search both ASCII and UTF-16LE strings:

  ```bash
  strings -a -t x GAME.exe
  strings -el -t x GAME.exe
  ```

- Map a string's file offset to its virtual address using `objdump -h`, then
  search disassembly for RIP-relative references to that address.
- Look near logging strings such as `DiscordCreate FAILED`, `Discord`, or
  presence-related errors. Error strings often sit close to initialization
  code even in stripped proprietary binaries.
- Discord snowflakes are usually 17–20 decimal digits, but scanning all such
  numbers generates many false positives. Context around the SDK call matters.
- An ID may be stored as a decimal string, a 64-bit integer, split across
  instructions, loaded from configuration, or decoded at runtime.
- Do not patch, inject into, debug, or attach to an anti-cheat-protected game.
  Reading files offline with `file`, `strings`, and `objdump` is preferable.

For deeper analysis, use Ghidra or another decompiler inside the `dev`
Distrobox rather than installing development tools on the Bazzite host. Trace
the call that populates `DiscordCreateParams.client_id`; the Discord Game SDK
headers can help identify the structure layout.

## Validate every candidate safely

Do not launch the game for validation. Use the included standalone Discord IPC
sender:

```bash
./scripts/test-discord-presence.py \
  --application-id 356877880938070016 \
  --details 'Application ID validation'
```

Keep it running while checking your Discord profile. Confirm all of these:

- Discord accepts the handshake rather than returning a close/error frame.
- The top line is the expected game name.
- The icon/art belongs to the expected game.
- Ctrl+C clears the activity.

Only add an ID to this project's configuration after that test succeeds.

## Add the game to the monitor

Edit `~/.config/discord-game-presence/config.toml`:

```toml
[[games]]
name = "Example Game"
application_id = "123456789012345678"
process_names = ["ExampleGame.exe"]
```

Use the exact executable basename visible in `/proc`, including `.exe` for
Proton games and any spaces or native suffix such as `.x86_64`. Inspect a
running game read-only with:

```bash
pgrep -a -f 'ExampleGame'
```

The monitor understands both Linux `/` paths and Windows `\` paths exposed by
Proton, and preserves spaces in native executable paths. Earlier entries have
priority when multiple configured games run.

Validate and watch the automatic reload:

```bash
discord-game-presence --check-config
journalctl --user -u discord-game-presence.service -f
```

## Safety and reliability notes

- Application IDs are public, but they are owned by somebody else. The owner
  can rename/delete the application or change its assets.
- Reusing an ID reproduces its registered identity but does not make the
  monitor an official or verified game executable.
- Avoid Wine RPC bridge services for games with anti-cheat unless the game's
  publisher explicitly permits them. A separate host monitor is safer because
  it does not enter the Wine prefix, inject code, or read game memory.
- Some games bundle a disabled or obsolete Discord SDK. Static recovery can
  still find the old ID, but only a standalone validation proves the Discord
  application remains usable.
- A game can initialize the Discord SDK without publishing an activity.
  Tabletop Simulator's native Linux build was observed opening a connection
  with Application ID `402572971681644545` but sending no RPC command, even
  after joining a game. The monitor therefore ignores idle handshakes and
  yields only when another client sends a command.
- Minecraft Java has no single canonical official Rich Presence ID. Presence
  mods commonly use their own application identity; prefer letting the mod
  publish its richer activity instead of adding Minecraft to this fallback
  monitor.

## Record discoveries

For each confirmed game, record the following in a commit or investigation
note so a future session can reassess it:

- Game and store/launcher
- Executable basename used for detection
- Discord Application ID
- Evidence source (RPC handshake, source/config, or static call-site analysis)
- Date and installed game version/build
- Standalone validation result and displayed name/icon

## Confirmed installed-game IDs

Validated against Discord's detectable-game catalog and public RPC application
endpoint on 2026-08-11/12:

| Game | Steam App ID | Discord Application ID | Executable basename |
| --- | ---: | ---: | --- |
| Big Walk | `1478500` | `1535497936258076854` | `Big Walk.exe` |
| Clair Obscur: Expedition 33 | `1903340` | `1364888648839073802` | `SandFall-Win64-Shipping.exe` |
| Far Far West | `3124540` | `1437603706886426675` | `FarFarWest-Win64-Shipping.exe` |
| Rocket League | `252950` | `356877880938070016` | `RocketLeague.exe` |
| Tabletop Simulator | `286160` | `363408834095742976` | `Tabletop Simulator.x86_64` |
| Windrose | `3041230` | `1440133627899023452` | `Windrose-Win64-Shipping.exe` |

Tabletop Simulator's managed assembly also contains the SDK sample ID
`418559331265675294`, which Discord resolves as “Test Application Please
Ignore.” Do not use it as the game's identity.
