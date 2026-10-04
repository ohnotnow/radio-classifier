"""One LLM call per transcribed video: the round 4 TV prompt from ant note racl-cwm6b, verbatim. The owner
decided not to tune it for one-off judgement calls, so change it only with fresh trial evidence."""
import json
import os
import time
from collections import defaultdict
from pathlib import Path

from .. import excludes
from .. import summarise as radio
from . import rules

VOCAB = json.loads((Path(__file__).parent / "genres.json").read_text())

SUBTITLE_SOURCE = "the programme's subtitle file"
ASR_SOURCE = "a machine transcript of the soundtrack (speech recognition, so names may be misspelled)"

TV_PROMPT = """You are describing one television programme from a home video collection, so that its owner can
find it again years later from a vague memory ("the one where...").

Below is {source}. Only the dialogue is captured: nothing that is only shown on screen. It is a home
recording: it may include trailers, continuity announcements, or the start of an unrelated programme.
Ignore anything that is not part of this programme.

The file path was typed by the collector and is often (not always) right about the series, title and
year: use it where the transcript is consistent with it.

Return JSON with these keys:
- "kind": exactly one of {kinds}
- "title": the programme's title, or null
- "series": the series or anthology strand it belongs to, or null
- "made_year": the year it was made or first broadcast, as a number, or null
- "made_year_basis": "path" if the path states it, "transcript" if the dialogue or an announcer states
  it, "guess" if you are inferring it, or null
- "summary": a single string of six to ten sentences on what happens, in order, naming the main
  characters. Spoilers are fine. Keep the concrete, memorable specifics (what people make, mend,
  steal, eat, or fight over) rather than generalising them away.
- "genres": one to three genres, exactly as named in the list below (the names before the colons)
- "subgenres": up to four subgenres, exactly as named in the list below (the names after the colons)
- "places": places the story is set in or visits, as specific as the dialogue allows
- "setting_era": when the story is set, as the dialogue suggests, or null
- "characters": the main characters as {{"name": ..., "description": ...}} objects, where description
  is a few words on who they are (occupation, relationship, role in the story)
- "set_pieces": up to eight short phrases naming distinctive objects, scenes, or situations a viewer
  might remember it by
- "tone": a few words on the mood (e.g. "bleak, slow, unsettling")
- "ignored": a few words describing any material you ignored, or null

Only use names that appear in the transcript or the path. Where a spoken name is clearly a mis-hearing,
correct it.

Use a subgenre only when it describes what the programme is mainly about, not something that merely
features in it. In particular, "Political Drama" and "Political Thriller" are for stories about politics
(elections, government, ideology, political intrigue); a story that merely includes officials, the
military, a ministry, or a government agency is not political for that reason alone.

These subgenres are this collection's own additions, so here is what they mean:
- Social Realism: everyday working-class or domestic life, told naturalistically ("kitchen sink"),
  often with a social or political point to make about how people live.
- Twist in the Tale: a self-contained story built towards an ironic or shocking final turn.
- Ghost Story: a story of a haunting or a revenant, in the M.R. James tradition, more about dread than gore.
- Uncanny: a feeling that something is wrong or not natural, without a ghost or monster necessarily
  being confirmed; unsettling rather than horrific.
- Post-Apocalyptic: survivors after a collapse of civilisation (plague, war, disaster).
- Eco-Thriller: suspense driven by environmental danger, pollution, or science and technology harming
  the natural world.

--- GENRES: SUBGENRES ---
{vocab}

--- FILE ---
{path}

--- TRANSCRIPT ---
{text}
"""


def vocab_text():
    return "\n".join(f"- {group}: {', '.join(subs)}" for group, subs in VOCAB.items())


def pick(con, like=None, exclude=()):
    """Transcribed, present files with no successful summary (a failed one is retried), in path order."""
    excluded = excludes.matcher(con, exclude)
    rows = con.execute(
        """
        SELECT f.id, f.path, f.artist, f.album, f.title, t.model AS source, t.text
        FROM files f
        JOIN transcripts t ON t.file_id = f.id
        LEFT JOIN video_summaries s ON s.file_id = f.id
        WHERE t.error IS NULL AND t.text != '' AND f.missing_since IS NULL
          AND (s.file_id IS NULL OR s.error IS NOT NULL)
          AND (? IS NULL OR f.path LIKE ?)
        ORDER BY f.path
        """,
        (like, like),
    ).fetchall()
    return [r for r in rows if not excluded(r)]


def prompt_for(row):
    source = SUBTITLE_SOURCE if (row["source"] or "").startswith("srt:") else ASR_SOURCE
    return TV_PROMPT.format(source=source, kinds=radio.KIND_LIST, vocab=vocab_text(), path=row["path"],
                            text=row["text"])


def as_year(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def js(value):
    return json.dumps(value if value is not None else [], ensure_ascii=False)


def save(con, row, model, reply):
    genres, subgenres, off_list = rules.check_vocab(reply.get("genres") or [], reply.get("subgenres") or [], VOCAB)
    con.execute(
        """
        INSERT OR REPLACE INTO video_summaries
            (file_id, model, source, title, series, made_year, made_year_basis, summary, genres, subgenres,
             places, setting_era, characters, set_pieces, tone, ignored, off_list, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (row["id"], model, row["source"], reply.get("title"), reply.get("series"), as_year(reply.get("made_year")),
         reply.get("made_year_basis"), rules.coerce_summary(reply.get("summary")), js(genres), js(subgenres),
         js(reply.get("places")), reply.get("setting_era"), js(reply.get("characters")), js(reply.get("set_pieces")),
         reply.get("tone"), reply.get("ignored"), js(off_list)),
    )
    return genres, subgenres, off_list


def summarise(con, like=None, limit=None, exclude=()):
    from dotenv import load_dotenv

    load_dotenv()
    model = os.environ.get("SUMMARY_MODEL")
    if not model:
        print("Set SUMMARY_MODEL (a litellm model name) in .env first.")
        return 2
    rows = pick(con, like, exclude)[:limit]
    if not rows:
        print("Nothing to summarise.")
        return 0
    print(f"Summarising {len(rows)} videos with {model}", flush=True)
    totals = defaultdict(lambda: [0, 0])
    ask = radio.make_llm(model, totals)
    failed = 0
    started = time.time()
    for i, row in enumerate(rows, 1):
        try:
            reply = ask(prompt_for(row))
            genres, subgenres, off_list = save(con, row, model, reply)
            line = f"{reply.get('title') or os.path.basename(row['path'])} | {', '.join(genres)} | {', '.join(subgenres)}"
            if off_list:
                line += f" | off the list: {', '.join(map(str, off_list))}"
        except Exception as exc:
            con.execute("INSERT OR REPLACE INTO video_summaries (file_id, model, error) VALUES (?, ?, ?)",
                        (row["id"], model, str(exc)[:500]))
            failed += 1
            line = f"FAILED ({str(exc)[:80]})  {os.path.basename(row['path'])}"
        con.commit()
        print(f"[{i}/{len(rows)}] {line}", flush=True)
    print(f"Done: {len(rows) - failed} summarised, {failed} failed, {(time.time() - started) / 60:.1f} min.")
    for name, (tokens_in, tokens_out) in totals.items():
        print(f"  {name}: {tokens_in:,} tokens in, {tokens_out:,} out")
    return 0
