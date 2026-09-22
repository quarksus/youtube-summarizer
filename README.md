# ytsum

Summarize a YouTube video from the command line, using its captions and the Claude API.

YouTube blocks most server-side fetchers, so the captions have to be pulled with a real
client rather than a plain HTTP request. `ytsum` does that, cleans up the subtitle file,
and hands the transcript to Claude with a prompt tuned for one specific failure mode:
auto-generated captions mangle names, and a summarizer that papers over the mangling
produces confident nonsense.

```
$ ytsum.py "https://www.youtube.com/live/zBNKrja8dyY" --style brief

- the host says a rival broker led the company by ~30% on throughput four years ago, but the company closed
  the gap and is now "a little ahead" [00:18:00].
- Founders (three founders — sp?) resigned operational roles voluntarily,
  remain on the board and own >50% [00:29:00].
```

## How it works

1. `yt-dlp` fetches the subtitle track only (`skip_download`) — no video is downloaded.
2. The VTT is flattened into plain text: markup stripped, rolling-caption duplicates
   collapsed, and `[HH:MM:SS]` markers inserted once a minute so the summary can cite
   jump points.
3. The transcript goes to Claude in a single streamed request. A 78-minute stream is
   ~28k tokens against a 1M-token context window, so chunking is the exception — only
   genuinely enormous transcripts get split into parts and synthesized.
4. Transcripts are cached in `transcripts/`, so re-running with a different `--style`
   or `--focus` is one API call and no re-fetch.

## Install

```bash
git clone https://github.com/quarksus/youtube-summarizer.git
cd youtube-summarizer
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env    # then paste your ANTHROPIC_API_KEY
```

Get an API key at [console.anthropic.com](https://console.anthropic.com/settings/keys).

## Use

```bash
.venv/bin/python ytsum.py "https://www.youtube.com/watch?v=VIDEO_ID"
```

| Flag | What it does |
| --- | --- |
| `--style detailed` | default; 600–1200 words under headings derived from the content |
| `--style brief` | ~200 words, 5–8 bullets |
| `--style notes` | dense nested study notes, timestamps on most bullets |
| `--focus "..."` | centre the summary on one thing; it will say so if the video barely covers it |
| `--lang de` | caption language — YouTube auto-translates, so this works on English videos too |
| `--transcript-only` | fetch and clean the captions, skip Claude entirely (no API cost) |
| `--refresh` | re-fetch instead of using the cached transcript |
| `--model` | defaults to `claude-opus-5` |
| `--out FILE` | write somewhere other than `summaries/` |

Summaries are written to `summaries/YYYY-MM-DD-<slug>-<style>.md`. Both output
directories are gitignored.

## The prompt is the interesting part

Most of the quality difference lives in the system prompt, not the plumbing. It holds
Claude to four rules:

- use only the transcript — no outside knowledge, and no guessing to smooth over a gap;
- mark uncertain proper nouns `(sp?)` rather than inventing a confident spelling;
- attribute claims to whoever made them instead of restating them as fact;
- cite `[HH:MM:SS]` timestamps for points worth jumping to.

The first two matter more than they look. Auto-generated captions garble names
constantly, and without those rules a summary will cheerfully invent a plausible-looking
name for a person who was never named.

## Limits

- **No captions, no summary.** Videos with captions disabled need audio transcription
  (e.g. Whisper). Not implemented.
- **TLS behind an inspecting proxy.** yt-dlp ships its own certifi bundle, which a
  TLS-inspecting corporate proxy will break. ytsum catches that and retries against the
  system trust store.
- **Debian/Ubuntu `ensurepip`.** If `python3 -m venv .venv` fails with "ensurepip is not
  available", install the matching venv package (`apt install python3.x-venv`).

## License

MIT — see [LICENSE](LICENSE).
