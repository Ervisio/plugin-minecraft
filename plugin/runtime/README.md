# Minecraft runtime

Install from this directory on the Ervisio Linux host:

```sh
sudo bash install-runtime.sh
```

Prerequisites: systemd, Python 3.11+ and Java compatible with your selected Minecraft version. The installer does not install Java, open firewall ports or create game servers.

Optional Linux access grant (allows administration of every managed server):

```sh
sudo bash install-runtime.sh --grant-user USERNAME
```

Sign out and sign back in after adding a group. Administrators can instead use Ervisio's normal privilege unlock. The service runs as `ervisio-minecraft`, listens on `/run/ervisio-minecraft/control.sock`, and keeps data under `/srv/ervisio/minecraft`. The runtime and JVMs continue running after the plugin page closes.

```sh
systemctl status ervisio-minecraft
journalctl -u ervisio-minecraft
```

Reinstalling upgrades runtime files and restarts the service. Servers share the service account; mods and JARs are executable code and are not isolated from other managed servers. Do not launch a second runtime against the same data directory.
