# long-command-guard

A small [Claude Code](https://docs.claude.com/en/docs/claude-code) hook that stops the model
from handing you a one-line command that carries a secret, and refuses one shell pattern
that turns a broken command into a leaked credential.

Standard-library Python, one file, no dependencies.

## The problem

At the end of a turn, a model often offers you a command to paste into your terminal. When
that command is one long line, the terminal wraps it. Copying a wrapped line out of some
terminals turns the soft wrap into a hard line break, and the shell then runs the command
in two halves.

Usually that just fails. In one real case it did worse. The command piped credential files
over `ssh` into `sudo python3 <script path>` on another machine. The paste broke the line
between `python3` and the script path, so the remote shell ran bare `python3` with the
credentials on standard input. Python read them as source code, failed to parse them, and
its error message printed an API token in full into the conversation and the transcript
on disk.

The command had been dry-run first, and the dry run passed. The break happens between the
rendered message and your paste, so no check on the command itself can catch it. The last
point where software can intervene is when the model finishes its message, which is what a
`Stop` hook sees.

## What the hook does

**On `Stop` and `SubagentStop`** it reads the finished assistant message and finds the
commands offered to you: fenced code blocks with no language or a shell language
(`bash`, `sh`, `zsh`, `console` and similar), and lines starting with `!`.

- **A command carrying a secret is refused.** The hook exits 2, so the turn does not end.
  The model is told to write the command to a script file and hand you a short
  `bash <path>` line instead. A secret is a path that holds credentials (`~/.ssh/...`,
  `~/.aws/...`, `.env`, `*.pem`, a path segment such as `credentials` or `id_ed25519`), a
  token with a well-known prefix, a value assigned to a name like `API_KEY=` or sent as
  `Bearer ...`, a private key block, or any literal with the shape of a random token.
- **A long command is noted, not refused.** A command over 100 characters on one logical
  line gets a quiet note to the model suggesting a script file. This happens at most twice
  per session.

**On `PreToolUse` for `Bash`** it denies a command the model wants to run itself when data
is piped (or redirected) into an interpreter on another host with no script path, such as
`... | ssh host sudo python3`. Naming the script (`... | ssh host sudo python3 /path/to/script.py`)
makes a split command fail closed: the path either exists or nothing runs.

Two rules hold everywhere:

- **It never repeats what it found.** A refusal names the kind of secret and its position
  (line and character range), never the value or the path.
- **It fails open.** Malformed input or any unexpected error ends the turn silently with
  exit 0. A guard that runs after every turn must not be able to wedge a session.

It cannot loop: when Claude Code reports `stop_hook_active: true` (a stop hook has already
spoken this turn), the hook returns success immediately.

## Install

1. Copy `long_command_guard.py` somewhere stable, for example
   `~/.claude/hooks/long_command_guard.py`.
2. Add the hook to `~/.claude/settings.json` (all projects) or a project's
   `.claude/settings.json`. Use the absolute path to the file:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          { "type": "command", "command": "python3 /absolute/path/to/long_command_guard.py" }
        ]
      }
    ],
    "SubagentStop": [
      {
        "hooks": [
          { "type": "command", "command": "python3 /absolute/path/to/long_command_guard.py" }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          { "type": "command", "command": "python3 /absolute/path/to/long_command_guard.py" }
        ]
      }
    ]
  }
}
```

If your settings file already has a `hooks` block, merge these entries into it.

3. Try it by hand:

~~~
printf '%s' '{"hook_event_name":"Stop","session_id":"demo","last_assistant_message":"Run:\n```\ncat ~/.aws/credentials | ssh host tee /tmp/c\n```"}' | python3 long_command_guard.py; echo "exit $?"
~~~

You should see a refusal on standard error and `exit 2`.

## Configuration

All optional, read from the environment the hook runs in.

| Variable | Effect | Default |
|---|---|---|
| `LONG_COMMAND_GUARD_CHARS` | Length above which one logical line is noted | `100` |
| `LONG_COMMAND_GUARD_QUIET` | `1` switches the length note off (useful for unattended runs where nobody pastes). Secrets are still refused. | off |
| `LONG_COMMAND_GUARD_STATE_DIR` | Where the per-session note count is kept | `$XDG_CACHE_HOME/long-command-guard`, else `~/.cache/long-command-guard` |

The state folder holds one small JSON file per session that was noted. Files older than
14 days are deleted the next time the hook writes one.

## How it talks to Claude Code

Tested with Claude Code 2.1.x (behaviour observed with 2.1.273). Hook behaviour can change
between releases; check the current hooks documentation if something does not fire.

- A refusal on `Stop`/`SubagentStop` is exit code 2 with the reason on standard error.
  Claude Code feeds that text back to the model, which answers again.
- A note is exit code 0 with `hookSpecificOutput.additionalContext` on standard output.
  Claude Code delivers it to the model as non-error feedback, and the model gets another
  turn to act on it.
- A `PreToolUse` denial is exit code 0 with `hookSpecificOutput.permissionDecision: "deny"`
  and a reason.
- The hook uses `last_assistant_message` from the hook input when present, and otherwise
  reads the last assistant text from the transcript file named in `transcript_path`.
- With 2.1.273, Claude Code caps consecutive stop-hook blocks in one turn at eight and then
  ends the turn anyway.

## Limits

- **It sees only what the model offers in a fenced shell block or a `!` line.** A command
  written in running prose, or in a fence labelled with another language, is not read.
- **Detection is pattern-based.** It can miss a secret with no recognisable shape, and it
  can refuse something harmless that looks like a token or a credential path. Script files
  (`.sh`, `.py` and similar) and notes (`.md`, `.log`) named after credentials are not
  treated as credentials. Commit SHAs and lower-case UUIDs are not treated as tokens.
- **The length threshold is a guess.** The hook cannot know your terminal width. It only
  notes, so a wrong guess costs one extra turn.
- **The `PreToolUse` check covers one shape:** a remote shell (`ssh`, `slogin`, `rsh`,
  `autossh`) fed by a pipe or a redirect, running an interpreter (`python`, `bash`, `sh`,
  `perl`, `ruby`, `node`, `php` and others) with no script path and no inline code flag. It
  does not try to judge whether the piped data is secret.
- **It does not stop you pasting.** It changes what the model offers; it does not inspect
  your clipboard or your terminal.
- **It is not a secret scanner for your repository.** It looks only at the commands in a
  finished message and at the model's own `Bash` calls.

## Running the tests

From this folder:

```
python3 -m unittest -v
```

The tests run the hook as a separate process with JSON on standard input, the way Claude
Code runs it, and redirect the state folder into a temporary directory.

## License

MIT. See [LICENSE](LICENSE).
