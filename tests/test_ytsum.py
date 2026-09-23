"""Tests for ytsum.

Focus is on the logic that has actually broken in practice: URL shapes,
caption cleanup, language fallback, credential storage and the network
failure paths. Nothing here touches YouTube or the Claude API.
"""

import json
import os
import stat

import anthropic
import httpx2
import pytest

import ytsum


# --------------------------------------------------------------- video ids

@pytest.mark.parametrize(
    "given",
    [
        "zBNKrja8dyY",
        "https://www.youtube.com/watch?v=zBNKrja8dyY",
        "https://youtu.be/zBNKrja8dyY",
        "https://www.youtube.com/live/zBNKrja8dyY",
        "https://www.youtube.com/shorts/zBNKrja8dyY",
        "https://www.youtube.com/embed/zBNKrja8dyY",
        "https://youtu.be/zBNKrja8dyY?si=Qgbi3HqBzevN0kaQ",
        "https://www.youtube.com/watch?v=zBNKrja8dyY&t=42s",
        "www.youtube.com/watch?v=zBNKrja8dyY",
    ],
)
def test_video_id_accepts_every_shape(given):
    assert ytsum.video_id(given) == "zBNKrja8dyY"


@pytest.mark.parametrize("given", ["", "not a url", "https://example.com/", "https://youtu.be/short"])
def test_video_id_rejects_rubbish(given):
    with pytest.raises(SystemExit):
        ytsum.video_id(given)


# ------------------------------------------------------------ vtt cleanup

def test_vtt_strips_headers_tags_and_entities():
    vtt = """WEBVTT
Kind: captions
Language: en

00:00:01.000 --> 00:00:03.000 align:start position:0%
so<00:00:01.400><c> we're</c> live &amp; well
"""
    assert ytsum.vtt_to_text(vtt).splitlines()[-1] == "so we're live & well"


def test_vtt_collapses_rolling_duplicates():
    """Auto-captions repeat the previous line in each cue; one copy should survive."""
    vtt = """WEBVTT

00:00:01.000 --> 00:00:03.000
first line

00:00:03.000 --> 00:00:05.000
first line
second line

00:00:05.000 --> 00:00:07.000
second line
third line
"""
    body = [l for l in ytsum.vtt_to_text(vtt).splitlines() if l and not l.startswith("[")]
    assert body == ["first line", "second line", "third line"]


def test_vtt_inserts_minute_markers():
    vtt = "WEBVTT\n\n"
    for minute in range(3):
        vtt += f"00:0{minute}:10.000 --> 00:0{minute}:12.000\nline {minute}\n\n"
    markers = [l for l in ytsum.vtt_to_text(vtt).splitlines() if l.startswith("[")]
    assert markers == ["[00:00:10]", "[00:01:10]", "[00:02:10]"]


def test_vtt_handles_empty_input():
    assert ytsum.vtt_to_text("WEBVTT\n\n") == ""


# --------------------------------------------------------------- formatting

@pytest.mark.parametrize(
    "seconds,expected", [(0, "00:00:00"), (61, "00:01:01"), (3661, "01:01:01"), (86399, "23:59:59")]
)
def test_fmt_ts(seconds, expected):
    assert ytsum.fmt_ts(seconds) == expected


@pytest.mark.parametrize("seconds,expected", [(59, "0m 59s"), (600, "10m 00s"), (5442, "1h 30m")])
def test_fmt_duration(seconds, expected):
    assert ytsum.fmt_duration(seconds) == expected


# ------------------------------------------------------- language selection

def test_en_falls_back_to_regional_variants():
    """The bug that made a real video unusable: it published only en-GB."""
    assert ytsum.language_candidates({"en-GB": [], "de": []}, "en") == ["en-GB"]


def test_exact_language_wins_over_variant():
    assert ytsum.language_candidates({"en": [], "en-GB": []}, "en") == ["en", "en-GB"]


def test_no_candidates_when_language_absent():
    assert ytsum.language_candidates({"de": [], "fr": []}, "en") == []


def test_manual_subtitles_beat_auto_captions():
    info = {
        "subtitles": {"en": [{"ext": "vtt", "url": "manual"}]},
        "automatic_captions": {"en": [{"ext": "vtt", "url": "auto"}]},
    }
    url, code, source = ytsum.pick_track(info, "en")
    assert (url, code, source) == ("manual", "en", "subtitles")


