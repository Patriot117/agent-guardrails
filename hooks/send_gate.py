#!/usr/bin/env python3
"""PreToolUse hook: the agent cannot send email I haven't approved.

Claude Code runs this before every matching tool call and pipes the call in as
JSON on stdin. Printing a "deny" decision blocks the call; printing nothing lets
it continue through the normal permission flow.

The rules:
  1. Protect the approval file and the gate's own files (the hooks/ and checks/
     folders, the sender, everything in .claude/, Python startup files like .pth
     and sitecustomize). The agent may read them, never write them.
  2. A send is allowed only if EVERY recipient has an approval line.
  3. A shell command that names the sender or any of those files or folders must be one of
     these plain shapes, or it's denied:
       - a read:  cat / head / tail / grep / wc / diff / ls / stat / file,
                  optionally piped into more of those
       - a send:  python3 examples/send_email.py --to a@b.com --subject ... --body-file ...
       - git status / diff / log / show / add / commit (so committing the gate
         isn't a false alarm), minus options that run programs
     No $, backticks, redirects, subshells, braces, ;, &, backslashes, carriage
     returns or newlines in that command, and nothing the gate can't parse.
     This is an allowlist on purpose. An earlier version tried to understand
     shell syntax and a reviewer got sends past it a dozen ways. Shell is too
     big to out-think, so the gate refuses what it can't fully read.
  4. Any command with a word the shell would expand into other names (a glob,
     zsh's (a|b) and <1-9>, a brace like {a,b}, $'...' quoting) or a letter outside ASCII must be a
     plain read or a safe git command, whatever it names. Those can't send or
     write, so it doesn't matter what the expansion turns into. That replaces
     guessing what a pattern could spell, which a reviewer beat four ways.
  5. If the gate's own logic hits an error, it denies.

Honest limits (see README): the gate recognizes commands that spell out the
sender's or the approval file's name. A command that assembles a name at
runtime (a variable) isn't recognized, nor is a write that never spells the
name out (copying a whole folder over it, `git checkout .`), and neither is a new
sending script the agent writes itself. If this script can't start at all
(python3 missing, a syntax error), Claude Code treats that as a hook error and
the call goes ahead. That's why this is one layer of three.
"""
from __future__ import annotations

import base64
import email
import email.utils
import json
import os
import re
import shlex
import sys
import unicodedata
from pathlib import Path

SENDER = "send_email.py"
GATE = "send_gate.py"
CHECKS = "outbound_checks.py"
GATE_DIR = Path(__file__).resolve().parent              # hooks/: a planted json.py here would run inside the gate
CHECKS_DIR = GATE_DIR.parent / "checks"
SETTINGS = ("settings.json", "settings.local.json")  # where Claude Code wires the hook
# Files Python runs at startup even under -I: a planted one can crash the gate before it decides.
STARTUP = re.compile(r"\.pth(?![\w.-])|sitecustomize|usercustomize|site-packages|dist-packages")
# The gate's folders, by name: a planted checks/__init__.py runs inside the sender's import.
# Matched at the end of any word, not just a whole one, so an option glued to the folder
# (`tar -Chooks`, `cp -tchecks`) counts. That also catches `gh pr checks`; the README lists it.
GUARDED_DIR = re.compile(r"(?:hooks|checks)(?!\w)|\.claude(?![\w.-])")
SENDER_FLAGS = {"--to", "--subject", "--body-file", "--attach", "--dry-run"}
FLAGS_WITH_VALUE = {"--to", "--subject", "--body-file", "--attach"}
SEND_VERBS = {"send", "reply", "forward"}
FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
READ_ONLY_CMDS = {"cat", "head", "tail", "grep", "wc", "diff", "ls", "stat", "file"}
# git subcommands that can name a guarded file without running it or changing its contents.
GIT_SAFE = {"status", "diff", "log", "show", "add", "commit"}
# Options that make those subcommands run a program or write a file. `git grep -O`
# runs any command as its "pager", which is why grep isn't on the list at all.
GIT_UNSAFE_OPTS = ("-O", "--open-files-in-pager", "--output", "--ext-diff", "--exec")
PYTHON = re.compile(r"^python3?(\.\d+)?$")  # matched on the basename, so /usr/bin/python3 counts
# Anything that can run a second command, substitute, or redirect. Banned outright
# in a command that names the sender or the approval file, even inside quotes.
SHELL_META = set("$`<>(){};&\n\r\\")


