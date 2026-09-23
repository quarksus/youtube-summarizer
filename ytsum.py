"""
ytsum - summarize a YouTube video from its captions, using Claude.

Run `ytsum` with no arguments and it will ask for what it needs: an API key on
first run, then a video. Flags are there for scripting, not for daily use.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime
from getpass import getpass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import anthropic
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

APP = "ytsum"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / APP
CRED_FILE = CONFIG_DIR / "credentials"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / APP
OUTPUT_DIR = Path.home() / "youtube-summarizer"

MODEL = "claude-opus-5"
# USD per million tokens: (input, output). Used only for the cost estimate.
PRICES = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

# Opus 5 takes 1M tokens of context; leave room for the prompt and the answer.
MAX_INPUT_TOKENS = 700_000

SYSTEM = """You summarize video transcripts for a reader who has not watched the video and wants the substance without the runtime.

Ground rules:
- Use only what is in the transcript. Never add background facts from your own knowledge, and never paper over a gap by guessing what was probably said.
- The transcript comes from auto-generated captions, so proper nouns, product names and numbers are often garbled. Write the most likely spelling and mark it "(sp?)" where you are not confident. If a passage is too garbled to interpret, say so rather than inventing a reading.
- Attribute claims to whoever made them ("the speaker argues", "the host's view") instead of stating them as fact.
- Prefer the specific over the generic: numbers, names, commitments, dates, disagreements. Cut pleasantries, sponsor reads and small talk.
- Cite timestamps as [HH:MM:SS], copied from the markers in the transcript, for points a reader might want to jump to.
- Output GitHub-flavoured Markdown. Start directly with the content - no preamble, no "this video discusses"."""

STYLES = {
    "brief": (
        "Write at most 200 words: one sentence saying what this is, then 5-8 bullets "
        "carrying the substance."
    ),
    "detailed": (
        "Write 600-1200 words under `##` headings you derive from what this video "
        "actually covers (not a fixed template). Carry the main argument or news, the "
        "supporting specifics, and anything notable from Q&A or audience interaction. "
        "End with a short `## Takeaways` list."
    ),
    "notes": (
        "Write dense study notes: nested bullets grouped under `##` topic headings, "
        "heavy on concrete detail, timestamps on most bullets. Favour completeness "
        "over prose."
    ),
}

class QuietLogger:
    """yt-dlp prints its own ERROR lines even when quiet; we format our own."""

    def debug(self, msg): pass

    def info(self, msg): pass

    def warning(self, msg): pass

    def error(self, msg): pass


CUE_TIME = re.compile(r"^(\d{2}):(\d{2}):(\d{2})\.\d{3}\s+-->")
TAG = re.compile(r"<[^>]+>")
SKIP_PREFIX = ("WEBVTT", "Kind:", "Language:", "NOTE", "STYLE", "REGION")


# --------------------------------------------------------------------------- ui


def paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stderr.isatty() else text


def bold(t: str) -> str:
    return paint(t, "1")


def dim(t: str) -> str:
    return paint(t, "2")


def green(t: str) -> str:
    return paint(t, "32")


def say(message: str = "") -> None:
    """Progress and prompts go to stderr so the summary itself can be piped."""
    print(message, file=sys.stderr, flush=True)


def ask_style() -> str:
    """Ask how long the summary should be, before any work is done."""
    say()
    say(bold("How long should the summary be?"))
    say("  1) Short      about 200 words of bullet points")
    say("  2) Detailed   600-1200 words under headings " + dim("(default)"))
    while True:
        choice = ask("Choose 1 or 2 [2]: ").lower()
        if choice in ("", "2", "d", "detailed", "long"):
            return "detailed"
        if choice in ("1", "s", "short", "brief"):
            return "brief"
        if choice in ("3", "n", "notes"):  # undocumented third option
            return "notes"
        say("Please type 1 or 2.")


def ask(prompt: str) -> str:
    try:
        sys.stderr.write(bold(prompt))
        sys.stderr.flush()
        return input().strip()
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("\nCancelled.")


def read_secret(prompt: str) -> str:
    """Read a secret, echoing '*' per character.

    getpass() shows nothing at all, which makes a failed paste look exactly like
    a frozen program. Echoing a mask means you can see the paste land.
    """
    if not sys.stdin.isatty():
        return getpass(prompt)
    try:
        import termios
        import tty
    except ImportError:  # not a POSIX terminal
        return getpass(prompt)

    sys.stderr.write(prompt)
    sys.stderr.flush()
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    buf = bytearray()
    try:
        tty.setraw(fd)
        while True:
            ch = os.read(fd, 1)
            if ch in (b"\r", b"\n", b"", b"\x04"):
                break
            if ch == b"\x03":
                raise KeyboardInterrupt
            if ch in (b"\x7f", b"\b"):
                if buf:
                    buf.pop()
                    sys.stderr.write("\b \b")
                    sys.stderr.flush()
                continue
            if ch == b"\x15":  # Ctrl-U clears the line
                sys.stderr.write("\b \b" * len(buf))
                sys.stderr.flush()
                buf.clear()
                continue
            if ch == b"\x1b":
                # Swallow arrow keys and bracketed-paste markers
                # (\x1b[200~ ... \x1b[201~) rather than masking them as characters.
                if os.read(fd, 1) == b"[":
                    while True:
                        tail = os.read(fd, 1)
                        if not tail or tail.isalpha() or tail == b"~":
                            break
                continue
            if ch >= b"\x20":
                buf.extend(ch)
                sys.stderr.write("*")
                sys.stderr.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        sys.stderr.write("\n")
    return buf.decode("utf-8", "ignore")


# ----------------------------------------------------------------------- the key


def load_key() -> tuple[str, str | None] | None:
    """Return (api_key, workspace_id) from the environment or the stored file."""
    env = os.environ.get("ANTHROPIC_API_KEY")
    if env and env.strip():
        return env.strip(), os.environ.get("ANTHROPIC_WORKSPACE_ID") or None
    if CRED_FILE.exists():
        try:
            data = json.loads(CRED_FILE.read_text())
            return data["api_key"].strip(), data.get("workspace_id") or None
        except (json.JSONDecodeError, KeyError, OSError):
            say(dim(f"Ignoring unreadable {CRED_FILE}"))
    return None


def save_key(key: str, workspace: str | None) -> None:
    """Write credentials so that only this user account can read them."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    payload = {"api_key": key}
    if workspace:
        payload["workspace_id"] = workspace
    fd = os.open(CRED_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(payload, handle)
        handle.write("\n")


def make_client(key: str, workspace: str | None = None):
    headers = {"anthropic-workspace-id": workspace} if workspace else None
    return anthropic.Anthropic(api_key=key, default_headers=headers)


NEEDS_WORKSPACE = "not scoped to a workspace"


def check_key(key: str, workspace: str | None) -> tuple[bool, str]:
    """Try the operation ytsum actually performs. count_tokens is free.

    Anything that works here can summarize; anything that fails here would have
    failed later, mid-video, after the captions were already fetched.
    """
    try:
        make_client(key, workspace).messages.count_tokens(
            model=MODEL, messages=[{"role": "user", "content": "hi"}]
        )
    except anthropic.AuthenticationError:
        return False, "invalid"
    except anthropic.APIConnectionError:
        return False, "offline"
    except anthropic.APIStatusError as err:
        detail = str(getattr(err, "message", "") or err)
        return False, NEEDS_WORKSPACE if NEEDS_WORKSPACE in detail else detail
    return True, ""


def store_key(key: str, workspace: str | None = None) -> tuple[str, str | None] | None:
    """Validate credentials, save them, and explain where they went."""
    sys.stderr.write("Checking it with Anthropic... ")
    sys.stderr.flush()
    ok, problem = check_key(key, workspace)

    if not ok and problem == NEEDS_WORKSPACE:
        say("needs a workspace.")
        say()
        say("This key belongs to your organisation but isn't tied to a workspace,")
        say("so Anthropic needs to know which workspace to bill and apply limits from.")
        say()
        say("Two ways forward:")
        say(dim("  a) Create a key inside a workspace: Console > Workspaces > your"))
        say(dim("     workspace > API keys. That key needs no extra setup."))
        say(dim("  b) Paste the workspace ID here (Console > Workspaces, it looks"))
        say(dim("     like wrkspc_...). ytsum will send it with every request."))
        say()
        entered = ask("Workspace ID (or press Enter to use a different key): ")
        if not entered:
            say()
            return None
        return store_key(key, entered)

    if not ok and problem == "invalid":
        say("rejected.")
        say("That key isn't valid. Check you copied all of it.\n")
        return None
    if not ok and problem == "offline":
        say("no connection.")
        say("Couldn't reach api.anthropic.com. Check your network and try again.\n")
        return None
    if not ok:
        say("refused.")
        say(f"Anthropic said: {problem[:200]}\n")
        return None

    say("works.")
    save_key(key, workspace)
    say()
    say(green("Key saved.") + f"  {CRED_FILE}")
    say("  - File permissions are 0600: only your user account can read it.")
    say("  - It lives in your home config folder, never in a project or git repo.")
    say("  - ytsum sends it to api.anthropic.com and nowhere else.")
    if workspace:
        say(f"  - Requests use workspace {workspace}.")
    say(dim("  Anyone with administrator access to this machine could still read it,"))
    say(dim("  so revoke the key in the Console if the machine is ever compromised."))
    say()
    return key, workspace


def setup_key() -> tuple[str, str | None]:
    if not sys.stdin.isatty():
        piped = sys.stdin.read().strip()
        if piped:
            stored = store_key(piped)
            if not stored:
                raise SystemExit("Key rejected.")
            return stored

    say()
    say(bold("First run - ytsum needs an Anthropic API key."))
    say("Create one at https://console.anthropic.com/settings/keys")
    say()
    say(dim("Paste with Ctrl+Shift+V, or middle-click (Cmd+V on macOS)."))
    say(dim("Ctrl+V does not paste in most Linux terminals."))
    say(dim("You will see one * per character. Press Enter when done."))
    say()

    empty = 0
    while True:
        try:
            key = read_secret("API key: ").strip()
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("\nCancelled.")

        if not key:
            empty += 1
            say("Nothing arrived at the prompt.")
            if empty >= 2:
                say()
                say("If your terminal won't paste, use one of these instead:")
                say(dim("  ytsum --set-key          then paste, press Enter, then Ctrl-D"))
                say(dim("  ANTHROPIC_API_KEY=sk-ant-... ytsum"))
                say()
            continue

        if not key.startswith("sk-ant-"):
            say(dim("That doesn't look like an Anthropic key - they begin with 'sk-ant-'."))
            if ask("Use it anyway? [y/N] ").lower() not in ("y", "yes"):
                say()
                continue

        stored = store_key(key)
        if stored:
            return stored


# ------------------------------------------------------------------- transcript


def video_id(text: str) -> str:
    """Accept a full URL in any of YouTube's shapes, or a bare video ID."""
    text = text.strip()
    if re.fullmatch(r"[\w-]{11}", text):
        return text
    parsed = urlparse(text if "//" in text else f"https://{text}")
    if parsed.netloc.endswith("youtu.be"):
        candidate = parsed.path.lstrip("/")
    elif "v" in parse_qs(parsed.query):
        candidate = parse_qs(parsed.query)["v"][0]
    else:
        candidate = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    if not re.fullmatch(r"[\w-]{11}", candidate):
        raise SystemExit(f"That doesn't look like a YouTube video: {text}")
    return candidate


def fmt_ts(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}h {s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m {s % 60:02d}s"


def vtt_to_text(vtt: str, marker_every: int = 60) -> str:
    """Flatten a VTT subtitle file into plain text with periodic time markers.

    Auto-generated captions roll: each cue repeats the tail of the previous one.
    Dropping any line identical to the last line we kept collapses that back
    into a single readable stream.
    """
    out: list[str] = []
    last_line: str | None = None
    cue_start: float | None = None
    next_marker = 0.0

    for raw in vtt.splitlines():
        line = raw.strip()
        match = CUE_TIME.match(line)
        if match:
            h, m, s = (int(x) for x in match.groups())
            cue_start = h * 3600 + m * 60 + s
            continue
        if not line or line.startswith(SKIP_PREFIX) or line.isdigit():
            continue
        text = html.unescape(TAG.sub("", line)).strip()
        if not text or text == last_line:
            continue
        if cue_start is not None and cue_start >= next_marker:
            out.append(f"\n[{fmt_ts(cue_start)}]")
            next_marker = (cue_start // marker_every + 1) * marker_every
        out.append(text)
        last_line = text

    return "\n".join(out).strip()


def ydl_options(lang: str, outdir: Path, no_certifi: bool) -> dict:
    opts = {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": [lang, f"{lang}-orig", f"{lang}.*"],
        "subtitlesformat": "vtt",
        "outtmpl": str(outdir / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": QuietLogger(),
    }
    if no_certifi:
        # Behind a TLS-inspecting proxy, yt-dlp's bundled certifi store rejects the
        # proxy's certificate; this falls back to the system trust store.
        opts["compat_opts"] = {"no-certifi"}
    return opts


def download(url: str, lang: str, outdir: Path) -> dict:
    """Fetch subtitle metadata, working around TLS proxies and rate limits.

    YouTube answers 429 when it has seen too many requests from an address.
    It usually clears within a minute or two, so a few spaced retries are worth
    more than an immediate failure.
    """
    delays = [5, 20, 45]
    no_certifi = False
    attempt = 0
    while True:
        try:
            with YoutubeDL(ydl_options(lang, outdir, no_certifi)) as ydl:
                return ydl.extract_info(url, download=True)
        except DownloadError as err:
            message = str(err)
            if "certificate" in message.lower() and not no_certifi:
                say(dim("TLS error - retrying with the system certificate store..."))
                no_certifi = True
                continue
            if "429" in message or "Too Many Requests" in message:
                if attempt < len(delays):
                    wait = delays[attempt]
                    attempt += 1
                    say(dim(f"YouTube is rate-limiting us; waiting {wait}s "
                            f"(try {attempt} of {len(delays)})..."))
                    time.sleep(wait)
                    continue
                raise SystemExit(
                    "YouTube is rate-limiting this network (HTTP 429).\n"
                    "It usually clears in a few minutes - wait, then run the same "
                    "command again.\nVideos already in the cache still work offline."
                ) from err
            if "Private video" in message or "members-only" in message.lower():
                raise SystemExit("That video is private or members-only.") from err
            if "Video unavailable" in message:
                raise SystemExit("That video is unavailable.") from err
            raise SystemExit(f"Could not reach that video.\n{message}") from err


def fetch(url: str, lang: str) -> tuple[dict, str]:
    """Return (metadata, transcript text) for a video, without downloading it."""
    with tempfile.TemporaryDirectory() as tmp:
        outdir = Path(tmp)
        info = download(url, lang, outdir)

        files = sorted(outdir.glob("*.vtt"))
        # Several tracks can match (en, en-orig, ...); prefer the exact language.
        files = [f for f in files if f.name.endswith(f".{lang}.vtt")] or files
        if not files:
            available = sorted(
                set(info.get("subtitles") or {}) | set(info.get("automatic_captions") or {})
            )
            hint = f"\nAvailable: {', '.join(available[:20])}" if available else ""
            raise SystemExit(
                f"This video has no '{lang}' captions.{hint}\n"
                "Pick another with --lang, or transcribe the audio yourself (e.g. Whisper)."
            )
        transcript = vtt_to_text(files[0].read_text(encoding="utf-8", errors="replace"))

    meta = {
        "id": info.get("id", ""),
        "title": info.get("title", "Untitled"),
        "channel": info.get("uploader") or info.get("channel") or "Unknown channel",
        "url": info.get("webpage_url", url),
        "duration": info.get("duration"),
        "upload_date": info.get("upload_date"),
    }
    return meta, transcript


def header(meta: dict) -> str:
    bits = [f"Title: {meta['title']}", f"Channel: {meta['channel']}", f"URL: {meta['url']}"]
    if meta.get("duration"):
        bits.append(f"Duration: {fmt_ts(meta['duration'])}")
    if meta.get("upload_date"):
        d = meta["upload_date"]
        bits.append(f"Published: {d[:4]}-{d[4:6]}-{d[6:]}")
    return "\n".join(bits)


# ---------------------------------------------------------------------- claude


class Spend:
    """Running total of tokens, so the footer can report the real cost."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.input = 0
        self.output = 0

    def add(self, usage) -> None:
        self.input += getattr(usage, "input_tokens", 0)
        self.output += getattr(usage, "output_tokens", 0)

    def summary(self) -> str:
        tokens = f"{self.input:,} in / {self.output:,} out"
        price = PRICES.get(self.model)
        if not price:
            return tokens
        usd = self.input / 1e6 * price[0] + self.output / 1e6 * price[1]
        return f"{tokens} · about ${usd:.2f}" if usd >= 0.01 else f"{tokens} · under $0.01"


def request(client, model: str, prompt: str, spend: Spend, echo: bool) -> str:
    """One streamed request. Streaming keeps long answers under the HTTP timeout."""
    with client.messages.stream(
        model=model,
        max_tokens=64000,
        thinking={"type": "adaptive"},
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for chunk in stream.text_stream:
            if echo:
                print(chunk, end="", flush=True)
        message = stream.get_final_message()
    if echo:
        print()
    spend.add(message.usage)
    return "".join(b.text for b in message.content if b.type == "text").strip()


def split_parts(transcript: str, parts: int) -> list[str]:
    lines = transcript.splitlines()
    size = len(lines) // parts + 1
    return ["\n".join(lines[i : i + size]) for i in range(0, len(lines), size)]


def summarize(client, model, meta_block, transcript, style, focus, spend) -> str:
    instruction = STYLES[style]
    if focus:
        instruction += (
            f"\n\nThe reader cares most about: {focus}. Lead with that, and say so "
            "plainly if the video barely touches it."
        )

    def one_pass(body: str, note: str = "") -> str:
        prompt = f"{meta_block}\n\n{instruction}\n{note}\n\n<transcript>\n{body}\n</transcript>"
        return request(client, model, prompt, spend, echo=True)

    counted = client.messages.count_tokens(
        model=model, system=SYSTEM, messages=[{"role": "user", "content": transcript}]
    ).input_tokens

    if counted <= MAX_INPUT_TOKENS:
        return one_pass(transcript)

    parts = split_parts(transcript, counted // MAX_INPUT_TOKENS + 1)
    say(dim(f"Long transcript (~{counted:,} tokens) - summarizing in {len(parts)} parts."))
    notes = []
    for i, part in enumerate(parts, 1):
        say(dim(f"  part {i} of {len(parts)}..."))
        prompt = (
            f"{meta_block}\n\nThis is part {i} of {len(parts)} of a long transcript. "
            "Take thorough notes on THIS PART ONLY - every distinct point, with timestamps. "
            "Do not write an introduction or a conclusion; these notes will be merged with "
            f"the others.\n\n<transcript_part>\n{part}\n</transcript_part>"
        )
        notes.append(f"## Part {i}\n{request(client, model, prompt, spend, echo=False)}")
    return one_pass(
        "\n\n".join(notes),
        note="\nThe material below is sequential notes taken from the full transcript, "
        "not the transcript itself. Treat it as the record of the video.",
    )


# ------------------------------------------------------------------------ main


def main() -> None:
    parser = argparse.ArgumentParser(
        prog=APP,
        description="Summarize a YouTube video from its captions, using Claude.",
        epilog="Run with no arguments and ytsum will ask you for a video.",
    )
    parser.add_argument("video", nargs="?", help="YouTube URL or bare video ID")
    parser.add_argument(
        "--style", choices=sorted(STYLES), default=None, help="skip the length question"
    )
    parser.add_argument("--focus", help="what the summary should centre on")
    parser.add_argument("--lang", default="en", help="caption language (default: en)")
    parser.add_argument("--model", default=MODEL, help=f"Claude model (default: {MODEL})")
    parser.add_argument("--out", type=Path, help="write the summary to this file")
    parser.add_argument("--transcript-only", action="store_true", help="captions only, no API call")
    parser.add_argument("--refresh", action="store_true", help="re-fetch instead of using the cache")
    parser.add_argument("--reset-key", action="store_true", help="replace the stored API key")
    parser.add_argument(
        "--set-key", action="store_true", help="store a key read from stdin or a prompt"
    )
    args = parser.parse_args()

    if args.set_key:
        setup_key()
        if not args.video:
            return

    if args.reset_key:
        CRED_FILE.unlink(missing_ok=True)
        say("Stored key deleted.")
        setup_key()
        if not args.video:
            return

    key = workspace = None
    if not args.transcript_only:
        key, workspace = load_key() or setup_key()

    target = args.video or ask("YouTube URL or video ID: ")
    vid = video_id(target)

    style = args.style
    if style is None:
        style = ask_style() if sys.stdin.isatty() and not args.transcript_only else "detailed"

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"{vid}.{args.lang}.txt"

    if cache.exists() and not args.refresh:
        meta_block, transcript = cache.read_text(encoding="utf-8").split("\n\n---\n\n", 1)
        say(dim(f"Using cached captions ({len(transcript.split()):,} words)."))
    else:
        say("Fetching captions...")
        meta, transcript = fetch(f"https://www.youtube.com/watch?v={vid}", args.lang)
        meta_block = header(meta)
        cache.write_text(f"{meta_block}\n\n---\n\n{transcript}\n", encoding="utf-8")
        length = f", {fmt_duration(meta['duration'])}" if meta.get("duration") else ""
        say(dim(f"Got {len(transcript.split()):,} words{length}."))

    title = next(
        (l.removeprefix("Title: ") for l in meta_block.splitlines() if l.startswith("Title: ")),
        "untitled",
    )
    say(bold(title))

    if args.transcript_only:
        print(transcript)
        return

    say(f"Summarizing with {args.model}...")
    say()
    spend = Spend(args.model)
    client = make_client(key, workspace)
    try:
        summary = summarize(
            client, args.model, meta_block, transcript, style, args.focus, spend
        )
    except anthropic.AuthenticationError:
        raise SystemExit(f"Anthropic rejected the stored key. Run `{APP} --reset-key`.")
    except anthropic.APIStatusError as err:
        detail = str(getattr(err, "message", "") or err)
        if NEEDS_WORKSPACE in detail:
            raise SystemExit(
                "The stored key isn't tied to a workspace, so Anthropic won't accept it.\n"
                f"Run `{APP} --reset-key` to add a workspace ID, or create a key inside a\n"
                "workspace at Console > Workspaces > API keys."
            ) from err
        raise SystemExit(f"Anthropic refused the request:\n{detail[:300]}") from err
    except KeyboardInterrupt:
        raise SystemExit("\nStopped.")

    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60]
    path = args.out or OUTPUT_DIR / f"{datetime.now():%Y-%m-%d}-{slug}-{style}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# {title}\n\n{meta_block}\n"
        f"Summarized: {datetime.now():%Y-%m-%d %H:%M} with {args.model} ({style})\n\n"
        f"---\n\n{summary}\n",
        encoding="utf-8",
    )
    say()
    say(dim("─" * 60))
    say(f"{spend.summary()}")
    say(f"Saved to {path}")


if __name__ == "__main__":
    main()
