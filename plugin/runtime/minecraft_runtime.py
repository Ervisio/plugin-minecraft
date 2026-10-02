#!/usr/bin/env python3
"""Unprivileged Minecraft server manager for the Ervisio plugin.

The HTTP API is deliberately only exposed on a Unix socket.  The service must
run as its own user; ownership and socket permissions are set by the systemd
unit, never by a request.  No request is passed through a shell.
"""
from __future__ import annotations

import argparse
import base64
import collections
import concurrent.futures
import datetime as dt
import fcntl
import hashlib
import http.server
import io
import json
import os
import pathlib
import re
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import tarfile
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from typing import Any

_MODULE_DIR = str(pathlib.Path(__file__).resolve().parent)
if _MODULE_DIR not in sys.path:
    sys.path.insert(0, _MODULE_DIR)

VERSION = "0.1.0"
_MISSING = object()
MAX_JSON = 256 * 1024
MAX_UPLOAD = 512 * 1024 * 1024
MAX_FILE_READ = 128 * 1024
MAX_TEXT = 192 * 1024
MAX_API = 8 * 1024 * 1024
ENGINES = {"vanilla", "paper", "fabric", "purpur", "forge", "neoforge", "custom"}
HOSTS = {"piston-meta.mojang.com", "piston-data.mojang.com", "launchermeta.mojang.com",
         "api.papermc.io", "fill.papermc.io", "fill-data.papermc.io", "meta.fabricmc.net", "api.purpurmc.org",
         "api.modrinth.com", "cdn.modrinth.com", "github.com", "objects.githubusercontent.com",
         "maven.minecraftforge.net", "files.minecraftforge.net", "maven.neoforged.net"}
NAME_RE = re.compile(r"^[\w .()\-]{1,64}$", re.UNICODE)
ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")
MC_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?(?:-[a-zA-Z0-9_.-]+)?$")
PLAYER_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")