def approvals_path() -> Path:
    override = os.environ.get("GUARDRAILS_APPROVALS")
    if override:
        return Path(override)
    root = os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parent.parent
    return Path(root) / "approvals.txt"


def approved_recipients(path: Path) -> set[str]:
    """Each line `APPROVED-SEND to=<address>` approves sends to that address.

    A human writes these lines and removes them. Missing file = nobody approved.
    """
    if not path.exists():
        return set()
    approved = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line.startswith("APPROVED-SEND"):
            continue
        match = re.search(r"\bto=(\S+)", line)
        if match:
            approved.add(match.group(1).lower())
    return approved


def tokenize(command: str) -> list[str]:
    """Split into words the way bash would for a plain command.

    No comment handling: bash only treats # as a comment at the start of a word,
    and shlex's default would hide `x#,evil@x` from us. A literal # is safer.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars="|")
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def norm(text: str) -> str:
    """Fold case and lookalike letters the way a case-insensitive disk would.

    `ſend_email.py` (long s) and full-width letters open the real file on a
    default macOS volume, so they have to count as the real name here too.
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    # Drop accents too (İ folds to i plus a dot), so a near-miss reads as the name.
    return "".join(c for c in unicodedata.normalize("NFD", folded) if not unicodedata.combining(c))


def expands(command: str) -> bool:
    """Does any unquoted word hold something the shell turns into other names?

    Globs (* ? [..]), braces with a , or .. inside, and $'...' / $"..." quoting,
    plus what zsh (the Mac default) and bash's extglob add: ( ) groups like
    send_emai(l).py, <1-9> number ranges, and with zsh's extendedglob ^, a ~
    inside a word and x#. On one plain line with no $ outside single quotes,
    quoted ones don't count (awk '{print $1, $2}' is fine). `$'` and `$"` count anywhere, even inside
    quotes. Letters outside ASCII count anywhere: a case-insensitive disk opens
    `ſend_email.py` as the real file, quoted or not.
    """
    if not command.isascii():
        return True
    if re.search(r"\$['\"]", command):
        return True
    # Quote tracking is only exact on one line with no comment, substitution or heredoc. A
    # quote in a `# it's` comment or a heredoc body means nothing to the shell, and quotes
    # inside "$(...)", `...` or ${...} start over, so any of those could make the tracker think
    # a glob is quoted when it isn't. Rounds 10 to 14 each found a new case, so rather than copy
    # the shell's rules, a command with any of them counts every glob or brace, quoted or not.
    # The same goes for text handed to another shell (eval, bash -c), which expands it again.
    # Shell names are looked for with quotes and backslashes taken out, so 'bash' and \eval count.
    if AMBIGUOUS.search(command) or HANDOFF.search(re.sub(r"[\"'\\]", "", command)):
        return bool(GLOB_ANYWHERE.search(command))
    return _expands(command)


AMBIGUOUS = re.compile(r"[#`\n\r]|\$[({]|<<")
HANDOFF = re.compile(
    r"(^|[\s;&|(/=])(eval|bash|sh|zsh|dash|ksh|csh|tcsh|fish|watch|ssh|su|script|flock|parallel)(\s|$)")
# The $( and $(( of a substitution aren't groups, and a ~ at the start of a word is the home folder.
# A # right after a letter is zsh's x#, even with quotes between them (send_ema'i'#l.py), but
# "# Notes" isn't.
GLOB_ANYWHERE = re.compile(
    r"[*?^]|\[.*\]|\{.*(,|\.\.).*\}|(?<![$(])\(|<\d*-\d*>|[^\s=:]~|[\w.)\]}][\"']*#", re.DOTALL)


