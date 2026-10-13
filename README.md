# claude-session-handoff

Move a Claude Code session to another machine, another project path, or another account, and resume it there with full history.

```
claude-handoff list                                    # find the session you want
claude-handoff pack 157ce8e3                           # bundle it into one file
claude-handoff import 157ce8e3.claude-session.tar.gz \
    --to-cwd ~/projects/AgentWebUI-Workspace           # install it on the other side
claude --resume 157ce8e3-a522-43f7-8720-b7fe0a917b0f
```

One Python file, standard library only, Python 3.8+. Works on Linux, macOS, Windows and WSL, in every direction:

| From \ To | Linux | macOS | Windows |
| --- | --- | --- | --- |
| **Linux** | ✓ | ✓ | ✓ |
| **macOS** | ✓ | ✓ | ✓ |
| **Windows** | ✓ | ✓ | ✓ |

Each of the nine directions has its own test, and CI runs the suite on real Linux, macOS and Windows machines.

## Why this exists

Claude Code saves each session as a JSONL transcript:

```
<config-dir>/projects/<encoded-project-path>/<session-id>.jsonl
<config-dir>/projects/<encoded-project-path>/<session-id>/        (subagents, large tool output)
```

Copying that file somewhere else usually fails quietly:

- `claude --resume` only looks in the folder named after the directory you run it from. `C:\Users\me\codes\app` becomes `C--Users-me-codes-app`, and `/home/me/app` becomes `-home-me-app`. Every character that isn't a letter or digit turns into `-`.
- The transcript is full of absolute paths from the old machine: in `cwd` fields, in tool calls, in shell commands, in file-history snapshots.
- On Windows those paths are JSON-escaped (`C:\\Users\\...`) and also show up in Git Bash form (`/c/Users/...`), so a plain `sed` silently misses most of them.

`claude-handoff` puts the session in the right folder and rewrites the paths properly, by parsing every line as JSON instead of running text substitution over it.

## Install

```bash
curl -fsSL -o ~/.local/bin/claude-handoff \
  https://raw.githubusercontent.com/TahrimWalid/claude-session-handoff/main/claude_handoff.py
chmod +x ~/.local/bin/claude-handoff
```

On Windows, download `claude_handoff.py` and run it with `py claude_handoff.py ...`.

## Usage

### Find a session

The easiest way: run `/status` inside the session you want to move. It shows the session ID. Then:

```
claude-handoff where 157ce8e3
```

This prints the transcript path, its sidecar folder, and ready-to-run `scp` commands. Any unique prefix of the ID works.

Without the tool, search for the ID directly:

```powershell
# Windows (PowerShell)
Get-ChildItem $HOME\.claude\projects -Recurse -Depth 1 -Filter "157ce8e3*"
```

```bash
# Linux / macOS
find ~/.claude/projects -maxdepth 2 -name '157ce8e3*'
```

Copy both results: the `.jsonl` and the folder with the same name.

To browse instead:

```
claude-handoff list [--cwd PATH] [--config-dir DIR] [-n 20] [--json]
```

This shows each session's id, last change, size, project path, and its title or first prompt.

### Windows laptop → Linux server

On Windows, with Python installed:

```powershell
py claude_handoff.py pack 157ce8e3
scp 157ce8e3.claude-session.tar.gz admin@server:~
```

Without Python on Windows, copy the `.jsonl` and the folder with the same name instead:

```powershell
cd $HOME\.claude\projects\C--Users-me-codes-AgentWebUI-Workspace
scp 157ce8e3-....jsonl admin@server:~/drop/
scp -r 157ce8e3-... admin@server:~/drop/
```

Then on the server, after cloning the repo:

```bash
claude-handoff import ~/157ce8e3.claude-session.tar.gz --to-cwd ~/projects/AgentWebUI-Workspace
# or:  claude-handoff import ~/drop/157ce8e3-....jsonl --to-cwd ~/projects/AgentWebUI-Workspace
```

### One account → another on the same machine

If you run several accounts with separate `CLAUDE_CONFIG_DIR` folders:

```bash
claude-handoff import 157ce8e3 --from-config-dir ~/.claude --config-dir ~/.claude2
CLAUDE_CONFIG_DIR=~/.claude2 claude --resume 157ce8e3-...
```

Add `--new-id` to make an independent fork, so both accounts can keep their own copy.

### Moved a project folder

```bash
claude-handoff import 157ce8e3 --to-cwd ~/new/location/app
```

### Import options

