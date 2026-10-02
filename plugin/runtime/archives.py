"""Transactional ZIP/tar import. Never execute files or follow archive links."""
from __future__ import annotations

import os
import pathlib
import shutil
import stat
import tarfile
import tempfile
import zipfile


class ArchiveError(ValueError):
    pass


def _relative(name: str) -> pathlib.PurePosixPath:
    if not name or len(name) > 4096 or "\\" in name or any(ord(c) < 32 for c in name):
        raise ArchiveError("Invalid archive entry name")
    # A leading ./ is common in tar files and is harmless; interior dot segments
    # and any parent segment are refused instead of normalised.
    while name.startswith("./"):
        name = name[2:]
    name = name.rstrip("/")
    if name in ("", "."):
        return pathlib.PurePosixPath(".")
    if name.startswith("/") or ":" in name.split("/")[0]:
        raise ArchiveError("Absolute archive paths are not allowed")
    if any(segment in ("", ".", "..") for segment in name.split("/")):
        raise ArchiveError("Archive entries must stay inside the destination")
    return pathlib.PurePosixPath(name)


def _ancestors_no_links(path: pathlib.Path) -> None:
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ArchiveError("Symlinks are not allowed in the destination path")


def _copy(source, target: pathlib.Path, expected: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    copied = 0
    with target.open("xb") as output:
        while block := source.read(min(1024 * 1024, expected - copied + 1)):
            copied += len(block)
            if copied > expected:
                raise ArchiveError("Archive entry exceeds its declared size")
            output.write(block)
    if copied != expected:
        raise ArchiveError("Archive entry is truncated")
    target.chmod(0o640)


def extract_archive(archive: pathlib.Path, destination: pathlib.Path,
                    max_bytes: int = 2 * 1024**3, max_files: int = 20000) -> dict:
    """Extract into a new/empty directory, or leave it unchanged on failure.

    The caller confines both paths to its managed server folder. Archives with
    links, devices, duplicate entries or ambiguous paths are rejected. Size and
    member limits apply before extraction; actual bytes are checked as well.
    """
    archive, destination = pathlib.Path(archive), pathlib.Path(destination)
    _ancestors_no_links(archive)
    _ancestors_no_links(destination)
    if not archive.is_file():
        raise ArchiveError("Archive is not a regular file")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ArchiveError("Choose a new or empty destination directory")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    staging = pathlib.Path(tempfile.mkdtemp(prefix=".extract-", dir=destination.parent))
    seen: set[str] = set()
    total = count = 0

    def entry(name: str, size: int, directory: bool) -> pathlib.Path | None:
        nonlocal total, count
        relative = _relative(name)
        if str(relative) == ".":
            if not directory:
                raise ArchiveError("Root entry must be a directory")
            return None
        if str(relative) in seen:
            raise ArchiveError("Duplicate archive entry")
        seen.add(str(relative))
        count += 1
        total += size
        if count > max_files or total > max_bytes or size < 0:
            raise ArchiveError("Archive exceeds the extraction limits")
        return staging.joinpath(*relative.parts)

    try:
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as package:
                members = package.infolist()
                if len(members) > max_files:
                    raise ArchiveError("Archive has too many entries")
                prepared = []
                for member in members:
                    mode = member.external_attr >> 16
                    kind = stat.S_IFMT(mode)
                    if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                        raise ArchiveError("Archive links and special files are not allowed")
                    if member.flag_bits & 1:
                        raise ArchiveError("Encrypted archives are not supported")
                    directory = member.is_dir()
                    if member.file_size > max_bytes:
                        raise ArchiveError("Archive entry is too large")
                    if member.file_size > 10 * 1024**2 and member.file_size > max(1, member.compress_size) * 1000:
                        raise ArchiveError("Archive compression ratio is too high")
                    target = entry(member.filename, 0 if directory else member.file_size, directory)
                    prepared.append((member, target, directory))
                for member, target, directory in prepared:
                    if target is None:
                        continue
                    if directory:
                        target.mkdir(parents=True, exist_ok=True, mode=0o750)
                    else:
                        with package.open(member) as source:
                            _copy(source, target, member.file_size)
        else:
            with tarfile.open(archive, "r:*") as package:
                prepared = []
                for member in package:
                    if not member.isfile() and not member.isdir():
                        raise ArchiveError("Archive links and special files are not allowed")
                    target = entry(member.name, 0 if member.isdir() else member.size, member.isdir())
                    prepared.append((member, target))
                for member, target in prepared:
                    if target is None:
                        continue
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True, mode=0o750)
                    else:
                        source = package.extractfile(member)
                        if source is None:
                            raise ArchiveError("Unreadable archive entry")
                        with source:
                            _copy(source, target, member.size)
        _ancestors_no_links(destination)
        if destination.exists():
            if any(destination.iterdir()):
                raise ArchiveError("Destination changed during extraction")
            destination.rmdir()
        os.replace(staging, destination)
        return {"files": count, "bytes": total, "path": destination.name}
    except (tarfile.TarError, zipfile.BadZipFile, OSError) as error:
        raise ArchiveError(f"Could not extract archive: {error}") from error
    finally:
        if staging.exists():
            shutil.rmtree(staging)
