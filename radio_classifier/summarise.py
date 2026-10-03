import json
import os
import re
import time
from collections import defaultdict

SUMMARISED_KINDS = ("drama", "reading")  # everything else stops at the story blurb
HEAD_SECONDS = 300  # opening of the first episode: premise, and usually the announcer
TAIL_SECONDS = 180  # close of the last episode: credits are often only read at the very end

STORY_PROMPT = """You are writing a listing for a radio programme, in the style of the Radio Times. It is most likely a
radio drama, but the collection also holds other kinds of programme and podcast.

Below is a machine transcript (speech recognition, so names may be misspelled) of the opening of the
first episode and the closing of the last episode. There are {episodes} episode(s).

Return JSON with these keys:
- "kind": exactly one of "drama" (acted fiction, including comedy series and soap operas), "reading"
  (a book or story read by one narrator), "panel" (panel game or quiz), "discussion" (talk show,
  interview, chat podcast), "documentary" (factual, including narrated history), "music", "unclear"
  (the excerpt is too short or fragmentary to tell)
- "kind_reason": a few words on what in the transcript decided "kind"
- "title": the programme's title, or null
- "writer": the writer (and adapter, if any), or null
- "cast": the actors as announced, as a list of {{"actor": ..., "role": ...}} objects, leads first;
  "role" is the character's full name, or null if not announced (empty list if no actors are announced)
- "synopsis": two or three sentences in the style of a Radio Times listing. Set up the premise; do not
  give away the ending.

Only include people who are named in the transcript. Do not add anyone from your own knowledge.
Speech recognition often mis-hears names: where a spoken name is clearly a mis-hearing of a real actor
or writer you know, use the correct spelling.
The folder and file names below were typed by the collector and are often (not always) right about the
title and author: use them where the transcript is consistent with them.
These are home recordings: they may include trailers, continuity announcements, or the start of an
unrelated programme where the tape ran on. Ignore anything that is not part of this programme.
If something is not known, use null or an empty list.

--- FILES ---
{files}

--- OPENING OF EPISODE 1 ---
{head}

--- CLOSING OF THE LAST EPISODE ---
{tail}
"""

EPISODE_PROMPT = """You are summarising one episode of a radio programme so that a listener can find their place in
the story, or find a scene they half-remember.

About the programme: {title}, {kind}. {synopsis}
Cast: {cast}
This is episode {n} of {total}.

Below is a machine transcript of the whole episode (speech recognition, so names may be misspelled;
use the spellings from the description above where they match). It is a home recording: it may
include trailers, continuity announcements, a recap of the previous episode, or the start of an
unrelated programme where the tape ran on. Ignore anything that is not part of this episode.

Return JSON with these keys:
- "summary": four to six sentences on what happens in this episode, in order, naming the main
  characters involved. Spoilers for this episode are fine. Do not re-explain the premise.
- "ignored": a few words describing any material you ignored, or null

--- TRANSCRIPT ---
{text}
"""


def natural_key(path):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path)]


def name_stem(basename):
    """Letters only, so '760501 Down Payment On Death 1_5.mp3' and '... 2_5.mp3' match."""
    return " ".join(re.findall(r"[a-z]+", basename.lower().split(".")[0]))


def find_stories(con, like=None, exclude=()):
    """First wave: a folder is a story if it holds one transcribed file, or if every file name is the
    same apart from its numbers. Folders of differently named files (anthologies, panel-show series,
    serials with titled episodes) are left for a later pass."""
    exclude_rx = [re.compile(pat, re.I) for pat in exclude]
    rows = con.execute(
        """
        SELECT f.id, f.path, f.duration, t.text
        FROM files f JOIN transcripts t ON t.file_id = f.id
        WHERE t.error IS NULL AND t.text != ''
        """
    ).fetchall()
    folders = defaultdict(list)
    for row in rows:
        folders[os.path.dirname(row["path"])].append(row)
    # --like picks folders; the single-series test below still sees the whole folder.
    wanted = None
    if like:
        wanted = {os.path.dirname(r["path"]) for r in con.execute("SELECT path FROM files WHERE path LIKE ?", (like,))}
    stories = []
    for folder, files in folders.items():
        if wanted is not None and folder not in wanted:
            continue
        if any(rx.search(folder) for rx in exclude_rx):
            continue
        if len({name_stem(os.path.basename(f["path"])) for f in files}) == 1:
            stories.append((folder, sorted(files, key=lambda f: natural_key(f["path"]))))
    return sorted(stories, key=lambda s: natural_key(s[0]))


def excerpt(con, file_id, after, before):
    rows = con.execute(
        "SELECT start, text FROM segments WHERE file_id = ? AND start >= ? AND start < ? ORDER BY start",
        (file_id, after, before),
    ).fetchall()
    return "\n".join(f"[{int(s // 60):02d}:{int(s % 60):02d}] {t}" for s, t in rows)


def make_llm(model, totals):
    """Returns prompt -> parsed JSON dict, keeping a running token count per model in totals."""
    from litellm import completion

    def ask(prompt):
        for attempt in (1, 2):
            response = completion(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                reasoning_effort="low",  # cheaper, and stops small models overthinking into emoji
                num_retries=2,
            )
            usage = response.usage
            totals[model][0] += usage.prompt_tokens
            totals[model][1] += usage.completion_tokens
            try:
                return json.loads(response.choices[0].message.content)
            except (TypeError, json.JSONDecodeError):
                # Seen once with Sonnet: an empty body with finish_reason "stop"; a rerun was fine.
                if attempt == 2:
                    raise RuntimeError(f"not JSON: {str(response.choices[0].message.content)[:200]!r}")

    return ask