def _expands(command: str) -> bool:
    words, word, quote, i, live_dollar = [], [], None, 0, False
    while i < len(command):
        c = command[i]
        if quote:
            if c == quote:
                quote = None
            elif c == "\\" and quote == '"':
                i += 1
            elif c == "$" and quote == '"':
                live_dollar = True
        elif c in "'\"":
            quote = c
        elif c == "\\":
            i += 1  # the next character is literal
        elif c.isspace():
            words.append("".join(word))
            word = []
        else:
            live_dollar = live_dollar or c == "$"
            word.append(c)
        i += 1
    words.append("".join(word))
    # A $ the shell expands could hold anything, a shell's name included (`$SHELL -c '...'`),
    # so with one, quoted globs count too.
    if live_dollar:
        return bool(GLOB_ANYWHERE.search(command))
    for w in words:
        if "*" in w or "?" in w:
            return True
        if re.search(r"\[.*\]", w) or re.search(r"\{.*(,|\.\.).*\}", w):
            return True
        if re.search(r"[(^]|<\d*-\d*>|[^=:]~", w):
            return True
    return False


def names(command: str, tokens: list[str], name: str) -> bool:
    """Does the command spell out `name` as a whole file name, in any case?

    A whole name, so `tests/test_send_gate.py` isn't `send_gate.py`.
    """
    pattern = re.compile(r"(?<![\w.-])" + re.escape(norm(name)) + r"(?![\w.-])")
    return any(pattern.search(norm(text)) for text in [command, *tokens])


def names_settings(command: str, tokens: list[str], trust_cwd: bool) -> bool:
    # Only Claude Code's settings files: under .claude/, or anywhere once a cd
    # makes the folder unknowable.
    return any(names(command, tokens, f".claude/{n}") or (not trust_cwd and names(command, tokens, n))
               for n in SETTINGS)


def is_plain_read(tokens: list[str]) -> bool:
    return bool(tokens) and tokens[0] in READ_ONLY_CMDS


def is_safe_git(tokens: list[str]) -> bool:
    if tokens[1:2] == ["--no-pager"]:
        tokens = tokens[:1] + tokens[2:]
    if len(tokens) < 2 or tokens[0] != "git" or tokens[1] not in GIT_SAFE:
        return False
    return not any(t.startswith(GIT_UNSAFE_OPTS) for t in tokens[2:])


def plain_send_args(tokens: list[str]) -> list[str] | None:
    """Return the sender's arguments if this is the one send shape we accept."""
    if tokens and os.path.basename(tokens[0]).lower() == SENDER:
        return tokens[1:]
    if len(tokens) > 1 and PYTHON.match(os.path.basename(tokens[0])) and os.path.basename(tokens[1]).lower() == SENDER:
        return tokens[2:]  # no interpreter flags allowed before the script
    return None


def recipients_from_args(args: list[str]) -> tuple[list[str], bool] | str:
    """Return (recipients, dry_run), or a deny reason if any argument is outside the plain shape.

    dry_run only counts where --dry-run sits as a flag, never as another flag's value.
    """
    found, dry_run, i = [], False, 0
    while i < len(args):
        token = args[i]
        flag, _, inline = token.partition("=")
        if flag not in SENDER_FLAGS:
            # Includes abbreviations like --t: the gate only reads exact flags.
            return f"argument {token!r} isn't one the gate can read; failing closed"
        if flag == "--dry-run":
            if inline:
                return "--dry-run takes no value; failing closed"
            dry_run = True
        elif flag in FLAGS_WITH_VALUE:
            if inline:
                value = inline
            elif i + 1 < len(args):
                i += 1
                value = args[i]
            else:
                return f"{flag} has no value; failing closed"
            if flag == "--to":
                found += [v.strip().lower() for v in value.split(",") if v.strip()]
        i += 1
    return found, dry_run


def check_recipients(recipients: list[str], approved: set[str]) -> str | None:
    """Return a deny reason, or None if every recipient is approved."""
    if not recipients:
        return "send with no recipients I can read; failing closed"
    missing = [r for r in recipients if r not in approved]
    if missing:
        return f"no APPROVED-SEND line for: {', '.join(missing)}"
    return None


