"""Fetch the two public, revision-pinned encoder assets without credentials."""

import argparse
import hashlib
import json
from pathlib import Path
import urllib.request


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=root / "models/advisor-ranker")
    args = parser.parse_args()
    manifest = json.loads((root / "src/subroute/data/advisor_ranker.json").read_text(encoding="utf-8"))["embedding"]
    args.destination.mkdir(parents=True, exist_ok=True)
    for name_key, hash_key in (("onnx_filename", "weights_sha256"), ("tokenizer_filename", "tokenizer_sha256")):
        name, expected = manifest[name_key], manifest[hash_key]
        if Path(name).name != name:
            raise ValueError("Model manifest filename must be a basename")
        target = args.destination / name
        if target.exists() and digest(target) == expected:
            print(f"Verified {name}")
            continue
        temporary = target.with_suffix(target.suffix + ".part")
        url = f"https://huggingface.co/{manifest['repository']}/resolve/{manifest['revision']}/{name}"
        try:
            # urllib does not read cached Hugging Face credentials or token files.
            with urllib.request.urlopen(url, timeout=60) as response, temporary.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
            if digest(temporary) != expected:
                raise ValueError(f"Digest mismatch for {name}")
            temporary.replace(target)
            print(f"Downloaded and verified {name}")
        finally:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