def test_falls_back_to_auto_captions():
    info = {"subtitles": {}, "automatic_captions": {"en": [{"ext": "vtt", "url": "auto"}]}}
    assert ytsum.pick_track(info, "en")[2] == "auto-generated captions"


def test_ignores_non_vtt_formats():
    info = {"subtitles": {"en": [{"ext": "srv3", "url": "no"}]}, "automatic_captions": {}}
    assert ytsum.pick_track(info, "en") is None


# -------------------------------------------------------------------- cost

def test_cost_uses_opus_pricing():
    spend = ytsum.Spend("claude-opus-5")
    spend.add(type("U", (), {"input_tokens": 1_000_000, "output_tokens": 1_000_000})())
    assert "$30.00" in spend.summary()          # 5 in + 25 out


def test_tiny_cost_is_not_reported_as_zero():
    spend = ytsum.Spend("claude-opus-5")
    spend.add(type("U", (), {"input_tokens": 10, "output_tokens": 1})())
    assert "under $0.01" in spend.summary()


def test_unknown_model_reports_tokens_only():
    spend = ytsum.Spend("some-future-model")
    spend.add(type("U", (), {"input_tokens": 5, "output_tokens": 7})())
    assert spend.summary() == "5 in / 7 out"


def test_spend_accumulates_across_calls():
    spend = ytsum.Spend("claude-opus-5")
    for _ in range(3):
        spend.add(type("U", (), {"input_tokens": 100, "output_tokens": 10})())
    assert spend.input == 300 and spend.output == 30


# ------------------------------------------------------------- credentials

@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setattr(ytsum, "CONFIG_DIR", tmp_path / "ytsum")
    monkeypatch.setattr(ytsum, "CRED_FILE", tmp_path / "ytsum" / "credentials")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)
    return tmp_path / "ytsum" / "credentials"


def test_key_round_trip(config):
    ytsum.save_key("sk-ant-test", None)
    assert ytsum.load_key() == ("sk-ant-test", None)


