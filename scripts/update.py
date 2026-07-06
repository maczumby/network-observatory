#!/usr/bin/env python3
"""
update.py — update network-observatory in place, without touching your data.

Fetches the latest code and copies it over the scripts/skills/docs. It never touches
`data/`, `exports/`, or `dashboard/` — your graph, flags, and notes are safe — then
rebuilds your map so the new version shows. Stdlib only, no dependencies.

The agent runs this when you say "update the network-observatory tool."

Usage:
    python3 scripts/update.py                 # auto: git pull if a clone, else download latest
    python3 scripts/update.py --from-zip PATH # update from a zip someone sent you
    python3 scripts/update.py --no-rebuild    # update code only, don't rebuild the map

Why this is safe: code and your data live in different places (data/, exports/, and
dashboard/ are never overwritten), and the database only ever adds tables, so a newer
version keeps working with your existing memory.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TARBALL = "https://github.com/maczumby/network-observatory/archive/refs/heads/main.tar.gz"
# Never overwrite these — they hold the user's data or local state.
PRESERVE = {"data", "exports", "dashboard", ".git"}


def read_version(root):
    fp = os.path.join(root, "VERSION")
    return open(fp, encoding="utf-8").read().strip() if os.path.exists(fp) else "(unknown)"


def _top_folder(d):
    """A GitHub tarball/zip extracts to a single top folder; find it (else use d)."""
    subs = [os.path.join(d, n) for n in os.listdir(d)
            if os.path.isdir(os.path.join(d, n))]
    return subs[0] if len(subs) == 1 else d


def sync_tree(src_root, dst_root):
    """Copy everything from src over dst except the preserved (data) dirs."""
    copied = 0
    for name in os.listdir(src_root):
        if name in PRESERVE:
            continue
        s = os.path.join(src_root, name)
        d = os.path.join(dst_root, name)
        if os.path.isdir(s):
            shutil.copytree(s, d, dirs_exist_ok=True)
        else:
            shutil.copy2(s, d)
        copied += 1
    return copied


def from_git(root):
    try:
        subprocess.run(["git", "-C", root, "pull", "--ff-only"], check=True)
        return True
    except Exception as e:
        print("  git pull failed:", e)
        return False


def fetch_tarball(dst_dir):
    tgz = os.path.join(dst_dir, "src.tar.gz")
    try:
        urllib.request.urlretrieve(TARBALL, tgz)
    except Exception as e:
        return None, f"download failed ({e}). If the repo is private, use --from-zip."
    with tarfile.open(tgz) as t:
        t.extractall(dst_dir)
    os.remove(tgz)
    return _top_folder(dst_dir), None


def from_zip(path, dst_dir):
    with zipfile.ZipFile(path) as z:
        z.extractall(dst_dir)
    return _top_folder(dst_dir)


def rebuild(root):
    if not os.path.exists(os.path.join(root, "data", "linkedin.db")):
        print("  (no data/linkedin.db yet — skipping map rebuild)")
        return
    print("  rebuilding your map…")
    subprocess.run([sys.executable, os.path.join(root, "scripts", "observatory_export.py")],
                   check=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-zip", help="update from a zip instead of the internet")
    ap.add_argument("--no-rebuild", action="store_true")
    a = ap.parse_args()

    old = read_version(REPO)
    print(f"Current version: {old}")

    if a.from_zip:
        with tempfile.TemporaryDirectory() as tmp:
            n = sync_tree(from_zip(a.from_zip, tmp), REPO)
        print(f"  updated {n} items from the zip")
    elif os.path.isdir(os.path.join(REPO, ".git")):
        if not from_git(REPO):
            sys.exit("Update failed. Download the latest zip and use: "
                     "python3 scripts/update.py --from-zip <path>")
    else:
        with tempfile.TemporaryDirectory() as tmp:
            top, err = fetch_tarball(tmp)
            if err:
                sys.exit("  " + err)
            n = sync_tree(top, REPO)
        print(f"  updated {n} items from the latest release")

    new = read_version(REPO)
    print(f"Now on: {new}" + ("  (already current)" if new == old else f"  (was {old})"))
    if not a.no_rebuild:
        rebuild(REPO)
    print("Done — your data in data/ was left untouched.")


if __name__ == "__main__":
    main()
