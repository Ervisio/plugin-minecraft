# Minecraft plugin runtime

The browser page is an Ervisio plugin frame. It reaches the manager through the SDK's `sdk.api.http` call to the
`minecraft` capability declared in `plugin/manifest.json`; Ervisio brokers that request to a local Unix socket. The
runtime accepts JSON HTTP requests on that socket and stores server metadata below `/srv/ervisio/minecraft`.

The runtime is a separate Python 3.11 service, intended to run as the dedicated non-login `ervisio-minecraft` account.
The systemd unit restricts its writable paths to the manager data and runtime socket. Minecraft Java processes are
children of that account, so the service does not need to run as root. The installer can optionally add a Linux user to
the `ervisio-minecraft` group; that grants broad control over every managed server and its files. Ervisio's manifest
marks the HTTP capability as administrator-only and permits that group to use it without an unlock prompt. One runtime
instance holds an exclusive lock on its data directory; a second instance cannot manage the same servers concurrently.

Each server has its own directory under `servers/<id>/`, a `server.properties` file, `eula.txt`, and software jar. The
runtime saves its server registry atomically in `state.json`; after service restart saved servers return offline rather
than claiming that an old Java process is still running. Console output is read per process and kept in bounded
in-memory logs. Server actions run as background jobs so a download or backup does not hold open a UI request.

All server file paths are relative to that server's directory. The manager rejects absolute paths, traversal components,
and symlink components. File uploads are sent in ordered chunks and assembled in a private staging directory before
being renamed into place. Backups are stored separately and restore extracts into a staging directory; archive entries
must be regular files or directories and must pass the same path checks before replacing the server directory.

Software metadata and jars are fetched from an allowlist of HTTPS hosts for Mojang, Paper, Fabric, Purpur, Modrinth, and
GitHub release assets. Redirects are checked against the same host allowlist. Mojang SHA-1 and Paper SHA-256 values are
verified when their metadata provides them. The runtime does not install or provision a Java runtime; Java must already
be installed on the host and available to the service.

The current manager keeps runtime state and recent console logs in memory or its local state files; it is not a
multi-host orchestrator, container sandbox, or account/role system for individual Minecraft servers. Operators who can
access the control socket can manage every server handled by this service. Backups are local archives, not off-host
copies. Mod/plugin discovery and safe installation depend on the implemented catalogue sources and their metadata;
review the API reference for the currently supported sources and operations.

## Development checks

Run the isolated Python suite with `python3 -m unittest discover -s tests -v` from the `minecraft/` project directory.
Tests use temporary directories, local sockets, and a fake Java executable. They do not install the systemd service,
download server binaries, or contact catalogue providers. Run `npm run check` for the plugin TypeScript check and Python
suite.

The runtime requires Python 3.11 or newer. The plugin uses Ervisio's existing SDK v3 HTTP capability; it adds no
Minecraft-specific daemon API. Ervisio's plugin frame now permits downloads so the browser can save exported files
obtained through the declared capability, such as a server log or configuration export. On `SIGTERM` or `SIGINT`, the
runtime sends `stop` to its managed Java processes, waits for shutdown, closes its socket, and releases the data lock.
