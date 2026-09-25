# radio-classifier

Finds the radio dramas hiding in a large, badly named mp3 collection, by listening to the files instead of trusting their names.

## What it does

If you have decades of accumulated audio (CD rips, cassette transfers, off-air recordings, forum downloads), the filenames and ID3 tags will lie to you. `931014 The Naked Nuns-3of6-(BBCR4).mp3` is easy enough to spot, but the 1975 thriller you half-remember is probably sitting in a folder called `Unknown Artist` with a name that tells you nothing.

radio-classifier works in three stages, each a separate command you can run and re-run:

1. `scan` walks your collection and records duration, bitrate, channels and tags for every audio file into a local SQLite database. It never writes to the source drive.
2. `classify` scores every file on how likely it is to be spoken-word drama, using duration, mono/stereo, bitrate, genre tags, episode-numbering patterns and keywords. A 90-minute mono file at 64kbps is probably not a pop single, whatever it's called.
3. `transcribe` runs a local speech-to-text model (Parakeet, via Apple's MLX) over the first three minutes of each candidate and indexes the text for full-text search. Radio dramas almost always open with a continuity announcement ("Omega Point, by Bruce Stewart, with Dinsdale Landen and Sydney Tafler..."), so title, author and cast end up searchable even when the filename is gibberish.

Then `search` finds things by what was said in them. An actor's name, a title fragment, a plot detail from the opening scene.

Once the first pass is done you can go further and transcribe whole files (see below), which makes plot details from anywhere in a serial searchable and records a timestamp for every sentence.

Everything runs locally, apart from a one-time model download from Hugging Face (about 2.5GB).

## Prerequisites

- An Apple Silicon Mac (transcription runs on MLX)
- [uv](https://docs.astral.sh/uv/)
- ffmpeg (`brew install ffmpeg`)
- Python 3.11+ (uv will handle this for you)

## Getting started

```sh
git clone https://github.com/ohnotnow/radio-classifier.git
cd radio-classifier
uv sync
```

Then point it at your collection:

```sh
uv run python -m radio_classifier scan /Volumes/BigDisk/music
uv run python -m radio_classifier classify
uv run python -m radio_classifier transcribe --workday
uv run python -m radio_classifier search "dinsdale landen"
```

`scan` takes minutes (about 75 files a second over USB), `classify` takes seconds, and `transcribe` is the long haul: budget a few days of on-and-off running for a five-figure collection. The database lives in `radio.db` in the current directory (`--db` to put it elsewhere), and transcription commits after every file, so you can kill any run at any moment and it resumes where it left off.

## The --workday flag

Transcription is GPU-heavy, which on a laptop means fans, and a machine that fights your video calls. `--workday` exists so you don't have to care:

- 07:00 to 23:00 it runs CPU-only on a single core. The GPU stays free, the machine stays usable, and you'll mostly forget it's running.
- Overnight it switches to the GPU at full speed, with an 8 second cool-down between files so the fans stay quiet while you sleep.

Start it once and leave it for days:

```sh
caffeinate -i uv run python -m radio_classifier transcribe --workday
```

## Whole-file transcripts and timestamps

The three-minute pass finds the continuity announcement, but the detail you actually remember ("his girlfriend had a shop") might turn up twenty minutes into episode four. Once the first pass is done, re-transcribe the candidates in full:

```sh
caffeinate -i uv run python -m radio_classifier transcribe --deepen --seconds 0 --workday
```

Whole files are processed in 180 second chunks, so memory stays under 4GB whatever the length. Every sentence is stored with its start and end time, and `search` then tells you where in the file the first match is:

```
[35146] (25m34, likely) /Volumes/BigDisk/music/Jim Eldridge/Unknown Album/760508 Down Payment On Death 2_5.mp3
       ... What about the >>shop<<? To hell with a >>shop<<. It's because of this attack, isn't it? ...
      first at 11:22: What about the shop?
```

`export ID` prints a transcript as SRT subtitles, which most media players will load alongside the audio (`export 35146 > "Down Payment On Death 2_5.srt"`). `--plain` gives just the text. Files that only have a three-minute transcript have no timestamps, so `export` needs `--plain` for those.

If you want manual control over the transcription schedule, `--gentle` forces CPU-only mode and `--pause N` sleeps N seconds between files. Running the whole thing under `taskpolicy -c background` makes it politer still.

## Commands

| Command | Purpose |
|---|---|
| `scan ROOT` | Walk a directory tree and record audio metadata. Incremental; prunes files that have gone. |
| `classify` | Score every file: likely / maybe / unlikely drama. |
| `transcribe` | Speech-to-text the start of candidate files. `--seconds N` (default 180, 0 = whole file), `--limit N`, `--verdict likely,maybe`, `--deepen` to re-transcribe with a longer window, `--exclude REGEX` (repeatable) to skip shows you don't need indexed, `--model` to use a different Hugging Face model, plus `--workday`, `--gentle`, `--pause`. |
| `search QUERY` | Full-text search over transcripts, paths and tags, with the time of the first matching sentence where known. `--any` matches any word instead of all. |
| `grep PATTERN` | Case-insensitive regex over paths and tags. Works before anything is transcribed. |
| `show ID` | One file's metadata, verdict, score reasons and transcript. |
| `export ID` | A transcript as SRT subtitles. `--plain` for text only. |
| `stats` | Collection totals and transcription progress. |

## How the classifier decides

Every file gets a score and a verdict you can inspect with `show`: the reasons column lists exactly what fired (`kw:bbc`, `dur:25m+`, `mono`, `genre-:rock`). Audio properties count for more than names, because names are the thing you cannot trust. False positives are cheap (a documentary gets transcribed and indexed, no harm done); false negatives are the enemy. Well-named files that fail the duration test (chaptered CD rips) get in on keywords, anonymous 90-minute files get in on duration.

The `--exclude` flag is for spoken-word you own but don't need indexed. Panel games, say, where a transcript of episode 412 adds nothing to your life.

## Performance

Measured on two Macs, so treat these as a guide rather than a promise. Per-file times include decoding and saving.

| Machine | Mode | 180s clip | Whole 28 minute episode |
|---|---|---|---|
| M1 | GPU | ~9.5s | not measured |
| M1 | CPU (`--gentle`) | ~15.5s | not measured |
| M6 | GPU | ~2s | ~21s (about 80x real time) |
| M6 | CPU (`--gentle`) | ~6.5s | ~65s |

A 41,000 file collection produced about 10,000 drama candidates, around 5,600 hours of audio. On the M6 the three-minute pass took one evening; transcribing every candidate in full is roughly 70 hours of GPU time, longer under `--workday` because daytime runs on the CPU.

## Contributing

It's early days and there's no test suite yet, so expect sharp edges. Fork, `uv sync`, and open an issue or PR on [GitHub](https://github.com/ohnotnow/radio-classifier) if something bites you or you teach it a new trick.

## Licence

MIT.
