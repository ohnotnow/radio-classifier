import json
import os
import re
import shutil
import sqlite3
import sys
import textwrap

from .summarise import cast_line

FILE_HITS = 500  # file matches fetched before grouping into stories, so a big serial can't crowd out the rest
EPISODES_SHOWN = 5  # matching episodes listed under a story in search results


def fmt_duration(seconds):
    if not seconds:
        return "?"
    return f"{int(seconds) // 60}m{int(seconds) % 60:02d}"


def fmt_clock(seconds):
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def fmt_srt_time(seconds):
    ms = round(seconds * 1000)
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def first_hit(con, file_id, words):
    """Earliest sentence mentioning any query word, so a hit can say where in the file to look."""
    for row in con.execute("SELECT start, text FROM segments WHERE file_id = ? ORDER BY start", (file_id,)):
        lowered = row["text"].lower()
        if any(w in lowered for w in words):
            return row
    return None


def quoted_query(query, any_word=False):
    words = [w for w in re.split(r"\s+", query.strip()) if w]
    joiner = " OR " if any_word else " "
    return joiner.join('"' + w.replace('"', '') + '"' for w in words)


def wrapped(text, indent="      ", first=None):
    """Wrap to the terminal (at most 100 wide) so continuation lines keep their indent. `first` is a
    different indent for the first line, e.g. "" for a heading whose overflow should hang."""
    width = min(shutil.get_terminal_size().columns - 1, 100)
    return textwrap.fill(text, width=width, initial_indent=indent if first is None else first,
                         subsequent_indent=indent)


def story_heading(story, episodes):
    parts = [story["kind"] or "?", f"{episodes} episode{'s' if episodes != 1 else ''}"]
    heading = f"[s{story['id']}] {story['title'] or os.path.basename(story['folder'])} ({', '.join(parts)})"
    return heading + (f", by {story['writer']}" if story["writer"] else "")


def build_story_index(con):
    """Title, writer, cast and synopsis of every story, in a TEMP table: a few thousand rows build in
    milliseconds, so there is nothing to keep in step with `summarise` and radio.db is never written."""
    con.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS temp.story_index USING fts5("
        "title, writer, cast, synopsis, tokenize = 'porter unicode61')"
    )
    con.execute("DELETE FROM temp.story_index")
    con.executemany(
        "INSERT INTO temp.story_index (rowid, title, writer, cast, synopsis) VALUES (?, ?, ?, ?, ?)",
        [(s["id"], s["title"] or "", s["writer"] or "", cast_line(json.loads(s["cast_list"] or "[]")), s["synopsis"] or "")
         for s in con.execute("SELECT * FROM stories WHERE error IS NULL")],
    )


