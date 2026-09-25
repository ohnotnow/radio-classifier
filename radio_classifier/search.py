import os
import re
import sqlite3


def fmt_duration(seconds):
    if not seconds:
        return "?"
    return f"{int(seconds) // 60}m{int(seconds) % 60:02d}"


def quoted_query(query, any_word=False):
    words = [w for w in re.split(r"\s+", query.strip()) if w]
    joiner = " OR " if any_word else " "
    return joiner.join('"' + w.replace('"', '') + '"' for w in words)


def search(con, query, limit=20, any_word=False):
    sql = """
        SELECT f.id, f.path, f.duration, f.verdict,
               snippet(search_index, 0, '>>', '<<', ' ... ', 16) AS snip
        FROM search_index
        JOIN files f ON f.id = search_index.rowid
        WHERE search_index MATCH ?
        ORDER BY bm25(search_index, 3.0, 1.0, 0.5, 0.5, 0.5)
        LIMIT ?
    """
    try:
        rows = con.execute(sql, (quoted_query(query, any_word), limit)).fetchall()
    except sqlite3.OperationalError as exc:
        print(f"Bad query: {exc}")
        return 2
    if not rows:
        print("No matches.")
        return 1
    for row in rows:
        print(f"[{row['id']}] ({fmt_duration(row['duration'])}, {row['verdict']}) {row['path']}")
        if row["snip"]:
            print(f"      {row['snip']}")
    return 0


def grep(con, pattern, limit=50):
    try:
        rx = re.compile(pattern, re.I)
    except re.error as exc:
        print(f"Bad regex: {exc}")
        return 2
    shown = 0
    for row in con.execute(
        """
        SELECT f.id, f.path, f.artist, f.album, f.title, f.duration, f.verdict,
               t.file_id IS NOT NULL AS transcribed
        FROM files f LEFT JOIN transcripts t ON t.file_id = f.id
        """
    ):
        haystack = " | ".join(str(v) for v in (row["path"], row["artist"], row["album"], row["title"]) if v)
        if rx.search(haystack):
            mark = "T" if row["transcribed"] else "-"
            print(f"[{row['id']}] {mark} ({fmt_duration(row['duration'])}, {row['verdict']}) {row['path']}")
            shown += 1
            if shown >= limit:
                print("... (limit reached)")
                break
    if not shown:
        print("No matches.")
        return 1
    return 0


def show(con, file_id):
    row = con.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    if not row:
        print(f"No file with id {file_id}")
        return 1
    print(row["path"])
    print(f"  duration: {fmt_duration(row['duration'])}  bitrate: {row['bitrate']}  channels: {row['channels']}")
    print(f"  artist: {row['artist']}  album: {row['album']}  title: {row['title']}  genre: {row['genre']}")
    print(f"  verdict: {row['verdict']} (score {row['score']}: {row['reasons']})")
    t = con.execute("SELECT * FROM transcripts WHERE file_id = ?", (file_id,)).fetchone()
    if t is None:
        print("  transcript: none")
    elif t["error"]:
        print(f"  transcript FAILED: {t['error']}")
    else:
        print(f"  transcript ({t['seconds']:.0f}s, {t['model']}):")
        print()
        print("  " + t["text"])
    return 0


def stats(con):
    total = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    print(f"{total} files scanned")
    for verdict, n in con.execute(
        "SELECT verdict, COUNT(*) FROM files GROUP BY verdict ORDER BY COUNT(*) DESC"
    ):
        print(f"  {verdict or 'unclassified':>12}: {n}")
    done, failed = con.execute(
        "SELECT SUM(error IS NULL), SUM(error IS NOT NULL) FROM transcripts"
    ).fetchone()
    print(f"transcripts: {done or 0} done, {failed or 0} failed")
    return 0
