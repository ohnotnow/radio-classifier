import argparse

from . import classify as classify_mod
from . import db, excludes, scan as scan_mod, search as search_mod, summarise as summarise_mod, transcribe as transcribe_mod


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="radio-classifier",
        description="Find the radio dramas hiding in a messy music collection.",
    )
    parser.add_argument("--db", help="SQLite database path (default: video.db for `video` commands, radio.db otherwise)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scan", help="Walk a directory tree and record audio metadata")
    p.add_argument("root")

    sub.add_parser("classify", help="Score every file: likely / maybe / unlikely drama")

    p = sub.add_parser("transcribe", help="Speech-to-text the start of candidate files")
    p.add_argument("--seconds", type=int, default=180, help="Seconds to transcribe; 0 = whole file (default: 180)")
    p.add_argument("--limit", type=int, help="Stop after N files")
    p.add_argument("--model", default=transcribe_mod.DEFAULT_MODEL,
                   help="Parakeet model on Hugging Face, e.g. mlx-community/parakeet-tdt-0.6b-v3 "
                        "for multilingual audio (default: %(default)s)")
    p.add_argument("--verdict", default="likely", help="Comma list: likely,maybe,unlikely or all (default: likely)")
    p.add_argument("--deepen", action="store_true", help="Also re-transcribe files whose existing transcript is shorter than --seconds")
    p.add_argument("--retry-failed", action="store_true", help="Also try again files whose last transcription failed")
    p.add_argument("--exclude", action="append", default=[], metavar="REGEX",
                   help="Also skip files whose path/tags match, on top of the `exclude` list (repeatable)")
    p.add_argument("--pause", type=float, default=0.0, metavar="SECS",
                   help="Cool-down sleep between files (thermal relief)")
    p.add_argument("--gentle", action="store_true",
                   help="CPU-only inference, leaves the GPU free for real work; "
                        "pair with: taskpolicy -c background uv run ...")
    p.add_argument("--workday", action="store_true",
                   help="Set-and-forget for multi-day runs: gentle CPU mode "
                        "07:00-23:00 so the computer stays usable, full-speed GPU "
                        "overnight with a cool-down pause so the fans stay quiet")

    p = sub.add_parser("summarise", help="Group files into stories; LLM blurb per story, summary per drama/reading episode")
    p.add_argument("--like", metavar="PATTERN", help="Only folders holding a file whose path matches this SQL LIKE pattern")
    p.add_argument("--limit", type=int, help="Stop after grouping N folders and describing N stories")
    p.add_argument("--exclude", action="append", default=[], metavar="REGEX",
                   help="Also skip files whose path/tags match, on top of the `exclude` list (repeatable)")
    p.add_argument("--dry-run", action="store_true", help="List the folders to group and count the work waiting; no LLM calls")
    p.add_argument("--group-only", action="store_true", help="Group folders into stories, then stop before describing them")

    p = sub.add_parser("exclude", help="Patterns for files never to transcribe or summarise")
    actions = p.add_subparsers(dest="action", required=True)
    a = actions.add_parser("add", help="Add a regex (case-insensitive, matched against path and tags) and show what it matches")
    a.add_argument("pattern")
    a.add_argument("--note", help="Why, for future you")
    actions.add_parser("list", help="List the patterns and how many files each matches")
    a = actions.add_parser("remove", help="Remove a pattern by id")
    a.add_argument("id", type=int)

    p = sub.add_parser("search", help="Full-text search transcripts (and paths/tags)")
    p.add_argument("query", nargs="+")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--any", action="store_true", help="Match any word instead of all words")

    p = sub.add_parser("grep", help="Regex search over paths and tags")
    p.add_argument("pattern")
    p.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("show", help="Show one file (ID) or one story (sID, e.g. s412)")
    p.add_argument("id", help="a file id, or s plus a story id")

    p = sub.add_parser("export", help="Print one file's transcript as SRT subtitles")
    p.add_argument("id", type=int)
    p.add_argument("--plain", action="store_true", help="Just the text, no timestamps")

    sub.add_parser("stats", help="Collection and progress counts")

    p = sub.add_parser("video", help="Transcribe and summarise a video collection (own database: video.db)")
    video = p.add_subparsers(dest="video_command", required=True)
    v = video.add_parser("scan", help="Walk a mounted share and record each video's duration, language and subtitles")
    v.add_argument("root")
    v.add_argument("--new-root", action="store_true",
                   help="Scan as a separate collection even if the videos look like ones already scanned elsewhere")
    v = video.add_parser("remount", help="A share came back under a new mount point: move the stored paths, "
                                          "keeping transcripts and summaries")
    v.add_argument("old")
    v.add_argument("new")
    v = video.add_parser("transcribe", help="Whole-file text for each video: subtitles, else speech-to-text")
    v.add_argument("--limit", type=int, help="Stop after N files")
    v.add_argument("--retry-failed", action="store_true", help="Also try again files whose last transcription failed")
    v.add_argument("--exclude", action="append", default=[], metavar="REGEX",
                   help="Also skip files whose path matches, on top of the `exclude` list (repeatable)")
    v.add_argument("--pause", type=float, default=0.0, metavar="SECS",
                   help="Cool-down sleep between files (thermal relief)")
    v.add_argument("--gentle", action="store_true",
                   help="CPU-only inference, leaves the GPU free for real work; "
                        "pair with: taskpolicy -c background uv run ...")
    v.add_argument("--workday", action="store_true",
                   help="Set-and-forget for multi-day runs: gentle CPU mode "
                        "07:00-23:00 so the computer stays usable, full-speed GPU "
                        "overnight with a cool-down pause so the fans stay quiet")
    v = video.add_parser("summarise", help="LLM summary, genres and set pieces for each transcribed video")
    v.add_argument("--like", metavar="PATTERN", help="Only files whose path matches this SQL LIKE pattern")
    v.add_argument("--limit", type=int, help="Stop after N files")
    v.add_argument("--exclude", action="append", default=[], metavar="REGEX",
                   help="Also skip files whose path matches, on top of the `exclude` list (repeatable)")

    args = parser.parse_args(argv)
    db_path = args.db or ("video.db" if args.command == "video" else "radio.db")
    if args.command == "video":
        from .video.db import connect_video

        con = connect_video(db_path)
        try:
            if args.video_command in ("scan", "remount"):
                from .video import scan as video_scan

                if args.video_command == "scan":
                    return video_scan.scan(con, args.root, new_root=args.new_root)
                return video_scan.remount(con, args.old, args.new)
            if args.video_command == "transcribe":
                from .video import transcribe as video_transcribe

                return video_transcribe.transcribe(con, limit=args.limit, exclude=args.exclude, pause=args.pause,
                                                   gentle=args.gentle, workday=args.workday,
                                                   retry_failed=args.retry_failed)
            if args.video_command == "summarise":
                from .video import summarise as video_summarise

                return video_summarise.summarise(con, like=args.like, limit=args.limit, exclude=args.exclude)
            print(f"video {args.video_command}: not implemented yet")
            return 2
        finally:
            con.close()
    if args.command == "transcribe" and "parakeet" not in args.model.lower():
        parser.error(f"--model must be a Parakeet model, got {args.model}")
    con = db.connect(db_path)
    try:
        if args.command == "scan":
            return scan_mod.scan(con, args.root)
        if args.command == "classify":
            return classify_mod.classify(con)
        if args.command == "transcribe":
            verdicts = ("likely", "maybe", "unlikely") if args.verdict == "all" else tuple(args.verdict.split(","))
            return transcribe_mod.transcribe(
                con, seconds=args.seconds, limit=args.limit,
                model=args.model, verdicts=verdicts, deepen=args.deepen,
                exclude=args.exclude, pause=args.pause, gentle=args.gentle,
                workday=args.workday, retry_failed=args.retry_failed,
            )
        if args.command == "summarise":
            return summarise_mod.summarise(con, like=args.like, limit=args.limit, exclude=args.exclude,
                                           dry_run_only=args.dry_run, group_only=args.group_only)
        if args.command == "exclude":
            if args.action == "add":
                return excludes.add(con, args.pattern, args.note)
            if args.action == "list":
                return excludes.list_all(con)
            return excludes.remove(con, args.id)
        if args.command == "search":
            return search_mod.search(con, " ".join(args.query), limit=args.limit, any_word=args.any)
        if args.command == "grep":
            return search_mod.grep(con, args.pattern, limit=args.limit)
        if args.command == "show":
            return search_mod.show(con, args.id)
        if args.command == "export":
            return search_mod.export(con, args.id, plain=args.plain)
        if args.command == "stats":
            return search_mod.stats(con)
    finally:
        con.close()
    return 2