def is_mcp_send(tool: str) -> bool:
    """Any MCP tool with send/reply/forward in its action name, from any server.

    Broad on purpose: `messages_send`, `draft_and_send`, `sendEmail` all count.
    A non-email tool that trips this (a chat app's send_message) is denied too;
    that's the safe direction to be wrong in.
    """
    parts = tool.split("__")
    if len(parts) < 3 or parts[0] != "mcp":
        return False
    words = re.split(r"[_\-\s]+|(?<=[a-z])(?=[A-Z])", parts[-1])
    return any(w.lower().startswith(v) for w in words for v in SEND_VERBS)


RECIPIENT_WORDS = {"to", "cc", "bcc", "recipient", "recipients", "address", "addresses",
                   "email", "emails", "attendee", "attendees", "invitee", "invitees"}
NOT_RECIPIENT_WORDS = {"from", "sender"}
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")


def key_words(key: str) -> set[str]:
    return {w.lower() for w in re.split(r"[_\-\s.]+|(?<=[a-z])(?=[A-Z])", str(key)) if w}


def mime_recipients(value: str) -> list[str] | None:
    """Recipients in a raw MIME message (Gmail's `raw` is base64url). None = can't read it."""
    text = value
    try:
        text = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8", "replace")
    except (ValueError, TypeError):
        pass
    msg = email.message_from_string(text)
    headers = [v for h in ("to", "cc", "bcc", "resent-to", "resent-cc", "resent-bcc")
               for v in msg.get_all(h, [])]
    if not headers:
        return None
    return [addr.lower() for _, addr in email.utils.getaddresses(headers) if addr]


def recipients_from_mcp(tool_input: dict) -> list[str] | str:
    """Every address the call names as a recipient, or a deny reason.

    Servers name these differently (`to`, `toRecipients`, `bcc_addresses`, nested
    `{"emailAddress": {"address": ...}}`, a raw MIME blob), so collect anything that
    looks like an address under any recipient-ish key. Recipients the server fills
    in itself, from a saved draft or reply-all, aren't in the call at all, so those
    calls are refused.
    """
    found, problems = [], []

    def walk(value, under_recipient):
        if isinstance(value, dict):
            for k, v in value.items():
                words = key_words(k)
                if "draft" in words and v:
                    problems.append("sends a saved draft; the gate can't see who it's addressed to")
                if {"reply", "all"} <= words | key_words(str(k).replace("all", "_all")) and v and \
                        str(v).strip().lower() not in {"false", "0", "no", "off", "none"}:
                    problems.append("reply-all adds recipients the gate can't see")
                if words & {"raw", "mime"} and isinstance(v, str):
                    parsed = mime_recipients(v)
                    if parsed is None:
                        problems.append("raw message the gate can't read recipients from")
                    else:
                        found.extend(parsed)
                    continue
                if words & NOT_RECIPIENT_WORDS:
                    continue  # the sender's own address isn't a recipient
                walk(v, under_recipient or bool(words & RECIPIENT_WORDS))
        elif isinstance(value, list):
            for v in value:
                walk(v, under_recipient)
        elif under_recipient and value is not None:
            found.extend(m.group(0).lower() for m in EMAIL.finditer(str(value)))
        elif isinstance(value, str) and EMAIL.fullmatch(value.strip()):
            # A bare address under a key I don't know (participants, members): count it.
            found.append(value.strip().lower())

    walk(tool_input, False)
    if problems:
        return f"{problems[0]}; failing closed"
    return found


MCP_READ_VERBS = ("read", "get", "list", "search", "find", "stat", "view", "query")
MCP_WRITE_WORDS = ("write", "replace", "update", "edit", "set", "create", "put", "move", "copy",
                   "delete", "remove", "rename", "append", "insert", "save", "upload", "patch",
                   "modify", "apply", "run", "exec", "overwrite", "truncate", "extract", "unzip",
                   "unlink", "symlink", "purge", "wipe", "chmod", "chown", "mkdir", "touch",
                   "restore", "install", "clear", "import", "sync", "drop", "merge", "reset", "store",
                   "trash", "add")


