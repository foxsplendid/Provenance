from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_capture_release_uses_an_independent_version_namespace() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release-capture.yml").read_text(
        encoding="utf-8"
    )

    assert "if: startsWith(github.event.release.tag_name, 'capture-v')" in workflow
    assert (
        'test "${{ github.event.release.tag_name }}" = "capture-v$version"'
        in workflow
    )
    assert "scriptorium-local-conversation-export-*.zip" in workflow
    assert "scriptorium-local-conversation-export-*.sha256" in workflow


def test_capture_readme_documents_release_asset_without_repository_clone() -> None:
    readme = (ROOT / "01-capture" / "README.md").read_text(encoding="utf-8")

    assert "capture-v1.0.0" in readme
    assert "rest of the repository are not required" in readme
