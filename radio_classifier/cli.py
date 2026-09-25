import argparse

from . import classify as classify_mod
from . import db, scan as scan_mod, search as search_mod, transcribe as transcribe_mod


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="radio-classifier",
        description="Find the radio dramas hiding in a messy music collection.",
    )
    parser.add_argument("--db", default="radio.db", help="SQLite database path (default: radio.db)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scan", help="Walk a directory tree and record audio metadata")
    p.add_argument("root")

    sub.add_parser("classify", help="Score every file: likely / maybe / unlikely drama")

    p = sub.add_parser("transcribe", help="Speech-to-text the start of candidate files")
    p.add_argument("--seconds", type=int, default=180, help="Seconds to transcribe; 0 = whole file (default: 180)")
    p.add_argument("--limit", type=int, help="Stop after N files")
    p.add_argument("--model", default=transcribe_mod.DEFAULT_MODEL)
    p.add_argument("--verdict", default="likely", help="Comma list: likely,maybe,unlikely or all (default: likely)")
    p.add_argument("--deepen", action="store_true", help="Also re-transcribe files whose existing transcript is shorter than --seconds")
    p.add_argument("--exclude", action="append", default=[], metavar="REGEX",
                   help="Skip files whose path/tags match (repeatable)")
    p.add_argument("--pause", type=float, default=0.0, metavar="SECS",
                   help="Cool-down sleep between files (thermal relief)")
    p.add_argument("--gentle", action="store_true",
                   help="CPU-only inference, leaves the GPU free for real work; "
                        "pair with: taskpolicy -c background uv run ...")
    p.add_argument("--workday", action="store_true",
                   help="Set-and-forget for multi-day runs: gentle CPU mode "
                        "07:00-23:00 so the computer stays usable, full-speed GPU "
                        "overnight with a cool-down pause so the fans stay quiet")

    p = sub.add_parser("search", help="Full-text search transcripts (and paths/tags)")
    p.add_argument("query", nargs="+")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--any", action="store_true", help="Match any word instead of all words")

    p = sub.add_parser("grep", help="Regex search over paths and tags")
    p.add_argument("pattern")
    p.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("show", help="Show one file's metadata, verdict and transcript")
    p.add_argument("id", type=int)

    p = sub.add_parser("export", help="Print one file's transcript as SRT subtitles")
    p.add_argument("id", type=int)
    p.add_argument("--plain", action="store_true", help="Just the text, no timestamps")

    sub.add_parser("stats", help="Collection and progress counts")

    args = parser.parse_args(argv)
    con = db.connect(args.db)
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
                workday=args.workday,
            )
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
