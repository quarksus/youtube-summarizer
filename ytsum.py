"""
ytsum - pull a YouTube video's captions and summarize them with Claude.

Fetches the subtitle track with yt-dlp (no video download), cleans the VTT into
readable text with [HH:MM:SS] markers, and sends it to Claude in one pass. The
transcript is cached, so re-running with a different --style or --focus costs
one API call and no re-fetch.

Requires ANTHROPIC_API_KEY in a local .env file (see .env.example).

Usage:
    .venv/bin/python ytsum.py <url>
    .venv/bin/python ytsum.py <url> --style brief
    .venv/bin/python ytsum.py <url> --focus "what they say about pricing"
    .venv/bin/python ytsum.py <url> --transcript-only
"""

from __future__ import annotations

import argparse
import html
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import anthropic
from dotenv import load_dotenv
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

HERE = Path(__file__).resolve().parent
TRANSCRIPT_DIR = HERE / "transcripts"
SUMMARY_DIR = HERE / "summaries"

MODEL = "claude-opus-5"
# Opus 5 has a 1M-token context window; leave room for the prompt and the answer.
# Above this we summarize the transcript in parts and synthesize the parts.
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

CUE_TIME = re.compile(r"^(\d{2}):(\d{2}):(\d{2})\.\d{3}\s+-->")
TAG = re.compile(r"<[^>]+>")
SKIP_PREFIX = ("WEBVTT", "Kind:", "Language:", "NOTE", "STYLE", "REGION")


def video_id(url: str) -> str:
    """Pull the 11-character id out of any of YouTube's URL shapes."""
    parsed = urlparse(url if "//" in url else f"https://{url}")
    if parsed.netloc.endswith("youtu.be"):
        candidate = parsed.path.lstrip("/")
    elif "v" in parse_qs(parsed.query):
        candidate = parse_qs(parsed.query)["v"][0]
    else:
        # /live/<id>, /shorts/<id>, /embed/<id>
        candidate = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    if not re.fullmatch(r"[\w-]{11}", candidate):
        raise SystemExit(f"Could not find a video id in: {url}")
    return candidate


def fmt_ts(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


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
    }
    if no_certifi:
        # Behind a TLS-inspecting proxy, yt-dlp's bundled certifi store rejects the
        # proxy's certificate; this falls back to the system trust store.
        opts["compat_opts"] = {"no-certifi"}
    return opts