def cast_line(cast):
    return "; ".join(f"{c['actor']} as {c['role']}" if c.get("role") else c["actor"] for c in cast)


def describe_story(con, ask, model, folder, files):
    first, last = files[0], files[-1]
    tail_from = (last["duration"] or 0) - TAIL_SECONDS
    if first is last:
        tail_from = max(tail_from, HEAD_SECONDS)  # short single file: don't repeat the head
    prompt = STORY_PROMPT.format(
        episodes=len(files),
        files="\n".join(f["path"] for f in files),
        head=excerpt(con, first["id"], 0, HEAD_SECONDS),
        tail=excerpt(con, last["id"], tail_from, 1e9),
    )
    try:
        r = ask(prompt)
        values = (r.get("kind"), r.get("kind_reason"), r.get("title"), r.get("writer"),
                  json.dumps(r.get("cast") or []), r.get("synopsis"), None)
    except Exception as exc:
        values = (None, None, None, None, None, None, str(exc)[:500])
    con.execute(
        """
        INSERT INTO stories (folder, kind, kind_reason, title, writer, cast_list, synopsis, error, model)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(folder) DO UPDATE SET
            kind = excluded.kind, kind_reason = excluded.kind_reason, title = excluded.title,
            writer = excluded.writer, cast_list = excluded.cast_list, synopsis = excluded.synopsis,
            error = excluded.error, model = excluded.model, created_at = datetime('now')
        """,
        (folder, *values, model),
    )
    return con.execute("SELECT * FROM stories WHERE folder = ?", (folder,)).fetchone()


def summarise_episode(con, ask, model, story, n, total, file_row):
    prompt = EPISODE_PROMPT.format(
        title=story["title"] or "untitled", kind=story["kind"], synopsis=story["synopsis"] or "",
        cast=cast_line(json.loads(story["cast_list"] or "[]")) or "not known", n=n, total=total, text=file_row["text"],
    )
    try:
        text, error = ask(prompt)["summary"], None
    except Exception as exc:
        text, error = None, str(exc)[:500]
    con.execute(
        "INSERT OR REPLACE INTO summaries (file_id, model, text, error) VALUES (?, ?, ?, ?)",
        (file_row["id"], model, text, error),
    )
    return error


def summarise(con, like=None, limit=None, exclude=(), dry_run=False):
    stories = find_stories(con, like, exclude)
    if dry_run:
        for folder, files in stories[:limit]:
            print(f"{len(files):4d}  {folder}")
        print(f"{len(stories)} stories, {sum(len(f) for _, f in stories)} files.")
        return 0

    from dotenv import load_dotenv

    load_dotenv()
    story_model, episode_model = os.environ.get("SYNOPSIS_MODEL"), os.environ.get("SUMMARY_MODEL")
    if not story_model or not episode_model:
        print("Set SYNOPSIS_MODEL and SUMMARY_MODEL (litellm model names) in .env first.")
        return 2
    totals = defaultdict(lambda: [0, 0])
    ask_story, ask_episode = make_llm(story_model, totals), make_llm(episode_model, totals)

    done_stories = {r["folder"] for r in con.execute("SELECT folder FROM stories WHERE error IS NULL")}
    done_episodes = {r["file_id"] for r in con.execute("SELECT file_id FROM summaries WHERE error IS NULL")}
    work = [
        (folder, files) for folder, files in stories
        if folder not in done_stories or any(f["id"] not in done_episodes for f in files)
    ]
    if limit:
        work = work[:limit]
    if not work:
        print("Nothing to summarise.")
        return 0
    print(f"Summarising {len(work)} stories with {story_model} (stories) and {episode_model} (episodes)", flush=True)

    started = time.time()
    failed = 0
    for i, (folder, files) in enumerate(work, 1):
        t0 = time.time()
        if folder in done_stories:
            story = con.execute("SELECT * FROM stories WHERE folder = ?", (folder,)).fetchone()
        else:
            story = describe_story(con, ask_story, story_model, folder, files)
        con.executemany(
            "INSERT OR REPLACE INTO story_files (file_id, story_id, episode) VALUES (?, ?, ?)",
            [(f["id"], story["id"], n) for n, f in enumerate(files, 1)],
        )
        con.commit()
        if story["error"]:
            failed += 1
            print(f"[{i}/{len(work)}] FAILED ({story['error'][:80]})  {folder}", flush=True)
            continue
        episodes = 0
        if story["kind"] in SUMMARISED_KINDS:
            for n, f in enumerate(files, 1):
                if f["id"] in done_episodes:
                    continue
                if summarise_episode(con, ask_episode, episode_model, story, n, len(files), f):
                    failed += 1
                episodes += 1
                con.commit()
        print(f"[{i}/{len(work)}] {time.time() - t0:5.1f}s  {story['kind']:<11} "
              f"{episodes:3d} ep  {story['title'] or os.path.basename(folder)}", flush=True)

    elapsed = time.time() - started
    print(f"Done: {len(work)} stories, {failed} failures, {elapsed / 60:.1f} min.")
    for model, (tokens_in, tokens_out) in totals.items():
        print(f"  {model}: {tokens_in:,} tokens in, {tokens_out:,} out")
    return 0