def search(con, query, limit=20, any_word=False):
    """Stories first-class, loose files as before. A story matches on its listing or on any of its
    episodes' transcripts, and appears once; results are ordered by best bm25 (lower is better),
    a story winning a tie with a file."""
    match = quoted_query(query, any_word)
    try:
        file_hits = con.execute(
            """
            SELECT f.id, f.path, f.duration, f.verdict, sf.story_id, sf.episode,
                   snippet(search_index, 0, '>>', '<<', ' ... ', 16) AS snip,
                   bm25(search_index, 3.0, 1.0, 0.5, 0.5, 0.5) AS rank
            FROM search_index
            JOIN files f ON f.id = search_index.rowid
            LEFT JOIN story_files sf ON sf.file_id = f.id
                AND sf.story_id IN (SELECT id FROM stories WHERE error IS NULL)
            WHERE search_index MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (match, FILE_HITS),
        ).fetchall()
        build_story_index(con)
        story_ranks = dict(con.execute(
            "SELECT rowid, bm25(story_index, 3.0, 2.0, 2.0, 1.0) FROM temp.story_index WHERE story_index MATCH ?",
            (match,),
        ).fetchall())
    except sqlite3.OperationalError as exc:
        print(f"Bad query: {exc}")
        return 2

    episodes_hit = {}  # story_id -> matching file rows, best first
    results = []  # (rank, 0 for a story / 1 for a file, story_id or file row)
    for row in file_hits:
        if row["story_id"] is None:
            results.append((row["rank"], 1, row))
        else:
            episodes_hit.setdefault(row["story_id"], []).append(row)
    for story_id in story_ranks.keys() | episodes_hit.keys():
        ranks = [story_ranks[story_id]] if story_id in story_ranks else []
        ranks += [r["rank"] for r in episodes_hit.get(story_id, [])[:1]]
        results.append((min(ranks), 0, story_id))
    if not results:
        print("No matches.")
        return 1

    words = [w.lower() for w in query.split()]
    for n, (_, is_file, item) in enumerate(sorted(results, key=lambda r: (r[0], r[1]))[:limit]):
        if n:
            print()
        if is_file:
            print(wrapped(f"[{item['id']}] ({fmt_duration(item['duration'])}, {item['verdict']}) {item['path']}", first=""))
            if item["snip"]:
                print(wrapped(item["snip"].strip()))
            hit = first_hit(con, item["id"], words)
            if hit:
                print(wrapped(f"first at {fmt_clock(hit['start'])}: {hit['text'][:100]}"))
            continue
        story = con.execute("SELECT * FROM stories WHERE id = ?", (item,)).fetchone()
        episodes = con.execute("SELECT COUNT(*) FROM story_files WHERE story_id = ?", (item,)).fetchone()[0]
        print(wrapped(story_heading(story, episodes), first=""))
        leads = cast_line(json.loads(story["cast_list"] or "[]")[:3])
        if leads:
            print(wrapped("with " + leads))
        if story["synopsis"]:
            print(wrapped(story["synopsis"]))
        hits = sorted(episodes_hit.get(item, []), key=lambda r: r["episode"])
        for row in hits[:EPISODES_SHOWN]:
            hit = first_hit(con, row["id"], words)
            where = f"{fmt_clock(hit['start'])}: {hit['text'][:80]}" if hit else (row["snip"] or "")
            print(wrapped(f"ep {row['episode']} [{row['id']}] {where.strip()}", indent="         ", first="      "))
        if len(hits) > EPISODES_SHOWN:
            print(f"      ... and {len(hits) - EPISODES_SHOWN} more episodes")
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


def show(con, ident):
    """`show 35146` is a file; `show s412` is a story."""
    if ident.lower().startswith("s") and ident[1:].isdigit():
        return show_story(con, int(ident[1:]))
    if not ident.isdigit():
        print(f"Not an id: {ident} (a file is a number, a story is s followed by a number)")
        return 2
    file_id = int(ident)
    row = con.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    if not row:
        print(f"No file with id {file_id}")
        return 1
    print(row["path"])
    print(f"  duration: {fmt_duration(row['duration'])}  bitrate: {row['bitrate']}  channels: {row['channels']}")
    print(f"  artist: {row['artist']}  album: {row['album']}  title: {row['title']}  genre: {row['genre']}")
    print(f"  verdict: {row['verdict']} (score {row['score']}: {row['reasons']})")
    story = con.execute(
        """
        SELECT s.*, sf.episode, (SELECT COUNT(*) FROM story_files WHERE story_id = s.id) AS episodes
        FROM story_files sf JOIN stories s ON s.id = sf.story_id
        WHERE sf.file_id = ? AND s.error IS NULL
        """,
        (file_id,),
    ).fetchone()
    if story:
        print(wrapped(f"story: episode {story['episode']} of {story_heading(story, story['episodes'])}",
                      indent="    ", first="  "))
        summary = con.execute(
            "SELECT text FROM summaries WHERE file_id = ? AND error IS NULL", (file_id,)
        ).fetchone()
        if summary:
            print()
            print(wrapped(summary["text"], indent="  "))
    t = con.execute("SELECT * FROM transcripts WHERE file_id = ?", (file_id,)).fetchone()
    if t is None:
        print("  transcript: none")
    elif t["error"]:
        print(f"  transcript FAILED: {t['error']}")
    else:
        print(f"  transcript ({t['seconds']:.0f}s, {t['model']}):")
        print()
        print(wrapped(t["text"], indent="  "))
    return 0


def show_story(con, story_id):
    story = con.execute("SELECT * FROM stories WHERE id = ?", (story_id,)).fetchone()
    if not story:
        print(f"No story with id s{story_id}")
        return 1
    episodes = con.execute(
        """
        SELECT f.id, f.path, f.duration, m.text AS summary
        FROM story_files sf
        JOIN files f ON f.id = sf.file_id
        LEFT JOIN summaries m ON m.file_id = f.id AND m.error IS NULL
        WHERE sf.story_id = ?
        ORDER BY sf.episode
        """,
        (story_id,),
    ).fetchall()
    print(wrapped(story_heading(story, len(episodes)), indent="    ", first=""))
    print(f"  {story['folder']}")
    if story["error"]:
        print(f"  listing FAILED: {story['error']}")
        return 0
    cast = cast_line(json.loads(story["cast_list"] or "[]"))
    if cast:
        print()
        print(wrapped("Cast: " + cast, indent="  "))
    if story["synopsis"]:
        print()
        print(wrapped(story["synopsis"], indent="  "))
    print()
    for n, ep in enumerate(episodes, 1):
        print(wrapped(f"{n}. [{ep['id']}] ({fmt_duration(ep['duration'])}) {os.path.basename(ep['path'])}",
                      indent="       ", first="  "))
        if ep["summary"]:
            print(wrapped(ep["summary"], indent="       "))
    return 0


def export(con, file_id, plain=False):
    t = con.execute("SELECT text, error FROM transcripts WHERE file_id = ?", (file_id,)).fetchone()
    if t is None or t["error"]:
        print(f"No transcript for id {file_id}", file=sys.stderr)
        return 1
    if plain:
        print(t["text"])
        return 0
    segments = con.execute(
        "SELECT start, end, text FROM segments WHERE file_id = ? ORDER BY start", (file_id,)
    ).fetchall()
    if not segments:
        print(f"No timestamps for id {file_id}; re-transcribe it, or use --plain", file=sys.stderr)
        return 1
    for n, seg in enumerate(segments, 1):
        print(f"{n}\n{fmt_srt_time(seg['start'])} --> {fmt_srt_time(seg['end'])}\n{seg['text']}\n")
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
