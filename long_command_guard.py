#!/usr/bin/env python3
"""long-command-guard: a Claude Code hook that keeps secrets out of pasted one-line commands.

WHY THIS EXISTS

A model often ends a turn by offering the user a command to paste into a terminal. When
that command is one long line, the terminal wraps it. Copying a wrapped line can turn the
soft wrap into a hard line break, and the shell then runs the command in two halves.

In one real case, a one-line command piped credential files over ssh into
`sudo python3 <script path>` on a remote machine. The paste broke the line between
`python3` and the script path, so the remote shell ran bare `python3` with the credentials
on standard input, as source code. Python's SyntaxError printed an API token in full, into
the conversation and into the transcript on disk.

The command had been dry-run first, and the dry run passed. The break happens between the
rendered output and the user's paste, so no check on the command itself can catch it. The
moment something other than the model can refuse is the moment the message is finished,
which is what a `Stop` hook sees.

WHAT IT DOES

On `Stop` and `SubagentStop`, it reads the finished assistant message and finds the
commands offered to the user to run: fenced blocks with no language or a shell language,
and lines beginning with `!` (Claude Code's shell-escape prefix).

  * A command carrying a secret is REFUSED. The turn does not end, and the model is told
    to write the command to a script file and hand over the short `bash <path>` line.
  * A command that is merely long, over 100 characters on one logical line, is NOTED
    quietly, at most twice per session, because a long line is a risk and not a certainty.
    Set LONG_COMMAND_GUARD_QUIET=1 to switch the length note off (for unattended runs where
    nobody pastes anything). A secret is refused either way.

On `PreToolUse` for `Bash`, it refuses data piped into an interpreter on another host with
no script path named, which is the exact shape whose failure echoed the token.

HOW A STOP HOOK SPEAKS. Two ways, and this hook uses both (observed with Claude Code
2.1.273).

  * To REFUSE, it exits 2 with the reason on standard error. Claude Code turns that into a
    blocking error and feeds the text back to the model.
  * To NOTE something without refusing, it exits 0 and prints
    `hookSpecificOutput.additionalContext`, which Claude Code delivers to the model as
    non-error feedback. The model gets another turn to act on it.

Either way the model answers again, so neither is free.

LOOP SAFETY. The hook input carries `stop_hook_active`, which is true once a stop hook has
spoken this turn, after a block and after a note alike. This hook checks it first and
returns success while it is true, so it can never hold a session in a loop. Claude Code
2.1.273 also caps consecutive stop-hook blocks in one turn (eight), after which it ends
the turn regardless.

IT NEVER REPEATS WHAT IT FOUND. A refusal names the family of secret and its position and
nothing else. A guard that quotes what it found would reproduce the failure it was built
to stop. The tests check this.

IT FAILS OPEN. Any unexpected exception exits 0 saying nothing. A guard that runs at the
end of every turn and can wedge a session is worse than the risk it covers.

CONFIGURATION (environment variables, all optional)

  LONG_COMMAND_GUARD_CHARS      length above which one logical line is noted (default 100)
  LONG_COMMAND_GUARD_QUIET      set to 1 to switch the length note off; secrets still refused
  LONG_COMMAND_GUARD_STATE_DIR  where the per-session note count is kept
                                (default $XDG_CACHE_HOME/long-command-guard,
                                else ~/.cache/long-command-guard)

Try it by hand:

    printf '%s' '{"hook_event_name":"Stop","session_id":"demo","stop_hook_active":false,
      "last_assistant_message":"Run this:\\n\\n```\\ncat ~/.aws/credentials | ssh host tee /tmp/c\\n```"}' \\
      | python3 long_command_guard.py; echo "exit $?"

Standard library only. Python 3.8 or later.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
from pathlib import Path
from typing import List, NamedTuple, Optional

__version__ = "0.1.0"


# =========================================================================================
# Part 1. The shapes a command offered to the user must not have.
#
# Every function in this part is pure: it reads no files and takes no environment except
# the length threshold. No function here ever returns the text it matched.
# =========================================================================================

# --------------------------------------------------------------- what counts as a command

# A fenced block whose language is one of these is a shell command. An empty language is
# included: a model offering a command to paste usually writes no language at all.
SHELL_LANGUAGES = {
    "", "sh", "bash", "zsh", "shell", "shellsession", "sh-session",
    "console", "terminal", "command", "cmd",
}

_FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})(.*)$")

# A line the user is told to paste with the `!` prefix. `![` is a markdown image and never
# a command.
_BANG_LINE_RE = re.compile(r"^\s*!(?!\[)\s*(\S.*)$")


class Offer(NamedTuple):
    """One command offered to the user: its text, and where it came from in the message."""

    text: str
    kind: str  # "fenced block" or "! line"
    line: int  # 1-based line number in the message where the command starts


def offered_commands(message_text: str) -> List[Offer]:
    """The commands a finished assistant message offers the user to run.

    Two forms count, because those are the two forms that mean "paste this": a fenced
    block with no language or a shell language, and a line beginning with `!`. A command
    named in prose is being discussed, not handed over, so it is not read.
    """
    if not isinstance(message_text, str) or not message_text.strip():
        return []

    offers: List[Offer] = []
    fence: Optional[str] = None
    fence_is_shell = False
    body: List[str] = []
    body_start = 0

    for number, line in enumerate(message_text.splitlines(), start=1):
        match = _FENCE_RE.match(line)
        if fence is None:
            if match and match.group(2)[0] in "`~":
                fence = match.group(2)
                info = match.group(3).strip()
                language = info.split()[0].lower() if info else ""
                fence_is_shell = language in SHELL_LANGUAGES
                body = []
                body_start = number + 1
                continue
            bang = _BANG_LINE_RE.match(line)
            if bang:
                offers.append(Offer(bang.group(1).strip(), "! line", number))
            continue
        # Inside a fence: it closes on a fence of the same character, at least as long.
        if match and match.group(2)[0] == fence[0] and len(match.group(2)) >= len(fence):
            if fence_is_shell:
                text = "\n".join(body).strip("\n")
                if text.strip():
                    offers.append(Offer(text, "fenced block", body_start))
            fence = None
            fence_is_shell = False
            body = []
            continue
        body.append(line)

    if fence is not None and fence_is_shell:
        # An unclosed fence at the end of the message still offered its contents.
        text = "\n".join(body).strip("\n")
        if text.strip():
            offers.append(Offer(text, "fenced block", body_start))
    return offers


def logical_lines(command_text: str) -> List[str]:
    """The command's lines, with backslash continuations joined into one line each.

    A wrapped paste is a risk per logical line, so the length test is applied to these.
    """
    joined: List[str] = []
    current = ""
    for line in command_text.splitlines():
        stripped = line.lstrip() if current else line
        stripped = stripped.rstrip()
        if stripped.endswith("\\"):
            current += stripped[:-1].rstrip() + " "
            continue
        current += stripped
        joined.append(current)
        current = ""
    if current:
        joined.append(current)
    return [line for line in joined if line.strip()]


# -------------------------------------------------------------------------- the secret test

class Finding(NamedTuple):
    """What was recognised and where. Never what was matched."""

    family: str  # "credential path", "token literal" or "private key block"
    line: int  # 1-based logical line within the command
    start: int  # 1-based character position on that line
    length: int  # how many characters the match ran for

    def where(self) -> str:
        return (
            f"line {self.line}, characters {self.start} to {self.start + self.length - 1}"
        )


# A path segment naming credentials. Matched as a whole word inside a path-shaped token.
_CREDENTIAL_WORDS = (
    "credential", "credentials", "creds", "secret", "secrets", "token", "tokens",
    "apikey", "api_key", "api-key", "password", "passwd", "netrc", "pgpass",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "service_account",
    "client_secret", "keyfile", "keystore", "htpasswd", "keyring",
)

# A file extension that holds a key or a certificate bundle.
_CREDENTIAL_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".ppk", ".kdbx", ".gpg")

# A folder whose contents are credentials whatever the file is called.
_CREDENTIAL_FOLDERS = ("/.ssh/", "/.aws/", "/.gnupg/", "/.docker/config.json")

# An extension that says the file is text about something, or code that acts on it, rather
# than the secret itself. A note, a log, a script or a page named after a credential is not
# a credential, and refusing one would cost a turn for nothing. The shell extensions matter
# most: the remedy this guard asks for is `bash <path>`, and that script is quite likely to
# be called something like `fill_creds.sh`. Refusing the remedy would be absurd.
_DESCRIPTIVE_SUFFIXES = (".md", ".log", ".py", ".html", ".htm", ".rst",
                         ".sh", ".bash", ".zsh", ".fish")

_PATHISH_RE = re.compile(r"[^\s\"'`;|&<>()]*[/.][^\s\"'`;|&<>()]*")

# Token shapes with a well-known provider prefix. Each requires enough trailing characters
# that a word of prose cannot match it.
_TOKEN_RE = re.compile(
    r"""(?x)
    \b(?:
        sk-ant-[A-Za-z0-9_\-]{20,}
      | sk-[A-Za-z0-9]{24,}
      | gh[pousr]_[A-Za-z0-9]{20,}
      | github_pat_[A-Za-z0-9_]{20,}
      | glpat-[A-Za-z0-9_\-]{16,}
      | xox[baprs]-[A-Za-z0-9\-]{12,}
      | AKIA[0-9A-Z]{16}
      | ASIA[0-9A-Z]{16}
      | AIza[0-9A-Za-z_\-]{30,}
      | ya29\.[0-9A-Za-z_\-]{20,}
      | hf_[A-Za-z0-9]{20,}
      | dop_v1_[a-f0-9]{32,}
      | shpat_[a-f0-9]{32}
      | npm_[A-Za-z0-9]{30,}
      | SG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}
    )
    """
)

# The generic rule: a literal that has the shape of a token, whatever issued it. A detector
# keyed only to known prefixes would miss a token from a service nobody listed, and the
# token in the case that motivated this hook had no well-known prefix.
#
# A candidate is a run of token characters. It is split on `_` and `-`, and it counts when
# one of its segments is at least this long, mixes upper case, lower case and digits, and is
# not pure hexadecimal. Each clause keeps out something ordinary:
#
#   * splitting on separators keeps a name like `2024_Project_Archive_Notes` out, whose
#     whole string mixes case and digits but whose segments are ordinary words;
#   * pure hexadecimal keeps a commit SHA out, because `git checkout <sha>` is a real
#     command and refusing it would be a real cost;
#   * requiring upper AND lower AND a digit keeps a lower-case UUID out;
#   * a candidate touching `/` or `.` is part of a path or a dotted name, not a bare literal,
#     so a long file path is not a token (a credential path is caught by its own test);
#   * a `base64,` payload is data, not a key.
_GENERIC_MIN_SEGMENT = 24

_CANDIDATE_RE = re.compile(r"[A-Za-z0-9_\-]{%d,}" % _GENERIC_MIN_SEGMENT)

_HEX_RE = re.compile(r"(?i)^[0-9a-f]+$")


def _looks_like_a_token(segment: str) -> bool:
    if len(segment) < _GENERIC_MIN_SEGMENT:
        return False
    if _HEX_RE.match(segment):
        return False
    return (
        any(char.isupper() for char in segment)
        and any(char.islower() for char in segment)
        and any(char.isdigit() for char in segment)
    )


def _generic_token_span(line: str) -> Optional[tuple]:
    """The span of the first token-shaped literal in the line, or None."""
    for match in _CANDIDATE_RE.finditer(line):
        before = line[match.start() - 1] if match.start() else " "
        after = line[match.end()] if match.end() < len(line) else " "
        if before in "/." or after in "/.":
            continue  # a path segment or a dotted name
        if "base64," in line[max(0, match.start() - 8):match.start()]:
            continue  # an encoded payload, not a key
        for segment in re.split(r"[_\-]", match.group(0)):
            if _looks_like_a_token(segment):
                return match.start(), len(match.group(0))
    return None


_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")

# A value assigned to a name that says it is a secret. Narrow on purpose: the name must say
# secret and the value must look like one.
_ASSIGNED_SECRET_RE = re.compile(
    r"""(?xi)
    \b[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|APIKEY|API_KEY|ACCESS_KEY|PRIVATE_KEY|CREDENTIAL)[A-Z0-9_]*
    \s*[=:]\s*
    ['"]?(?P<value>[A-Za-z0-9+/=_.\-]{16,})['"]?
    """
)

_BEARER_RE = re.compile(r"(?i)\bBearer\s+(?P<value>[A-Za-z0-9+/=_.\-]{16,})")

# A value that is plainly a stand-in rather than a secret.
_PLACEHOLDER_RE = re.compile(
    r"(?i)^(?:x+|y+|\.+|-+|_+|changeme|placeholder|your[-_]?\w*|my[-_]?\w*|"
    r"token|secret|password|value|none|null|true|false|\d+)$"
)


def _is_placeholder(value: str) -> bool:
    if _PLACEHOLDER_RE.match(value):
        return True
    if "/" in value or value.startswith("$"):
        # A path or a shell variable. The secret is not in the command.
        return True
    if len(set(value)) <= 2:
        return True
    return False


def _credential_path_span(line: str) -> Optional[tuple]:
    """The span of the first path-shaped token in the line that holds credentials."""
    for match in _PATHISH_RE.finditer(line):
        token = match.group(0).strip("\"'`,")
        if not token or len(token) < 3:
            continue
        lowered = token.lower()
        if lowered.endswith(_DESCRIPTIVE_SUFFIXES):
            continue
        base = lowered.rsplit("/", 1)[-1]
        hit = False
        if base == ".env" or base.startswith(".env."):
            hit = True
        elif lowered.endswith(_CREDENTIAL_SUFFIXES):
            hit = True
        elif any(folder in lowered for folder in _CREDENTIAL_FOLDERS):
            hit = True
        else:
            for word in _CREDENTIAL_WORDS:
                for found in re.finditer(re.escape(word), lowered):
                    before = lowered[found.start() - 1] if found.start() else "/"
                    after = lowered[found.end()] if found.end() < len(lowered) else "/"
                    if not before.isalnum() and not after.isalnum():
                        hit = True
                        break
                if hit:
                    break
        if hit:
            return match.start(), len(match.group(0))
    return None


def secret_finding(command_text: str) -> Optional[Finding]:
    """The first secret the command carries, by family and position, or None.

    Two families, plus the private key block: a path that holds credentials, and a literal
    with the shape of a token or key. A token literal is recognised either by a provider
    prefix or by its shape alone. The return value never contains the match.
    """
    for number, line in enumerate(logical_lines(command_text), start=1):
        key = _PRIVATE_KEY_RE.search(line)
        if key:
            return Finding("private key block", number, key.start() + 1, len(key.group(0)))
        token = _TOKEN_RE.search(line)
        if token:
            return Finding("token literal", number, token.start() + 1, len(token.group(0)))
        for pattern in (_ASSIGNED_SECRET_RE, _BEARER_RE):
            for match in pattern.finditer(line):
                value = match.group("value")
                if _is_placeholder(value):
                    continue
                return Finding(
                    "token literal", number, match.start("value") + 1, len(value)
                )
        span = _credential_path_span(line)
        if span is not None:
            return Finding("credential path", number, span[0] + 1, span[1])
        span = _generic_token_span(line)
        if span is not None:
            return Finding("token literal", number, span[0] + 1, span[1])
    return None


# -------------------------------------------------------------------------- the length test

THRESHOLD_VAR = "LONG_COMMAND_GUARD_CHARS"
DEFAULT_THRESHOLD = 100


def threshold() -> int:
    """The length above which one logical line is noted.

    100 characters: above an ordinary one-line command, below the length at which a wrap
    is near certain. The command in the motivating case was about 150. The user's terminal
    width is not knowable from inside a hook, so any number is a guess; this one only ever
    notes, so a wrong guess costs one turn.
    """
    raw = os.environ.get(THRESHOLD_VAR)
    if raw:
        try:
            value = int(raw.strip())
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_THRESHOLD


def long_line(command_text: str, limit: Optional[int] = None) -> Optional[tuple]:
    """The first logical line longer than the limit, as (line number, length), or None."""
    limit = threshold() if limit is None else limit
    for number, line in enumerate(logical_lines(command_text), start=1):
        if len(line) > limit:
            return number, len(line)
    return None


# -------------------------------------------------------------- the remote-interpreter test

# Programs that run a command on another machine.
REMOTE_SHELLS = {"ssh", "slogin", "rsh", "autossh"}

# Interpreters that read standard input as source code, and print what they read when it
# does not parse. That echo is what put a token in the transcript.
STDIN_INTERPRETERS = {
    "python", "python2", "python3", "bash", "sh", "zsh", "dash", "ksh",
    "perl", "ruby", "node", "php",
}

# ssh options that take a value, so the value is not mistaken for the host.
_SSH_VALUE_FLAGS = {"-o", "-i", "-p", "-l", "-F", "-c", "-m", "-b", "-e", "-w",
                    "-L", "-R", "-D", "-W", "-J", "-Q", "-E", "-I", "-S"}

# Interpreter flags that carry the program inline, so nothing is read from stdin.
_INLINE_CODE_FLAGS = {"-c", "-m", "-e", "--command", "--eval"}


def _tokens(text: str) -> Optional[List[str]]:
    try:
        tokens = shlex.split(text)
    except ValueError:
        return None
    # A redirection is not part of the command's words. Cutting it here keeps
    # `ssh host python3 < creds.txt` from reading the file name as a script path.
    for position, token in enumerate(tokens):
        if token and token[0] in "<>" or token in ("1>", "2>", "&>"):
            return tokens[:position]
    return tokens


def _stages(line: str) -> List[str]:
    """The line split on top-level pipes, quotes respected. One stage when it cannot be
    parsed, so an unparseable line is still judged as a whole."""
    stages: List[str] = []
    current: List[str] = []
    quote: Optional[str] = None
    previous = ""
    for char in line:
        if quote:
            if char == quote and previous != "\\":
                quote = None
            current.append(char)
        elif char in "\"'":
            quote = char
            current.append(char)
        elif char == "|":
            stages.append("".join(current))
            current = []
        else:
            current.append(char)
        previous = char
    stages.append("".join(current))
    return [stage for stage in stages if stage.strip()]


def _remote_command(stage_tokens: List[str]) -> Optional[List[str]]:
    """The words run on the far side of a remote shell, or None when this is not one."""
    if not stage_tokens:
        return None
    index = 0
    program = os.path.basename(stage_tokens[0])
    if program in ("sudo", "env", "command", "nohup"):
        index = 1
        while index < len(stage_tokens) and (
            stage_tokens[index].startswith("-") or "=" in stage_tokens[index]
        ):
            index += 1
        if index >= len(stage_tokens):
            return None
        program = os.path.basename(stage_tokens[index])
    if program not in REMOTE_SHELLS:
        return None
    index += 1
    # Skip ssh's own options, then the host operand.
    while index < len(stage_tokens):
        token = stage_tokens[index]
        if token in _SSH_VALUE_FLAGS:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        break
    if index >= len(stage_tokens):
        return None
    index += 1  # the host
    remote = stage_tokens[index:]
    if len(remote) == 1:
        # `ssh host "sudo python3"` hands the whole remote command as one word.
        retokenized = _tokens(remote[0])
        if retokenized:
            remote = retokenized
    return remote


def _bare_interpreter(remote: List[str]) -> Optional[str]:
    """The interpreter named with no script path to run, or None."""
    index = 0
    while index < len(remote) and (
        os.path.basename(remote[index]) in ("sudo", "env", "command", "nohup", "time")
        or remote[index].startswith("-")
        or "=" in remote[index]
    ):
        index += 1
    if index >= len(remote):
        return None
    program = os.path.basename(remote[index])
    if program not in STDIN_INTERPRETERS:
        return None
    arguments = remote[index + 1:]
    position = 0
    while position < len(arguments):
        argument = arguments[position]
        if argument in _INLINE_CODE_FLAGS:
            # The program is in the command line; nothing is read from standard input.
            return None
        if argument.startswith("-") and argument != "-":
            position += 1
            continue
        if argument == "-":
            # Standard input named explicitly. Still no script path, so a split command
            # still reads the data as source code.
            position += 1
            continue
        return None  # a script path
    return program


def remote_interpreter_finding(command_text: str) -> Optional[dict]:
    """A pipe into an interpreter on another host with no script path, or None.

    Refused whatever is on the pipe. The failure is the interpreter printing what it read,
    which is bad for any input, and a test for "is there a secret on this pipe" is the one
    test that cannot be made reliable. Naming a script path makes a split command fail
    closed: the path either exists or the command does not run.
    """
    for number, line in enumerate(logical_lines(command_text), start=1):
        stages = _stages(line)
        fed_by_pipe = len(stages) > 1
        for position, stage in enumerate(stages):
            stage_tokens = _tokens(stage)
            if not stage_tokens:
                continue
            fed = fed_by_pipe and position > 0
            if not fed and re.search(r"<\s*[^\s<]", stage):
                fed = True  # input redirected from a file
            if not fed:
                continue
            remote = _remote_command(stage_tokens)
            if remote is None:
                continue
            program = _bare_interpreter(remote)
            if program:
                return {"line": number, "interpreter": program}
    return None


# =========================================================================================
# Part 2. The hook: per-session note budget, messages, and the event handlers.
# =========================================================================================

STATE_DIR_VAR = "LONG_COMMAND_GUARD_STATE_DIR"
QUIET_VAR = "LONG_COMMAND_GUARD_QUIET"
STATE_SUBDIR = "long-command-guard"

# How often one session may be noted for length alone. A long line is a risk and not a
# certainty, so the guard says it and then stops saying it.
FLAG_BUDGET = 2

# A marker older than this belongs to a session that has ended.
MARKER_LIFETIME_SECONDS = 14 * 24 * 3600


# ------------------------------------------------------------------------- the state folder

def state_dir() -> Path:
    override = os.environ.get(STATE_DIR_VAR)
    if override:
        return Path(override)
    cache = os.environ.get("XDG_CACHE_HOME")
    base = Path(cache) if cache else Path(os.path.expanduser("~")) / ".cache"
    return base / STATE_SUBDIR


def session_key(session_id: str) -> str:
    """A safe file name for a session id."""
    safe = "".join(char for char in session_id if char.isalnum() or char in "-_")
    return (safe or "session") + ".json"


def flags_used(directory: Path, session_id: str) -> int:
    path = directory / session_key(session_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return int(data.get("flags", 0))
    except Exception:  # noqa: BLE001 - an unreadable marker means no flags recorded
        return 0


def record_flag(directory: Path, session_id: str, used: int) -> None:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        sweep(directory)
        path = directory / session_key(session_id)
        path.write_text(
            json.dumps({"flags": used + 1, "updated": int(time.time())}),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001 - a marker that cannot be written costs a repeat
        pass


def sweep(directory: Path, now: Optional[float] = None) -> int:
    """Delete markers for sessions that have long ended. Returns how many were removed."""
    now = time.time() if now is None else now
    removed = 0
    try:
        for entry in directory.glob("*.json"):
            try:
                if now - entry.stat().st_mtime > MARKER_LIFETIME_SECONDS:
                    entry.unlink()
                    removed += 1
            except OSError:
                continue
    except Exception:  # noqa: BLE001
        return removed
    return removed


def is_quiet() -> bool:
    """True when the user has switched the length note off."""
    return os.environ.get(QUIET_VAR, "").strip().lower() in ("1", "true", "yes", "on")


# ------------------------------------------------------------------------------ the messages

SCRIPT_FILE_INSTRUCTION = (
    "Write the command to a script file in the project or a scratch folder, make it "
    "executable, and hand the user only the short line that runs it, such as "
    "`bash <absolute path>`. Then finish the message again."
)


def secret_refusal(finding: Finding, kind: str) -> str:
    """What the model is told when a command offered to the user carries a secret.

    The finding's value never appears here.
    """
    return (
        f"Do not hand the user this command. The {kind} you just offered carries a "
        f"secret: a {finding.family} at {finding.where()} of the command. Copying a "
        f"one-line command out of a terminal can turn a wrap into a real line break, "
        f"which splits the command and runs half of it. That has sent credentials into a "
        f"bare remote `python3`, which printed an API token into the transcript. "
        + SCRIPT_FILE_INSTRUCTION
    )


def length_note(line_number: int, length: int, limit: int, last: bool) -> str:
    """What the model is told about a long line. This is advice, not a refusal: the
    message stands, and the model decides whether to hand the line over anyway."""
    tail = (
        " This is the second and last time this session will be told about length."
        if last else ""
    )
    return (
        f"The command you just offered the user is {length} characters on one line "
        f"(line {line_number}), over the {limit}-character guide. A line that long wraps "
        f"in a terminal, and copying a wrapped line can turn the wrap into a real line "
        f"break, which splits the command and runs half of it. Consider writing it to a "
        f"script file and handing over `bash <absolute path>` instead. Nothing is "
        f"blocked: if the line has to go as it is, say so and move on." + tail
    )


REMOTE_REFUSAL = (
    "This command pipes data into `{interpreter}` on another host with no script path "
    "given (line {line}). If the command is split, that interpreter reads the data as "
    "source code and its error message prints it; that is how an API token once reached "
    "a transcript. Name the script path on the remote side, "
    "so `... | ssh host sudo python3 /path/to/script.py`. Then a split command fails "
    "closed, because the path either exists or the command does not run."
)


# ------------------------------------------------------------------------------- the paths

def _text_of(content) -> str:
    """The text parts of a transcript message's content."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def last_assistant_message(payload: dict) -> str:
    """The finished message's text: from the input when the hook payload carries it, from
    the transcript file otherwise."""
    given = payload.get("last_assistant_message")
    if isinstance(given, str) and given.strip():
        return given
    path = payload.get("transcript_path")
    if not isinstance(path, str) or not path:
        return ""
    try:
        text = ""
        with open(os.path.expanduser(path), encoding="utf-8") as handle:
            for raw in handle:
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or entry.get("type") != "assistant":
                    continue
                message = entry.get("message")
                if not isinstance(message, dict):
                    continue
                joined = _text_of(message.get("content"))
                if joined.strip():
                    text = joined
        return text
    except Exception:  # noqa: BLE001 - no readable transcript means nothing to judge
        return ""


def stop_decision(payload: dict, directory: Path) -> Optional[tuple]:
    """What to do about the finished message: ("block", reason), ("note", text), or None."""
    if payload.get("stop_hook_active"):
        # A stop hook has already spoken this turn. Yield, so the guard can never hold a
        # session in a loop.
        return None

    message = last_assistant_message(payload)
    offers = offered_commands(message)
    if not offers:
        return None

    # Two passes, and the order matters. Every command is checked for a secret before any
    # of them is judged on length, so a message carrying both is refused for the secret and
    # never merely noted for being long. A model told only about length would shorten the
    # command and send the same secret again.
    for offer in offers:
        finding = secret_finding(offer.text)
        if finding is not None:
            return "block", secret_refusal(finding, offer.kind)

    if is_quiet():
        return None

    limit = threshold()
    longest = None
    for offer in offers:
        over = long_line(offer.text, limit)
        if over is not None:
            longest = over
            break

    if longest is None:
        return None

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        # No session to remember. Say it once for this turn rather than never.
        return "note", length_note(longest[0], longest[1], limit, last=False)

    used = flags_used(directory, session_id.strip())
    if used >= FLAG_BUDGET:
        return None
    record_flag(directory, session_id.strip(), used)
    return "note", length_note(
        longest[0], longest[1], limit, last=used + 1 >= FLAG_BUDGET)


def pre_tool_decision(payload: dict) -> Optional[str]:
    """The reason to deny this Bash call, or None to allow it."""
    if payload.get("tool_name") != "Bash":
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    finding = remote_interpreter_finding(command)
    if finding is None:
        return None
    return REMOTE_REFUSAL.format(interpreter=finding["interpreter"], line=finding["line"])


def deny_output(reason: str) -> str:
    return json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    })


def note_output(event: str, text: str) -> str:
    """A line delivered to the model while the turn is allowed to end."""
    return json.dumps({
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": text,
        }
    })


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
    except Exception:  # noqa: BLE001 - an input we cannot read is not a turn we judge
        return 0

    try:
        event = payload.get("hook_event_name")
        if event in ("Stop", "SubagentStop"):
            decision = stop_decision(payload, state_dir())
            if decision is None:
                return 0
            verdict, text = decision
            if verdict == "block":
                # Exit 2 with the reason on standard error: a blocking error whose text
                # is fed back to the model.
                sys.stderr.write(text + "\n")
                return 2
            # A note: the turn ends, and the line reaches the model as non-error feedback.
            print(note_output(event, text))
            return 0
        if event == "PreToolUse":
            reason = pre_tool_decision(payload)
            if reason:
                print(deny_output(reason))
            return 0
        return 0
    except Exception:  # noqa: BLE001 - fail open: say nothing, let the turn end
        return 0


if __name__ == "__main__":
    sys.exit(main())
