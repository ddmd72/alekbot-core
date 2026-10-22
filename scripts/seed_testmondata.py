#!/usr/bin/env python3
"""Copy a testmon database for `make test-affected`: seed_testmondata.py SOURCE DEST.

testmon keeps .testmondata in WAL mode, so a plain file copy taken while the other checkout is running can miss
what is still in the -wal file. SQLite's online backup API reads a consistent snapshot; if the source cannot be opened
that way (read-only directory, odd permissions) the file is copied as it is. Exit status 1 when nothing was copied.
"""
import shutil
import sqlite3
import sys


def seed(source: str, dest: str) -> str:
    """Returns "backup" or "copy"."""
    try:
        src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(dest)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        return "backup"
    except sqlite3.Error:
        shutil.copyfile(source, dest)
        return "copy"


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: seed_testmondata.py SOURCE DEST")
    print(f"seeded {sys.argv[2]} by {seed(sys.argv[1], sys.argv[2])}")
