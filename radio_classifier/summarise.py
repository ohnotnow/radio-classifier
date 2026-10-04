import json
import os
import re
import time
from collections import defaultdict

from . import excludes

KINDS = {
    "drama": "acted fiction, including comedy series and soap operas",
    "reading": "a book or story read by one narrator",
    "panel": "panel game or quiz",
    "discussion": "talk show, interview, chat podcast",
    "documentary": "factual, including narrated history",
    "music": None,
    "unclear": "the excerpt is too short or fragmentary to tell",
}
KIND_LIST = ", ".join(f'"{k}" ({v})' if v else f'"{k}"' for k, v in KINDS.items())
SUMMARISED_KINDS = ("drama", "reading")  # everything else stops at the story blurb
# Kinds from the grouping call worth a story call. Panel shows, documentaries and the like are left
# undescribed; "unclear" goes ahead because the story call reads minutes, not the grouping's seconds.
DESCRIBED_KINDS = SUMMARISED_KINDS + ("unclear",)
HEAD_SECONDS = 300  # opening of the first episode: premise, and usually the announcer
TAIL_SECONDS = 180  # close of the last episode: credits are often only read at the very end
GROUP_SECONDS = 30  # opening of each file shown to the grouping call: the announcer names the episode
GROUP_WINDOW = 80  # files per grouping call; bigger folders are grouped in windows, then merged

STORY_PROMPT = """You are writing a listing for a radio programme, in the style of the Radio Times. It is most likely a
radio drama, but the collection also holds other kinds of programme and podcast.

Below is a machine transcript (speech recognition, so names may be misspelled) of the opening of the
first episode and the closing of the last episode. There are {episodes} episode(s).

Return JSON with these keys:
- "kind": exactly one of {kinds}
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

GROUP_PROMPT = """The files below are all in one folder of a collection of radio recordings (mostly radio drama, but
also other kinds of programme and podcast). A folder may hold one serial in several episodes, several
separate programmes, or a mixture.

Split the files into stories. A story is one serial, or one programme that is complete in a single file.
All the episodes of a serial go in one story, in episode order. Only put different recordings in the
same story when the story carries on from one to the next (a serial, or a programme in numbered parts).
Separate editions of a series (a panel game, a documentary or current affairs strand, a collection of
self-contained cases) are each a story of their own, even when they share a series title or subject.
A trailer or preview is a story of its own, not an episode of the thing it advertises.

The collection has been copied between drives many times, so the same recording often appears more
than once: under the same name with a suffix, under a different name, or as an incomplete copy (a
shorter length). Copies of the same episode go together in that episode's list.

For each file you get its number, its file name, its length, and a machine transcript (speech
recognition, so names may be misspelled) of its first 30 seconds. The announcer often names the
programme and says which episode it is. These are home recordings: the first 30 seconds may be the end
of the previous programme, a continuity announcement, or a trailer for something else.

For each story also give "kind": exactly one of {kinds}.

Return JSON: {{"stories": [{{"title": "...", "kind": "...", "episodes": [[file numbers that are copies of episode 1], [copies of episode 2], ...]}}]}}
Most episodes will be a list of one file. Every file number must appear exactly once.

Folder: {folder}

{files}
"""

MERGE_PROMPT = """A folder of a radio recording collection was too big to sort in one go, so it was split into
batches by file name, and each batch was sorted into stories separately. Because the file names are
messy, parts of one serial, or copies of the same recording, can end up in different batches.

Below is every story found, one per line: its id, kind, title, and its files with lengths. Find the
stories that belong together: parts of the same serial, or copies of the same recording. Only list
stories that should be joined; leave everything else out. Separate editions of a series (different
cases, different episodes of a panel game or documentary strand) are not joined.

Return JSON: {{"joins": [[story ids that belong together], ...]}}