def mcp_touches_guarded(tool: str, tool_input: dict, guarded: list[str]) -> bool:
    """An MCP tool (a filesystem server, say) that names a guarded file and isn't a pure read.

    Pure read = starts with a read verb and has no write word anywhere, so
    `search_and_replace` and `write_query` don't count as reads.
    """
    blob = norm(json.dumps(tool_input, ensure_ascii=False))
    if not (any(norm(g) in blob for g in guarded) or GUARDED_DIR.search(blob)):
        return False
    words = [w.lower() for w in re.split(r"[_\-\s]+|(?<=[a-z])(?=[A-Z])", tool.split("__")[-1]) if w]
    pure_read = bool(words) and words[0].startswith(MCP_READ_VERBS) and not any(
        w.startswith(MCP_WRITE_WORDS) for w in words)
    return not pure_read


def decide_bash(command: str, approvals: Path, cwd: str) -> str | None:
    # The shell deletes backslash-newline in most places, so `che\<newline>cks` runs as `checks`,
    # but not after a `#` or inside single quotes. Rather than copy those rules, names are looked
    # for in several spellings, joined and not. Backslash and newline are in SHELL_META, checked on
    # the raw text below, so a command that names anything and has a continuation is refused.
    guarded = [approvals.name, GATE, CHECKS]
    # A third spelling drops every parameter ($X, ${...}, $@, $1...), which may well be empty, so
    # `send_em${X}ail.py` and `send_em$X''ail.py` still read as the sender. A name is a letter or
    # `_` then word characters; a positional is one digit, so `send_em$1ail.py` keeps its `ail`.
    joined = command.replace("\\\n", "")
    params = r"\$(\{[^}]*\}|[A-Za-z_]\w*|[@*#?$!0-9-])"
    variants = list(dict.fromkeys([command, joined, re.sub(params, "", joined)]))
    try:
        token_sets = [tokenize(v) for v in variants]
        parsed = True
    except ValueError:
        # An unmatched quote (`# it's`) or a lone trailing backslash. Run every check below on a
        # rough split with quotes and backslashes dropped, so `send_em''ail.py` still reads as
        # the sender, and refuse if any of them fires.
        variants = [re.sub(r"[\"'\\]", "", v) for v in variants]
        token_sets = [v.split() for v in variants]
        parsed = False
    tokens = token_sets[0]
    spellings = list(zip(variants, token_sets))
    texts = [*variants, *(t for ts in token_sets for t in ts)]

    trust_cwd = not any(re.search(r"\b(cd|pushd)\b", v) for v in variants)
    expanding = any(expands(v) for v in [command, *variants])
    names_sender = any(norm(SENDER[:-3]) in norm(t) for t in texts)
    # Folders count as well as names: `cp evil.json .claude/` or `> checks/__init__.py` writes
    # inside them without naming a protected file. Matched anywhere in a word, so `-t.claude`,
    # `-Chooks`, `--target-directory=checks` and `.claude/.` count too. A shell already sitting inside one
    # of those folders makes every bare filename suspect.
    here = Path(cwd).resolve()
    in_guarded_dir = ".claude" in {norm(p) for p in here.parts} or any(
        d == here or d in here.parents for d in (GATE_DIR, CHECKS_DIR))
    names_guarded = (in_guarded_dir
                     or any(names(v, ts, g) for v, ts in spellings for g in guarded)
                     or any(names_settings(v, ts, trust_cwd) for v, ts in spellings)
                     or any(STARTUP.search(norm(t)) or GUARDED_DIR.search(norm(t)) for t in texts))
    if not (names_sender or names_guarded or expanding):
        return None
    if not parsed:
        return "couldn't parse a command that touches the send path or the gate's files; failing closed"

    if SHELL_META & set(command):
        if not (names_sender or names_guarded):
            return ("a command with a glob, {a,b} brace or $'...' in it can't also contain "
                    "$ ` < > ( ) { } ; & \\ or newlines; spell the file names out")
        return ("commands that name send_email.py, approvals.txt or the gate's own files can't "
                "contain $ ` < > ( ) { } ; & \\ or newlines; run it as one plain command")

    segments, current = [], []
    for token in tokens:
        if token == "|":
            segments.append(current)
            current = []
        elif set(token) == {"|"}:
            return "only a single | between plain reads is allowed here"  # || is an or-chain
        else:
            current.append(token)
    segments.append(current)

    if all(is_plain_read(seg) for seg in segments):
        return None  # reading the sender or the approvals is fine
    if len(segments) == 1 and is_safe_git(segments[0]):
        return None  # committing or diffing a file doesn't run or change it
    if expanding:
        return ("a command with a glob, {a,b} brace, $'...' quoting or a non-ASCII letter can "
                "only be a plain read or git; spell the file names out")
    if names_guarded:
        return ("the agent can't change approvals.txt or the gate's own files "
                "(hooks/, checks/, .claude/, Python startup files); a human does that")
    if len(segments) > 1:
        return "a send can't be part of a pipe; run send_email.py as one plain command"

    args = plain_send_args(segments[0])
    if args is None:
        return ("send_email.py has to run as plain `python3 examples/send_email.py --to ...`; "
                "anything else that names it is refused")
    parsed = recipients_from_args(args)
    if isinstance(parsed, str):
        return parsed
    recipients, dry_run = parsed
    if dry_run:
        return None  # rehearsal never transmits, keep it frictionless
    return check_recipients(recipients, approved_recipients(approvals))


