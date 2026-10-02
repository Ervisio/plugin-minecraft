"""Official Forge/NeoForge installer catalogue; shell scripts are never run.

NeoForge installation contract: https://docs.neoforged.net/user/docs/server/
Catalogue data is fetched from the official Maven repositories, not mirrors.
"""
from __future__ import annotations

import functools
import json
import pathlib
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

HOSTS = {"maven.minecraftforge.net", "files.minecraftforge.net", "maven.neoforged.net"}
USER_AGENT = "ervisio-minecraft/0.1.0 (https://github.com/Ervisio)"
FORGE = "https://maven.minecraftforge.net/net/minecraftforge/forge"
NEO = "https://maven.neoforged.net/releases/net/neoforged/neoforge"
NAME = re.compile(r"^[0-9][0-9A-Za-z_.+-]{0,100}$")


class CatalogueError(ValueError):
    pass


def _check(url: str) -> None:
    p = urllib.parse.urlsplit(url)
    if p.scheme != "https" or p.hostname not in HOSTS or p.port not in (None, 443) or p.username or p.password:
        raise CatalogueError("Installer metadata URL is not allowed")


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read(url: str, limit: int = 8 * 1024**2) -> bytes:
    _check(url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.build_opener(_Redirect()).open(request, timeout=30) as response:
        _check(response.url)
        data = response.read(limit + 1)
    if len(data) > limit:
        raise CatalogueError("Installer catalogue response is too large")
    return data


def _key(version: str) -> tuple:
    numbers = tuple(int(v) for v in re.findall(r"\d+", version))
    return (numbers, "beta" not in version and "alpha" not in version, version)


def neo_minecraft_version(loader: str) -> str | None:
    parts = loader.split("-")[0].split(".")
    if len(parts) < 3 or not all(x.isdigit() for x in parts):
        return None
    major, minor = int(parts[0]), int(parts[1])
    if 20 <= major <= 25:
        return f"1.{major}" + (f".{minor}" if minor else "")
    if major >= 26 and len(parts) >= 4:
        patch = int(parts[2])
        return f"{major}.{minor}" + (f".{patch}" if patch else "")
    return None


@functools.lru_cache(maxsize=8)
def _catalogue(engine: str, time_bucket: int) -> tuple[str, ...]:
    del time_bucket
    if engine == "forge":
        raw = _read(FORGE + "/maven-metadata.xml")
        if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
            raise CatalogueError("Unexpected XML entities in catalogue")
        names = [e.text for e in ET.fromstring(raw).findall("./versioning/versions/version") if e.text]
    elif engine == "neoforge":
        # The current Maven REST listing also works when metadata.xml is not
        # served through the repository's file URL.
        listing = json.loads(_read("https://maven.neoforged.net/api/maven/details/releases/net/neoforged/neoforge"))
        names = [item.get("name", "") for item in listing.get("files", []) if item.get("type") == "DIRECTORY"]
    else:
        raise CatalogueError("Unknown mod loader")
    return tuple(name for name in names if NAME.fullmatch(name))


def _all(engine: str) -> tuple[str, ...]:
    return _catalogue(engine, int(time.time()) // 300)


def versions(engine: str) -> list[str]:
    if engine == "forge":
        values = {version.split("-", 1)[0] for version in _all(engine) if "-" in version}
    else:
        values = {neo_minecraft_version(version) for version in _all(engine)}
    return sorted((v for v in values if v), key=_key, reverse=True)


def loader_versions(engine: str, mc_version: str) -> list[str]:
    if not NAME.fullmatch(mc_version):
        raise CatalogueError("Invalid Minecraft version")
    if engine == "forge":
        values = [v for v in _all(engine) if v.startswith(mc_version + "-")]
    else:
        values = [v for v in _all(engine) if neo_minecraft_version(v) == mc_version]
    return sorted(values, key=_key, reverse=True)


def resolve(engine: str, mc_version: str, loader_version: str | None = None) -> dict:
    available = loader_versions(engine, mc_version)
    if not available:
        raise CatalogueError(f"No {engine} installer is available for Minecraft {mc_version}")
    if loader_version:
        if engine == "forge" and not loader_version.startswith(mc_version + "-"):
            loader_version = mc_version + "-" + loader_version
        if loader_version not in available:
            raise CatalogueError("Loader version is not compatible with this Minecraft version")
        chosen = loader_version
    else:
        stable = [v for v in available if "beta" not in v and "alpha" not in v]
        chosen = (stable or available)[0]
    base, filename = (FORGE, "forge") if engine == "forge" else (NEO, "neoforge")
    url = f"{base}/{chosen}/{filename}-{chosen}-installer.jar"
    checksum = _read(url + ".sha1", 4096).decode("ascii").strip().split()[0]
    if not re.fullmatch(r"[0-9a-fA-F]{40}", checksum):
        raise CatalogueError("Installer checksum is invalid")
    return {"url": url, "loaderVersion": chosen, "sha1": checksum.lower()}


def launch_args(server_dir: pathlib.Path, engine: str, loader_version: str) -> list[str]:
    root = pathlib.Path(server_dir)
    vendor = "net/minecraftforge/forge" if engine == "forge" else "net/neoforged/neoforge"
    candidate = root / "libraries" / vendor / loader_version / "unix_args.txt"
    for ancestor in (root, *root.parents):
        if ancestor.is_symlink():
            raise CatalogueError("Symlinks are not allowed in the server path")
    if candidate.is_file():
        relative = candidate.relative_to(root)
        for part in (candidate, *candidate.parents):
            if part == root:
                break
            if part.is_symlink():
                raise CatalogueError("Installer launch arguments must not be symlinks")
        if candidate.stat().st_size > 256 * 1024:
            raise CatalogueError("Installer launch arguments are too large")
        return ["@" + relative.as_posix()]
    if engine == "forge":
        # Older Forge installers emit an executable universal/server JAR.
        jars = [p for p in root.glob(f"forge-{loader_version}*.jar")
                if not p.is_symlink() and "installer" not in p.name and p.is_file()]
        if len(jars) == 1:
            return ["-jar", jars[0].name]
    raise CatalogueError("Installer did not create supported server launch arguments")