{stories}
"""


def natural_key(path):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path)]


def name_stem(basename):
    """Letters only, so '760501 Down Payment On Death 1_5.mp3' and '... 2_5.mp3' match."""
    return " ".join(re.findall(r"[a-z]+", basename.lower().split(".")[0]))




def find_folders(con, like=None, exclude=()):
    """Transcribed files by folder, both in name order, leaving out files matching the excludes table or
    `exclude`. --like picks folders; everything after still sees the whole folder (racl-Ed6UZ)."""
    excluded = excludes.matcher(con, exclude)
    rows = con.execute(
        """
        SELECT f.id, f.path, f.duration, f.artist, f.album, f.title, t.text
        FROM files f JOIN transcripts t ON t.file_id = f.id
        WHERE t.error IS NULL AND t.text != ''
        """
    ).fetchall()
    folders = defaultdict(list)
    for row in rows:
        if not excluded(row):
            folders[os.path.dirname(row["path"])].append(row)
    if like:
        wanted = {os.path.dirname(r["path"]) for r in con.execute("SELECT path FROM files WHERE path LIKE ?", (like,))}
        folders = {k: v for k, v in folders.items() if k in wanted}
    return {
        folder: sorted(files, key=lambda f: natural_key(f["path"]))
        for folder, files in sorted(folders.items(), key=lambda kv: natural_key(kv[0]))
    }


def single_series(files):
    """One file, or every file name the same apart from its numbers: one story, no LLM needed. Folders
    of differently named files (anthologies, serials with titled episodes) go to the grouping call."""
    return len({name_stem(os.path.basename(f["path"])) for f in files}) == 1


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


def ask_checked(ask, prompt, check):
    """ask(prompt) passed through check(reply); a reply that fails the check gets one more try."""
    for attempt in (1, 2):
        reply = ask(prompt)
        try:
            return check(reply)
        except Exception as exc:
            if attempt == 2:
                raise RuntimeError(f"unusable reply: {exc}") from exc


def minutes(f):
    return f"{(f['duration'] or 0) / 60:.0f} min"


def ask_groups(con, ask, folder, files):
    """One grouping call: files -> [{"title", "kind", "episodes": [[file, copies...], ...]}], every file
    in exactly one episode."""
    blocks = "\n\n".join(
        f"--- FILE {n}: {os.path.basename(f['path'])} ({minutes(f)})\n"
        + (excerpt(con, f["id"], 0, GROUP_SECONDS) or "(nothing said)")
        for n, f in enumerate(files, 1)
    )

    def check(reply):
        stories = reply["stories"]
        numbers = sorted(n for s in stories for ep in s["episodes"] for n in ep)
        if numbers != list(range(1, len(files) + 1)):
            raise ValueError(f"file numbers are not 1 to {len(files)} once each: {numbers}")
        return [
            {"title": s.get("title"), "kind": s.get("kind") if s.get("kind") in KINDS else "unclear",
             "episodes": [[files[n - 1] for n in ep] for ep in s["episodes"] if ep]}
            for s in stories if any(s["episodes"])
        ]

    return ask_checked(ask, GROUP_PROMPT.format(kinds=KIND_LIST, folder=folder, files=blocks), check)


def merge_windows(con, ask, folder, stories):
    """For a folder grouped in windows: one call over every story found asks which are parts or copies
    of the same thing (in a messy folder they can sit hundreds of files apart), then each join is
    grouped again from its files."""
    lines = [
        f"S{i} [{s['kind']}] {s['title']}: "
        + "; ".join(f"{os.path.basename(f['path'])} ({minutes(f)})" for ep in s["episodes"] for f in ep)
        for i, s in enumerate(stories, 1)
    ]

    def check(reply):
        joins = [[int(str(i).lstrip("Ss")) for i in join] for join in reply["joins"]]
        joins = [join for join in joins if len(join) > 1]
        ids = [i for join in joins for i in join]
        if len(ids) != len(set(ids)) or not all(1 <= i <= len(stories) for i in ids):
            raise ValueError(f"joins repeat or invent story ids: {joins}")
        return joins

    joins = ask_checked(ask, MERGE_PROMPT.format(stories="\n".join(lines)), check)
    joined = {i for join in joins for i in join}
    merged = [s for i, s in enumerate(stories, 1) if i not in joined]
    for join in joins:
        files = [f for i in join for ep in stories[i - 1]["episodes"] for f in ep]
        merged += ask_groups(con, ask, folder, sorted(files, key=lambda f: natural_key(f["path"])))
    return merged


def group_folder(con, ask, folder, files):
    if len(files) <= GROUP_WINDOW:
        return ask_groups(con, ask, folder, files)
    stories = []
    for start in range(0, len(files), GROUP_WINDOW):
        stories += ask_groups(con, ask, folder, files[start:start + GROUP_WINDOW])
    return merge_windows(con, ask, folder, stories)


def save_stories(con, folder, stories):
    for s in sorted(stories, key=lambda s: natural_key(s["episodes"][0][0]["path"])):
        story_id = con.execute(
            "INSERT INTO stories (folder, kind, title) VALUES (?, ?, ?)", (folder, s["kind"], s["title"])
        ).lastrowid
        con.executemany(
            "INSERT INTO story_files (file_id, story_id, episode) VALUES (?, ?, ?)",
            [(f["id"], story_id, n) for n, ep in enumerate(s["episodes"], 1) for f in ep],
        )


def group_series(con, folder, files):
    """A single-series folder is one story. Files transcribed after it was made join it, renumbered."""
    ids = [f["id"] for f in files]
    row = con.execute(
        f"SELECT story_id FROM story_files WHERE file_id IN ({','.join('?' * len(ids))})", ids
    ).fetchone()
    if row is None:
        save_stories(con, folder, [{"title": None, "kind": None, "episodes": [[f] for f in files]}])
    else:
        con.executemany(
            "INSERT OR REPLACE INTO story_files (file_id, story_id, episode) VALUES (?, ?, ?)",
            [(f["id"], row["story_id"], n) for n, f in enumerate(files, 1)],
        )


def ungrouped(con, folders):
    """(folder, all its files, the files not in a story yet) for folders with something to group. A
    mixed folder that gains files later only has the new ones grouped: they make new stories."""
    grouped = {r[0] for r in con.execute("SELECT file_id FROM story_files")}
    todo = []
    for folder, files in folders.items():
        new = [f for f in files if f["id"] not in grouped]
        if new:
            todo.append((folder, files, new))
    return todo


def group(con, ask, folders, limit=None):
    """Make the stories for every folder with ungrouped files, saving each folder as soon as it is
    grouped, so a re-run never groups it again (LLM grouping is not repeatable). Returns failures."""
    todo = ungrouped(con, folders)[:limit]
    failed = 0
    for i, (folder, files, new) in enumerate(todo, 1):
        t0 = time.time()
        try:
            if single_series(files):
                group_series(con, folder, files)
                made = "one story"
            else:
                stories = group_folder(con, ask, folder, new)
                save_stories(con, folder, stories)
                made = f"{len(stories)} stories"
            con.commit()
        except Exception as exc:
            con.rollback()
            failed += 1
            print(f"[{i}/{len(todo)}] FAILED ({str(exc)[:200]})  {folder}", flush=True)
            continue
        print(f"[{i}/{len(todo)}] {time.time() - t0:5.1f}s  {len(new):4d} files, {made:<11}  {folder}", flush=True)
    return failed


def episodes_of(con, story_id):
    """One file per episode, in order: the longest copy, since incomplete copies are shorter."""
    rows = con.execute(
        """
        SELECT f.id, f.path, f.duration, t.text, sf.episode
        FROM story_files sf JOIN files f ON f.id = sf.file_id JOIN transcripts t ON t.file_id = f.id
        WHERE sf.story_id = ?
        ORDER BY sf.episode, f.duration DESC
        """,
        (story_id,),
    ).fetchall()
    best = {}
    for row in rows:
        best.setdefault(row["episode"], row)
    return list(best.values())


def summarised(con):
    return {r[0] for r in con.execute("SELECT file_id FROM summaries WHERE error IS NULL")}


def is_described(story):
    return story["model"] is not None and story["error"] is None


def to_describe(con, folders):
    """(story, episodes) for stories in these folders still needing a story call or episode summaries."""
    in_scope = {f["id"] for files in folders.values() for f in files}
    story_of = dict(con.execute("SELECT file_id, story_id FROM story_files").fetchall())
    done_episodes = summarised(con)
    work = []
    for story_id in sorted({story_of[i] for i in in_scope if i in story_of}):
        story = con.execute("SELECT * FROM stories WHERE id = ?", (story_id,)).fetchone()
        if is_described(story):
            if story["kind"] not in SUMMARISED_KINDS:
                continue
            episodes = episodes_of(con, story_id)
            if all(e["id"] in done_episodes for e in episodes):
                continue
        elif story["kind"] is None or story["kind"] in DESCRIBED_KINDS:
            episodes = episodes_of(con, story_id)
        else:
            continue  # grouped as a panel show, documentary etc.: kept as a story, never described
        work.append((story, episodes))
    return work


def cast_line(cast):
    return "; ".join(f"{c['actor']} as {c['role']}" if c.get("role") else c["actor"] for c in cast)


def describe_story(con, ask, model, story_id, folder, files):
    first, last = files[0], files[-1]
    tail_from = (last["duration"] or 0) - TAIL_SECONDS
    if first is last:
        tail_from = max(tail_from, HEAD_SECONDS)  # short single file: don't repeat the head
    prompt = STORY_PROMPT.format(
        kinds=KIND_LIST,
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
        UPDATE stories SET kind = ?, kind_reason = ?, title = ?, writer = ?, cast_list = ?, synopsis = ?,
            error = ?, model = ?, created_at = datetime('now')
        WHERE id = ?
        """,
        (*values, model, story_id),
    )
    return con.execute("SELECT * FROM stories WHERE id = ?", (story_id,)).fetchone()


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


