import os
import re
import sqlite3
from collections import Counter

FOLDERS_SHOWN = 15  # folders listed when a pattern is added, most matches first


def haystack(row):
    """What exclude patterns and `grep` match against: the path and the tags."""
    return " | ".join(str(v) for v in (row["path"], row["artist"], row["album"], row["title"]) if v)


def matcher(con, extra=()):
    """Returns row -> True for a file matching a pattern in the excludes table, or one of `extra`
    (from --exclude on the command line). Rows need path, artist, album and title."""
    patterns = [r["pattern"] for r in con.execute("SELECT pattern FROM excludes")] + list(extra)
    rxs = [re.compile(p, re.I) for p in patterns]
    return lambda row: any(rx.search(haystack(row)) for rx in rxs)


def matching_files(con, rx):
    return [r for r in con.execute("SELECT path, artist, album, title FROM files") if rx.search(haystack(r))]


def add(con, pattern, note=None):
    try:
        rx = re.compile(pattern, re.I)
    except re.error as exc:
        print(f"Bad regex: {exc}")
        return 2
    try:
        con.execute("INSERT INTO excludes (pattern, note) VALUES (?, ?)", (pattern, note))
    except sqlite3.IntegrityError:
        print(f"Already excluded: {pattern}")
        return 1
    con.commit()
    hits = matching_files(con, rx)
    folders = Counter(os.path.dirname(r["path"]) for r in hits)
    print(f"Excluded {pattern!r}: matches {len(hits)} files in {len(folders)} folders.")
    for folder, n in folders.most_common(FOLDERS_SHOWN):
        print(f"  {n:5d}  {folder}")
    if len(folders) > FOLDERS_SHOWN:
        print(f"  ... and {len(folders) - FOLDERS_SHOWN} more folders (`grep` lists every file)")
    return 0


def list_all(con):
    rows = con.execute("SELECT * FROM excludes ORDER BY id").fetchall()
    if not rows:
        print("No exclude patterns.")
        return 0
    for row in rows:
        n = len(matching_files(con, re.compile(row["pattern"], re.I)))
        print(f"[{row['id']}] {row['pattern']}  ({n} files){'  ' + row['note'] if row['note'] else ''}")
    return 0


def remove(con, exclude_id):
    if not con.execute("DELETE FROM excludes WHERE id = ?", (exclude_id,)).rowcount:
        print(f"No exclude pattern with id {exclude_id}")
        return 1
    con.commit()
    print(f"Removed exclude pattern {exclude_id}.")
    return 0