def test_saved_key_is_not_world_readable(config):
    """A credential file others can read is the whole point of getting this right."""
    ytsum.save_key("sk-ant-test", None)
    mode = stat.S_IMODE(os.stat(config).st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"
    assert stat.S_IMODE(os.stat(config.parent).st_mode) == 0o700


def test_workspace_id_round_trip(config):
    ytsum.save_key("sk-ant-test", "wrkspc_123")
    assert ytsum.load_key() == ("sk-ant-test", "wrkspc_123")


def test_workspace_id_omitted_when_absent(config):
    ytsum.save_key("sk-ant-test", None)
    assert "workspace_id" not in json.loads(config.read_text())


def test_environment_variable_wins_over_stored_key(config, monkeypatch):
    ytsum.save_key("sk-ant-stored", None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
    assert ytsum.load_key()[0] == "sk-ant-from-env"


def test_corrupt_credentials_do_not_crash(config):
    config.parent.mkdir(parents=True)
    config.write_text("this is not json")
    assert ytsum.load_key() is None


def test_missing_credentials_return_none(config):
    assert ytsum.load_key() is None


# ------------------------------------------------------- key validation

def _status_error(cls, status, message):
    request = httpx2.Request("GET", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, request=request)
    return cls(message, response=response, body=None)


def test_invalid_key_is_reported_as_invalid(monkeypatch):
    def boom(*a, **k):
        raise _status_error(anthropic.AuthenticationError, 401, "nope")
    monkeypatch.setattr(ytsum, "make_client", lambda *a, **k: type(
        "C", (), {"messages": type("M", (), {"count_tokens": staticmethod(boom)})()})())
    assert ytsum.check_key("sk-ant-x", None) == (False, "invalid")


def test_workspace_scoping_error_is_recognised(monkeypatch):
    """The real failure: an org key that cannot call the Messages API at all."""
    def boom(*a, **k):
        raise _status_error(
            anthropic.PermissionDeniedError, 403,
            "This API key is not scoped to a workspace, so this request must include...",
        )
    monkeypatch.setattr(ytsum, "make_client", lambda *a, **k: type(
        "C", (), {"messages": type("M", (), {"count_tokens": staticmethod(boom)})()})())
    ok, problem = ytsum.check_key("sk-ant-x", None)
    assert ok is False and problem == ytsum.NEEDS_WORKSPACE


def test_offline_is_distinguished_from_a_bad_key(monkeypatch):
    def boom(*a, **k):
        raise anthropic.APIConnectionError(
            request=httpx2.Request("GET", "https://api.anthropic.com/v1/messages"))
    monkeypatch.setattr(ytsum, "make_client", lambda *a, **k: type(
        "C", (), {"messages": type("M", (), {"count_tokens": staticmethod(boom)})()})())
    assert ytsum.check_key("sk-ant-x", None) == (False, "offline")


def test_working_key_passes(monkeypatch):
    monkeypatch.setattr(ytsum, "make_client", lambda *a, **k: type(
        "C", (), {"messages": type("M", (), {"count_tokens": staticmethod(lambda *a, **k: None)})()})())
    assert ytsum.check_key("sk-ant-x", None) == (True, "")


# ------------------------------------------------------------ caption fetch

class _Response:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body.encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _rate_limited(times, then="WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhello\n"):
    calls = {"n": 0}

    class Opener:
        def open(self, request, timeout=None):
            calls["n"] += 1
            if calls["n"] <= times:
                raise ytsum.urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
            return _Response(then)

    return Opener(), calls


def test_caption_fetch_retries_then_succeeds(monkeypatch):
    opener, calls = _rate_limited(2)
    monkeypatch.setattr(ytsum.urllib.request, "build_opener", lambda *a: opener)
    monkeypatch.setattr(ytsum.time, "sleep", lambda s: None)
    assert "hello" in ytsum.http_get("http://example/captions")
    assert calls["n"] == 3


def test_caption_fetch_gives_up_with_useful_advice(monkeypatch):
    opener, _ = _rate_limited(99)
    monkeypatch.setattr(ytsum.urllib.request, "build_opener", lambda *a: opener)
    monkeypatch.setattr(ytsum.time, "sleep", lambda s: None)
    with pytest.raises(SystemExit) as exc:
        ytsum.http_get("http://example/captions")
    assert "cookies-from-browser" in str(exc.value)


def test_non_429_errors_are_not_retried(monkeypatch):
    calls = {"n": 0}

    class Opener:
        def open(self, request, timeout=None):
            calls["n"] += 1
            raise ytsum.urllib.error.HTTPError("u", 404, "Not Found", {}, None)

    monkeypatch.setattr(ytsum.urllib.request, "build_opener", lambda *a: Opener())
    with pytest.raises(SystemExit):
        ytsum.http_get("http://example/captions")
    assert calls["n"] == 1


# ------------------------------------------------------- style prompt

@pytest.mark.parametrize(
    "typed,expected",
    [("", "detailed"), ("1", "brief"), ("2", "detailed"), ("short", "brief"), ("detailed", "detailed")],
)
def test_style_prompt(monkeypatch, typed, expected):
    monkeypatch.setattr("builtins.input", lambda: typed)
    assert ytsum.ask_style() == expected


def test_style_prompt_reasks_after_nonsense(monkeypatch):
    answers = iter(["banana", "1"])
    monkeypatch.setattr("builtins.input", lambda: next(answers))
    assert ytsum.ask_style() == "brief"


# --------------------------------------------------------------- packaging

def test_declared_python_floor_covers_our_dependencies():
    """A floor lower than a dependency's is a broken install, not a warning.

    The first CI run caught exactly this: the code itself runs on 3.9, but
    anthropic 1.x requires 3.10, so `pip install` failed for 3.9 users.
    """
    import importlib.metadata as md
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    pyproject = root / "pyproject.toml"
    if not pyproject.exists():          # installed without the sdist layout
        pytest.skip("pyproject.toml not available")

    declared = tomllib.load(pyproject.open("rb"))["project"]["requires-python"]
    ours = tuple(int(p) for p in declared.lstrip(">=").split(".")[:2])

    for package in ("anthropic", "yt-dlp"):
        spec = md.metadata(package).get("Requires-Python", "")
        floors = [s for s in spec.split(",") if ">=" in s]
        if not floors:
            continue
        theirs = tuple(int(p) for p in floors[0].split(">=")[1].strip().split(".")[:2])
        assert ours >= theirs, (
            f"{package} needs Python >={'.'.join(map(str, theirs))} "
            f"but we declare {declared}"
        )