def fetch(url: str, lang: str) -> tuple[dict, str]:
    """Return (metadata, transcript text) for a video, without downloading it."""
    with tempfile.TemporaryDirectory() as tmp:
        outdir = Path(tmp)
        try:
            with YoutubeDL(ydl_options(lang, outdir, no_certifi=False)) as ydl:
                info = ydl.extract_info(url, download=True)
        except DownloadError as err:
            if "certificate" not in str(err).lower():
                raise SystemExit(f"yt-dlp failed: {err}") from err
            print("TLS error, retrying with the system certificate store...", file=sys.stderr)
            with YoutubeDL(ydl_options(lang, outdir, no_certifi=True)) as ydl:
                info = ydl.extract_info(url, download=True)

        files = sorted(outdir.glob("*.vtt"))
        # Several tracks can match (en, en-orig, ...); prefer the exact language.
        exact = [f for f in files if f.name.endswith(f".{lang}.vtt")]
        files = exact or files
        if not files:
            available = sorted(
                set(info.get("subtitles") or {}) | set(info.get("automatic_captions") or {})
            )
            hint = f" Available languages: {', '.join(available[:20])}" if available else ""
            raise SystemExit(
                f"No '{lang}' captions for this video.{hint}\n"
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


def ask(client: anthropic.Anthropic, model: str, prompt: str, echo: bool) -> tuple[str, object]:
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
    text = "".join(b.text for b in message.content if b.type == "text").strip()
    return text, message.usage


def split_parts(transcript: str, parts: int) -> list[str]:
    lines = transcript.splitlines()
    size = len(lines) // parts + 1
    return ["\n".join(lines[i : i + size]) for i in range(0, len(lines), size)]


def summarize(
    client: anthropic.Anthropic,
    model: str,
    meta_block: str,
    transcript: str,
    style: str,
    focus: str | None,
) -> str:
    instruction = STYLES[style]
    if focus:
        instruction += f"\n\nThe reader cares most about: {focus}. Lead with that, and say so plainly if the video barely touches it."

    def one_pass(body: str, note: str = "") -> str:
        prompt = (
            f"{meta_block}\n\n{instruction}\n{note}\n\n"
            f"<transcript>\n{body}\n</transcript>"
        )
        text, usage = ask(client, model, prompt, echo=True)
        print(
            f"\n[{usage.input_tokens} in / {usage.output_tokens} out]",
            file=sys.stderr,
        )
        return text

    counted = client.messages.count_tokens(
        model=model,
        system=SYSTEM,
        messages=[{"role": "user", "content": transcript}],
    ).input_tokens

    if counted <= MAX_INPUT_TOKENS:
        return one_pass(transcript)

    # Too long for one request: take notes on each part, then synthesize.
    parts = split_parts(transcript, counted // MAX_INPUT_TOKENS + 1)
    print(f"Transcript is ~{counted} tokens; summarizing in {len(parts)} parts.", file=sys.stderr)
    notes = []
    for i, part in enumerate(parts, 1):
        prompt = (
            f"{meta_block}\n\nThis is part {i} of {len(parts)} of a long transcript. "
            "Take thorough notes on THIS PART ONLY - every distinct point, with timestamps. "
            "Do not write an introduction or a conclusion; these notes will be merged with "
            f"the others.\n\n<transcript_part>\n{part}\n</transcript_part>"
        )
        print(f"--- part {i}/{len(parts)} ---", file=sys.stderr)
        text, _ = ask(client, model, prompt, echo=False)
        notes.append(f"## Part {i}\n{text}")
    joined = "\n\n".join(notes)
    return one_pass(
        joined,
        note="\nThe material below is sequential notes taken from the full transcript, "
        "not the transcript itself. Treat it as the record of the video.",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize a YouTube video with Claude.")
    parser.add_argument("url", help="YouTube URL (watch, youtu.be, /live/ or /shorts/)")
    parser.add_argument("--style", choices=sorted(STYLES), default="detailed")
    parser.add_argument("--focus", help="what the summary should center on")
    parser.add_argument("--lang", default="en", help="caption language (default: en)")
    parser.add_argument("--model", default=MODEL, help=f"Claude model (default: {MODEL})")
    parser.add_argument("--out", type=Path, help="write the summary here instead of summaries/")
    parser.add_argument("--transcript-only", action="store_true", help="fetch captions, skip Claude")
    parser.add_argument("--refresh", action="store_true", help="re-fetch even if cached")
    args = parser.parse_args()

    load_dotenv(HERE / ".env")
    TRANSCRIPT_DIR.mkdir(exist_ok=True)

    vid = video_id(args.url)
    cache = TRANSCRIPT_DIR / f"{vid}.{args.lang}.txt"

    if cache.exists() and not args.refresh:
        meta_block, transcript = cache.read_text(encoding="utf-8").split("\n\n---\n\n", 1)
        print(f"Using cached transcript: {cache.name}", file=sys.stderr)
    else:
        meta, transcript = fetch(args.url, args.lang)
        meta_block = header(meta)
        cache.write_text(f"{meta_block}\n\n---\n\n{transcript}\n", encoding="utf-8")
        print(f"Transcript saved: {cache.name} ({len(transcript.split())} words)", file=sys.stderr)

    title = next(
        (l.removeprefix("Title: ") for l in meta_block.splitlines() if l.startswith("Title: ")),
        "untitled",
    )

    if args.transcript_only:
        print(transcript)
        return

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("Warning: no ANTHROPIC_API_KEY in .env or environment.", file=sys.stderr)

    client = anthropic.Anthropic()
    try:
        summary = summarize(client, args.model, meta_block, transcript, args.style, args.focus)
    except anthropic.AuthenticationError:
        raise SystemExit("Claude rejected the credentials. Put a working ANTHROPIC_API_KEY in .env.")

    SUMMARY_DIR.mkdir(exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60]
    path = args.out or SUMMARY_DIR / f"{datetime.now():%Y-%m-%d}-{slug}-{args.style}.md"
    path.write_text(
        f"# {title}\n\n{meta_block}\n"
        f"Summarized: {datetime.now():%Y-%m-%d %H:%M} with {args.model} ({args.style})\n\n"
        f"---\n\n{summary}\n",
        encoding="utf-8",
    )
    print(f"\nSaved: {path}", file=sys.stderr)


if __name__ == "__main__":
    main()
