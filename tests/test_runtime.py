"""Isolated tests for the Minecraft control service (no host Java or network)."""
from __future__ import annotations

import importlib.util
import base64
import pathlib
import socket
import http.client
import io
import json
import signal
import sys
import tarfile
import tempfile
import time
import threading
import subprocess
import unittest
from unittest import mock


RUNTIME_FILE = pathlib.Path(__file__).parents[1] / "plugin" / "runtime" / "minecraft_runtime.py"
RUNTIME_DIR = RUNTIME_FILE.parent
sys.path.insert(0, str(RUNTIME_DIR))
SPEC = importlib.util.spec_from_file_location("minecraft_runtime", RUNTIME_FILE)
runtime = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(runtime)


class SafePathTests(unittest.TestCase):
    def test_rejects_absolute_traversal_dot_and_windows_separators(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for value in ("/etc/passwd", "../outside", "dir/../outside", "./file", "dir\\file", ""):
                with self.subTest(path=value), self.assertRaises(runtime.APIError) as caught:
                    runtime.safe_path(root, value)
                self.assertEqual(caught.exception.code, "invalid_path")

    def test_rejects_symlink_components_and_final_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "root"
            outside = pathlib.Path(tmp) / "outside"
            root.mkdir()
            outside.mkdir()
            (outside / "secret").write_text("outside", encoding="utf-8")
            (root / "link").symlink_to(outside, target_is_directory=True)
            (root / "final-link").symlink_to(outside / "secret")
            for value in ("link/secret", "final-link"):
                with self.subTest(path=value), self.assertRaises(runtime.APIError):
                    runtime.safe_path(root, value)

    def test_allows_nested_regular_path_and_explicit_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "folder").mkdir()
            self.assertEqual(runtime.safe_path(root, "folder/file"), root / "folder/file")
            self.assertEqual(runtime.safe_path(root, "", allow_root=True), root)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = pathlib.Path(self.tmp.name)
        self.manager = runtime.MinecraftRuntime(self.data, java="/nonexistent/java")

    def tearDown(self):
        self.manager.close()
        self.tmp.cleanup()

    def create_custom(self, name="Survival", port=25565, **extra):
        body = {"name": name, "engine": "custom", "port": port,
                "version": "1.21.8", "memoryMB": 1024, "eula": True}
        body.update(extra)
        return self.manager.create(body)

    def test_requires_eula_before_creating_server(self):
        with self.assertRaises(runtime.APIError) as caught:
            self.manager.create({"name": "No EULA", "engine": "custom"})
        self.assertEqual(caught.exception.code, "eula_required")
        self.assertEqual(self.manager.list_servers()["servers"], [])

    def test_eula_parser_requires_exact_true_value_before_launch(self):
        server = self.create_custom()
        root = self.manager.servers_dir / server["id"]
        (root / "server.jar").write_bytes(b"fixture only")
        (root / "eula.txt").write_text("eula=trueish\n", encoding="utf-8")
        with self.assertRaises(runtime.APIError) as caught:
            self.manager._start(server["id"])
        self.assertEqual(caught.exception.code, "eula_required")
        self.assertNotIn(server["id"], self.manager.processes)

    def test_rejects_invalid_engine_version_port_memory_and_jvm_execution_flags(self):
        cases = [
            ({"engine": "unknown"}, "invalid_engine"),
            ({"version": "latest"}, "invalid_version"),
            ({"port": 80}, "invalid_input"),
            ({"memoryMB": 128}, "invalid_input"),
            ({"jvmArgs": ["-javaagent:/tmp/payload.jar"]}, "invalid_input"),
        ]
        for change, code in cases:
            body = {"name": "Config", "engine": "custom", "version": "1.21.8",
                    "port": 25565, "memoryMB": 1024, "eula": True, **change}
            with self.subTest(change=change), self.assertRaises(runtime.APIError) as caught:
                self.manager.create(body)
            self.assertEqual(caught.exception.code, code)
        self.assertFalse(self.manager.servers)

    def test_rejects_port_collision_on_create_and_edit(self):
        first = self.create_custom("First", 25565)
        second = self.create_custom("Second", 25566)
        with self.assertRaises(runtime.APIError) as caught:
            self.manager.create({"name": "Third", "engine": "custom", "port": 25565,
                                 "version": "1.21.8", "memoryMB": 1024, "eula": True})
        self.assertEqual(caught.exception.code, "port_in_use")
        with self.assertRaises(runtime.APIError) as caught:
            self.manager.patch_server(second["id"], {"port": first["port"]})
        self.assertEqual(caught.exception.code, "port_in_use")

    def test_persists_metadata_and_properties_across_runtime_restart(self):
        server = self.create_custom("Creative", 25600, motd="Builders", seed="seed-42")
        runtime_instance = self.manager
        runtime_instance.close()
        manager2 = runtime.MinecraftRuntime(self.data, java="/nonexistent/java")
        try:
            restored = manager2.server_view(server["id"])
            self.assertEqual(restored["name"], "Creative")
            self.assertEqual(restored["port"], 25600)
            config = (manager2.servers_dir / server["id"] / "server.properties").read_text()
            self.assertIn("server-port=25600", config)
            self.assertIn("motd=Builders", config)
            self.assertIn("eula=true", (manager2.servers_dir / server["id"] / "eula.txt").read_text())
        finally:
            manager2.close()

    def test_delete_requires_name_confirmation(self):
        server = self.create_custom()
        with self.assertRaises(runtime.APIError) as caught:
            self.manager.delete_server(server["id"], {"confirm": "wrong"})
        self.assertEqual(caught.exception.code, "confirmation_required")
        self.assertIn(server["id"], self.manager.servers)

    def test_file_chunk_upload_requires_contiguous_offsets_and_commits_exact_bytes(self):
        server = self.create_custom()
        payload = b"jar fixture\x00payload"
        part1, part2 = payload[:6], payload[6:]
        first = self.manager.file_upload(server["id"], {
            "path": "mods/example.jar", "total": len(payload),
            "offset": 0, "data": base64.b64encode(part1).decode(), "final": False})
        with self.assertRaises(runtime.APIError) as caught:
            self.manager.file_upload(server["id"], {
                "path": "mods/example.jar", "total": len(payload),
                "offset": 5, "uploadId": first["uploadId"],
                "data": base64.b64encode(part2).decode(), "final": False})
        self.assertEqual(caught.exception.code, "upload_offset")
        done = self.manager.file_upload(server["id"], {
            "path": "mods/example.jar", "total": len(payload),
            "offset": len(part1), "uploadId": first["uploadId"],
            "data": base64.b64encode(part2).decode(), "final": True})
        self.assertEqual(done["next"], len(payload))
        self.assertEqual((self.manager.servers_dir / server["id"] / "mods/example.jar").read_bytes(), payload)

    def test_file_apis_reject_symlink_escape(self):
        server = self.create_custom()
        outside = self.data / "outside.txt"
        outside.write_text("private", encoding="utf-8")
        (self.manager.servers_dir / server["id"] / "escape").symlink_to(outside)
        for operation in (
            lambda: self.manager.file_read(server["id"], "escape", 0, 100),
            lambda: self.manager.file_text(server["id"], {"path": "escape", "text": "changed"}),
            lambda: self.manager.file_upload(server["id"], {
                "path": "escape", "total": 0, "offset": 0, "data": "", "final": True}),
        ):
            with self.subTest(operation=operation), self.assertRaises(runtime.APIError) as caught:
                operation()
            self.assertEqual(caught.exception.code, "invalid_path")
        self.assertEqual(outside.read_text(encoding="utf-8"), "private")

    def test_server_properties_are_reserved_from_file_upload_and_text_editor(self):
        server = self.create_custom()
        body = {"path": "server.properties", "total": 4, "offset": 0,
                "data": base64.b64encode(b"evil").decode(), "final": True}
        for operation in (
            lambda: self.manager.file_upload(server["id"], body),
            lambda: self.manager.file_text(server["id"], {"path": "server.properties", "text": "evil=true"}),
        ):
            with self.subTest(operation=operation), self.assertRaises(runtime.APIError) as caught:
                operation()
            self.assertEqual(caught.exception.code, "reserved_file")
        self.assertIn("server-port=25565", (self.manager.servers_dir / server["id"] / "server.properties").read_text())

    def test_file_listing_reports_file_and_directory_types(self):
        server = self.create_custom()
        root = self.manager.servers_dir / server["id"]
        (root / "mods/sample.jar").write_bytes(b"jar")
        entries = {entry["name"]: entry for entry in self.manager.file_list(server["id"])["entries"]}
        self.assertEqual(entries["mods"]["type"], "directory")
        self.assertEqual(entries["server.properties"]["type"], "file")
        mods = self.manager.file_list(server["id"], "mods")["entries"]
        self.assertEqual([(entry["name"], entry["type"]) for entry in mods], [("sample.jar", "file")])

    def test_backup_round_trip_and_restore_rejects_link_archive_transactionally(self):
        server = self.create_custom()
        root = self.manager.servers_dir / server["id"]
        (root / "mods/sample.txt").write_text("before", encoding="utf-8")
        outside = self.data / "outside.txt"
        outside.write_text("safe", encoding="utf-8")
        (root / "outside-link").symlink_to(outside)
        record = {"progress": 0}
        self.manager._backup_create(server["id"], record)
        backup = self.manager.backups(server["id"])["backups"][0]
        archive_path = self.manager._backup_path(server["id"], backup["id"])
        with tarfile.open(archive_path, "r:gz") as archive:
            self.assertNotIn("outside-link", archive.getnames())
        chunk = self.manager.backup_file(server["id"], backup["id"], 0, 64)
        self.assertEqual(base64.b64decode(chunk["data"]), archive_path.read_bytes()[:64])
        self.assertEqual(chunk["size"], archive_path.stat().st_size)
        (root / "mods/sample.txt").write_text("after", encoding="utf-8")
        self.manager._backup_restore(server["id"], backup["id"], record)
        self.assertEqual((root / "mods/sample.txt").read_text(), "before")

        malicious_id = "malicious"
        malicious_path = self.manager._backup_path(server["id"], malicious_id)
        malicious_path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(malicious_path, "w:gz") as archive:
            member = tarfile.TarInfo("escape")
            member.type = tarfile.SYMTYPE
            member.linkname = str(outside)
            archive.addfile(member)
        prior = (root / "mods/sample.txt").read_text()
        with self.assertRaises(runtime.APIError) as caught:
            self.manager._backup_restore(server["id"], malicious_id, record)
        self.assertEqual(caught.exception.code, "archive_invalid")
        self.assertEqual((root / "mods/sample.txt").read_text(), prior)
        self.assertEqual(outside.read_text(), "safe")

    def test_schedule_validation_and_persistence(self):
        server = self.create_custom()
        schedules = [{"id": "nightly", "action": "backup", "hour": 3, "minute": 15, "enabled": True}]
        self.assertEqual(self.manager.put_schedules(server["id"], {"schedules": schedules}), {"ok": True})
        self.assertEqual(self.manager.schedules(server["id"])["schedules"][0]["action"], "backup")
        with self.assertRaises(runtime.APIError) as caught:
            self.manager.put_schedules(server["id"], {"schedules": [
                {"id": "bad", "action": "shell", "hour": 0, "minute": 0}]})
        self.assertEqual(caught.exception.code, "invalid_action")
        with self.assertRaises(runtime.APIError):
            self.manager.put_schedules(server["id"], {"schedules": [
                {"id": "bad", "action": "backup", "hour": 24, "minute": 0}]})

    def test_backup_restore_reconciles_properties_port_to_managed_port(self):
        server = self.create_custom(port=26701)
        backup_id = "port-mismatch"
        archive_path = self.manager._backup_path(server["id"], backup_id)
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive_path, "w:gz") as archive:
            raw = b"server-port=29999\nmax-players=42\n"
            item = tarfile.TarInfo("server.properties")
            item.size = len(raw)
            archive.addfile(item, io.BytesIO(raw))
        self.manager._backup_restore(server["id"], backup_id, {"progress": 0})
        restored = self.manager.properties(server["id"])["values"]
        self.assertEqual(restored["server-port"], "26701")
        self.assertEqual(restored["max-players"], "42")
        self.assertEqual(self.manager.servers[server["id"]]["port"], 26701)

    def test_full_server_import_validates_files_and_reconciles_managed_port(self):
        server = self.create_custom(port=26702)
        archive_path = self.data / "incoming.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            for name, raw in (("server.properties", b"server-port=29998\nmax-players=32\n"),
                              ("server.jar", b"imported custom jar"),
                              ("world/level.dat", b"world fixture")):
                item = tarfile.TarInfo(name)
                item.size = len(raw)
                archive.addfile(item, io.BytesIO(raw))
        root = self.manager.servers_dir / server["id"]
        (root / "old-file.txt").write_text("old", encoding="utf-8")
        self.manager._import_server(server["id"], archive_path, {"progress": 0})
        self.assertFalse((root / "old-file.txt").exists())
        self.assertEqual((root / "server.jar").read_bytes(), b"imported custom jar")
        self.assertTrue((root / "world/level.dat").is_file())
        self.assertEqual(self.manager.properties(server["id"])["values"]["server-port"], "26702")

        invalid_archive = self.data / "missing-properties.tar.gz"
        with tarfile.open(invalid_archive, "w:gz") as archive:
            raw = b"replacement"
            item = tarfile.TarInfo("server.jar")
            item.size = len(raw)
            archive.addfile(item, io.BytesIO(raw))
        prior_properties = (root / "server.properties").read_text()
        prior_jar = (root / "server.jar").read_bytes()
        with self.assertRaises(runtime.APIError) as caught:
            self.manager._import_server(server["id"], invalid_archive, {"progress": 0})
        self.assertEqual(caught.exception.code, "archive_invalid")
        self.assertEqual((root / "server.properties").read_text(), prior_properties)
        self.assertEqual((root / "server.jar").read_bytes(), prior_jar)

    def test_disabling_an_already_disabled_addon_is_idempotent(self):
        server = self.create_custom()
        root = self.manager.servers_dir / server["id"]
        (root / "mods/example.jar").write_bytes(b"addon")
        self.manager.addon_action(server["id"], {
            "action": "toggle", "path": "mods/example.jar", "enabled": False})
        self.assertEqual(self.manager.addon_action(server["id"], {
            "action": "toggle", "path": "mods/example.jar.disabled", "enabled": False}), {"ok": True})
        self.assertTrue((root / "mods/example.jar.disabled").is_file())
        self.assertFalse((root / "mods/example.jar.disabled.disabled").exists())

    def test_modrinth_dependency_plan_installs_required_jars_with_sha512(self):
        server = self.create_custom()
        self.manager.servers[server["id"]].update(engine="paper", version="1.21.8")
        root_hash, dependency_hash = "a" * 128, "b" * 128
        catalog = {
            "root-project": [{"id": "root-v1", "files": [{"primary": True, "filename": "root.jar",
                            "url": "https://cdn.modrinth.com/root.jar", "hashes": {"sha512": root_hash}}],
                              "dependencies": [{"project_id": "required-lib", "dependency_type": "required"},
                                               {"project_id": "optional-lib", "dependency_type": "optional"}]}],
            "required-lib": [{"id": "lib-v1", "files": [{"primary": True, "filename": "lib.jar",
                            "url": "https://cdn.modrinth/lib.jar", "hashes": {"sha512": dependency_hash}}],
                              "dependencies": [{"project_id": "root-project", "dependency_type": "required"}]}],
        }
        self.manager._modrinth_versions = lambda project, engine, version: catalog.get(project, [])
        calls = []
        def fake_download(url, destination, sha512=None, **kwargs):
            calls.append((url, sha512))
            pathlib.Path(destination).write_bytes(url.encode())
        with mock.patch.object(runtime, "download", side_effect=fake_download):
            self.manager._install_addon(server["id"], "root-project", {"progress": 0})
        installed = self.manager.addons(server["id"])["addons"]
        self.assertEqual({item["name"] for item in installed}, {"root.jar", "lib.jar"})
        self.assertEqual({digest for _, digest in calls}, {root_hash, dependency_hash})
        self.assertFalse(any("optional" in url for url, _ in calls))
        self.assertEqual(len(calls), 2)

    def test_forge_loader_catalogue_route_returns_compatible_builds(self):
        import forge_catalog
        with mock.patch.object(forge_catalog, "loader_versions", return_value=["1.21.1-52.0.1"]):
            result = self.manager.dispatch("GET", "/v1/catalog/loaders", {
                "engine": ["forge"], "version": ["1.21.1"]})
        self.assertEqual(result, {"versions": ["1.21.1-52.0.1"]})


