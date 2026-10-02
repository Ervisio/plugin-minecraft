# Minecraft plugin for Ervisio

Run and manage several **Minecraft Java Edition** servers from [Ervisio](https://github.com/Ervisio/ervisio): create a
server, pick its software and version, then use its console, files, configuration, mods and plugins, players, worlds,
backups and daily schedules. English and Italian interface.

The plugin has two parts:

* **The page** (this repository's `src/`): an Ervisio plugin (SDK contract 3) that runs in Ervisio's sandboxed frame.
* **The runtime** (`plugin/runtime/`): a small Python service, `ervisio-minecraft`, that owns the Java processes, so
  servers keep running when nobody has the page open. It runs as its own non-root account and answers only on the Unix
  socket `/run/ervisio-minecraft/control.sock`, which the plugin's manifest declares as an HTTP capability.

## Install

1. In Ervisio, **Plugins › Browse › Minecraft › Install** (the marketplace package is signed by the Ervisio team).
2. Install the runtime on the machine, as root, from the installed plugin folder:

   ```sh
   sudo bash /var/lib/ervisio/plugins/minecraft/runtime/install-runtime.sh
   ```

   It creates the `ervisio-minecraft` system account and service, nothing else: no game server, no EULA acceptance,
   no firewall change. Add `--grant-user NAME` to let a Linux user manage the servers without unlocking administrator
   rights (they must sign out and in again).
3. Install a Java runtime that fits your Minecraft version (Java 21 for current releases). It is not installed for you;
   each server can use its own Java command.

Requirements: systemd, Python 3.11 or newer, Java. Ervisio administrators use the page after unlocking administrator
rights; members of the `ervisio-minecraft` group use it directly.

| What | Where |
|---|---|
| Service | `ervisio-minecraft.service` (`systemctl status ervisio-minecraft`, `journalctl -u ervisio-minecraft`) |
| Runtime | `/usr/lib/ervisio-minecraft/` |
| Servers, backups | `/srv/ervisio/minecraft/` |
| API socket | `/run/ervisio-minecraft/control.sock` |

Running the installer again updates the runtime and restarts the service, which stops the servers for a moment; data
stays where it is.

## What it does

* Several servers, each with its own port and Java heap; start, stop, restart, force stop; start at boot.
* Software: Vanilla, Paper, Purpur, Fabric, Forge, NeoForge (with the loader build) or your own `server.jar`.
  Downloads come from the official services and are checked against the published checksums; the Forge/NeoForge
  installer runs directly through Java, never through a shell.
* Live console with command history; files with upload, download, text editor, rename, extract and import of a whole
  server archive; `server.properties` with the common settings as fields.
* Mods and plugins from Modrinth, filtered by loader and Minecraft version, with required dependencies (SHA-512
  checked); turn on/off, update, remove, or upload a JAR.
* Players (online, whitelist, operators, bans), worlds (switch, delete), backups (create, download, restore) and daily
  backup/restart schedules.

The Minecraft EULA must be accepted when a server is created: <https://www.minecraft.net/eula>.

## Limits

* All servers run under the same `ervisio-minecraft` account: a mod or JAR can read the other servers' files. Only
  install software you trust. `-Xmx` limits the Java heap, not the whole process.
* TPS is not shown: Minecraft does not report it without a plugin. Player counts come from the server list ping.
* Downloads from the page (files, backups, console log) need an Ervisio release newer than 0.4.0 (frame `allow-downloads`); logos show from the release after 0.4.0 too.
* Not a hosting panel: no Bedrock, no remote nodes, no per-customer accounts.

## Develop

Node 24 and npm; Python 3.11+ for the runtime tests.

```sh
npm ci
npm test          # runtime tests (fake Java, no network)
npm run build     # typecheck, bundle, copy plugin/ into dist/minecraft/
npm run pack      # dist/minecraft-<version>.tar.gz + .sha256
```

Load `dist/minecraft/` in **Plugins › Developer** (developer mode). For a runtime of your own, run it on a scratch
folder and point a *copy* of the manifest at its socket:

```sh
python3 plugin/runtime/minecraft_runtime.py --data /tmp/mc-dev/data --socket /tmp/mc-dev/control.sock --java /path/to/java
```

`scripts/verify-integration.cjs` drives the real Ervisio daemon, the runtime and a fake Java in Chromium (see its
header for the environment variables). The runtime API is described in [docs/API.md](docs/API.md), the design in
[docs/architecture.md](docs/architecture.md).

## Release

Tag `vX.Y.Z` equal to the version in `plugin/manifest.json` and `package.json`, with a `CHANGELOG.md` section. The
release workflow attaches the package; the [Ervisio plugin registry](https://github.com/Ervisio/plugins) picks it up,
reviews and signs it.

## License

MIT. Minecraft is a trademark of Mojang Synergies AB; this project is not affiliated with Mojang or Microsoft.
