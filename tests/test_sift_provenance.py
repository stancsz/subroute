from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_sift_provenance.py"
SPEC = importlib.util.spec_from_file_location("check_sift_provenance", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


def git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def test_normalize_changes_only_crlf():
    assert checker.normalize(b"a\r\nb\rc") == b"a\nb\rc"


def test_compare_checks_pin_and_expected_patch_set(tmp_path, monkeypatch):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git(upstream, "init", "-q")
    git(upstream, "config", "user.name", "Provenance test")
    git(upstream, "config", "user.email", "provenance-test@example.invalid")

    upstream_sift = upstream / "src" / "sift"
    upstream_sift.mkdir(parents=True)
    (upstream_sift / "core.py").write_text("upstream\n", encoding="utf-8")
    (upstream_sift / "__init__.py").write_text("same\n", encoding="utf-8")
    git(upstream, "add", "src/sift/core.py", "src/sift/__init__.py")
    git(upstream, "-c", "user.name=Provenance test", "-c",
        "user.email=provenance-test@example.invalid", "commit", "-qm", "pin")
    commit = git(upstream, "rev-parse", "HEAD")

    checkout = tmp_path / "checkout"
    vendored_sift = checkout / "src" / "sift"
    vendored_sift.mkdir(parents=True)
    (vendored_sift / "core.py").write_text("gateway patch\n", encoding="utf-8")
    (vendored_sift / "__init__.py").write_text("same\n", encoding="utf-8")
    document = vendored_sift / "UPSTREAM.md"
    document.write_text(f"Base: Sift 0.10.0, commit {commit},\n", encoding="utf-8")

    monkeypatch.setattr(checker, "ROOT", checkout)
    monkeypatch.setattr(checker, "UPSTREAM_DOC", document)
    monkeypatch.setattr(checker, "FILES", ("core.py", "__init__.py"))
    monkeypatch.setattr(checker, "EXPECTED_PATCHED", frozenset({"core.py"}))

    receipt = checker.compare(upstream)
    assert receipt["actual_differences_match_contract"] is True
    assert [(row["file"], row["identical"]) for row in receipt["files"]] == [
        ("src/sift/core.py", False),
        ("src/sift/__init__.py", True),
    ]
    assert "gateway patch" in receipt["files"][0]["unified_diff"]

    monkeypatch.setattr(checker, "EXPECTED_PATCHED", frozenset())
    assert checker.compare(upstream)["actual_differences_match_contract"] is False