def describe(con, ask_story, ask_episode, story_model, episode_model, folders, limit=None):
    """Story calls for grouped stories of a described kind, then episode summaries for dramas and
    readings (one copy per episode). Returns failures."""
    work = to_describe(con, folders)[:limit]
    if not work:
        print("Nothing to describe.")
        return 0
    print(f"Describing {len(work)} stories with {story_model} (stories) and {episode_model} (episodes)", flush=True)
    done_episodes = summarised(con)
    failed = 0
    for i, (story, episodes) in enumerate(work, 1):
        t0 = time.time()
        if not is_described(story):
            story = describe_story(con, ask_story, story_model, story["id"], story["folder"], episodes)
            con.commit()
        if story["error"]:
            failed += 1
            print(f"[{i}/{len(work)}] FAILED ({story['error'][:80]})  s{story['id']} {story['folder']}", flush=True)
            continue
        summarised_now = 0
        if story["kind"] in SUMMARISED_KINDS:
            for f in episodes:
                if f["id"] in done_episodes:
                    continue
                if summarise_episode(con, ask_episode, episode_model, story, f["episode"], len(episodes), f):
                    failed += 1
                summarised_now += 1
                con.commit()
        print(f"[{i}/{len(work)}] {time.time() - t0:5.1f}s  {story['kind']:<11} {summarised_now:3d} ep  "
              f"s{story['id']} {story['title'] or os.path.basename(story['folder'])}", flush=True)
    return failed


