#!/usr/bin/env python3
"""Build a deterministic extension archive from an explicit file allowlist."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parent

EXTENSION_FILES = (
    "LICENSE",
    "NOTICE",
    "PRIVACY.md",
    "README.md",
    "manifest.json",
    "src/content/chatgpt.js",
    "src/content/claude.js",
    "src/lib/export-common.js",
    "src/lib/zip-store.js",
    "src/popup/popup.css",
    "src/popup/popup.html",
    "src/popup/popup.js",
)

PROJECT_FILES = frozenset(
    {
        *EXTENSION_FILES,
        "package.json",
        "pack.py",
        "tests/make_zip_fixture.js",
        "tests/test_extension.py",
        "tests/test_runtime.js",
    }
)

PROJECT_DIRECTORIES = frozenset(
    {
        "dist",
        "src",
        "src/content",
        "src/lib",
        "src/popup",
        "tests",
    }
)

VERSION_PATTERN = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){0,3}$")


class PackError(RuntimeError):
    """An expected, user-facing packaging validation error."""


def is_link_or_reparse(path: Path, directory_entry: os.DirEntry[str] | None = None) -> bool:
    if directory_entry is not None and directory_entry.is_symlink():
        return True
    if path.is_symlink():
        return True
    is_junction = getattr(os.path, "isjunction", None)
    if is_junction is not None and is_junction(path):
        return True
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError as error:
        raise PackError(f"Cannot inspect path: {path}") from error
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_flag and attributes & reparse_flag)


def require_plain_file(path: Path) -> None:
    if is_link_or_reparse(path):
        raise PackError(f"Links are not allowed: {path.relative_to(ROOT).as_posix()}")
    if not path.is_file():
        raise PackError(f"Required file is missing: {path.relative_to(ROOT).as_posix()}")


def require_valid_version(value: object, source: str) -> str:
    if not isinstance(value, str) or not VERSION_PATTERN.fullmatch(value):
        raise PackError(
            f"{source} version must contain 1 to 4 non-negative decimal components."
        )
    components = [int(component) for component in value.split(".")]
    if any(component > 65535 for component in components):
        raise PackError(f"{source} version components must be between 0 and 65535.")
    if not any(components):
        raise PackError(f"{source} version must not be all zero.")
    return value


def read_versions() -> str:
    manifest_path = ROOT / "manifest.json"
    package_path = ROOT / "package.json"
    require_plain_file(manifest_path)
    require_plain_file(package_path)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PackError("manifest.json and package.json must be readable JSON files.") from error

    declared_manifest_version = manifest.get("manifest_version")
    if type(declared_manifest_version) is not int or declared_manifest_version != 3:
        raise PackError("manifest.json must declare manifest_version 3.")

    manifest_version = require_valid_version(manifest.get("version"), "manifest.json")
    package_version = require_valid_version(package.get("version"), "package.json")
    if manifest_version != package_version:
        raise PackError(
            f"Version mismatch: manifest.json={manifest_version!r}, package.json={package_version!r}."
        )
    return manifest_version


def scan_project(allowed_files: frozenset[str]) -> None:
    def visit(directory: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as error:
            raise PackError(f"Cannot scan directory: {directory}") from error

        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(ROOT).as_posix()
            if is_link_or_reparse(path, entry):
                raise PackError(f"Links are not allowed: {relative}")
            if entry.is_dir(follow_symlinks=False):
                if relative not in PROJECT_DIRECTORIES:
                    raise PackError(f"Unknown directory: {relative}")
                visit(path)
                continue
            if entry.is_file(follow_symlinks=False):
                if relative not in allowed_files:
                    raise PackError(f"Unknown file: {relative}")
                continue
            raise PackError(f"Unsupported filesystem entry: {relative}")

    visit(ROOT)
    missing = sorted(PROJECT_FILES - allowed_files.intersection(PROJECT_FILES))
    if missing:
        raise PackError(f"Internal allowlist error for required files: {', '.join(missing)}")
    for relative in PROJECT_FILES:
        require_plain_file(ROOT / relative)


def deterministic_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def verify_archive(archive_path: Path, version: str) -> None:
    expected = set(EXTENSION_FILES)
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise PackError("Built archive contains duplicate entries.")
            if set(names) != expected:
                missing = sorted(expected - set(names))
                extra = sorted(set(names) - expected)
                raise PackError(f"Built archive entry mismatch; missing={missing}, extra={extra}.")
            damaged = archive.testzip()
            if damaged is not None:
                raise PackError(f"Built archive failed CRC verification at {damaged}.")
            archived_manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PackError("Built archive could not be reopened and verified.") from error
    archived_manifest_version = archived_manifest.get("manifest_version")
    if type(archived_manifest_version) is not int or archived_manifest_version != 3:
        raise PackError("The archived manifest is not Manifest V3.")
    if archived_manifest.get("version") != version:
        raise PackError("The archived manifest version does not match the package version.")


def path_is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def require_no_link_components(path: Path, directory: Path) -> None:
    try:
        relative = path.relative_to(directory)
    except ValueError as error:
        raise PackError("The output path must stay inside the project root.") from error

    current = directory
    for component in relative.parts:
        current /= component
        if os.path.lexists(current) and is_link_or_reparse(current):
            raise PackError(
                f"Output links are not allowed: {current.relative_to(ROOT).as_posix()}"
            )


def build(output_argument: str | None) -> Path:
    version = read_versions()
    default_relative = f"dist/scriptorium-local-conversation-export-{version}.zip"
    default_output = ROOT / Path(default_relative)
    allowed_files = frozenset({*PROJECT_FILES, default_relative})

    requested_output = Path(output_argument).expanduser() if output_argument else default_output
    if not requested_output.is_absolute():
        requested_output = ROOT / requested_output
    output = Path(os.path.abspath(requested_output))
    if not path_is_within(output, ROOT):
        raise PackError("The output path must stay inside the project root.")
    require_no_link_components(output, ROOT)

    resolved_output = output.resolve(strict=False)
    resolved_root = ROOT.resolve(strict=True)
    if not path_is_within(resolved_output, resolved_root):
        raise PackError("The output path must stay inside the project root.")
    if resolved_output != default_output.resolve(strict=False):
        raise PackError(f"In-project output must be exactly {default_relative}.")

    scan_project(allowed_files)
    output.parent.mkdir(exist_ok=True)
    require_no_link_components(output, ROOT)
    if not path_is_within(output.resolve(strict=False), resolved_root):
        raise PackError("The output path must stay inside the project root.")

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b",
            prefix=".scriptorium-pack-",
            suffix=".tmp",
            dir=output.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
        with zipfile.ZipFile(
            temporary_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for relative in EXTENSION_FILES:
                source = ROOT / relative
                require_plain_file(source)
                archive.writestr(deterministic_info(relative), source.read_bytes())
        verify_archive(temporary_path, version)
        require_no_link_components(output, ROOT)
        if not path_is_within(output.resolve(strict=False), resolved_root):
            raise PackError("The output path must stay inside the project root.")
        os.replace(temporary_path, output)
        temporary_path = None
        verify_archive(output, version)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()

    return output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        help="Output ZIP path. It must be the versioned dist path under the project root.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        output = build(args.output)
    except PackError as error:
        print(f"pack.py: {error}", file=sys.stderr)
        return 1
    print(f"Built and verified: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
