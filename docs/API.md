# Local Minecraft runtime API

These routes are implemented by `plugin/runtime/minecraft_runtime.py` and are reachable only through the Unix socket
`/run/ervisio-minecraft/control.sock`. The browser plugin calls them through Ervisio's `minecraft` HTTP capability; the
daemon manifest limits which methods and paths it will broker. Request bodies must be JSON objects and are limited to
256 KiB by the local HTTP handler. A successful action that can take time returns a `jobId`; poll `GET /v1/jobs` until its `status` is `completed` or `failed` (with `error`). Switching software to `custom` records the new engine and version once the job finds `server.jar`. API errors
are returned as `{"error":{"code":"…","message":"…"}}` with the associated HTTP status.

Every route requires access to the Unix socket. The socket is owned by `ervisio-minecraft`; members of that group can
control every server in the runtime. The API itself does not add per-server users or role checks.

## Routes

| Method and route | Purpose |
|---|---|
| `GET /v1/health` | Runtime version, default Java command, data directory, uptime in seconds, and `disk` (`total`, `free` bytes of the data filesystem). |
| `GET /v1/servers` | List managed server summaries. |
| `POST /v1/servers` | Create a server directory, initial configuration and EULA file; install selected software in a background job except for `custom`. |
| `GET /v1/servers/{id}` | Read one server summary. |
| `PATCH /v1/servers/{id}` | Edit server name, engine, version, port, memory, Java command, JVM arguments, and autostart. |
| `DELETE /v1/servers/{id}` | Delete a stopped server after exact-name confirmation (`{"confirm":"server name"}`). |
| `GET /v1/jobs`, `GET /v1/activity` | Read recent background jobs and activity events. |
| `GET /v1/catalog/versions?engine=…` | List available releases for vanilla, Paper, Fabric, Purpur, Forge, or NeoForge. |
| `GET /v1/catalog/loaders?engine=forge\|neoforge&version=…` | List Forge or NeoForge builds for a Minecraft release. |
| `GET /v1/catalog/addons?server={id}&q=…` | Search Modrinth for add-ons compatible with the selected server. |
| `POST /v1/servers/{id}/power` | Start, stop, restart, or force-kill a server (`{"action":"start"}`). |
| `POST /v1/servers/{id}/command` | Send one console command (`{"command":"say hello"}`). |
| `GET /v1/servers/{id}/logs?after={cursor}` | Read up to 500 console lines after a sequence cursor. |
| `GET /v1/servers/{id}/files?path=…` | List files and directories below the server root. Omit `path` for the root. Entries include `name`, `type` (`file` or `directory`), `size`, and `mtime`. |
| `GET /v1/servers/{id}/file?path=…&offset=…&length=…` | Read a file chunk as base64, with total size and next offset. |
| `PUT /v1/servers/{id}/file` | Upload one ordered base64 chunk. Repeat with the returned `uploadId` and `next`; set `final:true` on the last chunk. |
| `PATCH /v1/servers/{id}/file` | Replace a small UTF-8 text file (`{"path":"config/example.txt","text":"…"}`). |
| `POST /v1/servers/{id}/files` | Perform a file action: `mkdir`, `move`, `delete`, `extract`, or `import`. See below. |
| `GET /v1/servers/{id}/properties` | Read `server.properties` as text and parsed key/value pairs. |
| `PUT /v1/servers/{id}/properties` | Replace validated properties text; port and world path are checked and the server port registry is updated. |
| `POST /v1/servers/{id}/software` | Queue a server software/version change (`engine`, `version`, optional Forge/NeoForge `loaderVersion`). |
| `GET /v1/servers/{id}/addons` | List JARs in the `mods/` and `plugins/` folders. |
| `POST /v1/servers/{id}/addons` | Install/update from Modrinth, toggle an add-on, or delete it after path confirmation. |
| `GET /v1/servers/{id}/players` | Read whitelist, operators, bans, and online players reported by server ping. |
| `POST /v1/servers/{id}/players` | Send `op`, `deop`, whitelist, ban, pardon, or kick commands to an online server. |
| `GET /v1/servers/{id}/worlds` | Find world folders with `level.dat` and report the active world. |
| `POST /v1/servers/{id}/worlds` | Activate a stopped server world or delete an inactive world after confirmation. |
| `GET /v1/servers/{id}/backups` | List local backup archives. |
| `POST /v1/servers/{id}/backups` | Create, restore, or delete a backup. Restore/delete require exact backup ID confirmation. |
| `GET /v1/servers/{id}/backup-file?backupId=…&offset=…&length=…` | Read a local backup in base64 chunks for browser download. |
| `GET /v1/servers/{id}/schedules` | Read saved backup/restart schedules. |
| `PUT /v1/servers/{id}/schedules` | Replace up to 20 local-time daily schedules, each `backup` or `restart`. |

