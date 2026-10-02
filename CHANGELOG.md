# Changelog

## 0.1.0

Initial Minecraft Java server manager for Ervisio (plugin SDK contract 3).

- Approved layout (round 041, variant A): inner sidebar, server cards with live CPU/memory, status filters, details in a
  right drawer (bottom sheet on phones). Panels follow the plugin surface convention (`--sunk` panels, `--surface` items).
- Persistent native runtime (`ervisio-minecraft` service): several servers, console, chunked file transfers, text editor,
  `server.properties` editor with structured fields, Vanilla/Paper/Purpur/Fabric/Forge/NeoForge/custom JAR software,
  Modrinth add-ons with dependencies, players, worlds, backups (download, restore) and daily schedules.
- Every long action is a runtime job: the interface waits for it, shows progress and reports the real outcome.
- Own dialogs for prompts and typed confirmations (the plugin frame blocks native dialogs); Escape closes the top layer.
- English and Italian interface; runtime error codes are translated, runtime messages are in English.
