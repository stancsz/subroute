"""Compare the vendored Sift files with their documented upstream pin."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import difflib
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_DOC = ROOT / "src" / "sift" / "UPSTREAM.md"
FILES = (
    "core.py",
    "litellm_proxy.py",
    "litellm_responses.py",
    "__init__.py",
)
EXPECTED_PATCHED = frozenset({"core.py", "litellm_proxy.py", "litellm_responses.py"})


def normalize(source: bytes) -> bytes:
    """Normalize only CRLF; preserve any other source bytes and line endings."""
    source.decode("utf-8")
    return source.replace(b"\r\n", b"\n")


def pinned_commit() -> str:
    document = UPSTREAM_DOC.read_text(encoding="utf-8")
    match = re.search(r"^Base:.*?commit ([0-9a-f]{40})(?:,|\s|$)", document, re.MULTILINE)
    if match is None:
        raise ValueError(f"Could not read the pinned Sift commit from {UPSTREAM_DOC}")
    return match.group(1)


def git_bytes(repository: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or f"git {' '.join(arguments)} failed")
    return completed.stdout


def compare(upstream_repository: Path) -> dict:
    commit = pinned_commit()
    git_bytes(upstream_repository, "cat-file", "-e", f"{commit}^{{commit}}")
    upstream_head = git_bytes(upstream_repository, "rev-parse", "HEAD").decode().strip()
    rows = []
    for name in FILES:
        vendored = normalize((ROOT / "src" / "sift" / name).read_bytes())
        upstream = normalize(git_bytes(
            upstream_repository,
            "show",
            f"{commit}:src/sift/{name}",
        ))
        identical = vendored == upstream
        row = {
            "file": f"src/sift/{name}",
            "vendored_sha256": hashlib.sha256(vendored).hexdigest(),
            "upstream_sha256": hashlib.sha256(upstream).hexdigest(),
            "identical": identical,
            "expected_difference": name in EXPECTED_PATCHED,
        }
        if not identical:
            row["unified_diff"] = "".join(difflib.unified_diff(
                upstream.decode("utf-8").splitlines(keepends=True),
                vendored.decode("utf-8").splitlines(keepends=True),
                fromfile=f"{commit}:src/sift/{name}",
                tofile=f"src/sift/{name}",
            ))
        rows.append(row)

    expected = {row["file"].rsplit("/", 1)[1] for row in rows if not row["identical"]}
    return {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Pinned upstream Git blobs versus vendored checkout; no runtime or provider check.",
        "upstream_repository": str(upstream_repository.resolve()),
        "upstream_head": upstream_head,
        "pinned_commit": commit,
        "normalization": "UTF-8 validation; CRLF to LF only",
        "expected_patched_files": sorted(EXPECTED_PATCHED),
        "actual_differences_match_contract": expected == EXPECTED_PATCHED,
        "files": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--upstream-repo",
        type=Path,
        default=Path("D:/github/Sift"),
        help="local Sift Git clone containing the pinned commit",
    )
    parser.add_argument("--output", type=Path, help="write the JSON receipt to this path")
    args = parser.parse_args()

    try:
        receipt = compare(args.upstream_repo)
        rendered = json.dumps(receipt, indent=2) + "\n"
        if args.output:
            args.output.write_text(rendered, encoding="utf-8", newline="\n")
        else:
            sys.stdout.write(rendered)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Sift provenance check failed: {error}", file=sys.stderr)
        return 2
    return 0 if receipt["actual_differences_match_contract"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
