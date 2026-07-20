#!/usr/bin/env python3
"""Run dependency-free syntax, ZIP round-trip, and strict packer checks."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile
import zlib


ROOT = Path(__file__).resolve().parents[1]

JAVASCRIPT_FILES = (
    "src/popup/popup.js",
    "src/lib/zip-store.js",
    "src/lib/export-common.js",
    "src/content/chatgpt.js",
    "src/content/claude.js",
    "tests/make_zip_fixture.js",
    "tests/test_runtime.js",
)

EXPECTED_EXTENSION_FILES = (
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

FIXTURE_CONTENT = {
    "plain.json": b'{"ok":true}\n',
    "对话-🌟.json": '{"message":"你好，世界 🌍"}\n'.encode("utf-8"),
    "nested/emoji-😀.json": '{"value":"café"}\n'.encode("utf-8"),
}


def run(
    command: list[str],
    expect_success: bool = True,
    *,
    cwd: Path = ROOT,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    succeeded = result.returncode == 0
    if succeeded != expect_success:
        raise AssertionError(
            f"Command returned {result.returncode}: {command}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def check_javascript(node: str) -> None:
    for relative in JAVASCRIPT_FILES:
        run([node, "--check", str(ROOT / relative)])
    run([node, str(ROOT / "tests" / "test_runtime.js")])


def check_zip_round_trip(node: str, directory: Path) -> None:
    archive_path = directory / "writer-fixture.zip"
    run([node, str(ROOT / "tests" / "make_zip_fixture.js"), str(archive_path)])
    with zipfile.ZipFile(archive_path, "r") as archive:
        if archive.namelist() != list(FIXTURE_CONTENT):
            raise AssertionError(f"Unexpected fixture entries: {archive.namelist()}")
        if archive.testzip() is not None:
            raise AssertionError("Python zipfile reported a CRC failure.")
        for info in archive.infolist():
            expected = FIXTURE_CONTENT[info.filename]
            actual = archive.read(info.filename)
            if info.compress_type != zipfile.ZIP_STORED:
                raise AssertionError(f"Entry is not STORE-only: {info.filename}")
            if (info.flag_bits & 0x0800) == 0:
                raise AssertionError(f"Entry lacks the UTF-8 flag: {info.filename}")
            if info.file_size != len(expected) or info.compress_size != len(expected):
                raise AssertionError(f"Entry size mismatch: {info.filename}")
            if info.CRC != zlib.crc32(expected) & 0xFFFFFFFF:
                raise AssertionError(f"CRC mismatch: {info.filename}")
            if actual != expected:
                raise AssertionError(f"Round-trip content mismatch: {info.filename}")


def write_json(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def run_packer(project: Path, *arguments: str, expect_success: bool = True) -> subprocess.CompletedProcess[str]:
    return run(
        [sys.executable, "-B", str(project / "pack.py"), *arguments],
        expect_success=expect_success,
        cwd=project,
    )


def check_package(directory: Path) -> None:
    project = directory / "isolated-project"
    shutil.copytree(
        ROOT,
        project,
        ignore=shutil.ignore_patterns("dist", "__pycache__", "*.pyc"),
    )
    manifest_path = project / "manifest.json"
    package_path = project / "package.json"
    original_manifest = manifest_path.read_bytes()
    original_package = package_path.read_bytes()
    manifest = json.loads(original_manifest.decode("utf-8"))
    package = json.loads(original_package.decode("utf-8"))
    version = manifest["version"]
    archive_path = project / "dist" / f"scriptorium-local-conversation-export-{version}.zip"

    run_packer(project)
    first_archive = archive_path.read_bytes()
    with zipfile.ZipFile(archive_path, "r") as archive:
        names = archive.namelist()
        if names != list(EXPECTED_EXTENSION_FILES):
            raise AssertionError(f"Packaged entry set mismatch: {names}")
        if archive.testzip() is not None:
            raise AssertionError("Packaged extension failed CRC verification.")
        for relative in EXPECTED_EXTENSION_FILES:
            if archive.read(relative) != (project / relative).read_bytes():
                raise AssertionError(f"Packaged bytes differ from source: {relative}")

    run_packer(project)
    if archive_path.read_bytes() != first_archive:
        raise AssertionError("Repeated package builds are not byte-for-byte deterministic.")

    if manifest.get("version") != package.get("version"):
        raise AssertionError("Manifest and package versions do not match.")

    invalid_versions = (
        ("0", "must not be all zero"),
        ("0.0.0.0", "must not be all zero"),
        ("1.2.3.4.5", "1 to 4"),
        ("65536", "between 0 and 65535"),
    )
    for invalid_version, expected_error in invalid_versions:
        invalid_manifest = dict(manifest)
        invalid_package = dict(package)
        invalid_manifest["version"] = invalid_version
        invalid_package["version"] = invalid_version
        write_json(manifest_path, invalid_manifest)
        write_json(package_path, invalid_package)
        result = run_packer(project, expect_success=False)
        if expected_error not in result.stderr:
            raise AssertionError(
                f"Invalid version {invalid_version!r} failed for the wrong reason: {result.stderr}"
            )

    manifest_path.write_bytes(original_manifest)
    invalid_package = dict(package)
    invalid_package["version"] = "65536"
    write_json(package_path, invalid_package)
    result = run_packer(project, expect_success=False)
    if "package.json version components" not in result.stderr:
        raise AssertionError(f"Invalid package version failed for the wrong reason: {result.stderr}")

    package_path.write_bytes(original_package)
    mismatched_package = dict(package)
    mismatched_package["version"] = "1.0.1"
    write_json(package_path, mismatched_package)
    result = run_packer(project, expect_success=False)
    if "Version mismatch" not in result.stderr:
        raise AssertionError(f"Version mismatch failed for the wrong reason: {result.stderr}")

    package_path.write_bytes(original_package)
    mv2_manifest = dict(manifest)
    mv2_manifest["manifest_version"] = 2
    write_json(manifest_path, mv2_manifest)
    result = run_packer(project, expect_success=False)
    if "manifest_version 3" not in result.stderr:
        raise AssertionError(f"Manifest V2 failed for the wrong reason: {result.stderr}")

    manifest_path.write_bytes(original_manifest)
    outside_output = directory / "outside-project.zip"
    result = run_packer(
        project,
        "--output",
        str(outside_output),
        expect_success=False,
    )
    if "inside the project root" not in result.stderr or outside_output.exists():
        raise AssertionError(f"Outside output failed for the wrong reason: {result.stderr}")

    link_path = project / "custom-output.zip"
    link_target = project / "regular-target.zip"
    link_target.write_bytes(b"target")
    try:
        link_path.symlink_to(link_target)
    except OSError as error:
        print(f"Packer link regression: skipped ({error})")
    else:
        result = run_packer(
            project,
            "--output",
            link_path.name,
            expect_success=False,
        )
        if "Output links are not allowed" not in result.stderr:
            raise AssertionError(f"Linked output failed for the wrong reason: {result.stderr}")
    finally:
        if link_path.is_symlink():
            link_path.unlink()
        if link_target.exists():
            link_target.unlink()

    unknown_path = project / "unexpected-file-for-pack-test.tmp"
    unknown_path.write_text("temporary allowlist test\n", encoding="utf-8")
    result = run_packer(project, expect_success=False)
    if "Unknown file: unexpected-file-for-pack-test.tmp" not in result.stderr:
        raise AssertionError(f"Packer rejected an unknown file for the wrong reason: {result.stderr}")


def main() -> int:
    node = shutil.which("node")
    if node is None:
        raise SystemExit("Node.js is required for JavaScript syntax and ZIP writer tests.")

    with tempfile.TemporaryDirectory(prefix="scriptorium-extension-test-") as temporary:
        temporary_directory = Path(temporary)
        check_javascript(node)
        check_zip_round_trip(node, temporary_directory)
        check_package(temporary_directory)

    print(f"JavaScript syntax: {len(JAVASCRIPT_FILES)} files passed")
    print("ZIP writer: STORE, UTF-8, CRC-32, Unicode, and round-trip checks passed")
    print(
        "Packer: MV3/version bounds, root/link safety, exact source bytes, "
        "determinism, allowlist, and CRC checks passed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