def dry_run(con, folders):
    todo = ungrouped(con, folders)
    asking = 0
    for folder, files, new in todo:
        if single_series(files):
            how = "one story"
        else:
            asking += 1
            windows = -(-len(new) // GROUP_WINDOW)
            how = f"LLM, {windows} windows" if windows > 1 else "LLM"
        print(f"{len(new):5d}  {how:<15}  {folder}")
    print(f"{len(todo)} folders to group ({asking} by LLM), {sum(len(new) for *_, new in todo)} files.")
    work = to_describe(con, folders)
    print(f"Already grouped and waiting: {sum(not is_described(s) for s, _ in work)} story calls, "
          f"{sum(is_described(s) for s, _ in work)} described stories with episodes to summarise.")
    return 0


def summarise(con, like=None, limit=None, exclude=(), dry_run_only=False, group_only=False):
    folders = find_folders(con, like, exclude)
    if dry_run_only:
        return dry_run(con, folders)

    from dotenv import load_dotenv

    load_dotenv()
    story_model, episode_model = os.environ.get("SYNOPSIS_MODEL"), os.environ.get("SUMMARY_MODEL")
    if not story_model or not episode_model:
        print("Set SYNOPSIS_MODEL and SUMMARY_MODEL (litellm model names) in .env first.")
        return 2
    totals = defaultdict(lambda: [0, 0])
    # Grouping uses the stronger model: the cheap one flip-flopped between runs on whether a series of
    # self-contained editions is one story or many (racl-mjBCN).
    ask_story, ask_episode = make_llm(story_model, totals), make_llm(episode_model, totals)

    started = time.time()
    print(f"Grouping with {story_model}", flush=True)
    failed = group(con, ask_story, folders, limit)
    if not group_only:
        failed += describe(con, ask_story, ask_episode, story_model, episode_model, folders, limit)
    print(f"Done: {failed} failures, {(time.time() - started) / 60:.1f} min.")
    for model, (tokens_in, tokens_out) in totals.items():
        print(f"  {model}: {tokens_in:,} tokens in, {tokens_out:,} out")
    return 0