def is_guarded_path(target: str, approvals: Path, cwd: str) -> bool:
    path = Path(cwd, target).resolve()
    if norm(str(path)) == norm(str(approvals.resolve())):
        return True
    if norm(path.name) in {norm(approvals.name), GATE, SENDER, CHECKS}:
        return True
    folders = {norm(str(GATE_DIR)), norm(str(CHECKS_DIR))}
    if any(norm(str(parent)) in folders for parent in path.parents):
        return True
    if STARTUP.search(norm(str(path))):
        return True
    return any(norm(parent.name) == ".claude" for parent in path.parents)


def decide(event: dict) -> str | None:
    """Return a deny reason, or None to let the call through."""
    tool = event.get("tool_name", "")
    tool_input = event.get("tool_input") or {}
    approvals = approvals_path()

    if tool in FILE_TOOLS:
        target = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if target and is_guarded_path(target, approvals, event.get("cwd") or os.getcwd()):
            return ("the agent can't edit approvals.txt or the gate's own files "
                    "(hooks/, checks/, .claude/, send_email.py, Python startup files); a human does that")
        return None

    if tool.startswith("mcp__"):
        if mcp_touches_guarded(tool, tool_input, [approvals.name, GATE, SENDER, CHECKS, *SETTINGS, ".pth",
                                                       "sitecustomize", "usercustomize", "site-packages",
                                                       "dist-packages"]):
            return "the agent can't change approvals.txt or the gate's own files; a human does that"
        if is_mcp_send(tool):
            action = key_words(tool.split("__")[-1])
            if "draft" in action:
                return "sends a saved draft; the gate can't see who it's addressed to; failing closed"
            if not action & {"reply", "forward"} and any("id" in key_words(k) for k in tool_input):
                return "send refers to a saved message by id; the gate can't see its recipients; failing closed"
            recipients = recipients_from_mcp(tool_input)
            if isinstance(recipients, str):
                return recipients
            return check_recipients(recipients, approved_recipients(approvals))

    command = tool_input.get("command")
    if isinstance(command, str):
        # Bash, and any other tool that runs a shell command (Monitor, a shell MCP).
        cwd = event.get("cwd") or os.getcwd()
        return decide_bash(command, approvals, cwd)
    return None


def deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"send_gate: {reason}",
        }
    }))


def main() -> int:
    try:
        reason = decide(json.load(sys.stdin))
    except Exception as exc:  # noqa: BLE001 - fail closed, on purpose
        reason = f"gate error ({type(exc).__name__}); failing closed"
    if reason:
        deny(reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
