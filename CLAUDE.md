# radio-classifier

Local CLI that finds radio dramas in a large, badly named audio collection by transcribing them. See README.md for the user-facing picture, and run `ant foundation` at the start of a session for the project's purpose and design commitments (`ant list` has the rest of the notes).

## Running things

- `uv run python -m radio_classifier <command>`; commands are `scan`, `classify`, `transcribe`, `summarise`, `exclude`, `search`, `grep`, `show`, `export`, `stats`, plus the `video` group (`video scan|transcribe|summarise`), which defaults to `video.db` instead of `radio.db` and refuses a database holding classified radio files. Design: ant note racl-cwm6b, work: ait epic racl-VqXmZ.
- Scratch scripts that import the package need `PYTHONPATH=.` (it isn't installed as a package).
- Tests: `uv run python -m pytest` (`python -m` puts the repo on the import path). They cover only the pure decisions in `video/rules.py`; the cases live in `tests/fixtures/video_rules.json` so a port could run the same ones. ASR, LLM and share-walking code is checked by running it.

## Data you must not damage

- `radio.db` in the repo root is the owner's real database (gitignored): 41,000 scanned files and days of transcription work. Inspect it with `sqlite3 -readonly radio.db`. Never delete or rebuild it; test experiments in scratch scripts that don't write to it, and ask before any run that rewrites existing transcripts.
- The audio collection itself is strictly read-only. Nothing in this project writes to the source drive.
- Transcripts, segments, summaries and story links are archive (hours of GPU and paid LLM calls): nothing deletes them automatically. Scans mark gone files `missing_since` and changed ones `changed_at`, keeping everything. Any code that removes those rows must be an explicit, owner-run command that lists what it will remove (ant racl-9X77J).

## Layout

- `cli.py`: argparse subcommands, dispatches to the modules below.
- `db.py`: schema (`files`, `transcripts`, `segments`, `stories`, `story_files`, `summaries`, `excludes`, FTS5 `search_index`) and write helpers.
- `scan.py`, `classify.py`: metadata sweep and heuristic scoring.
- `transcribe.py`: candidate selection (`pick_candidates`), ffmpeg clipping (`clip_to_wav`, with an optional `start` seek), ASR (`make_asr`), the per-file commit loop with `--workday` scheduling (`run_loop`, shared with `video transcribe`: radio's `transcribe` passes it a `process(row)` that clips and transcribes).
- `video/`: the `video` commands. `db.py` (video-only tables, radio-database guard), `rules.py` (pure decisions, fixture-tested), `scan.py` (scan and `remount`), `transcribe.py` (subtitles, else v2 or v3 chosen by audio tag or a mid-file probe).
- `summarise.py`: grouping folders into stories (LLM for mixed folders, windows plus a merge pass for big ones), story listings, episode summaries. Design and trial evidence in ant note racl-mjBCN.
- `excludes.py`: the `excludes` table of regexes that `transcribe` and `summarise` skip, and the path/tags haystack they and `grep` match against.
- `search.py`: `search`, `grep`, `show`, `export`, `stats`.

## Things learned the hard way

- parakeet-mlx must be called with `chunk_duration`: unchunked, a 28 minute file asks Metal for 29GB and fails. 180s chunks peak under 4GB, and sentence timestamps stay absolute across chunk joins (checked: last sentence ends at the file's length).
- Sentence length is capped with `SentenceConfig(max_words=30)`, not `max_duration`, which splits mid-word.
- `--deepen` does nothing unless `--seconds` is larger than the existing transcripts (use `--seconds 0` for whole files). It skips files that already have a whole-file transcript, so re-doing those needs a one-off script (e.g. swap `transcribe.pick_candidates` for a function returning the rows you want, then call `transcribe.transcribe`).
- `mx.clear_cache()` after every file is load-bearing: without it the MLX buffer cache grew to 11.6GB over 80 files.
- Output from `uv run ... | grep` is block-buffered, so progress lines only appear at the end of a background run; check progress in the database instead.

## Hardware

The owner's machine is an M6 Mac mini with 24GB. Measured there: GPU about 80x real time on whole files, CPU (`--gentle`) about 3x slower than GPU. Older numbers in the ant notes are from an M1.