| Option | What it does |
| --- | --- |
| `--to-cwd PATH` | Project path where the session should resume (default: where it ran, if that exists here) |
| `--from-cwd PATH` | Override the source project path read from the transcript |
| `--map OLD=NEW` | Rewrite another path prefix too, e.g. `--map 'C:\Users\me=/home/me'` (repeatable) |
| `--config-dir DIR` | Account to import into (default: `$CLAUDE_CONFIG_DIR` or `~/.claude`) |
| `--from-config-dir DIR` | Account to look a session id up in |
| `--new-id` | Give the copy a fresh session id |
| `--force` | Replace an existing copy (the old one is kept as `.bak-<time>`) |
| `--dry-run` | Show what would change, write nothing |
| `--keep-mtime` | Keep the original modification time |
| `--no-history` | Don't copy `/rewind` checkpoints |
| `--project-dir DIR` | Write into this exact folder (for project paths over 200 characters) |

## What it handles

- **Every path form of the source root, per OS:**
  - **Windows:** `C:\x\app`, `C:/x/app`, any letter case, `/c/x/app` (Git Bash), `/mnt/c/x/app` (WSL), `/cygdrive/c/x/app`, and UNC paths (`\\nas\share\app`).
  - **macOS:** any letter case (macOS disks are case-insensitive by default), `/var` vs `/private/var`, and decomposed Unicode in names like `café`.
  - **Linux:** exact and case-sensitive, so `/home/me/App` is not `/home/me/app`.
- **The right separators on the target OS.**
  - **Linux and macOS:** always `/`.
  - **Windows:** a value that is just a path (like `cwd` or a `file_path`) gets backslashes. A path inside a shell command gets `D:/x/app`, because Claude Code runs commands through Git Bash, which would mangle backslashes.
- **Only real matches.** `...\app-old\x.py` is left alone when the root is `...\app`, and `~/home/a/p` is never mistaken for `/home/a/p`. When roots are nested, the longest one wins.
- **Signed thinking blocks are left byte-for-byte.** Assistant thinking blocks carry a signature, so they aren't touched, and the tool tells you how many still mention the old path.
- **Paths used as JSON keys** are rewritten too, as in file-history snapshots used by `/rewind`.
- **Subagent transcripts** in the session's folder are rewritten. Other files are copied as they are.
- **`/rewind` checkpoints** (`file-history/<id>`) come along. With `--new-id`, every `sessionId` field is renamed, including in subagents.
- **Messy input.** A UTF-8 BOM, CRLF line endings and broken lines are handled; lines that aren't valid JSON are copied verbatim and counted.
- **Safe writes.** Everything is staged next to the destination and verified (same line count, nothing newly unparseable) before atomic renames. The transcript lands last, so a half-imported session never shows up. Existing files are backed up, never overwritten silently. New files are created with mode `0600`.
- **Same-file protection.** It refuses to "import" a session onto itself, including when two accounts share `projects/` through a symlink.
- **The 30-day cleanup.** Claude Code deletes transcripts idle longer than `cleanupPeriodDays` (30 by default). Imports get a fresh modification time, and with `--keep-mtime` you get a warning if the file would be deleted on next start.
- **Sessions still in use.** If a transcript was written in the last two minutes, you're warned to exit that session first.
- **Leftovers.** Paths under the old home folder but outside the project (for example `C:\Users\me\.claude\...` or `/Users/me/.zshrc`) are listed so you can add `--map` for them.
- **Files in use.** On Windows, if Claude Code has the destination open, the import stops with a clear message. Nothing is left half-written.
- **Very long project paths.** Over 200 characters, Claude Code adds a hash to the folder name that differs between builds. Rather than guess it, the tool reuses the folder Claude Code made, or tells you to run `claude` there once first.
- **Unsafe bundles** (absolute paths, `..`, symlinks or devices) are rejected before anything is extracted.

## Limitations

- **Claude desktop's sidebar may not list imported sessions.** Resume them with `claude --resume <id>` in a terminal.
- **Paths with spaces after the project root** are rewritten only up to the first space. The root itself can contain spaces.
- **Old shell commands stay old.** History from Windows still shows PowerShell commands. Tell Claude the project moved when you resume.
- **Credentials, MCP OAuth tokens and folder trust don't transfer.** Log in on the new machine, and accept the trust prompt the first time.
- **No live sync.** Each import is a copy. Don't run the same session in two places at once.
- **Undocumented format.** The transcript format belongs to Claude Code and isn't documented. This tool changes as little as it can, but a future version could change the layout.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The suite has 45 tests: all nine OS directions, the OS-specific path forms, the safety checks, and record shapes taken from real transcripts. CI runs them on Linux, macOS and Windows.

Beyond the unit tests, the tool was checked end to end against Claude Code 2.1.296 using a real 1.8 MB, 337-line transcript:

- `pack`, then `import` into a different project folder and a different `CLAUDE_CONFIG_DIR`. Real `claude --resume` loaded it and answered a question about early history correctly.
- Handoff to another account with `--new-id`. The fork resumed with full history.
- Linux → Windows paths → Linux. All records were identical apart from the folder path, and the result resumed with full history.
- A made-up session ID was rejected with "No conversation found", which confirms the resumes above were real.

## License

MIT