## File and archive operations

All file paths are relative to the server's directory. Absolute paths, `..`, dot segments, backslashes, and symbolic
links are refused. `server.properties`, `eula.txt`, and runtime state files cannot be changed through generic upload or
text-edit requests. `server.jar` can be uploaded for a custom server, but file actions cannot move or delete it. The
server's root is never a valid `extract` destination.

An upload chunk is at most 128 KiB decoded, and a complete file may be at most 512 MiB. The first request supplies `path`, `total`, `offset:0`, and base64 `data`;
the response supplies an upload ID and next offset. Later requests must match the original server, path and total size,
and the offset must equal the bytes already received. The final chunk must end exactly at `total`; the staged file is
then renamed into its destination.

File actions use a JSON body with `action` and `path`. `mkdir` creates one directory. `move` also takes a relative
`target`. `delete` requires `confirm` equal to the source path. `extract` takes a relative `target` and queues extraction
of ZIP or tar archives. Only regular files and directories are accepted; links, devices, duplicate entries, traversal,
oversized archives, and pre-populated destinations are rejected. Extraction stages content beside the destination and
commits only after validation. `import` takes `confirm` equal to the server name and extracts an archive containing
`server.properties`, then rewrites its port to the managed port before replacing the server directory. Backup restore
also requires `server.properties` and reconciles its port with the managed port.

## Software and add-ons

Supported server engines are vanilla, Paper, Fabric, Purpur, Forge, NeoForge, and `custom`. Forge and NeoForge versions
are resolved from their official Maven catalogue; the runtime downloads the installer JAR after validating its SHA-1,
runs it under the dedicated service account, then starts using its generated argument file or supported Forge JAR.
Custom software expects an uploaded `server.jar`. Vanilla Mojang and Paper metadata checksums are verified when
published; Forge/NeoForge installer SHA-1 checksums are verified before setup. The Forge installer runs directly through
Java with `--installServer`, and its output is streamed to console logs; the installer has a ten-minute timeout.

Modrinth add-on searches and installs are filtered to the server version and loader. Required Modrinth dependencies are
planned recursively with a limit of 20 levels and 100 projects. Every artifact must have a SHA-512 and a safe `.jar`
filename. Add-on installation and software changes require the server to be stopped. Existing unmanaged JARs can be
toggled or deleted; catalog updates are available only for add-ons recorded as managed by this plugin.

## EULA and lifecycle

Server creation requires `{"eula":true}`. This records the creator's explicit acceptance as `eula=true` in the server's
`eula.txt`; it does not fetch or launch a server. Starting requires an existing server JAR (or generated Forge launch
arguments) and the recorded acceptance. Java is started directly with an argument vector, never via a shell. Each
server has its own process, stdin console, stdout log reader and background job. A runtime restart reloads saved server
metadata with all servers marked offline; it does not reconnect to orphan processes. The service holds an exclusive
lock on its data directory, so a second runtime cannot manage the same data. `SIGTERM` and `SIGINT` send `stop` to
running servers and wait for those processes before the service removes its socket and releases the lock.

Error messages are in English and meant for operators; the plugin translates the common codes for the interface. Errors include `not_found`, `invalid_input`, `invalid_path`, `eula_required`, `port_in_use`, `server_running`,
`confirmation_required`, `hash_mismatch`, `archive_invalid`, and upstream/catalogue errors. Exact codes are returned in
the error object and can be shown directly in an operator-facing diagnostic.
