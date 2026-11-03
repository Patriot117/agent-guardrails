"""Every rule in the gate gets a test that shows it blocking, not just passing."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "hooks" / "send_gate.py"
sys.path.insert(0, str(ROOT / "hooks"))
import send_gate  # noqa: E402


@pytest.fixture
def approvals(tmp_path, monkeypatch):
    path = tmp_path / "approvals.txt"
    path.write_text(
        "# written by a human, one line per approved send\n"
        'APPROVED-SEND to=buyer@example.gov subject="Quote 1042"\n'
    )
    monkeypatch.setenv("GUARDRAILS_APPROVALS", str(path))
    return path


def bash(cmd):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


def run_hook(event, env_path):
    """Run the hook the way Claude Code does: JSON in on stdin, decision out."""
    proc = subprocess.run(
        [sys.executable, str(GATE)],
        input=event if isinstance(event, str) else json.dumps(event),
        capture_output=True, text=True,
        env={"GUARDRAILS_APPROVALS": str(env_path), "PATH": "/usr/bin:/bin"},
    )
    return json.loads(proc.stdout)["hookSpecificOutput"] if proc.stdout.strip() else None


# --- sends -------------------------------------------------------------------

def test_approved_send_passes(approvals):
    assert send_gate.decide(bash("python3 examples/send_email.py --to buyer@example.gov --subject Q --body-file b.txt")) is None


def test_unapproved_send_blocked(approvals):
    reason = send_gate.decide(bash("python3 examples/send_email.py --to someone@else.com --subject Q --body-file b.txt"))
    assert "someone@else.com" in reason


def test_one_unapproved_recipient_blocks_the_whole_send(approvals):
    reason = send_gate.decide(bash("python3 examples/send_email.py --to buyer@example.gov,cc@else.com --subject Q --body-file b.txt"))
    assert "cc@else.com" in reason


def test_hidden_cc_blocked(approvals):
    assert send_gate.decide(bash("python3 examples/send_email.py --to buyer@example.gov --bcc=leak@else.com --subject Q --body-file b.txt"))


def test_send_without_recipient_fails_closed(approvals):
    assert "no recipients" in send_gate.decide(bash("python3 examples/send_email.py --subject Q --body-file b.txt"))


def test_send_chained_after_harmless_command_blocked(approvals):
    assert send_gate.decide(bash("ls && python3 examples/send_email.py --to x@else.com --subject Q --body-file b.txt"))


def test_env_prefix_does_not_hide_send(approvals):
    assert send_gate.decide(bash("DEBUG=1 python3 examples/send_email.py --to x@else.com --subject Q --body-file b.txt"))


def test_shell_wrapper_fails_closed(approvals):
    assert send_gate.decide(bash("bash -c 'python3 examples/send_email.py --to buyer@example.gov'"))


def test_dry_run_allowed(approvals):
    assert send_gate.decide(bash("python3 examples/send_email.py --to anyone@else.com --subject Q --body-file b.txt --dry-run")) is None


def test_mention_is_not_a_send(approvals):
    # Don't cry wolf: reading or searching the sender is not sending.
    assert send_gate.decide(bash("grep -n smtp examples/send_email.py")) is None
    assert send_gate.decide(bash("cat examples/send_email.py | wc -l")) is None


def test_mcp_send_unapproved_blocked(approvals):
    event = {"tool_name": "mcp__gmail__send_message", "tool_input": {"to": "x@else.com", "subject": "hi"}}
    assert send_gate.decide(event)


def test_mcp_send_approved_passes(approvals):
    event = {"tool_name": "mcp__gmail__send_message", "tool_input": {"to": ["buyer@example.gov"]}}
    assert send_gate.decide(event) is None


def test_mcp_read_is_not_a_send(approvals):
    assert send_gate.decide({"tool_name": "mcp__gmail__search_threads", "tool_input": {"query": "x"}}) is None


def test_missing_approval_file_blocks_everything(tmp_path, monkeypatch):
    monkeypatch.setenv("GUARDRAILS_APPROVALS", str(tmp_path / "nope.txt"))
    assert send_gate.decide(bash("python3 examples/send_email.py --to buyer@example.gov"))


def test_commented_out_approval_does_not_count(approvals):
    approvals.write_text("# APPROVED-SEND to=buyer@example.gov\n")
    assert send_gate.decide(bash("python3 examples/send_email.py --to buyer@example.gov"))


# --- the approval file ---------------------------------------------------------

def test_agent_cannot_write_approvals_with_file_tools(approvals):
    for tool in ("Write", "Edit", "MultiEdit"):
        assert send_gate.decide({"tool_name": tool, "tool_input": {"file_path": str(approvals)}})


def test_agent_cannot_write_approvals_from_shell(approvals):
    assert send_gate.decide(bash("echo 'APPROVED-SEND to=x@else.com' >> approvals.txt"))
    assert send_gate.decide(bash("sed -i '' 's/a/b/' approvals.txt"))
    assert send_gate.decide(bash("cp mine.txt approvals.txt"))


def test_agent_can_read_approvals(approvals):
    assert send_gate.decide(bash("cat approvals.txt")) is None
    assert send_gate.decide(bash("grep buyer approvals.txt")) is None


def test_other_file_edits_pass(approvals, tmp_path):
    assert send_gate.decide({"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "draft.md")}}) is None


# --- fail closed, end to end ---------------------------------------------------------

def test_hook_denies_unapproved_send_end_to_end(approvals):
    out = run_hook(bash("python3 examples/send_email.py --to x@else.com --subject Q --body-file b.txt"), approvals)
    assert out["permissionDecision"] == "deny"


def test_hook_silent_on_approved_send_end_to_end(approvals):
    assert run_hook(bash("python3 examples/send_email.py --to buyer@example.gov --subject Q --body-file b.txt"), approvals) is None


def test_garbage_input_fails_closed(approvals):
    out = run_hook("{not json", approvals)
    assert out["permissionDecision"] == "deny"
    assert "failing closed" in out["permissionDecisionReason"]


def test_unparseable_send_command_fails_closed(approvals):
    assert send_gate.decide(bash("python3 examples/send_email.py --to 'x@else.com")) is not None


def test_git_restore_of_approvals_blocked(approvals):
    assert send_gate.decide(bash("git checkout HEAD~3 -- approvals.txt"))


# --- bypasses found in review (each was allowed before the fix) ----------------

SEND = "examples/send_email.py --to x@else.com --subject Q --body-file b.txt"


@pytest.mark.parametrize("cmd", [
    f"cd examples\npython3 {SEND}",                 # newline is a command separator
    f"timeout 5 python3 {SEND}",                    # prefix wrappers
    f"nohup python3 {SEND}",
    f"time python3 {SEND}",
    f"(python3 {SEND})",                            # subshell
    f"{{ python3 {SEND}; }}",
    f"if true; then python3 {SEND}; fi",
    f"echo $(python3 {SEND})",
    f"uv run {SEND}",
    f"python3 -W ignore {SEND}",                    # interpreter flags
    "python3 -m examples.send_email --to x@else.com",
])
def test_unusual_send_shapes_denied(approvals, cmd):
    assert send_gate.decide(bash(cmd)), cmd


def test_wrappers_denied_even_for_approved_recipient(approvals):
    # The gate refuses shapes it can't fully read, whoever the recipient is.
    assert send_gate.decide(bash("timeout 5 python3 examples/send_email.py --to buyer@example.gov"))


def test_abbreviated_flag_denied(approvals):
    assert send_gate.decide(bash("python3 examples/send_email.py --to buyer@example.gov --t x@else.com"))


def test_inline_to_flag_read(approvals):
    assert send_gate.decide(bash("python3 examples/send_email.py --to=x@else.com --subject Q"))
    assert send_gate.decide(bash("python3 examples/send_email.py --to=buyer@example.gov --subject Q")) is None


def test_sender_rejects_abbreviations():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "send_email.py"), "--to", "buyer@example.gov",
         "--subject", "Q", "--body-file", __file__, "--t", "x@else.com", "--dry-run"],
        capture_output=True, text=True,
    )
    assert "unrecognized arguments: --t" in proc.stderr, proc.stderr


@pytest.mark.parametrize("tool", [
    "mcp__claude_ai_Gmail__send_message",
    "mcp__claude_ai_Gmail__reply",
    "mcp__claude_ai_Gmail__forward",
    "mcp__outlook__send_mail",
])
def test_real_world_mcp_send_names_gated(approvals, tool):
    assert send_gate.decide({"tool_name": tool, "tool_input": {"to": "x@else.com"}}), tool


def test_mcp_drafts_and_reads_pass(approvals):
    for tool in ("mcp__claude_ai_Gmail__create_draft", "mcp__claude_ai_Gmail__search_threads"):
        assert send_gate.decide({"tool_name": tool, "tool_input": {}}) is None


def test_approvals_protection_ignores_case(approvals):
    upper = str(approvals.parent / "APPROVALS.TXT")
    assert send_gate.decide({"tool_name": "Write", "tool_input": {"file_path": upper}})
    assert send_gate.decide(bash("echo x >> APPROVALS.TXT"))


def test_approvals_glob_write_denied(approvals):
    assert send_gate.decide(bash("cp mine.txt approval?.txt"))


def test_globs_pass_in_reads_and_git(approvals):
    for cmd in ("ls tests/*", "grep -n deny hooks/*.py", "cat approval?.txt", "git add tests/*.py",
                "python3 -m pytest tests/", "awk '{print $1, $2}' notes.txt"):
        assert send_gate.decide(bash(cmd)) is None, cmd


def test_globs_outside_reads_are_the_accepted_cost(approvals):
    assert send_gate.decide(bash("python3 -m pytest tests/*"))


def test_more_reads_of_approvals_pass(approvals):
    for cmd in ("grep -i buyer approvals.txt", "ls -l approvals.txt", "stat approvals.txt"):
        assert send_gate.decide(bash(cmd)) is None, cmd


# --- second review round: each of these got past the first fix ------------------

EVIL = "--to evil@attacker.test --subject s --body-file body.txt"


@pytest.mark.parametrize("cmd", [
    f"ls;(python3 examples/send_email.py {EVIL})",               # ; and a subshell
    f"ls |& python3 examples/send_email.py {EVIL}",
    f"echo issue#12; python3 examples/send_email.py {EVIL}",     # # isn't a comment mid-word
    f"cat examples/send_email.py | python3 - {EVIL}",            # script piped into python
    f"ls examples/send_email.py | xargs -I{{}} python3 {{}} {EVIL}",
    f"python3 examples/send_e*.py {EVIL}",                       # glob for the sender
    f'python3 examples/send""_email.py {EVIL}',                  # quote-split name
    f"python3 examples/send\\_email.py {EVIL}",                  # backslash-split name
    f"cat $(echo examples/send_email.py) {EVIL}",                # substitution inside a read
    f"cat `echo examples/send_email.py`",
    f"cat <(python3 examples/send_email.py {EVIL})",
])
def test_second_round_send_bypasses_denied(approvals, cmd):
    assert send_gate.decide(bash(cmd)), cmd


def test_hash_glued_to_approved_address_is_not_approved(approvals):
    cmd = "python3 examples/send_email.py --to buyer@example.gov#,evil@attacker.test --subject s --body-file b.txt"
    assert send_gate.decide(bash(cmd))


@pytest.mark.parametrize("cmd", [
    "cat evil.txt >& approvals.txt",
    "cat evil.txt 1<>approvals.txt",
    'cp mine.txt appro""vals.txt',
    "cp mine.txt appro{vals,}.txt",                              # brace expansion
])
def test_second_round_approval_writes_denied(approvals, cmd):
    assert send_gate.decide(bash(cmd)), cmd


@pytest.mark.parametrize("tool", [
    "mcp__gmail__messages_send",
    "mcp__gmail__draft_and_send",
    "mcp__sendgrid__send",
    "mcp__composio__GMAIL_SEND_EMAIL",
    "mcp__mail__sendEmail",
])
def test_send_verb_anywhere_in_mcp_name_gated(approvals, tool):
    assert send_gate.decide({"tool_name": tool, "tool_input": {"to": "x@else.com"}}), tool


def test_committing_the_sender_passes(approvals):
    # Don't cry wolf: git work that names the sender doesn't run it.
    for cmd in ("git add examples/send_email.py",
                'git commit -m "tighten send_email.py flags"',
                "git diff examples/send_email.py"):
        assert send_gate.decide(bash(cmd)) is None, cmd


def test_redirect_on_a_send_is_refused(approvals):
    # Documented cost of the allowlist: even an approved send can't carry 2>&1.
    assert send_gate.decide(bash("python3 examples/send_email.py --to buyer@example.gov --subject Q --body-file b.txt 2>&1"))


def test_dry_run_only_counts_as_a_flag(approvals):
    # --dry-run sitting where --subject's value goes must not unlock a real send.
    assert send_gate.decide(bash("python3 examples/send_email.py --to evil@attacker.test --subject --dry-run --body-file b.txt"))
    assert send_gate.decide(bash("python3 examples/send_email.py --to evil@attacker.test --dry-run=0"))


# --- third review round ----------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    f"git grep -O'python3 examples/send_email.py {EVIL}' smtp",   # -O runs a "pager"
    f"git grep --open-files-in-pager=sh smtp examples/send_email.py",
    "git diff --output=examples/send_email.py",
    "git diff --ext-diff examples/send_email.py",
    "git -c core.pager=sh log examples/send_email.py",
    "git blame examples/send_email.py",                            # not on the list
])
def test_git_that_can_run_or_write_is_refused(approvals, cmd):
    assert send_gate.decide(bash(cmd)), cmd


@pytest.mark.parametrize("cmd", [
    "git status examples/send_email.py",
    "git log examples/send_email.py",
    "git show HEAD:examples/send_email.py",
])
def test_plain_git_on_the_sender_passes(approvals, cmd):
    assert send_gate.decide(bash(cmd)) is None, cmd


def test_or_chain_refused(approvals):
    assert send_gate.decide(bash(f"cat x || python3 examples/send_email.py {EVIL}"))


def test_sender_by_path_is_checked(approvals):
    assert send_gate.decide(bash(f"./examples/send_email.py {EVIL}"))
    assert send_gate.decide(bash("./examples/send_email.py --to buyer@example.gov --subject Q --body-file b.txt")) is None


@pytest.mark.parametrize("tool_input", [
    {"recipients": ["evil@attacker.test"]},
    {"toRecipients": [{"emailAddress": {"address": "evil@attacker.test"}}]},
    {"to": "buyer@example.gov", "Bcc": "evil@attacker.test"},
])
def test_mcp_recipients_found_under_any_key_shape(approvals, tool_input):
    assert send_gate.decide({"tool_name": "mcp__outlook__send_mail", "tool_input": tool_input}), tool_input


def test_mcp_write_to_approvals_refused(approvals):
    write = {"tool_name": "mcp__filesystem__write_file",
             "tool_input": {"path": str(approvals), "content": "APPROVED-SEND to=evil@attacker.test"}}
    assert send_gate.decide(write)
    read = {"tool_name": "mcp__filesystem__read_file", "tool_input": {"path": str(approvals)}}
    assert send_gate.decide(read) is None


# --- fourth round: names the shell expands differently than the gate read them --

SENDER_SPELLINGS = [
    "send_emai{l..l}.py",        # brace sequence
    "send_emai{l,x}.py",          # brace list
    "send_emai[^x].py",           # bash negation
    "send_emai[!x].py",
    "ſend_email.py",         # long s, opens the real file on a case-insensitive disk
    "SEND_EMAIL.PY",
    "examples/se*_email.py",
]


@pytest.mark.parametrize("name", SENDER_SPELLINGS)
def test_sender_spellings_are_gated(approvals, name):
    assert send_gate.decide(bash(f"python3 {name} {EVIL}"))


@pytest.mark.parametrize("name", ["approvals.tx{t..t}", "approval[^x].txt", "approvalſ.txt", "APPROVALS.TXT"])
def test_approval_spellings_cant_be_written(approvals, name):
    assert send_gate.decide(bash(f"cp evil.txt {name}"))


@pytest.mark.parametrize("cmd", [
    "rm hooks/send_gate.py",
    "cp /dev/null .claude/settings.json",
    "mv x .claude/settings.local.json",
    "chmod 000 hooks/send_ga*.py",
])
def test_gate_files_cant_be_changed_from_shell(approvals, cmd):
    assert send_gate.decide(bash(cmd))


@pytest.mark.parametrize("path", ["hooks/send_gate.py", "examples/send_email.py",
                                  ".claude/settings.json", ".claude/settings.local.json"])
def test_gate_files_cant_be_changed_with_file_tools(approvals, path):
    assert send_gate.decide({"tool_name": "Write", "tool_input": {"file_path": path, "content": ""}})


def test_reading_and_committing_gate_files_still_passes(approvals):
    for cmd in ["cat hooks/send_gate.py", "git diff hooks/send_gate.py", "git add hooks/send_gate.py",
                "grep deny hooks/send_gate.py"]:
        assert send_gate.decide(bash(cmd)) is None, cmd


def test_other_settings_files_pass(approvals):
    assert send_gate.decide({"tool_name": "Write", "tool_input": {"file_path": "app/settings.json", "content": ""}}) is None


def test_any_tool_with_a_command_is_gated(approvals):
    event = {"tool_name": "Monitor", "tool_input": {"command": f"python3 examples/send_email.py {EVIL}"}}
    assert send_gate.decide(event)


def test_mcp_draft_send_and_reply_all_refused(approvals):
    assert send_gate.decide({"tool_name": "mcp__mail__send_draft", "tool_input": {"draft_id": "r123"}})
    assert send_gate.decide({"tool_name": "mcp__mail__reply", "tool_input": {
        "to": ["buyer@example.gov"], "reply_all": True, "body": "hi"}})


@pytest.mark.parametrize("payload", [
    {"to": ["buyer@example.gov"], "bcc_addresses": ["leak@evil.test"]},
    {"to": ["buyer@example.gov"], "BccAddresses": ["leak@evil.test"]},
    {"recipient_email": "leak@evil.test"},
    {"message": {"toRecipients": [{"emailAddress": {"address": "leak@evil.test"}}]}},
])
def test_mcp_recipient_key_shapes(approvals, payload):
    assert "leak@evil.test" in send_gate.decide({"tool_name": "mcp__mail__send_message", "tool_input": payload})


def test_mcp_raw_mime_recipients_checked(approvals):
    import base64
    raw = base64.urlsafe_b64encode(b"To: buyer@example.gov\r\nBcc: leak@evil.test\r\nSubject: Q\r\n\r\nhi").decode()
    assert "leak@evil.test" in send_gate.decide({"tool_name": "mcp__gmail__send_message", "tool_input": {"raw": raw}})
    ok = base64.urlsafe_b64encode(b"To: buyer@example.gov\r\nSubject: Q\r\n\r\nhi").decode()
    assert send_gate.decide({"tool_name": "mcp__gmail__send_message", "tool_input": {"raw": ok}}) is None
    assert send_gate.decide({"tool_name": "mcp__gmail__send_message", "tool_input": {"raw": "bm90IG1pbWU"}})


def test_from_address_is_not_a_recipient(approvals):
    assert send_gate.decide({"tool_name": "mcp__mail__send_message", "tool_input": {
        "to": ["buyer@example.gov"], "from_email": "me@mainstay.test"}}) is None


@pytest.mark.parametrize("tool", ["mcp__fs__search_and_replace", "mcp__db__write_query", "mcp__fs__read_and_update"])
def test_mcp_tool_with_a_write_word_cant_touch_approvals(approvals, tool):
    assert send_gate.decide({"tool_name": tool, "tool_input": {"path": "approvals.txt"}})


def test_mcp_pure_read_of_approvals_passes(approvals):
    assert send_gate.decide({"tool_name": "mcp__fs__read_text_file", "tool_input": {"path": "approvals.txt"}}) is None


def test_fewer_false_denials(approvals):
    assert send_gate.decide(bash("git --no-pager log examples/send_email.py")) is None
    assert send_gate.decide(bash("/usr/bin/python3 examples/send_email.py --to buyer@example.gov --subject Q --body-file b.txt")) is None
    assert send_gate.decide(bash("python3 -m pytest -q tests/test_send_gate.py")) is None
    assert send_gate.decide(bash("jq . .vscode/settings.json")) is None
    assert send_gate.decide(bash("python3 ~/tools/report.py")) is None


def test_dotted_capital_i_is_gated(approvals):
    assert send_gate.decide(bash(f"python3 SEND_EMAİL.py {EVIL}"))


# --- fifth round: the expansion rule instead of guessing what a pattern spells --

@pytest.mark.parametrize("cmd", [
    f"python3 examples/send_emai[[:alpha:]].py {EVIL}",
    "tee -a approval[[:lower:]].txt",
    "tee -a approval{{s,x},y}.txt",
    "tee -a .{/approv,}als.txt",
    "tee .{/hooks/send_g,}ate.py",
    f"python3 $'\\x73'end_email.py {EVIL}",
    "cp x conf/*",
    "tee notes\u2024txt",
])
def test_expanding_writes_and_sends_refused(approvals, cmd):
    assert send_gate.decide(bash(cmd))


@pytest.mark.parametrize("path", ["hooks/shlex.py", "hooks/json.py", "checks/outbound_checks.py", "checks/re.py"])
def test_gate_and_check_folders_cant_be_written(approvals, path):
    root = str(Path(send_gate.__file__).resolve().parent.parent)
    event = {"tool_name": "Write", "cwd": root, "tool_input": {"file_path": path, "content": ""}}
    assert send_gate.decide(event)


def test_checks_file_cant_be_changed_from_shell(approvals):
    assert send_gate.decide(bash("tee checks/outbound_checks.py"))


def test_shadow_module_next_to_the_gate_is_ignored_with_isolated_mode(approvals, tmp_path):
    import shutil
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    shutil.copy(GATE, hooks / "send_gate.py")
    (hooks / "shlex.py").write_text("import sys; sys.exit(0)\n")
    event = bash(f"python3 examples/send_email.py {EVIL}")
    proc = subprocess.run([sys.executable, "-I", str(hooks / "send_gate.py")], input=json.dumps(event),
                          capture_output=True, text=True, env={"GUARDRAILS_APPROVALS": str(approvals)})
    assert "deny" in proc.stdout


def test_shipped_settings_route_every_tool_through_an_isolated_hook():
    settings = json.loads((ROOT / "examples" / "settings.json").read_text())
    entry = settings["hooks"]["PreToolUse"][0]
    assert entry["matcher"] == "*"
    assert entry["hooks"][0]["command"].startswith("python3 -I ")


@pytest.mark.parametrize("tool,payload", [
    ("mcp__outlook__send_draft_message", {"messageId": "AAMk1", "to": ["buyer@example.gov"]}),
    ("mcp__x__send_draft", {"id": "d1", "to": ["buyer@example.gov"]}),
    ("mcp__x__send_message", {"id": "d1", "to": ["buyer@example.gov"]}),
    ("mcp__mail__reply", {"to": ["buyer@example.gov"], "reply_all": "true"}),
    ("mcp__mail__reply", {"to": ["buyer@example.gov"], "replyAll": 1}),
    ("mcp__mail__send", {"to": ["buyer@example.gov"], "participants": ["evil@attacker.test"]}),
    ("mcp__mail__send", {"to": ["buyer@example.gov"], "members": [{"mail": "evil@attacker.test"}]}),
])
def test_fifth_round_mcp_sends_refused(approvals, tool, payload):
    assert send_gate.decide({"tool_name": tool, "tool_input": payload})


def test_reply_without_reply_all_passes(approvals):
    assert send_gate.decide({"tool_name": "mcp__mail__reply", "tool_input": {
        "message_id": "m1", "to": ["buyer@example.gov"], "reply_all": False}}) is None


@pytest.mark.parametrize("cmd", ["cp /tmp/x/settings.local.json .claude/", "cp settings.json .claude",
                                 "mv s.json ~/.claude/", "install -m 644 s.json ./.claude/", "mv .claude .claude_old",
                                 "cp -t.claude s.json", "cp --target-directory=.claude s.json",
                                 "cp s.json .claude/.", "cp s.json .cla''ude/"])
def test_writes_into_the_settings_folder_refused(approvals, cmd):
    assert send_gate.decide(bash(cmd))


def test_reading_the_settings_folder_passes(approvals):
    assert send_gate.decide(bash("ls .claude/")) is None


@pytest.mark.parametrize("path", ["/usr/lib/python3.12/site-packages/evil.pth", "/tmp/lib/sitecustomize.py",
                                  "/home/u/.local/lib/usercustomize.py", "x/site-packages/a/b.py"])
def test_python_startup_files_unwritable(approvals, path):
    assert send_gate.decide({"tool_name": "Write", "tool_input": {"file_path": path, "content": ""}})


@pytest.mark.parametrize("cmd", ["cp evil.pth \"$(python3 -m site --user-site)\"/", "tee /opt/x/sitecustomize.py",
                                 "cp x.py /usr/lib/python3/site-packages/", "pip download a.pth"])
def test_python_startup_files_unwritable_from_shell(approvals, cmd):
    assert send_gate.decide(bash(cmd))


def test_python_startup_files_unwritable_over_mcp(approvals):
    event = {"tool_name": "mcp__fs__write_file", "tool_input": {"path": "/lib/site-packages/x.pth", "content": ""}}
    assert send_gate.decide(event)


@pytest.mark.parametrize("cmd", ["cp /tmp/evil.py checks/__init__.py", "echo x > checks/__init__.py",
                                 "tee checks/__init__.py", "sed -i s/a/b/ checks/__init__.py",
                                 "cp x.py hooks/helper.py", "mv hooks h_old", "rm -r checks"])
def test_gate_folders_unwritable_from_shell(approvals, cmd):
    assert send_gate.decide(bash(cmd))


@pytest.mark.parametrize("path", ["checks/__init__.py", "hooks/helper.py", ".claude/agents/reviewer.md",
                                  ".claude/commands/x.md"])
def test_gate_folders_unwritable_from_file_tools(approvals, path):
    full = str(ROOT / path)
    for tool in ("Write", "Edit"):
        assert send_gate.decide({"tool_name": tool, "tool_input": {"file_path": full, "content": ""}})


@pytest.mark.parametrize("path", ["checks/__init__.py", ".claude/agents/reviewer.md", "hooks/x.py"])
def test_gate_folders_unwritable_over_mcp(approvals, path):
    event = {"tool_name": "mcp__fs__write_file", "tool_input": {"path": str(ROOT / path), "content": ""}}
    assert send_gate.decide(event)


@pytest.mark.parametrize("cmd", ["cp x settings.local.json", "cp x ../settings.local.json", "touch notes.md"])
def test_a_shell_inside_claude_dir_is_guarded(approvals, tmp_path, cmd):
    inside = tmp_path / ".claude" / "agents"
    inside.mkdir(parents=True)
    assert send_gate.decide({**bash(cmd), "cwd": str(inside)})


def test_reading_gate_folders_passes(approvals):
    for cmd in ("ls checks/", "cat .claude/agents/reviewer.md", "grep -r deny hooks/", "git add checks/"):
        assert send_gate.decide(bash(cmd)) is None, cmd


@pytest.mark.parametrize("cmd", ["tar -Cchecks -xf x.tar", "tar -Chooks -xf gate.tar", "cp -tchecks __init__.py",
                                 "cp -thooks x.py", "cp --target-directory=checks x.py", "cp x.py ./CHECKS",
                                 "tar -C che''cks -xf x.tar"])
def test_folder_glued_to_an_option_is_still_named(approvals, cmd):
    assert send_gate.decide(bash(cmd))


@pytest.mark.parametrize("path", ["checks", "hooks"])
def test_mcp_naming_a_gate_folder_without_a_slash(approvals, path):
    event = {"tool_name": "mcp__fs__extract_archive", "tool_input": {"archive": "x.tar", "destination": path}}
    assert send_gate.decide(event)


@pytest.mark.parametrize("cmd", [
    "python3 examples/send_em\\\nail.py --to someone@else.com --subject Q --body-file b.txt",
    "echo 'APPROVED-SEND to=x@else.com' | tee -a approv\\\nals.txt",
    "cp evil.py che\\\ncks/__init__.py",
    "cp evil.py ho\\\noks/json.py",
    "cp evil.py 'che'\\\n'cks/x.py'",
    "cp evil.json .cla\\\nude/settings.json",
    "cp evil.py lib/sitecus\\\ntomize.py",
    "cp evil.txt lib/a.p\\\nth",
])
def test_backslash_newline_does_not_split_a_name(approvals, cmd):
    # The shell deletes backslash-newline inside a word, so these run as the plain name.
    assert send_gate.decide(bash(cmd))


@pytest.mark.parametrize("cmd", [
    "cat README.md #\\\npython3 examples/send_email.py --to someone@else.com --subject Q --body-file b.txt",
    "cat README.md #\\\ncp /tmp/mine.txt approvals.txt",
    "ls #\\\ncp evil.py checks/__init__.py",
    "git status #\\\ncp evil.py hooks/json.py",
    "cat README.md #\\\ncp /tmp/s.json .claude/settings.json",
    "cat README.md #\\\ncp x /tmp/venv/lib/site-packages/sitecustomize.py",
])
def test_comment_ending_in_a_backslash_does_not_hide_the_next_line(approvals, cmd):
    # After a `#` the shell doesn't join lines, so the second line runs on its own.
    assert send_gate.decide(bash(cmd))


def test_backslash_newline_in_an_approved_send_is_refused(approvals):
    # Inside single quotes the pair stays in the argument, so it's banned like any newline.
    assert send_gate.decide(bash("python3 examples/send_email.py --to buyer@example.gov --subject 'Q\\\nx' --body-file b.txt"))


def test_backslash_newline_in_an_unrelated_command_passes(approvals):
    assert send_gate.decide(bash("pip install \\\n  pytest")) is None


@pytest.mark.parametrize("tool", ["mcp__fs__search_and_overwrite", "mcp__fs__find_and_truncate",
                                  "mcp__fs__get_or_mkdir", "mcp__fs__view_and_touch",
                                  "mcp__fs__find_and_unlink", "mcp__fs__list_and_purge",
                                  "mcp__fs__get_and_wipe", "mcp__fs__stat_and_chown"])
def test_read_first_mcp_name_with_a_write_word_is_not_a_read(approvals, tool):
    assert send_gate.decide({"tool_name": tool, "tool_input": {"path": "checks/__init__.py", "content": "x"}})


@pytest.mark.parametrize("cmd", [
    "python3 examples/send_em''ail.py --to evil@x.com --subject s --body-file b.txt # it's",
    "python3 examples/send_em\\ail.py --to evil@x.com --subject s --body-file b.txt # it's",
    'python3 examples/send_em""ail.py --to evil@x.com --subject s --body-file b.txt # it\'s',
    "python3 examples/send_emai?.py --to evil@x.com --subject s --body-file b.txt # it's",
    "python3 examples/$'send_emai\\x6c.py' --to evil@x.com --subject s --body-file b.txt # it's",
    "python3 examples/send_em''ail.py --to evil@x.com --subject s --body-file b.txt \\",
    "cp evil.txt appro''vals.txt # it's",
    "cp evil.py checks/__init__.py # it's",
    "cp evil.py checks/__init__.py \\",
    "mv hooks hooks_old # it's",
    "cp evil.py hooks/send_ga''te.py # it's",
    "cp evil.md .claude/agents/reviewer.md # it's",
    "cp evil.md .claude/agents/reviewer.md \\",
    "cp evil.json .claude/sett''ings.json # it's",
    "cp evil.json ~/.claude/settings.json # it's",
    "cp evil.py /tmp/venv/lib/site-packages/sitecustomize.py # it's",
    "cp evil.pth /tmp/venv/lib/site-packages/x.pth # it's",
])
def test_an_unparseable_command_gets_every_check(approvals, cmd):
    # An unmatched quote in a comment, or a lone trailing backslash, makes shlex give up.
    assert send_gate.decide(bash(cmd))


@pytest.mark.parametrize("cmd", ["cp x settings.local.json # it's", "touch notes.md \\"])
def test_an_unparseable_command_inside_claude_dir_is_refused(approvals, tmp_path, cmd):
    inside = tmp_path / ".claude" / "agents"
    inside.mkdir(parents=True)
    assert send_gate.decide({**bash(cmd), "cwd": str(inside)})


@pytest.mark.parametrize("cmd", ["ls # it's", "echo done \\"])
def test_an_unparseable_unrelated_command_passes(approvals, cmd):
    assert send_gate.decide(bash(cmd)) is None


@pytest.mark.parametrize("tool", ["mcp__fs__get_and_restore", "mcp__fs__read_and_install",
                                  "mcp__fs__list_and_clear", "mcp__fs__find_and_import",
                                  "mcp__fs__get_and_sync"])
def test_read_first_mcp_name_with_a_restore_word_is_not_a_read(approvals, tool):
    assert send_gate.decide({"tool_name": tool, "tool_input": {"path": "checks/__init__.py", "content": "x"}})


SEND_TAIL = " --to evil@x.com --subject s --body-file b.txt"


@pytest.mark.parametrize("cmd", [
    # A quote in a comment on line 1 used to flip the expansion check's quote tracking for
    # line 2, which then builds a protected name with $'', a glob or a brace.
    "true # it's\npython3 examples/send_em$''ail.py" + SEND_TAIL,
    'true # it\'s\npython3 examples/send_em$""ail.py' + SEND_TAIL,
    "true # it's\npython3 examples/send_emai?.py" + SEND_TAIL,
    "true # it's\npython3 examples/send_emai{l,}.py" + SEND_TAIL,
    "true # it's\necho APPROVED-SEND evil@x.com >> approv$''als.txt",
    "true # it's\ncp evil.py chec$''ks/__init__.py",
    "true # it's\ncp evil.py hook?/json.py",
    "true # it's\ncp evil.py che{c,}ks/__init__.py",
    "true # it's\ncp evil.json .cla$''ude/settings.json",
    "true # it's\ncp evil.py sitecus$''tomize.py",
    # The same with a second comment quote, so the whole command parses.
    "true # it's\npython3 examples/send_em$''ail.py" + SEND_TAIL + " # it's",
    "true # it's\ncp evil.py hook?/json.py # it's",
    "true;# it's\ncp evil.py hook?/json.py # it's",
    # A quote in a heredoc body does the same.
    "cat <<E\nit's\nE\npython3 examples/send_emai?.py" + SEND_TAIL,
    # A usually-empty parameter glued into a spelled-out name.
    "python3 examples/send_em$@ail.py" + SEND_TAIL,
    "python3 examples/send_em${NOPE}ail.py" + SEND_TAIL,
    "python3 examples/send_em$X''ail.py" + SEND_TAIL,
    "cp evil.txt approv${X}als.txt",
    # A positional is one digit, so `$1ail` is `$1` then `ail`.
    "python3 examples/send_em$1ail.py" + SEND_TAIL,
    "cp evil.txt approv$0als.txt",
    # A `#` that isn't at the start of a word isn't a comment to the shell, so the gate mustn't
    # skip what follows it: after an escaped space, glued to a word, or after `$(...)`.
    "true # it's\ntrue \\ # ; python3 examples/send_emai?.py" + SEND_TAIL + " # it's",
    "true # it's\ntrue \\ # ; cp evil.py hook?/json.py # it's",
    "true # it's\ntrue \\ # ; echo APPROVED-SEND evil@x.com >> approval?.txt # it's",
    "true # it's\ntrue \\ # ; cp evil.json .claud?/settings.json # it's",
    "true # it's\ntrue x# ; cp evil.py hook?/json.py # it's",
    "true # it's\ntrue $(true)# ; cp evil.py hook?/json.py # it's",
    "true # it's\ntrue \\;# ; cp evil.py hook?/json.py # it's",
    # A `#` inside backticks or a nested ${...} isn't a comment to the shell either, and quotes
    # inside "$(...)" start over.
    "true # it's\necho `true # x` ; python3 examples/send_emai?.py" + SEND_TAIL + " # it's",
    "true # it's\necho ${x:-${y} #} ; python3 examples/send_emai?.py" + SEND_TAIL + " # it's",
    "true # it's\necho `true # x` ; cp evil.py hook?/json.py # it's",
    "echo \"$(echo \")\" ; cp evil.py hook?/json.py \")\"",
    "echo \"`echo \"x\" ; cp evil.py hook?/json.py`\"",
    # The cost the README lists: a quoted glob character no longer passes once there's a comment.
    "python3 -c \"print(2*3)\" # check",
    # zsh, the Mac default, and bash's extglob have globs beyond * ? [..] {..}.
    "python3 examples/send_emai(l).py" + SEND_TAIL,
    "python3 examples/send_emai@(l).py" + SEND_TAIL,
    "python3 examples/send_emai(l|x).py" + SEND_TAIL,
    "echo APPROVED-SEND evil@x.com >> approval(s).txt",
    "cp evil.py chec(k)s/__init__.py",
    "cp evil.py hook<->s/json.py",
    "python3 examples/send_emai#l.py" + SEND_TAIL,
    "python3 examples/send_ema^xil.py" + SEND_TAIL,
    "cp evil.py hooks~x/json.py",
    # Another shell expands quoted text again.
    "eval 'python3 examples/send_emai?.py" + SEND_TAIL + "'",
    "bash -c 'python3 examples/send_emai?.py" + SEND_TAIL + "'",
    "sh -c 'cp evil.py hook?/json.py'",
    "/bin/zsh -c 'cp evil.py chec(k)s/__init__.py'",
    # However the shell's name is spelled, or if a variable holds it.
    "'bash' -c 'cp /tmp/x approval?.txt'",
    "b''ash -c 'cp /tmp/x approval?.txt'",
    "=bash -c 'cp /tmp/x approval?.txt'",
    "\\eval 'python3 examples/send_emai?.py" + SEND_TAIL + "'",
    "$SHELL -c 'cp /tmp/x approval?.txt'",
    "\"$SHELL\" -c 'cp evil.py hook?/json.py'",
    "watch 'cp evil.py hook?/json.py'",
    # zsh's x# still counts when the x is quoted.
    "python3 examples/send_ema'i'#l.py" + SEND_TAIL,
    "cp evil.py chec'k'#s/__init__.py",
    "cp evil.py 'hooks'~x/json.py # x",
    # The costs the README lists for those.
    "bash -c 'ls *.txt'",
    "grep -rn \"a.*b\" $(git ls-files)",
    "(cd build && make)",
    "npm i foo@^1.2",
    "find . -name \"*.py\" -newer \"$REF\"",
    "awk -v h=\"$HOME\" '{print $1, $2}' data.txt",
    "git commit -m \"$(cat <<'EOF'\nfix(x): y\nEOF\n)\"",
    "curl https://example.com/a#frag",
])
def test_a_comment_or_heredoc_quote_cant_hide_an_expansion(approvals, cmd):
    assert send_gate.decide(bash(cmd))


@pytest.mark.parametrize("tool", ["mcp__fs__list_and_reset", "mcp__fs__find_and_store",
                                  "mcp__fs__get_or_add", "mcp__fs__read_and_trash"])
def test_read_first_mcp_name_with_a_reset_word_is_not_a_read(approvals, tool):
    assert send_gate.decide({"tool_name": tool, "tool_input": {"path": "checks/__init__.py", "content": "x"}})


@pytest.mark.parametrize("cmd", ["ls; echo hi # it's", "python3 -m pytest -q # it's",
                                 "echo $HOME", "awk '{print $1, $2}' data.txt",
                                 "echo a\\ #b", "git log HEAD~1 # last", "ls ~/notes",
                                 "python3 -c \"print(1)\"", "bash scripts/test.sh",
                                 "echo \"# Notes\" >> notes.md", "echo \"$HOME\"/notes",
                                 "git stash show stash@{0}"])
def test_unrelated_commands_with_comments_or_variables_pass(approvals, cmd):
    assert send_gate.decide(bash(cmd)) is None


@pytest.mark.parametrize("tool", ["mcp__github__list_commits", "mcp__blog__list_posts"])
def test_mcp_reads_with_ordinary_nouns_pass(approvals, tool):
    assert send_gate.decide({"tool_name": tool, "tool_input": {"path": "checks/outbound_checks.py"}}) is None


def test_checks_is_a_regular_package():
    # A namespace package loses to any checks.py on sys.path, like one planted in examples/.
    assert (ROOT / "checks" / "__init__.py").is_file()


def test_dist_packages_is_a_startup_dir(approvals):
    assert send_gate.decide({"tool_name": "Write", "tool_input": {"file_path": "/usr/lib/python3/dist-packages/a.pth"}})