class LifecycleTests(unittest.TestCase):
    """Use a local fake Java process; never launch Java or download a server jar."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = pathlib.Path(self.tmp.name) / "data"
        self.fake_java = pathlib.Path(self.tmp.name) / "fake-java"
        self.fake_java.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "print('FAKE SERVER READY', flush=True)\n"
            "for line in sys.stdin:\n"
            "    command = line.strip()\n"
            "    print('COMMAND:' + command, flush=True)\n"
            "    if command == 'stop': break\n"
            "print('FAKE SERVER STOPPED', flush=True)\n", encoding="utf-8")
        self.fake_java.chmod(0o700)
        self.manager = runtime.MinecraftRuntime(self.data, java=str(self.fake_java))

    def tearDown(self):
        self.manager.close()
        self.tmp.cleanup()

    def create(self, name, port):
        result = self.manager.create({"name": name, "engine": "custom", "version": "1.21.8",
                                      "port": port, "memoryMB": 512, "java": str(self.fake_java), "eula": True})
        (self.manager.servers_dir / result["id"] / "server.jar").write_bytes(b"fake jar marker")
        return result

    def wait_job(self, job_id, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            match = next((j for j in self.manager.jobs if j["id"] == job_id), None)
            if match and match["status"] != "running":
                return match
            time.sleep(.02)
        self.fail("job did not finish")

    def test_two_fake_servers_keep_separate_console_processes_and_stop_cleanly(self):
        alpha = self.create("Alpha", 25601)
        beta = self.create("Beta", 25602)
        alpha_start = self.manager.power(alpha["id"], {"action": "start"})
        beta_start = self.manager.power(beta["id"], {"action": "start"})
        self.assertEqual(self.wait_job(alpha_start["jobId"])["status"], "completed")
        self.assertEqual(self.wait_job(beta_start["jobId"])["status"], "completed")
        self.assertNotEqual(self.manager.processes[alpha["id"]].pid, self.manager.processes[beta["id"]].pid)
        self.manager._command(alpha["id"], "say alpha-only")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            lines = self.manager.logs_view(alpha["id"], 0)["lines"]
            if any("COMMAND:say alpha-only" in line["text"] for line in lines):
                break
            time.sleep(.02)
        self.assertTrue(any("COMMAND:say alpha-only" in line["text"] for line in self.manager.logs_view(alpha["id"], 0)["lines"]))
        self.assertFalse(any("alpha-only" in line["text"] for line in self.manager.logs_view(beta["id"], 0)["lines"]))
        stop = self.manager.power(alpha["id"], {"action": "stop"})
        self.assertEqual(self.wait_job(stop["jobId"])["status"], "completed")
        self.assertIsNotNone(self.manager.processes[alpha["id"]].poll())
        self.assertIsNone(self.manager.processes[beta["id"]].poll())


    def test_switching_to_custom_software_records_engine_and_version(self):
        server = self.create("Switch", 25603)
        with self.manager.lock:
            self.manager.servers[server["id"]].update(engine="paper", version="1.21.1")
        job = self.manager.software(server["id"], {"engine": "custom", "version": "1.21.4"})
        self.assertEqual(self.wait_job(job["jobId"])["status"], "completed")
        view = self.manager.server_view(server["id"])
        self.assertEqual((view["engine"], view["version"]), ("custom", "1.21.4"))

    def test_health_reports_free_disk(self):
        health = self.manager.dispatch("GET", "/v1/health")
        self.assertGreater(health["disk"]["total"], 0)
        self.assertLessEqual(health["disk"]["free"], health["disk"]["total"])


class UnixHTTPRouteTests(unittest.TestCase):
    """Exercise the same Unix socket and HTTP handler used by the service."""

    class UnixConnection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.connect(self.host)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.socket_path = root / "control.sock"
        self.manager = runtime.MinecraftRuntime(root / "data", java="/missing/java")
        self.server = runtime.UnixHTTPServer(str(self.socket_path), self.manager)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.manager.close()
        self.tmp.cleanup()

    def request(self, method, path, body=None):
        connection = self.UnixConnection(str(self.socket_path))
        raw = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if raw is not None else {}
        connection.request(method, path, body=raw, headers=headers)
        response = connection.getresponse()
        result = json.loads(response.read())
        status = response.status
        connection.close()
        return status, result

    def test_http_health_crud_and_errors_over_unix_socket(self):
        status, health = self.request("GET", "/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["version"], runtime.VERSION)
        status, created = self.request("POST", "/v1/servers", {
            "name": "HTTP test", "engine": "custom", "version": "1.21.8",
            "port": 26551, "memoryMB": 512, "eula": True})
        self.assertEqual(status, 200)
        sid = created["id"]
        status, listed = self.request("GET", "/v1/servers")
        self.assertEqual(status, 200)
        self.assertEqual([row["id"] for row in listed["servers"]], [sid])
        status, failure = self.request("GET", "/v1/unknown")
        self.assertEqual(status, 404)
        self.assertEqual(failure["error"]["code"], "not_found")
        status, failure = self.request("POST", "/v1/servers", {"name": "No EULA", "engine": "custom"})
        self.assertEqual(status, 400)
        self.assertEqual(failure["error"]["code"], "eula_required")
        status, failure = self.request("POST", "/v1/servers", [])
        self.assertEqual(status, 400)
        self.assertEqual(failure["error"]["code"], "invalid_input")


class RuntimeProcessTests(unittest.TestCase):
    """Cover real process locking and SIGTERM cleanup in an isolated temp tree."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.data = self.root / "data"
        self.socket_path = self.root / "control.sock"
        self.fake_java = self.root / "fake-java"
        self.fake_java.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            "print('FAKE SERVER READY', flush=True)\n"
            "for line in sys.stdin:\n"
            "    if line.strip() == 'stop':\n"
            "        pathlib.Path('graceful.marker').write_text('stopped')\n"
            "        print('FAKE SERVER STOPPED', flush=True)\n"
            "        break\n", encoding="utf-8")
        self.fake_java.chmod(0o700)
        self.child_processes = []

    def tearDown(self):
        for process in self.child_processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            if process.stderr:
                process.stderr.close()
        self.tmp.cleanup()

    def spawn_runtime(self, sock):
        process = subprocess.Popen(
            [sys.executable, str(RUNTIME_FILE), "--socket", str(sock), "--data", str(self.data),
             "--java", str(self.fake_java)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        self.child_processes.append(process)
        return process

    def call(self, method, route, body=None, sock=None):
        class Connection(http.client.HTTPConnection):
            def connect(inner_self):
                inner_self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                inner_self.sock.connect(str(sock or self.socket_path))
        connection = Connection(str(sock or self.socket_path))
        raw = json.dumps(body).encode() if body is not None else None
        try:
            connection.request(method, route, body=raw,
                               headers={"Content-Type": "application/json"} if raw is not None else {})
            response = connection.getresponse()
            result = json.loads(response.read())
            return response.status, result
        finally:
            connection.close()

    def wait_healthy(self, process, sock, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(f"runtime exited early: {process.stderr.read() if process.stderr else ''}")
            try:
                status, result = self.call("GET", "/v1/health", sock=sock)
                if status == 200:
                    return result
            except OSError:
                time.sleep(.025)
        self.fail("runtime Unix socket did not become healthy")

    def test_singleton_lock_blocks_second_runtime_and_sigterm_stops_game_then_releases_lock(self):
        first = self.spawn_runtime(self.socket_path)
        self.wait_healthy(first, self.socket_path)

        second_socket = self.root / "second.sock"
        second = self.spawn_runtime(second_socket)
        second_output, second_error = second.communicate(timeout=3)
        if second.stderr:
            second.stderr.close()
        self.assertNotEqual(second.returncode, 0, second_output)
        self.assertIn("Another Minecraft runtime", second_error)
        self.assertFalse(second_socket.exists())

        status, server = self.call("POST", "/v1/servers", {
            "name": "Graceful", "engine": "custom", "version": "1.21.8",
            "port": 26801, "memoryMB": 512, "eula": True, "java": str(self.fake_java)})
        self.assertEqual(status, 200, server)
        sid = server["id"]
        payload = b"local fake jar marker"
        status, _ = self.call("PUT", f"/v1/servers/{sid}/file", {
            "path": "server.jar", "total": len(payload), "offset": 0,
            "data": base64.b64encode(payload).decode(), "final": True})
        self.assertEqual(status, 200)
        start_status, job = self.call("POST", f"/v1/servers/{sid}/power", {"action": "start"})
        self.assertEqual(start_status, 200)
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            _, jobs = self.call("GET", "/v1/jobs")
            started = next((item for item in jobs["jobs"] if item["id"] == job["jobId"]), None)
            if started and started["status"] != "running":
                break
            time.sleep(.025)
        self.assertIsNotNone(started)
        self.assertEqual(started["status"], "completed")

        first.send_signal(signal.SIGTERM)
        self.assertEqual(first.wait(timeout=8), 0)
        marker = self.data / "servers" / sid / "graceful.marker"
        self.assertTrue(marker.is_file(), "runtime SIGTERM should send stop through Java stdin")
        self.assertFalse(self.socket_path.exists())

        replacement = self.spawn_runtime(self.socket_path)
        self.wait_healthy(replacement, self.socket_path)
        replacement.send_signal(signal.SIGTERM)
        self.assertEqual(replacement.wait(timeout=5), 0)
        self.assertFalse(self.socket_path.exists())


class PropertiesTests(unittest.TestCase):
    def test_parse_and_update_properties_preserving_comments(self):
        text = "# server config\nserver-port=25565\nmotd=Old\n"
        changed = runtime.properties_set(text, {"server-port": "25570", "difficulty": "hard"})
        self.assertTrue(changed.startswith("# server config\n"))
        self.assertEqual(runtime.properties_parse(changed), {
            "server-port": "25570", "motd": "Old", "difficulty": "hard"})


if __name__ == "__main__":
    unittest.main()