class APIError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def fail(code: str, message: str, status: int = 400) -> None:
    raise APIError(code, message, status)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def atomic_json(path: pathlib.Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, separators=(",", ":"))
            fh.flush(); os.fsync(fh.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def load_json(path: pathlib.Path, default: Any) -> Any:
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default


def safe_path(root: pathlib.Path, raw: str, *, allow_root: bool = False) -> pathlib.Path:
    """Reject traversal and symlinks, including symlinked intermediate directories."""
    if not isinstance(raw, str) or "\x00" in raw or "\\" in raw:
        fail("invalid_path", "Invalid path")
    path = pathlib.PurePosixPath(raw)
    if path.is_absolute() or any(x in ("..", ".") for x in raw.split("/")):
        fail("invalid_path", "The path must be relative and must not contain ..")
    parts = [p for p in path.parts if p]
    if not parts and not allow_root:
        fail("invalid_path", "The path cannot be empty")
    result = root
    for part in parts:
        result = result / part
        if result.is_symlink():
            fail("invalid_path", "Symbolic links are not allowed")
    return result


def valid_name(value: Any, what: str = "name") -> str:
    if not isinstance(value, str) or not NAME_RE.fullmatch(value) or value.strip() != value:
        fail("invalid_name", f"Invalid {what}")
    return value


def valid_id(value: Any) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        fail("invalid_id", "Invalid identifier")
    return value


def int_range(value: Any, low: int, high: int, field: str) -> int:
    if isinstance(value, bool):
        fail("invalid_input", f"Invalid {field}")
    try:
        num = int(value)
    except (ValueError, TypeError):
        fail("invalid_input", f"Invalid {field}")
    if num < low or num > high:
        fail("invalid_input", f"{field} must be between {low} and {high}")
    return num


class CheckedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


OPENER = urllib.request.build_opener(CheckedRedirect())


def check_url(url: str) -> None:
    try:
        parsed = urllib.parse.urlsplit(url)
        approved = parsed.scheme == "https" and parsed.hostname in HOSTS and parsed.port in (None, 443) and not parsed.username and not parsed.password
    except ValueError:
        approved = False
    if not approved:
        fail("remote_url_rejected", "Download source not allowed")


def fetch_bytes(url: str, limit: int = MAX_API) -> bytes:
    check_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": "Ervisio-Minecraft/1.0", "Accept": "application/json"})
    try:
        with OPENER.open(req, timeout=25) as response:
            data = response.read(limit + 1)
            if len(data) > limit:
                fail("remote_too_large", "Remote response too large")
            return data
    except (urllib.error.URLError, TimeoutError) as exc:
        fail("remote_error", f"Remote service unavailable: {exc}", 502)


def fetch_json(url: str) -> Any:
    try:
        return json.loads(fetch_bytes(url))
    except json.JSONDecodeError:
        fail("remote_error", "Invalid remote response", 502)


def download(url: str, destination: pathlib.Path, sha1: str | None = None,
             sha512: str | None = None, progress=None) -> None:
    check_url(url)
    temp = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.download")
    hashes = {"sha1": hashlib.sha1(), "sha512": hashlib.sha512()}
    total = 0
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Ervisio-Minecraft/1.0"})
        with OPENER.open(req, timeout=40) as response, temp.open("wb") as fh:
            expected = int(response.headers.get("Content-Length", "0"))
            if expected > MAX_UPLOAD:
                fail("remote_too_large", "Download too large")
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_UPLOAD:
                    fail("remote_too_large", "Download too large")
                fh.write(chunk)
                for hasher in hashes.values():
                    hasher.update(chunk)
                if progress and expected:
                    progress(min(95, int(total * 100 / expected)))
            fh.flush(); os.fsync(fh.fileno())
        if sha1 and hashes["sha1"].hexdigest().lower() != sha1.lower():
            fail("hash_mismatch", "SHA-1 check failed")
        if sha512 and hashes["sha512"].hexdigest().lower() != sha512.lower():
            fail("hash_mismatch", "SHA-512 check failed")
        os.replace(temp, destination)
    except (urllib.error.URLError, TimeoutError) as exc:
        fail("remote_error", f"Download failed: {exc}", 502)
    finally:
        temp.unlink(missing_ok=True)


def properties_parse(text: str) -> dict[str, str]:
    result = {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and stripped[0] not in "#!" and "=" in stripped:
            key, value = stripped.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def properties_set(text: str, updates: dict[str, str]) -> str:
    lines = text.splitlines()
    seen = set()
    for index, line in enumerate(lines):
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in updates and not line.lstrip().startswith(("#", "!")):
            lines[index] = f"{key}={updates[key]}"; seen.add(key)
    for key, value in updates.items():
        if key not in seen:
            lines.append(f"{key}={value}")
    return "\n".join(lines).rstrip() + "\n"


def atomic_text(path: pathlib.Path, data: str) -> None:
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as fh:
            fh.write(data); fh.flush(); os.fsync(fh.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        part = value & 0x7f; value >>= 7
        out.append(part | (0x80 if value else 0))
        if not value:
            return bytes(out)


def recv_varint(sock: socket.socket) -> int:
    out = 0
    for offset in range(0, 35, 7):
        byte = sock.recv(1)
        if not byte:
            raise EOFError
        out |= (byte[0] & 127) << offset
        if not byte[0] & 128:
            return out
    raise ValueError("Varint too long")


def slp_status(port: int, timeout: float = .5) -> dict[str, Any] | None:
    """Minecraft Server List Ping; count reflects actual online users."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            host = b"localhost"
            packet = varint(765) + varint(len(host)) + host + port.to_bytes(2, "big") + b"\x01"
            sock.sendall(varint(len(packet) + 1) + b"\x00" + packet + b"\x01\x00")
            recv_varint(sock); packet_id = recv_varint(sock)
            if packet_id != 0:
                return None
            length = recv_varint(sock)
            if length > 64 * 1024:
                return None
            data = bytearray()
            while len(data) < length:
                chunk = sock.recv(length - len(data))
                if not chunk: return None
                data.extend(chunk)
            return json.loads(data)
    except (OSError, EOFError, ValueError, json.JSONDecodeError):
        return None


def proc_metrics(pid: int) -> tuple[int, float] | None:
    try:
        status = pathlib.Path(f"/proc/{pid}/status").read_text()
        rss_kb = int(re.search(r"^VmRSS:\s+(\d+)", status, re.M).group(1))
        stat = pathlib.Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        ticks = int(stat[11]) + int(stat[12])
        return rss_kb, ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, AttributeError, IndexError, ValueError):
        return None


class MinecraftRuntime:
    def __init__(self, data: str | pathlib.Path = "/srv/ervisio/minecraft", java: str = "java"):
        self.data = pathlib.Path(data).absolute()
        self.java = java
        self.servers_dir = self.data / "servers"
        self.backups_dir = self.data / "backups"
        self.uploads_dir = self.data / "uploads"
        for path in (self.data, self.servers_dir, self.backups_dir, self.uploads_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data / "state.json"
        raw = load_json(self.state_path, {"servers": {}})
        self.servers: dict[str, dict] = raw.get("servers", {})
        self.lock = threading.RLock()
        self.server_locks: dict[str, threading.RLock] = collections.defaultdict(threading.RLock)
        self.processes: dict[str, subprocess.Popen] = {}
        self.reader_threads: dict[str, threading.Thread] = {}
        self.logs: dict[str, collections.deque] = collections.defaultdict(lambda: collections.deque(maxlen=3000))
        self.online_players: dict[str, dict[str, dict]] = collections.defaultdict(dict)
        self.cursors: dict[str, int] = collections.defaultdict(int)
        self.jobs: collections.deque = collections.deque(maxlen=200)
        self.activity: collections.deque = collections.deque(maxlen=300)
        self.started = time.monotonic()
        self._stop = threading.Event()
        self._last_cpu: dict[str, tuple[float, float]] = {}
        for sid, server in self.servers.items():
            server["state"] = "offline"
            server.pop("jobId", None)
        self._save()
        threading.Thread(target=self._scheduler, daemon=True, name="minecraft-scheduler").start()
        for sid, server in list(self.servers.items()):
            if server.get("autostart"):
                self.job(sid, "autostart", lambda rec, sid=sid: self._start(sid, rec))

    def close(self):
        self._stop.set()
        threads = []
        for sid in list(self.processes):
            def stop_one(sid=sid):
                try: self._stop_server(sid, timeout=20)
                except Exception: pass
            thread = threading.Thread(target=stop_one, daemon=True)
            thread.start(); threads.append(thread)
        for thread in threads: thread.join(timeout=30)
        for thread in list(self.reader_threads.values()): thread.join(timeout=5)

    def _save(self) -> None:
        with self.lock:
            atomic_json(self.state_path, {"servers": self.servers})

    def _dir(self, sid: str) -> pathlib.Path:
        valid_id(sid)
        path = self.servers_dir / sid
        if path.is_symlink(): fail("invalid_path", "Unsafe server folder")
        return path

    def _get(self, sid: str) -> dict:
        valid_id(sid)
        with self.lock:
            if sid not in self.servers: fail("not_found", "Server not found", 404)
            return self.servers[sid]

    def _event(self, sid: str | None, action: str, result: str) -> None:
        with self.lock: self.activity.appendleft({"time": now(), "serverId": sid, "action": action, "result": result})

    def job(self, sid: str | None, action: str, fn) -> dict:
        record = {"id": uuid.uuid4().hex[:12], "serverId": sid, "action": action,
                  "status": "running", "progress": 0}
        with self.lock: self.jobs.appendleft(record)
        def run():
            try:
                if sid:
                    with self.server_locks[sid]: fn(record)
                else: fn(record)
                record["status"] = "completed"; record["progress"] = 100
                self._event(sid, action, "completed")
            except Exception as exc:
                record["status"] = "failed"
                record["error"] = exc.message if isinstance(exc, APIError) else str(exc)
                self._event(sid, action, "failed")
                if sid and sid in self.servers and action in ("create", "software", "start", "autostart"):
                    with self.lock:
                        self.servers[sid]["state"] = "error"
                        self.servers[sid]["error"] = record["error"]
                        self._save()
        threading.Thread(target=run, daemon=True, name=f"mc-job-{action}").start()
        return {"jobId": record["id"]}

    def server_view(self, sid: str) -> dict:
        with self.lock: out = dict(self._get(sid))
        process = self.processes.get(sid)
        if process and process.poll() is None:
            result = proc_metrics(process.pid)
            if result:
                rss, seconds = result
                out["rssMB"] = round(rss / 1024)
                old = self._last_cpu.get(sid)
                stamp = time.monotonic()
                out["cpu"] = round(100 * (seconds - old[1]) / (stamp - old[0]), 1) if old and stamp > old[0] else 0.0
                self._last_cpu[sid] = (stamp, seconds)
            else: out.update(cpu=None, rssMB=0)
            slp = slp_status(out["port"])
            if slp and out["state"] == "starting":
                with self.lock:
                    if sid in self.servers and self.servers[sid]["state"] == "starting":
                        self.servers[sid]["state"] = "online"; self._save()
                out["state"] = "online"
            players = slp.get("players", {}) if slp else {}
            out["playersOnline"] = players.get("online")
            out["maxPlayers"] = players.get("max")
            if slp and slp.get("version", {}).get("name"):
                out["reportedVersion"] = slp["version"]["name"]
        else:
            out.update(cpu=None, rssMB=0, playersOnline=None, maxPlayers=None)
            if out["state"] in ("online", "starting", "stopping"):
                out["state"] = "offline"
        out["tps"] = None
        return out

    def list_servers(self) -> dict:
        ids = list(self.servers)
        if len(ids) < 2:
            return {"servers": [self.server_view(sid) for sid in ids]}
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(ids))) as pool:
            return {"servers": list(pool.map(self.server_view, ids))}

    def _config(self, body: dict, previous: dict | None = None) -> dict:
        previous = previous or {}
        name = valid_name(body.get("name", previous.get("name")))
        engine = body.get("engine", previous.get("engine", "vanilla"))
        if engine not in ENGINES: fail("invalid_engine", "Unsupported server software")
        version = body.get("version", previous.get("version"))
        if not isinstance(version, str) or not MC_RE.fullmatch(version):
            fail("invalid_version", "Invalid Minecraft version")
        port = int_range(body.get("port", previous.get("port", 25565)), 1024, 65535, "port")
        memory = int_range(body.get("memoryMB", previous.get("memoryMB", 2048)), 512, 131072, "memoryMB")
        autostart = body.get("autostart", previous.get("autostart", False))
        if not isinstance(autostart, bool): fail("invalid_input", "autostart must be a boolean")
        java = body.get("java", previous.get("java", self.java))
        if not isinstance(java, str) or not java or len(java) > 512 or any(c in java for c in "\x00\n\r"):
            fail("invalid_input", "Invalid Java path")
        args = body.get("jvmArgs", previous.get("jvmArgs", []))
        if not isinstance(args, list) or len(args) > 32 or any(not isinstance(a, str) or len(a) > 256 or not a.startswith("-") or any(c in a for c in "\x00\n\r") for a in args):
            fail("invalid_input", "Invalid JVM arguments")
        # JVM options capable of executing arbitrary native code or files are rejected.
        forbidden = ("-javaagent", "-agentlib", "-agentpath", "-Xbootclasspath", "-Djava.security.manager", "-Duser.dir", "-XX:OnError", "-XX:OnOutOfMemoryError", "-XX:StartFlightRecording")
        if any(a.startswith(forbidden) or a in ("-jar", "-cp", "-classpath") for a in args):
            fail("invalid_input", "JVM argument not allowed")
        loader_version = body.get("loaderVersion", previous.get("loaderVersion"))
        if loader_version is not None and (not isinstance(loader_version, str) or not re.fullmatch(r"[0-9][0-9A-Za-z_.+\-]{0,100}", loader_version)):
            fail("invalid_input", "Invalid loader version")
        result = dict(previous)
        result.update(name=name, engine=engine, version=version, port=port, memoryMB=memory,
                      autostart=autostart, java=java, jvmArgs=args, loaderVersion=loader_version)
        return result

    def create(self, body: dict) -> dict:
        if body.get("eula") is not True: fail("eula_required", "You must accept the Minecraft EULA")
        config = self._config(body)
        if "seed" in body and (not isinstance(body["seed"], str) or len(body["seed"]) > 128 or "\n" in body["seed"]):
            fail("invalid_input", "Invalid seed")
        if "motd" in body and (not isinstance(body["motd"], str) or len(body["motd"]) > 256 or "\n" in body["motd"]):
            fail("invalid_input", "Invalid MOTD")
        with self.lock:
            if any(s["port"] == config["port"] for s in self.servers.values()):
                fail("port_in_use", "Another server already uses this port", 409)
            sid = uuid.uuid4().hex[:12]
            config.update(id=sid, state="offline", created=now())
            directory = self._dir(sid)
            self.servers[sid] = config
            try:
                directory.mkdir(mode=0o750)
                for sub in ("mods", "plugins", "worlds"):
                    (directory / sub).mkdir(mode=0o750)
                props = f"server-port={config['port']}\nmax-players=20\nonline-mode=true\n"
                props = properties_set(props, {"level-seed": body.get("seed", ""), "motd": body.get("motd", "A Minecraft Server")})
                atomic_text(directory / "server.properties", props)
                atomic_text(directory / "eula.txt", "# EULA accepted by server creator\neula=true\n")
                self._save()
            except Exception:
                del self.servers[sid]
                if directory.exists(): shutil.rmtree(directory)
                raise
        if config["engine"] != "custom":
            config.update(self.job(sid, "create", lambda rec: self._install_software(sid, config["engine"], config["version"], rec, config.get("loaderVersion"))))
        return self.server_view(sid)

    def patch_server(self, sid: str, body: dict) -> dict:
        server = self._get(sid)
        with self.server_locks[sid], self.lock:
            updated = self._config(body, server)
            if any(other != sid and s["port"] == updated["port"] for other, s in self.servers.items()):
                fail("port_in_use", "Another server already uses this port", 409)
            if server["state"] not in ("offline", "error") and any(updated[k] != server.get(k) for k in ("engine", "version", "port", "java", "jvmArgs", "memoryMB", "loaderVersion")):
                fail("server_running", "Stop the server before changing how it starts", 409)
            old_software = (server["engine"], server["version"], server.get("loaderVersion"))
            software_changed = old_software != (updated["engine"], updated["version"], updated.get("loaderVersion"))
            if software_changed and updated["engine"] != "custom":
                server.update({k: v for k, v in updated.items() if k not in ("engine", "version", "loaderVersion")})
            else:
                server.update(updated)
            props_path = self._dir(sid) / "server.properties"
            if str(updated["port"]) != properties_parse(props_path.read_text()).get("server-port"):
                atomic_text(props_path, properties_set(props_path.read_text(), {"server-port": str(updated["port"])}))
            self._save()
        if software_changed and updated["engine"] != "custom":
            server.update(self.job(sid, "software", lambda rec: self._install_software(sid, updated["engine"], updated["version"], rec, updated.get("loaderVersion"))))
        return self.server_view(sid)

    def delete_server(self, sid: str, body: dict) -> dict:
        server = self._get(sid)
        if body.get("confirm") != server["name"]:
            fail("confirmation_required", "Confirm with the exact server name")
        with self.server_locks[sid], self.lock:
            if self.processes.get(sid) and self.processes[sid].poll() is None:
                fail("server_running", "Stop the server before deleting it", 409)
            server_dir = self._dir(sid)
            backup_dir = self.backups_dir / sid
            if backup_dir.is_symlink(): fail("invalid_path", "Unsafe backup folder")
            removed_server = self.servers_dir / f".deleted-{uuid.uuid4().hex}"
            removed_backup = self.backups_dir / f".deleted-{uuid.uuid4().hex}"
            os.rename(server_dir, removed_server)
            try:
                if backup_dir.exists(): os.rename(backup_dir, removed_backup)
            except Exception:
                os.rename(removed_server, server_dir)
                raise
            del self.servers[sid]
            try: self._save()
            except Exception:
                self.servers[sid] = server
                os.rename(removed_server, server_dir)
                if removed_backup.exists(): os.rename(removed_backup, backup_dir)
                raise
            shutil.rmtree(removed_server, ignore_errors=True)
            shutil.rmtree(removed_backup, ignore_errors=True)
        self._event(sid, "delete", "completed")
        return {"ok": True}

    def _install_software(self, sid: str, engine: str, version: str, record: dict | None = None,
                          loader_version: str | None = None) -> None:
        server = self._get(sid)
        if server["state"] not in ("offline", "error"):
            fail("server_running", "Stop the server to change its software", 409)
        directory = self._dir(sid)
        if engine == "custom":
            if not (directory / "server.jar").is_file():
                fail("jar_missing", "Upload server.jar before starting", 409)
            with self.lock:
                server.update(engine="custom", version=version, loaderVersion=None, state="offline")
                server.pop("error", None)
                self._save()
            return
        if not MC_RE.fullmatch(version): fail("invalid_version", "Invalid Minecraft version")
        if engine == "vanilla":
            manifest = fetch_json("https://piston-meta.mojang.com/mc/game/version_manifest_v2.json")
            item = next((item for item in manifest.get("versions", []) if item.get("id") == version), None)
            if not item: fail("version_not_found", "Minecraft version not found", 404)
            detail = fetch_json(item["url"])
            artifact = detail["downloads"]["server"]
            url, sha1 = artifact["url"], artifact["sha1"]
        elif engine == "paper":
            api = "https://fill.papermc.io/v3/projects/paper/versions/" + urllib.parse.quote(version, safe="") + "/builds"
            builds = fetch_json(api)
            if isinstance(builds, dict): builds = builds.get("builds", [])
            stable = [b for b in builds if b.get("channel") == "STABLE"]
            build = max(stable or builds, key=lambda b: int(b.get("id", 0))) if builds else None
            if not build: fail("version_not_found", "Paper build not found", 404)
            artifact = build.get("downloads", {}).get("server:default") or build.get("downloads", {}).get("application")
            if not artifact: fail("remote_error", "Paper download not available", 502)
            url = artifact["url"]; sha1 = None
        elif engine == "fabric":
            loaders = fetch_json("https://meta.fabricmc.net/v2/versions/loader/" + urllib.parse.quote(version, safe=""))
            installers = fetch_json("https://meta.fabricmc.net/v2/versions/installer")
            if not loaders or not installers: fail("version_not_found", "Fabric loader not available", 404)
            loader = next((x for x in loaders if x.get("loader", {}).get("stable")), loaders[0])["loader"]["version"]
            installer = next((x for x in installers if x.get("stable")), installers[0])["version"]
            url = "https://meta.fabricmc.net/v2/versions/loader/" + urllib.parse.quote(version) + "/" + urllib.parse.quote(loader) + "/" + urllib.parse.quote(installer) + "/server/jar"
            sha1 = None
        elif engine == "purpur":
            detail = fetch_json("https://api.purpurmc.org/v2/purpur/" + urllib.parse.quote(version, safe=""))
            latest = detail.get("builds", {}).get("latest")
            if not latest: fail("version_not_found", "Purpur build not found", 404)
            url = "https://api.purpurmc.org/v2/purpur/" + urllib.parse.quote(version, safe="") + "/" + str(latest) + "/download"
            sha1 = None
        elif engine in ("forge", "neoforge"):
            try:
                import forge_catalog
                selection = forge_catalog.resolve(engine, version, loader_version)
            except (ValueError, urllib.error.URLError) as exc:
                fail("version_not_found", str(exc), 404)
            url, sha1 = selection["url"], selection["sha1"]
        else:
            fail("invalid_engine", "Unsupported server software")
        dest = directory / "server.jar"
        # Preserve the previous binary when a new release cannot be installed.
        staged = directory / f".server-{uuid.uuid4().hex}.jar"
        try:
            download(url, staged, sha1=sha1, progress=(lambda p: record.update(progress=p)) if record else None)
            if engine == "paper" and artifact.get("checksums", {}).get("sha256"):
                digest = hashlib.sha256()
                with staged.open("rb") as fh:
                    while chunk := fh.read(1024 * 1024): digest.update(chunk)
                actual = digest.hexdigest()
                if actual.lower() != artifact["checksums"]["sha256"].lower():
                    fail("hash_mismatch", "Paper SHA-256 check failed")
            if engine in ("forge", "neoforge"):
                installer = directory / f".{engine}-installer-{uuid.uuid4().hex}.jar"
                os.replace(staged, installer)
                try:
                    self._run_installer(sid, [server.get("java", self.java), "-jar", installer.name, "--installServer"], directory)
                    try: forge_catalog.launch_args(directory, engine, selection["loaderVersion"])
                    except ValueError as exc: fail("installer_failed", str(exc))
                finally: installer.unlink(missing_ok=True)
            else:
                os.replace(staged, dest)
            with self.lock:
                server.update(engine=engine, version=version, loaderVersion=selection["loaderVersion"] if engine in ("forge", "neoforge") else None, state="offline")
                server.pop("error", None)
                self._save()
        finally:
            staged.unlink(missing_ok=True)

    def _run_installer(self, sid: str, argv: list[str], directory: pathlib.Path) -> None:
        try:
            process = subprocess.Popen(argv, cwd=directory, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, encoding="utf-8", errors="replace", start_new_session=True)
        except OSError as exc:
            fail("installer_failed", f"Could not start the installer: {exc}")
        def read_output():
            try:
                for line in process.stdout:
                    self._append_log(sid, "[Installer] " + line)
            finally:
                process.stdout.close()
        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        try:
            code = process.wait(timeout=600)
        except subprocess.TimeoutExpired:
            try: os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError: pass
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                process.wait(timeout=5)
            fail("installer_timeout", "The installer timed out after 10 minutes")
        finally:
            reader.join(timeout=5)
        if code != 0:
            fail("installer_failed", f"The installer exited with code {code}")

    def software(self, sid: str, body: dict) -> dict:
        server = self._get(sid)
        engine = body.get("engine", server["engine"])
        version = body.get("version", server["version"])
        loader_version = body.get("loaderVersion", server.get("loaderVersion"))
        if engine not in ENGINES or not isinstance(version, str) or not MC_RE.fullmatch(version):
            fail("invalid_input", "Invalid software or version")
        if server["state"] not in ("offline", "error"):
            fail("server_running", "Stop the server to change its software", 409)
        return self.job(sid, "software", lambda rec: self._install_software(sid, engine, version, rec, loader_version))

    def _append_log(self, sid: str, line: str) -> None:
        with self.lock:
            self.cursors[sid] += 1
            self.logs[sid].append({"seq": self.cursors[sid], "text": line.rstrip("\r\n")[:4096]})

    def _reader(self, sid: str, process: subprocess.Popen) -> None:
        try:
            for line in process.stdout:
                self._append_log(sid, line)
                joined = re.search(r"\b([A-Za-z0-9_]{3,16}) joined the game\b", line)
                left = re.search(r"\b([A-Za-z0-9_]{3,16}) left the game\b", line)
                if joined: self.online_players[sid][joined.group(1)] = {"name": joined.group(1)}
                if left: self.online_players[sid].pop(left.group(1), None)
                if re.search(r"\bDone \([\d.]+s\)!", line):
                    with self.lock:
                        if sid in self.servers and self.servers[sid]["state"] == "starting":
                            self.servers[sid]["state"] = "online"; self._save()
        except (OSError, ValueError):
            pass
        code = process.wait()
        self.online_players[sid].clear()
        try:
            if process.stdin: process.stdin.close()
            if process.stdout: process.stdout.close()
        except OSError:
            pass
        with self.lock:
            if self.processes.get(sid) is process and sid in self.servers:
                state = self.servers[sid]["state"]
                self.servers[sid]["state"] = "offline" if state == "stopping" or code == 0 else "error"
                if code != 0 and state != "stopping": self.servers[sid]["error"] = f"Java terminato con codice {code}"
                self._save()
        self._append_log(sid, f"[Ervisio] Process exited (code {code})")

    def _start(self, sid: str, record: dict | None = None) -> None:
        server = self._get(sid)
        current = self.processes.get(sid)
        if current and current.poll() is None: fail("server_running", "The server is already running", 409)
        directory = self._dir(sid)
        jar = directory / "server.jar"
        if server["engine"] not in ("forge", "neoforge") and (jar.is_symlink() or not jar.is_file()):
            fail("jar_missing", "Upload or install server.jar before starting", 409)
        eula = properties_parse((directory / "eula.txt").read_text(encoding="utf-8")) if (directory / "eula.txt").is_file() else {}
        if eula.get("eula", "").lower() != "true":
            fail("eula_required", "The EULA must be accepted")
        with self.lock:
            server["state"] = "starting"; server.pop("error", None); self._save()
        launch = ["-jar", "server.jar"]
        if server["engine"] in ("forge", "neoforge"):
            try:
                import forge_catalog
                launch = forge_catalog.launch_args(directory, server["engine"], server.get("loaderVersion") or "")
            except ValueError as exc:
                fail("jar_missing", str(exc), 409)
        args = [server.get("java", self.java), f"-Xms{min(server['memoryMB'], 1024)}M",
                f"-Xmx{server['memoryMB']}M", *server.get("jvmArgs", []), *launch, "nogui"]
        try:
            process = subprocess.Popen(args, cwd=directory, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                       bufsize=1, start_new_session=True, close_fds=True)
        except (OSError, ValueError) as exc:
            fail("java_start_failed", f"Could not start Java: {exc}", 500)
        self.processes[sid] = process
        self.online_players[sid].clear()
        reader = threading.Thread(target=self._reader, args=(sid, process), daemon=True, name=f"mc-log-{sid}")
        self.reader_threads[sid] = reader
        reader.start()
        self._append_log(sid, f"[Ervisio] Started Java process, PID {process.pid}")

    def _command(self, sid: str, command: str) -> None:
        process = self.processes.get(sid)
        if not process or process.poll() is not None or not process.stdin:
            fail("server_offline", "The server is not running", 409)
        if not isinstance(command, str) or not command.strip() or len(command) > 2048 or any(c in command for c in "\r\n\x00"):
            fail("invalid_command", "Invalid command")
        try:
            process.stdin.write(command.lstrip("/") + "\n"); process.stdin.flush()
        except (BrokenPipeError, OSError):
            fail("server_offline", "Server console unavailable", 409)

    def _stop_server(self, sid: str, timeout: int = 30, kill: bool = False) -> None:
        server = self._get(sid)
        process = self.processes.get(sid)
        if not process or process.poll() is not None:
            server["state"] = "offline"; self._save(); return
        with self.lock: server["state"] = "stopping"; self._save()
        if not kill:
            try: self._command(sid, "stop")
            except APIError: pass
            try: process.wait(timeout=timeout)
            except subprocess.TimeoutExpired: pass
        if process.poll() is None:
            try: os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError: pass
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                process.wait(timeout=5)
        with self.lock: server["state"] = "offline"; self._save()

    def power(self, sid: str, body: dict) -> dict:
        action = body.get("action")
        if action not in ("start", "stop", "restart", "kill"):
            fail("invalid_action", "Invalid power action")
        self._get(sid)
        def perform(rec):
            if action in ("stop", "restart", "kill"): self._stop_server(sid, kill=action == "kill")
            if action in ("start", "restart"): self._start(sid, rec)
        return self.job(sid, action, perform)

    def logs_view(self, sid: str, after: int) -> dict:
        self._get(sid)
        with self.lock:
            lines = [line for line in self.logs[sid] if line["seq"] > after][:500]
            return {"lines": lines, "cursor": lines[-1]["seq"] if lines else self.cursors[sid]}

    def _file_path(self, sid: str, raw: str, *, allow_root: bool = False) -> pathlib.Path:
        root = self._dir(sid)
        path = safe_path(root, raw, allow_root=allow_root)
        if any(part.startswith((".server-", ".mc-upload-", ".ervisio-", ".addons-")) for part in path.relative_to(root).parts):
            fail("invalid_path", "Reserved path")
        return path

    def file_list(self, sid: str, raw: str = "") -> dict:
        self._get(sid)
        root = self._file_path(sid, raw, allow_root=True)
        if not root.is_dir(): fail("not_found", "Folder not found", 404)
        entries = []
        for item in sorted(root.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if item.is_symlink(): continue
            if item.name.startswith((".server-", ".mc-upload-", ".ervisio-", ".addons-")): continue
            stat = item.stat()
            entries.append({"name": item.name, "type": "directory" if item.is_dir() else "file",
                            "size": stat.st_size if item.is_file() else 0, "mtime": stat.st_mtime})
            if len(entries) >= 1000: break
        return {"path": raw, "entries": entries}

    def file_read(self, sid: str, raw: str, offset: int, length: int) -> dict:
        path = self._file_path(sid, raw)
        if not path.is_file(): fail("not_found", "File not found", 404)
        offset = int_range(offset, 0, MAX_UPLOAD, "offset")
        length = int_range(length, 1, MAX_FILE_READ, "length")
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(offset); data = fh.read(length)
        return {"data": base64.b64encode(data).decode("ascii"), "size": size,
                "offset": offset, "next": offset + len(data), "eof": offset + len(data) >= size}

    def file_upload(self, sid: str, body: dict) -> dict:
        self._get(sid)
        target = self._file_path(sid, body.get("path"))
        if target.name in ("eula.txt", "server.properties", "state.json"):
            fail("reserved_file", "Change this file through its dedicated settings")
        if not target.parent.is_dir(): fail("not_found", "Destination folder not found", 404)
        if target.is_dir(): fail("invalid_path", "The destination is a folder")
        total = int_range(body.get("total"), 0, MAX_UPLOAD, "total")
        offset = int_range(body.get("offset", 0), 0, MAX_UPLOAD, "offset")
        encoded = body.get("data")
        if not isinstance(encoded, str) or len(encoded) > 192 * 1024:
            fail("invalid_input", "Upload chunk too large")
        try: data = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error): fail("invalid_input", "Invalid base64")
        if len(data) > 128 * 1024 or offset + len(data) > total:
            fail("invalid_input", "Invalid chunk size")
        upload_id = body.get("uploadId")
        if offset == 0 and not upload_id:
            upload_id = uuid.uuid4().hex
            meta = {"serverId": sid, "path": body["path"], "total": total, "created": time.time()}
            atomic_json(self.uploads_dir / f"{upload_id}.json", meta)
            (self.uploads_dir / f"{upload_id}.part").touch(exist_ok=False)
        else:
            valid_id(upload_id)
            meta = load_json(self.uploads_dir / f"{upload_id}.json", None)
            if not meta or meta.get("serverId") != sid or meta.get("path") != body["path"] or meta.get("total") != total:
                fail("upload_not_found", "Upload not found", 404)
        part = self.uploads_dir / f"{upload_id}.part"
        if part.stat().st_size != offset:
            fail("upload_offset", "Upload offset does not match", 409)
        with part.open("ab") as fh:
            fh.write(data); fh.flush(); os.fsync(fh.fileno())
        next_offset = offset + len(data)
        if body.get("final"):
            if next_offset != total: fail("upload_incomplete", "Incomplete upload", 409)
            os.replace(part, target)
            (self.uploads_dir / f"{upload_id}.json").unlink(missing_ok=True)
        return {"uploadId": upload_id, "next": next_offset}

    def file_text(self, sid: str, body: dict) -> dict:
        path = self._file_path(sid, body.get("path"))
        if path.name in ("eula.txt", "server.properties", "state.json"):
            fail("reserved_file", "Change this file through its dedicated settings")
        value = body.get("text")
        if not isinstance(value, str) or len(value.encode()) > MAX_TEXT:
            fail("invalid_input", "Text too large")
        if not path.is_file(): fail("not_found", "File not found", 404)
        if path.stat().st_size > MAX_TEXT: fail("invalid_input", "The file is too large for the editor")
        atomic_text(path, value)
        return {"ok": True}

    def files_action(self, sid: str, body: dict) -> dict:
        action, raw = body.get("action"), body.get("path")
        path = self._file_path(sid, raw)
        if path.name in ("eula.txt", "server.properties", "state.json") or (path.name == "server.jar" and action != "delete"):
            fail("reserved_file", "This file is managed by a dedicated action")
        if action == "mkdir":
            if not path.parent.is_dir(): fail("not_found", "Parent folder not found", 404)
            path.mkdir(mode=0o750)
        elif action == "move":
            target = self._file_path(sid, body.get("target"))
            if not path.exists() or not target.parent.is_dir() or target.exists():
                fail("invalid_path", "Invalid source or destination")
            if target.name in ("eula.txt", "server.properties", "server.jar", "state.json"):
                fail("reserved_file", "This file is managed by a dedicated action")
            os.rename(path, target)
        elif action == "delete":
            if body.get("confirm") != raw: fail("confirmation_required", "Confirm with the exact path")
            if path.name == "server.jar": self._require_stopped(sid)
            if path.is_dir(): shutil.rmtree(path)
            elif path.is_file(): path.unlink()
            else: fail("not_found", "Path not found", 404)
        elif action == "extract":
            target = self._file_path(sid, body.get("target"))
            if not path.is_file(): fail("not_found", "Archive not found", 404)
            if target == self._dir(sid): fail("invalid_path", "Choose a destination folder")
            if target.exists() and (not target.is_dir() or any(target.iterdir())):
                fail("invalid_path", "The destination must be new or empty")
            def perform(record):
                try:
                    from archives import extract_archive
                    extract_archive(path, target)
                except ValueError as exc:
                    fail("archive_invalid", str(exc))
            return self.job(sid, "extract", perform)
        elif action == "import":
            self._require_stopped(sid)
            if body.get("confirm") != self._get(sid)["name"]:
                fail("confirmation_required", "Confirm with the exact server name")
            if not path.is_file(): fail("not_found", "Archive not found", 404)
            return self.job(sid, "import", lambda record: self._import_server(sid, path, record))
        else: fail("invalid_action", "Invalid file action")
        return {"ok": True}

    def _import_server(self, sid: str, archive: pathlib.Path, record: dict) -> None:
        self._require_stopped(sid)
        source = self._dir(sid)
        staged = self.servers_dir / f".import-{uuid.uuid4().hex}"
        previous = self.servers_dir / f".old-{uuid.uuid4().hex}"
        try:
            from archives import extract_archive
            extract_archive(archive, staged)
            props_path = staged / "server.properties"
            if not props_path.is_file() or props_path.is_symlink():
                fail("archive_invalid", "The archive has no server.properties")
            self._validate_imported_properties(sid, staged)
            for private in staged.glob(".ervisio-*"):
                if private.is_file(): private.unlink()
            if self._get(sid)["engine"] == "custom" and not (staged / "server.jar").is_file():
                fail("archive_invalid", "A custom server needs a server.jar")
            os.rename(source, previous)
            try: os.rename(staged, source)
            except Exception:
                os.rename(previous, source); raise
            shutil.rmtree(previous)
        except ValueError as exc:
            fail("archive_invalid", str(exc))
        finally:
            if staged.exists(): shutil.rmtree(staged)

    def _validate_imported_properties(self, sid: str, staged: pathlib.Path) -> None:
        props_path = staged / "server.properties"
        if not props_path.is_file() or props_path.is_symlink() or props_path.stat().st_size > MAX_TEXT:
            fail("archive_invalid", "server.properties is missing or too large")
        try: text = props_path.read_text(encoding="utf-8")
        except UnicodeDecodeError: fail("archive_invalid", "server.properties is not UTF-8")
        parsed = properties_parse(text)
        int_range(parsed.get("max-players", 20), 1, 10000, "max-players")
        valid_name(parsed.get("level-name", "world"), "world name")
        safe_path(staged, parsed.get("level-name", "world"))
        atomic_text(props_path, properties_set(text, {"server-port": str(self._get(sid)["port"])}))

    def properties(self, sid: str) -> dict:
        path = self._dir(sid) / "server.properties"
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        return {"text": text, "values": properties_parse(text)}

    def put_properties(self, sid: str, body: dict) -> dict:
        server = self._get(sid)
        value = body.get("text")
        if not isinstance(value, str) or len(value.encode()) > MAX_TEXT or "\x00" in value:
            fail("invalid_input", "Invalid properties")
        parsed = properties_parse(value)
        port = int_range(parsed.get("server-port", server["port"]), 1024, 65535, "server-port")
        int_range(parsed.get("max-players", 20), 1, 10000, "max-players")
        if any(other != sid and s["port"] == port for other, s in self.servers.items()):
            fail("port_in_use", "Another server already uses this port", 409)
        if server["state"] not in ("offline", "error") and port != server["port"]:
            fail("server_running", "Stop the server to change its port", 409)
        valid_name(parsed.get("level-name", "world"), "world name")
        safe_path(self._dir(sid), parsed.get("level-name", "world"))
        value = properties_set(value, {"server-port": str(port)})
        with self.server_locks[sid], self.lock:
            atomic_text(self._dir(sid) / "server.properties", value)
            server["port"] = port
            self._save()
        return {"ok": True}

    def _backup_path(self, sid: str, backup_id: str) -> pathlib.Path:
        valid_id(backup_id)
        return self.backups_dir / sid / f"{backup_id}.tar.gz"

    def backups(self, sid: str) -> dict:
        self._get(sid)
        root = self.backups_dir / sid
        entries = []
        if root.is_dir():
            for path in root.glob("*.tar.gz"):
                if path.is_symlink() or not ID_RE.fullmatch(path.name[:-7]): continue
                stat = path.stat()
                entries.append({"id": path.name[:-7], "name": path.name[:-7], "size": stat.st_size,
                                "created": dt.datetime.fromtimestamp(stat.st_mtime, dt.timezone.utc).isoformat(timespec="seconds")})
        return {"backups": sorted(entries, key=lambda x: x["created"], reverse=True)}

    def backup_file(self, sid: str, backup_id: str, offset: int, length: int) -> dict:
        self._get(sid)
        path = self._backup_path(sid, backup_id)
        if path.is_symlink() or not path.is_file(): fail("not_found", "Backup not found", 404)
        offset = int_range(offset, 0, MAX_UPLOAD * 4, "offset")
        length = int_range(length, 1, MAX_FILE_READ, "length")
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(offset); data = fh.read(length)
        return {"data": base64.b64encode(data).decode("ascii"), "size": size,
                "offset": offset, "next": offset + len(data), "eof": offset + len(data) >= size}

    def _require_stopped(self, sid: str) -> None:
        process = self.processes.get(sid)
        if process and process.poll() is None:
            fail("server_running", "Stop the server first", 409)

    def _backup_create(self, sid: str, record: dict) -> None:
        self._require_stopped(sid)
        source = self._dir(sid)
        target_dir = self.backups_dir / sid
        target_dir.mkdir(mode=0o750, exist_ok=True)
        backup_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
        target = self._backup_path(sid, backup_id)
        temporary = target.with_suffix(".tmp")
        try:
            with tarfile.open(temporary, "w:gz", dereference=False) as archive:
                for base, dirs, files in os.walk(source, followlinks=False):
                    base_path = pathlib.Path(base)
                    dirs[:] = [d for d in dirs if not (base_path / d).is_symlink()]
                    for filename in files:
                        item = base_path / filename
                        if item.is_symlink() or not item.is_file() or filename.startswith(".server-"):
                            continue
                        archive.add(item, arcname=str(item.relative_to(source)), recursive=False)
                        if temporary.stat().st_size > MAX_UPLOAD * 4:
                            fail("backup_too_large", "The backup is over the 2 GiB limit")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def _backup_restore(self, sid: str, backup_id: str, record: dict) -> None:
        self._require_stopped(sid)
        archive_path = self._backup_path(sid, backup_id)
        if not archive_path.is_file() or archive_path.is_symlink():
            fail("not_found", "Backup not found", 404)
        source = self._dir(sid)
        staged = self.servers_dir / f".restore-{uuid.uuid4().hex}"
        previous = self.servers_dir / f".old-{uuid.uuid4().hex}"
        staged.mkdir(mode=0o750)
        try:
            with tarfile.open(archive_path, "r:gz") as archive:
                members = archive.getmembers()
                if len(members) > 100000: fail("archive_invalid", "Too many files in the backup")
                total = 0
                for member in members:
                    if not member.isfile() and not member.isdir():
                        fail("archive_invalid", "The backup contains an unsupported entry")
                    output = safe_path(staged, member.name)
                    if member.isdir(): output.mkdir(mode=0o750, parents=True, exist_ok=True); continue
                    total += member.size
                    if total > MAX_UPLOAD * 4: fail("archive_too_large", "The extracted backup is too large")
                    output.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
                    with archive.extractfile(member) as input_fh, output.open("xb") as output_fh:
                        shutil.copyfileobj(input_fh, output_fh, 1024 * 1024)
            self._validate_imported_properties(sid, staged)
            os.rename(source, previous)
            try: os.rename(staged, source)
            except Exception:
                os.rename(previous, source); raise
            shutil.rmtree(previous)
        finally:
            if staged.exists(): shutil.rmtree(staged)

    def backup_action(self, sid: str, body: dict) -> dict:
        self._get(sid)
        action = body.get("action")
        if action == "create":
            self._require_stopped(sid)
            return self.job(sid, "backup", lambda rec: self._backup_create(sid, rec))
        backup_id = valid_id(body.get("backupId"))
        if body.get("confirm") != backup_id:
            fail("confirmation_required", "Confirm with the exact backup ID")
        if action == "restore":
            self._require_stopped(sid)
            return self.job(sid, "restore", lambda rec: self._backup_restore(sid, backup_id, rec))
        if action == "delete":
            path = self._backup_path(sid, backup_id)
            if not path.is_file() or path.is_symlink(): fail("not_found", "Backup not found", 404)
            path.unlink(); self._event(sid, "backup-delete", "completed")
            return {"ok": True}
        fail("invalid_action", "Invalid backup action")

    def worlds(self, sid: str) -> dict:
        self._get(sid)
        root = self._dir(sid)
        active = self.properties(sid)["values"].get("level-name", "world")
        found = []
        for item in root.iterdir():
            if item.is_symlink() or not item.is_dir(): continue
            if (item / "level.dat").is_symlink() or not (item / "level.dat").is_file(): continue
            found.append({"name": item.name, "path": item.name, "active": item.name == active})
        return {"worlds": sorted(found, key=lambda x: x["name"].lower())}

    def world_action(self, sid: str, body: dict) -> dict:
        name = valid_name(body.get("name"), "world name")
        path = self._file_path(sid, name)
        if not path.is_dir() or not (path / "level.dat").is_file():
            fail("not_found", "World not found", 404)
        action = body.get("action")
        if action == "activate":
            props = self.properties(sid)["text"]
            return self.put_properties(sid, {"text": properties_set(props, {"level-name": name})})
        if action == "delete":
            self._require_stopped(sid)
            if body.get("confirm") != name: fail("confirmation_required", "Confirm with the world name")
            if self.properties(sid)["values"].get("level-name", "world") == name:
                fail("world_active", "Make another world active before deleting this one", 409)
            shutil.rmtree(path)
            return {"ok": True}
        fail("invalid_action", "Invalid world action")

    def players(self, sid: str) -> dict:
        server = self._get(sid)
        directory = self._dir(sid)
        data = {}
        for key, filename in (("whitelist", "whitelist.json"), ("ops", "ops.json"), ("banned", "banned-players.json")):
            target = self._file_path(sid, filename)
            value = load_json(target, [])
            data[key] = value if isinstance(value, list) else []
        online = []
        if server["state"] == "online":
            status = slp_status(server["port"])
            online = status.get("players", {}).get("sample", []) if status else []
        samples = [{"name": p.get("name", ""), "uuid": p.get("id")} for p in online if isinstance(p, dict)]
        data["online"] = samples or list(self.online_players[sid].values())
        return data

    def player_action(self, sid: str, body: dict) -> dict:
        self._get(sid)
        action = body.get("action")
        commands = {"op": "op", "deop": "deop", "whitelist-add": "whitelist add",
                    "whitelist-remove": "whitelist remove", "ban": "ban", "pardon": "pardon", "kick": "kick"}
        if action not in commands: fail("invalid_action", "Invalid player action")
        name = body.get("name")
        if not isinstance(name, str) or not PLAYER_RE.fullmatch(name): fail("invalid_name", "Invalid player name")
        reason = body.get("reason", "")
        if not isinstance(reason, str) or len(reason) > 128 or any(c in reason for c in "\r\n\x00"):
            fail("invalid_input", "Invalid reason")
        command = f"{commands[action]} {name}" + (f" {reason}" if reason and action in ("ban", "kick") else "")
        self._command(sid, command)
        return {"ok": True}

    def _addon_index_path(self, sid: str) -> pathlib.Path:
        return self._dir(sid) / ".ervisio-addons.json"

    def _addon_index(self, sid: str) -> dict:
        data = load_json(self._addon_index_path(sid), {})
        return data if isinstance(data, dict) else {}

    def addons(self, sid: str) -> dict:
        self._get(sid)
        index = self._addon_index(sid)
        found = []
        for folder in ("mods", "plugins"):
            root = self._dir(sid) / folder
            if not root.is_dir(): continue
            for item in sorted(root.iterdir()):
                if item.is_symlink() or not item.is_file() or not item.name.endswith((".jar", ".jar.disabled")):
                    continue
                relative = f"{folder}/{item.name}"
                metadata = index.get(relative, {})
                found.append({"name": item.name.removesuffix(".disabled"), "path": relative,
                              "enabled": item.name.endswith(".jar"), "projectId": metadata.get("projectId"),
                              "versionId": metadata.get("versionId")})
        return {"addons": found}

    def _modrinth_versions(self, project_id: str, engine: str, mc_version: str) -> list[dict]:
        if not ID_RE.fullmatch(project_id): fail("invalid_input", "Invalid project ID")
        loader = "paper" if engine in ("paper", "purpur") else engine
        query = urllib.parse.urlencode({"loaders": json.dumps([loader]), "game_versions": json.dumps([mc_version])})
        versions = fetch_json("https://api.modrinth.com/v2/project/" + project_id + "/version?" + query)
        return versions if isinstance(versions, list) else []

    def _plan_addon(self, project_id: str, engine: str, mc_version: str,
                    plan: dict, *, version_id: str | None = None, depth: int = 0) -> None:
        if depth > 20 or len(plan) > 100: fail("dependency_limit", "Too many dependencies")
        if project_id in plan: return
        versions = self._modrinth_versions(project_id, engine, mc_version)
        version = next((v for v in versions if v.get("id") == version_id), None) if version_id else (versions[0] if versions else None)
        if not version: fail("addon_incompatible", f"No server version of {project_id} works with this server", 404)
        files = version.get("files", [])
        artifact = next((f for f in files if f.get("primary")), files[0] if files else None)
        if not artifact or not artifact.get("url") or not artifact.get("hashes", {}).get("sha512"):
            fail("addon_invalid", "The add-on file has no SHA-512 checksum")
        filename = artifact.get("filename", "")
        if not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9_.+()\-]{1,180}\.jar", filename):
            fail("addon_invalid", "Invalid add-on file name")
        plan[project_id] = {"versionId": version["id"], "artifact": artifact, "filename": filename}
        for dep in version.get("dependencies", []):
            if dep.get("dependency_type") != "required": continue
            dep_project = dep.get("project_id")
            dep_version = dep.get("version_id")
            if dep_project:
                self._plan_addon(dep_project, engine, mc_version, plan, version_id=dep_version, depth=depth + 1)
            elif dep_version:
                detail = fetch_json("https://api.modrinth.com/v2/version/" + urllib.parse.quote(dep_version, safe=""))
                self._plan_addon(detail["project_id"], engine, mc_version, plan, version_id=dep_version, depth=depth + 1)

    def _install_addon(self, sid: str, project_id: str, record: dict) -> None:
        server = self._get(sid)
        if server["engine"] not in ("paper", "purpur", "fabric", "forge", "neoforge"):
            fail("addon_unsupported", "This server software does not support add-ons", 409)
        folder = "plugins" if server["engine"] in ("paper", "purpur") else "mods"
        plan: dict[str, dict] = {}
        self._plan_addon(project_id, server["engine"], server["version"], plan)
        directory = self._dir(sid) / folder
        stage = self._dir(sid) / f".addons-{uuid.uuid4().hex}"
        stage.mkdir(mode=0o750)
        try:
            filenames = set()
            for n, (project, item) in enumerate(plan.items(), start=1):
                if item["filename"] in filenames:
                    fail("addon_conflict", "Two add-ons use the same file name", 409)
                filenames.add(item["filename"])
                artifact = item["artifact"]
                download(artifact["url"], stage / item["filename"], sha512=artifact["hashes"]["sha512"])
                record["progress"] = int(90 * n / len(plan))
            index = self._addon_index(sid)
            for project, item in plan.items():
                relative = f"{folder}/{item['filename']}"
                old = next((p for p, meta in index.items() if meta.get("projectId") == project), None)
                if old and old != relative:
                    old_path = self._file_path(sid, old)
                    if old_path.is_file(): old_path.unlink()
                    del index[old]
                os.replace(stage / item["filename"], directory / item["filename"])
                index[relative] = {"projectId": project, "versionId": item["versionId"]}
            atomic_json(self._addon_index_path(sid), index)
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def addon_action(self, sid: str, body: dict) -> dict:
        self._get(sid)
        action = body.get("action")
        if action == "install":
            project = valid_id(body.get("projectId"))
            return self.job(sid, "addon-install", lambda rec: self._install_addon(sid, project, rec))
        path = body.get("path")
        if not isinstance(path, str) or not re.fullmatch(r"(?:mods|plugins)/[A-Za-z0-9_.+()\-]{1,180}\.jar(?:\.disabled)?", path):
            fail("invalid_path", "Invalid add-on path")
        source = self._file_path(sid, path)
        if not source.is_file(): fail("not_found", "Add-on not found", 404)
        if action == "update":
            project = self._addon_index(sid).get(path, {}).get("projectId")
            if not project: fail("addon_unmanaged", "This add-on was not installed from the catalogue", 409)
            return self.job(sid, "addon-update", lambda rec: self._install_addon(sid, project, rec))
        index = self._addon_index(sid)
        if action == "toggle":
            enabled = body.get("enabled")
            if not isinstance(enabled, bool): fail("invalid_input", "enabled must be a boolean")
            target_path = path.removesuffix(".disabled") if enabled else (path if path.endswith(".disabled") else path + ".disabled")
            if target_path == path: return {"ok": True}
            target = self._file_path(sid, target_path)
            if target.exists(): fail("addon_conflict", "The destination file already exists", 409)
            os.rename(source, target)
            if path in index: index[target_path] = index.pop(path)
            atomic_json(self._addon_index_path(sid), index)
            return {"ok": True}
        if action == "delete":
            if body.get("confirm") != path: fail("confirmation_required", "Confirm with the exact path")
            source.unlink(); index.pop(path, None); atomic_json(self._addon_index_path(sid), index)
            return {"ok": True}
        fail("invalid_action", "Invalid add-on action")

    def catalog_versions(self, engine: str) -> dict:
        if engine not in ENGINES: fail("invalid_engine", "Unsupported software")
        if engine in ("vanilla", "custom", "fabric"):
            manifest = fetch_json("https://piston-meta.mojang.com/mc/game/version_manifest_v2.json")
            versions = [v["id"] for v in manifest.get("versions", []) if v.get("type") == "release"]
        elif engine == "paper":
            data = fetch_json("https://fill.papermc.io/v3/projects/paper/versions")
            versions = [v["version"]["id"] for v in data.get("versions", [])]
        elif engine == "purpur":
            data = fetch_json("https://api.purpurmc.org/v2/purpur")
            versions = list(reversed(data.get("versions", [])))
        else:
            try:
                import forge_catalog
                versions = forge_catalog.versions(engine)
            except (ValueError, urllib.error.URLError) as exc:
                fail("remote_error", str(exc), 502)
        return {"versions": versions[:200]}

    def catalog_loaders(self, engine: str, version: str) -> dict:
        if engine not in ("forge", "neoforge") or not MC_RE.fullmatch(version):
            fail("invalid_input", "Invalid software or version")
        try:
            import forge_catalog
            return {"versions": forge_catalog.loader_versions(engine, version)}
        except (ValueError, urllib.error.URLError) as exc:
            fail("remote_error", str(exc), 502)

    def catalog_addons(self, sid: str, query: str) -> dict:
        server = self._get(sid)
        if server["engine"] not in ("paper", "purpur", "fabric", "forge", "neoforge"):
            return {"hits": []}
        if not isinstance(query, str) or len(query) > 100: fail("invalid_input", "Invalid search")
        loader = "paper" if server["engine"] in ("paper", "purpur") else server["engine"]
        facets = [["versions:" + server["version"]], ["categories:" + loader]]
        params = urllib.parse.urlencode({"query": query, "facets": json.dumps(facets), "limit": 20})
        results = fetch_json("https://api.modrinth.com/v2/search?" + params)
        return {"hits": [{"projectId": h.get("project_id"), "title": h.get("title"),
                          "description": h.get("description"), "icon": h.get("icon_url"),
                          "downloads": h.get("downloads")} for h in results.get("hits", [])]}

    def schedules(self, sid: str) -> dict:
        self._get(sid)
        try: value = load_json(self._dir(sid) / ".ervisio-schedules.json", [])
        except (json.JSONDecodeError, UnicodeDecodeError): value = []
        return {"schedules": value if isinstance(value, list) else []}

    def put_schedules(self, sid: str, body: dict) -> dict:
        self._get(sid)
        schedules = body.get("schedules")
        if not isinstance(schedules, list) or len(schedules) > 20:
            fail("invalid_input", "Invalid schedule list")
        verified, seen = [], set()
        for schedule in schedules:
            if not isinstance(schedule, dict): fail("invalid_input", "Invalid schedule")
            schedule_id = valid_id(schedule.get("id", uuid.uuid4().hex[:10]))
            if schedule_id in seen: fail("invalid_input", "Duplicate schedule ID")
            seen.add(schedule_id)
            action = schedule.get("action")
            if action not in ("backup", "restart"): fail("invalid_action", "Invalid scheduled action")
            hour = int_range(schedule.get("hour"), 0, 23, "hour")
            minute = int_range(schedule.get("minute"), 0, 59, "minute")
            enabled = schedule.get("enabled", True)
            if not isinstance(enabled, bool): fail("invalid_input", "enabled must be a boolean")
            verified.append({"id": schedule_id, "action": action, "hour": hour,
                             "minute": minute, "enabled": enabled, "lastRun": schedule.get("lastRun")})
        with self.server_locks[sid]:
            atomic_json(self._dir(sid) / ".ervisio-schedules.json", verified)
        return {"ok": True}

    def _scheduled_backup(self, sid: str, record: dict) -> None:
        process = self.processes.get(sid)
        was_running = bool(process and process.poll() is None)
        if was_running: self._stop_server(sid)
        try: self._backup_create(sid, record)
        finally:
            if was_running: self._start(sid)

    def _scheduler(self) -> None:
        while not self._stop.wait(15):
            current = dt.datetime.now().astimezone()
            for sid in list(self.servers):
                with self.server_locks[sid]:
                    path = self._dir(sid) / ".ervisio-schedules.json"
                    try: schedules = load_json(path, [])
                    except (json.JSONDecodeError, UnicodeDecodeError, OSError): continue
                    if not isinstance(schedules, list): continue
                    changed = False
                    for schedule in schedules:
                        if not isinstance(schedule, dict): continue
                        if not schedule.get("enabled") or schedule.get("hour") != current.hour or schedule.get("minute") != current.minute:
                            continue
                        marker = current.strftime("%Y-%m-%d")
                        if str(schedule.get("lastRun", "")).startswith(marker): continue
                        schedule["lastRun"] = current.isoformat(timespec="seconds"); changed = True
                        action = schedule.get("action")
                        if action == "backup": self.job(sid, "scheduled-backup", lambda rec, sid=sid: self._scheduled_backup(sid, rec))
                        elif action == "restart": self.job(sid, "scheduled-restart", lambda rec, sid=sid: (self._stop_server(sid), self._start(sid)))
                    if changed: atomic_json(path, schedules)

    def dispatch(self, method: str, route: str, query: dict[str, list[str]] | None = None,
                 body: Any = _MISSING) -> dict:
        """Pure routing surface used by both the Unix HTTP handler and tests."""
        query = {} if query is None else query
        body = {} if body is _MISSING else body
        if not isinstance(body, dict): fail("invalid_input", "The body must be a JSON object")
        def q(key: str, default: str = "") -> str:
            value = query.get(key, [default])
            return value[0] if value else default
        parts = [p for p in route.split("/") if p]
        if parts[:1] != ["v1"]: fail("not_found", "Endpoint not found", 404)
        if parts == ["v1", "health"] and method == "GET":
            disk = shutil.disk_usage(self.data)
            return {"version": VERSION, "java": self.java, "data": str(self.data),
                    "uptime": int(time.monotonic() - self.started),
                    "disk": {"total": disk.total, "free": disk.free}}
        if parts == ["v1", "servers"]:
            if method == "GET": return self.list_servers()
            if method == "POST": return self.create(body)
        if parts == ["v1", "jobs"] and method == "GET":
            with self.lock: return {"jobs": [dict(job) for job in self.jobs]}
        if parts == ["v1", "activity"] and method == "GET":
            with self.lock: return {"events": list(self.activity)}
        if parts == ["v1", "catalog", "versions"] and method == "GET":
            return self.catalog_versions(q("engine", "vanilla"))
        if parts == ["v1", "catalog", "loaders"] and method == "GET":
            return self.catalog_loaders(q("engine"), q("version"))
        if parts == ["v1", "catalog", "addons"] and method == "GET":
            return self.catalog_addons(q("server"), q("q"))
        if len(parts) < 3 or parts[:2] != ["v1", "servers"]:
            fail("not_found", "Endpoint not found", 404)
        sid = valid_id(parts[2])
        if len(parts) == 3:
            if method == "GET": return self.server_view(sid)
            if method == "PATCH": return self.patch_server(sid, body)
            if method == "DELETE": return self.delete_server(sid, body)
        if len(parts) != 4: fail("not_found", "Endpoint not found", 404)
        endpoint = parts[3]
        if endpoint == "power" and method == "POST": return self.power(sid, body)
        if endpoint == "command" and method == "POST":
            self._command(sid, body.get("command")); return {"ok": True}
        if endpoint == "logs" and method == "GET":
            return self.logs_view(sid, int_range(q("after", "0"), 0, 2**63-1, "after"))
        if endpoint == "files":
            if method == "GET": return self.file_list(sid, q("path"))
            if method == "POST": return self.files_action(sid, body)
        if endpoint == "file":
            if method == "GET": return self.file_read(sid, q("path"), q("offset", "0"), q("length", str(MAX_FILE_READ)))
            if method == "PUT": return self.file_upload(sid, body)
            if method == "PATCH": return self.file_text(sid, body)
        if endpoint == "properties":
            if method == "GET": return self.properties(sid)
            if method == "PUT": return self.put_properties(sid, body)
        if endpoint == "software" and method == "POST": return self.software(sid, body)
        if endpoint == "addons":
            if method == "GET": return self.addons(sid)
            if method == "POST": return self.addon_action(sid, body)
        if endpoint == "players":
            if method == "GET": return self.players(sid)
            if method == "POST": return self.player_action(sid, body)
        if endpoint == "worlds":
            if method == "GET": return self.worlds(sid)
            if method == "POST": return self.world_action(sid, body)
        if endpoint == "backups":
            if method == "GET": return self.backups(sid)
            if method == "POST": return self.backup_action(sid, body)
        if endpoint == "backup-file" and method == "GET":
            return self.backup_file(sid, q("backupId"), q("offset", "0"), q("length", str(MAX_FILE_READ)))
        if endpoint == "schedules":
            if method == "GET": return self.schedules(sid)
            if method == "PUT": return self.put_schedules(sid, body)
        fail("not_found", "Endpoint not found", 404)


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True
    def __init__(self, socket_path: str, runtime: MinecraftRuntime):
        self.runtime = runtime
        super().__init__(socket_path, RuntimeHandler)


class RuntimeHandler(http.server.BaseHTTPRequestHandler):
    server_version = "ErvisioMinecraft/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        # Avoid logging file contents, console commands, or credentials.
        pass

    def _respond(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try: self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError): pass

    def _serve(self):
        try:
            if self.headers.get("Transfer-Encoding"):
                fail("invalid_input", "Transfer-Encoding not supported")
            try: length = int(self.headers.get("Content-Length", "0"))
            except ValueError: fail("invalid_input", "Invalid Content-Length")
            if length < 0 or length > MAX_JSON:
                fail("request_too_large", "Request too large", 413)
            raw = self.rfile.read(length) if length else b""
            try: body = json.loads(raw) if raw else {}
            except json.JSONDecodeError: fail("invalid_json", "Invalid JSON")
            parsed = urllib.parse.urlsplit(self.path)
            result = self.server.runtime.dispatch(self.command, parsed.path,
                                                  urllib.parse.parse_qs(parsed.query, keep_blank_values=True), body)
            self._respond(200, result)
        except APIError as exc:
            self._respond(exc.status, {"error": {"code": exc.code, "message": exc.message}})
        except Exception as exc:
            self._respond(500, {"error": {"code": "internal_error", "message": str(exc)}})

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _serve

    def do_OPTIONS(self):
        self._respond(405, {"error": {"code": "method_not_allowed", "message": "Method not supported"}})

    do_HEAD = do_OPTIONS


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Ervisio Minecraft runtime")
    parser.add_argument("--socket", default="/run/ervisio-minecraft/control.sock")
    parser.add_argument("--data", default="/srv/ervisio/minecraft")
    parser.add_argument("--java", default="java")
    args = parser.parse_args(argv)
    socket_path = pathlib.Path(args.socket).absolute()
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    data_path = pathlib.Path(args.data).absolute()
    data_path.mkdir(parents=True, exist_ok=True)
    lock_file = (data_path / ".runtime.lock").open("a+")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError("Another Minecraft runtime already manages this data directory")
    if socket_path.exists():
        if not socket_path.is_socket(): raise RuntimeError("Control path is not a socket")
        probe = socket.socket(socket.AF_UNIX)
        try:
            probe.settimeout(.5)
            probe.connect(str(socket_path))
        except (ConnectionRefusedError, FileNotFoundError):
            socket_path.unlink()
        else:
            raise RuntimeError("Minecraft control socket is already active")
        finally:
            probe.close()
    runtime = MinecraftRuntime(data_path, args.java)
    previous_umask = os.umask(0o027)
    def shutdown_signal(signum, frame):
        raise KeyboardInterrupt
    previous_term = signal.signal(signal.SIGTERM, shutdown_signal)
    previous_int = signal.signal(signal.SIGINT, shutdown_signal)
    try:
        with UnixHTTPServer(str(socket_path), runtime) as server:
            os.chmod(socket_path, 0o660)
            try: server.serve_forever(poll_interval=.5)
            except KeyboardInterrupt: pass
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
        os.umask(previous_umask)
        runtime.close()
        socket_path.unlink(missing_ok=True)
        lock_file.close()


if __name__ == "__main__":
    main()
