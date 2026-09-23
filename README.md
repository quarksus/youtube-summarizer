# ytsum

Summarize a YouTube video from the command line. Paste a link, get a summary.

```
$ ytsum
YouTube URL or video ID: zBNKrja8dyY

How long should the summary be?
  1) Short      about 200 words of bullet points
  2) Detailed   600-1200 words under headings (default)
Choose 1 or 2 [2]: 1

Fetching captions...
No 'en' track; using 'en-GB'.
Got 5,914 words, 29m 02s.
INTERSTELLAR (2014) Breakdown | Ending Explained, Easter Eggs, Hidden Details...
Summarizing with claude-opus-5...

- **Time as motif:** The host argues Nolan is obsessed with time; the Endurance is
  shaped like a clock face with 12 compartments, and on Miller's planet the
  soundtrack's ticks land every 1.25 seconds - one tick per day passing on Earth
  [00:01:11].
- **Black hole:** Gargantua was rendered from Einstein's equations at ~100 hours per
  frame; robots TARS (an anagram of "star") and CASE/KIPP nod to Kip Thorne [00:02:50].
- **Practical effects:** 500 acres of real corn were planted, later sold back for a
  profit [00:05:20].
────────────────────────────────────────────────────────────
11,829 in / 656 out · about $0.08
Saved to ~/youtube-summarizer/2026-09-23-interstellar-2014-breakdown-brief.md
```

## Install

```bash
pipx install yt-tldw
```

or with [uv](https://docs.astral.sh/uv/): `uv tool install yt-tldw`.

The distribution is called **yt-tldw**; the command it installs is **`ytsum`**.

No pipx or uv? One line, no prerequisites:

```bash
curl -fsSL https://raw.githubusercontent.com/quarksus/youtube-summarizer/main/install.sh | bash
```

That checks your Python, builds an isolated environment, installs the
dependencies and puts `ytsum` on your PATH. It only asks for a password if your
system is missing Python's `venv` package and there is no way around it — on
most machines it never needs one.

## Upgrading

```bash
pipx upgrade yt-tldw
```

Worth doing when YouTube changes something: most breakages are fixed by a newer
`yt-dlp`, which comes along with the upgrade.

## First run

Run `ytsum`. It asks for an Anthropic API key once
([get one here](https://console.anthropic.com/settings/keys)), checks the key
actually works, and saves it to `~/.config/ytsum/credentials` with `0600`
permissions — readable only by your user account, never in a project folder or a
git repo, and sent only to `api.anthropic.com`.

Anyone with root on your machine can still read that file, as with any stored
credential. If the machine is ever compromised, revoke the key in the Console.

The prompt masks what you type with `*`, so you can see the paste land. Note that
**Ctrl+V does not paste in most Linux terminals** — use Ctrl+Shift+V or middle-click
(Cmd+V on macOS).

If your terminal fights you, two alternatives:

```bash
ytsum --set-key                  # paste, press Enter, then Ctrl-D
echo "sk-ant-..." | ytsum --set-key
ANTHROPIC_API_KEY=sk-ant-... ytsum    # skip the stored key entirely
```

After that, `ytsum` goes straight to asking for a video. Replace the key any time
with `ytsum --reset-key`.

## Everyday use

```bash
ytsum                                      # asks you for a video
ytsum zBNKrja8dyY                          # bare video ID
ytsum "https://youtu.be/zBNKrja8dyY"       # any YouTube URL shape
ytsum --style brief <url>                  # ~200 words instead of ~1000
ytsum --focus "what they say about pricing" <url>
```

| Flag | What it does |
| --- | --- |
| `--style detailed` | skip the length question; 600–1200 words under headings |
| `--style brief` | skip the length question; ~200 words, 5–8 bullets |
| `--style notes` | dense nested study notes, timestamps on most bullets |
| `--focus "..."` | centre the summary on one thing; it says so if the video barely covers it |
| `--lang de` | caption language — YouTube auto-translates, so this works on English videos too |
| | `en` also matches `en-GB`/`en-US`, and human-written subtitles win over auto-generated |
| `--transcript-only` | print the cleaned captions, no API call, no cost |
| `--refresh` | re-fetch instead of using the cached captions |
| `--cookies-from-browser firefox` | fetch as your signed-in YouTube account if throttled |
| `--reset-key` | replace the stored API key |
| `--set-key` | store a key from a prompt or piped in from stdin |
| `--model` | defaults to `claude-opus-5` |
| `--out FILE` | write the summary somewhere specific |

Summaries are written to `~/youtube-summarizer/` as `YYYY-MM-DD-<title>-<style>.md`,
so they collect in one place wherever you run the command from. `--out FILE`
overrides it.
Captions are cached in `~/.cache/ytsum/`, so re-running the same video with a
different `--style` costs one API call and no re-fetch.

Every run ends with the tokens used and an estimated cost, so there are no
surprises on the bill.

## How it works

1. `yt-dlp` fetches the subtitle track only — no video is downloaded.
2. The VTT is flattened into plain text: markup stripped, rolling-caption
   duplicates collapsed, `[HH:MM:SS]` markers inserted once a minute so the
   summary can cite jump points.
3. The transcript goes to Claude in a single streamed request. A 78-minute stream
   is ~28k tokens against a 1M-token context window, so chunking is the exception
   — only enormous transcripts get split into parts and synthesized.

## The prompt is the interesting part

Most of the quality difference lives in the system prompt, not the plumbing. It
holds Claude to four rules:

- use only the transcript — no outside knowledge, and no guessing to fill a gap;
- mark uncertain proper nouns `(sp?)` rather than inventing a confident spelling;
- attribute claims to whoever made them instead of restating them as fact;
- cite `[HH:MM:SS]` timestamps for points worth jumping to.

The first two matter more than they look. Auto-generated captions garble names
constantly, and without those rules a summary will cheerfully invent a
plausible-looking name for someone who was never named.

## If your key isn't tied to a workspace

Keys created at the organisation level (Console → Settings → API keys) are not
scoped to a workspace, and Anthropic rejects every Messages API request from them
unless you say which workspace to use. ytsum detects this during setup and offers
to store a workspace ID alongside the key.

The simpler fix is to create the key inside a workspace instead: Console →
Workspaces → your workspace → API keys. Such a key needs no extra configuration.

## Limits

- **No captions, no summary.** Videos with captions disabled would need audio
  transcription (e.g. Whisper). Not implemented.
- **YouTube rate limiting.** ytsum reads a video's caption index through yt-dlp
  and then fetches the subtitle file itself, because yt-dlp's own subtitle
  download draws HTTP 429 far sooner than a plain request for the same URL.
  Measured on a throttled connection: human-written subtitles fetched fine this
  way while yt-dlp's downloader was still being refused.

  Auto-generated captions are throttled harder and can still fail while manual
  subtitles succeed. There is no trick for that one - wait it out, or try
  `--cookies-from-browser firefox` (also `chrome`, `brave`, `chromium`), which
  only helps if you are actually signed in to YouTube in that browser. Reading
  Chrome or Brave cookies on Linux needs the `secretstorage` package. Cached
  videos keep working regardless.
- **TLS behind an inspecting proxy.** yt-dlp ships its own certificate bundle,
  which a corporate TLS-inspecting proxy breaks. ytsum detects that and retries
  against the system trust store.

## Uninstall

```bash
rm -rf ~/.local/share/ytsum ~/.local/bin/ytsum ~/.config/ytsum ~/.cache/ytsum
```

## License

MIT — see [LICENSE](LICENSE).
