"""Generate or verify the offline Kekborn Genesis file inventory."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
TRUNK_COMMIT = "3e1b381cfb6e6e4ea08626338297e5ec30f6940c"
MANIFEST = "manifest.json"


def git_paths(*options, pathspec=()):
    output = subprocess.check_output(
        ["git", "ls-files", "-z", *options, "--", *pathspec], cwd=ROOT
    )
    return {os.fsdecode(path) for path in output.split(b"\0") if path}


def inventory_paths():
    tracked = git_paths("--cached")
    added = git_paths(
        "--others", "--exclude-standard", pathspec=("README.md", "tools/manifest/")
    )
    return sorted((tracked | added) - {MANIFEST})


def file_record(relative_path):
    path = ROOT / relative_path
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode):
        target = os.fsencode(os.readlink(path))
        size = len(target)
        digest = hashlib.sha256(target)
    elif stat.S_ISREG(mode):
        size = 0
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
    else:
        raise ValueError(f"{relative_path}: expected a regular file or symbolic link")
    return {"path": relative_path, "bytes": size, "sha256": digest.hexdigest()}


def build_manifest():
    snapshot = json.loads((ROOT / "snapshot.json").read_bytes())
    return {
        "trunkCommit": TRUNK_COMMIT,
        "snapshotTime": snapshot["snapshotTime"],
        "snapshotBlocks": {
            name: {
                "chainId": chain["chainId"],
                "block": chain["block"],
                "blockTimeUtc": chain["blockTimeUtc"],
            }
            for name, chain in sorted(snapshot["chains"].items())
        },
        "selfExclusion": "manifest.json is excluded because a file cannot contain its own SHA-256 hash.",
        "files": [file_record(path) for path in inventory_paths()],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify the saved manifest")
    args = parser.parse_args()
    manifest_path = ROOT / MANIFEST
    try:
        manifest = build_manifest()
        expected = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
        count = len(manifest["files"])
        if args.check:
            if manifest_path.read_bytes() != expected:
                print("FAIL: manifest.json differs from the offline rebuild", file=sys.stderr)
                return 1
            print(f"PASS: manifest.json matches the offline rebuild ({count} files)")
        else:
            manifest_path.write_bytes(expected)
            print(f"Wrote manifest.json ({count} files)")
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
