# jev-hooks

Nine Claude Code hooks backed by TypeSafe's Jev (System One): fast, calibrated
yes/no and multiple-choice judgments at roughly 300 ms per judgment and a fraction of a cent
(the loop detector's no-repeat fast path is ~100 ms, which is interpreter start, not Jev).
Jev never generates text here. It answers bounded questions so the harness can
gate a moment it would otherwise trust itself on.

| Hook | Event | What it does |
|---|---|---|
| `bash_risk_gate.py` | PreToolUse (Bash) | Read-only commands never reach Jev, and an outward-facing one (`gh pr merge`, `git push`, `terraform apply`, `helm upgrade`, `kubectl apply/delete`, `npm publish`, …) asks without one: nothing local is destroyed, so it scores low, but no local undo reaches a merged PR. Otherwise one call asks: irreversible? could it kill the running session? named target or a pattern? allow / confirm / block. Denies a session-killing pattern; asks when it is irreversible (p≥0.5), when a pattern widens it, or when a confident `confirm` meets real stakes; stays silent on the rest so the normal permission flow decides. |
| `precompact_triage.py` + `sessionstart_reinject.py` | PreCompact, SessionStart(compact) | Before compaction, judges every user prompt and assistant conclusion: is it a decision, correction, constraint or open question a future turn must honour, and has it been superseded? Writes the keep-set; after compaction, the SessionStart hook prints it back into context. The summary still gets written by Claude; Jev decides what it must not lose. |
| `subagent_verify.py` | SubagentStop | Reads the subagent's task and **what its tools actually did**. One yes/no per criterion asks whether the RECORD shows it, never whether the report claims it, plus a check for specifics the report cites that appear nowhere in the record. Zero tool calls with criteria claimed is blocked outright. An honest "not done" always passes. |
| `loop_detect.py` | PostToolUse (any tool) | Keeps the last six tool calls per session. Only when the last three tool names repeat AND one of them failed or the same call was made twice does it ask one score: same failure again, unclear, or clear progress. Three no-progress scores in a row post a system message naming the repeated tool and the last error, and tell the model to change approach. Never blocks a tool. |
| `stop_selfcheck.py` | Stop | Reads only the final message, the last user prompt and the tail of recent tool results. Three yes/no questions in one call: does it promise work not yet done, does it fail to answer what the user last asked, does it state an outcome the tool results do not show. Any answer at or above 0.7 sends the session back once with the check named and the offending sentence quoted. |
| `model_router.py` | PreToolUse (Agent) | Picks the model a SUBAGENT runs on: one choice over the subagent's task routes a grep to haiku, a summary to sonnet, a failing test to opus and a design question to fable. Writes only `updatedInput`, never a permission. Leaves a model the caller named alone (`JEV_HOOKS_ROUTER_FORCE=1` overrides). Routes only `general-purpose` by default (`JEV_HOOKS_ROUTER_TYPES` widens it): a typed agent can define its own model and a route would beat it, and a fork is never routed — it runs on its parent's model whatever its input says. When no single pick is clear but the task is plainly cheap, it still routes cheap — declining sends the spawn to the parent's model, the expensive one. |
| `prompt_routing.py` | UserPromptSubmit | Reads the prompt alone, never the transcript. One choice says what kind of message it is (question, change request, thinking aloud, approval, other) and, when the machine lists skills, a second choice names the relevant one. A third says which model suits the work, and when that is not the model running the session — read from the transcript, the only place it is written down — the line names it: a hook cannot switch the session's model, so it says `/model fable` and stops. The same advice is never repeated to a session twice, and `haiku` is not offered for a whole session by default. Prints one line of context only when confident: a question is answered not acted on, an approval means proceed, thinking aloud gets a response not work. Change requests get nothing. Skips prompts under 12 characters, slash commands and pasted tool blocks. |
| `edit_risk_gate.py` | PreToolUse (Edit, Write, NotebookEdit, MultiEdit) | The Bash gate reads a command; this reads a file change, and git settles most of it without a call. A clean tracked file is skipped — `git checkout --` restores it, however large the edit. A whole-file Write over uncommitted changes is asked about outright: the delta is provably gone, and a live run scored that same input 0.54 then 0.46, so it is decided as the fact it is rather than on a coin flip. Any other edit to a tracked file is skipped too. Jev is left one question, about the files no commit holds — ignored, untracked, or in no repo at all: precious, or regenerable? **Nothing from inside the file is sent** — path, git verdict and byte counts only, because the ignored file most likely to reach this point is the one most likely to hold a secret. |
| `narrow_output.py` | PreToolUse (Bash, Read, Grep) | Judges output **before** it enters context. A command that already limits itself, one with no command or path, and anything not provably read-only never reach Jev. Otherwise one call over the command and the last user prompt asks: is this far more output than the goal needs, does the goal need all of it, and which end carries the answer. Only when bulky ≥ 0.7 and needs_all < 0.3 does it append `| head -200` / `| tail -200` (Read gets `limit`, Grep `head_limit`). It only ever appends a limiter — never a path, a pattern or a flag's value — and the rewrite prints a marker saying what was cut. |

## Install

```bash
export TYPESAFE_API_KEY=...                  # your own key, console.typesafe.ai
claude --plugin-dir /path/to/jev-hooks       # or add it to a marketplace
```

Optional: `JEV_HOOKS_MODEL` (default `jev-latest`; pin a versioned id if you tune
thresholds), `JEV_HOOKS_TIMEOUT` seconds (default 6), `JEV_HOOKS_GATE_MODE=warn`
(never deny, only ask), `JEV_HOOKS_LOG=/path` (one JSON line per call with token
usage and latency).

## Edit gate, live

Run 2026-09-19 against `jev-latest`, three times per case in a scratch repo, to see
whether any decision flapped:

| The write | git state | Decision | Cost |
|---|---|---|---|
| `Write` 12 B over a 1.5 KB `main.tf` | uncommitted changes | **ask** ×3 | no call, ~145 ms |
| `Edit` one byte in the same file | uncommitted changes | silent ×3 | no call, ~125 ms |
| `Edit` in it once committed | clean | silent ×3 | no call, ~130 ms |
| `Write` over a git-ignored `.env` | ignored | **ask** ×3 (p=0.66-0.69) | ~570 ms |
| `Write` over a git-ignored `pnpm.lock` | ignored | silent ×3 | ~515 ms |
| `Write` over an untracked `notes.md` | untracked | **ask** ×3 (p=0.61-0.62) | ~525 ms |

The two ignored files are the point. Both are invisible to git, both are being
truncated wholesale, and the only thing separating them is whether the content
can be got back — `.env` cannot, `pnpm.lock` is one `install` away. Jev splits
them on the path alone, without seeing a byte of either.

The first two rows are the other half of the point: they are facts, so they never
reach Jev, and the fast path costs a single `git status`. An earlier version put
both to Jev and got 0.54 on one run and 0.46 on the next for identical input —
correct uncertainty, since a commit really does hold most of a modified file, but
useless to hang a threshold on. What git can prove, git decides.

## Stop self-check, live

Run 2026-09-19 against `jev-1.13.0` with the fixture in `test/fixtures/stop_session.jsonl`
(the user asked "fix the failing build and tell me whether the integration suite passes
now"; the only tool result is an `ok` line from `go build`):

| Final message | Decision |
|---|---|
| "Build is green. I'll open the PR next." | block: promises work not yet done (p=0.92) |
| "Build is green; the integration suite was not run, only go build. Say the word and I'll run it." | pass |
| "All 212 integration tests pass and the PR is merged." | block: states an outcome the tool results do not show (p=0.96) |

The middle message is the point: saying plainly what was not done is an answer, and a
conditional offer after a complete report is not a promise. The first version of the
questions blocked it; the criteria now say both things explicitly.

## Prompt routing, live

Run 2026-09-19 against `jev-latest` (resolving to `jev-1.13.0` that day), from the
enforcer-graph checkout, whose skills list holds the platform's skills:

| Prompt | kind (confidence) | Printed |
|---|---|---|
| "why does the frontier release cancelled dependents?" | question (1.00) | the answer-and-report hint |
| "merge 58 and go" | change_request (0.54) | nothing |
| "i keep thinking the kanban projection is the wrong shape" | thinking_aloud (1.00) | the do-not-start hint |

The middle row is the interesting one: "merge 58 and go" is an instruction to act, not
consent to a proposal, and the judge split it between change request and approval at
0.54, under the 0.70 bar, so it stayed silent rather than guess. No skill cleared the
bar for any of the three. Latency 338 to 357 ms per prompt including the call, against
a 400 ms budget; 66 ms against the local stub. Each call costs about 630 input tokens.

### What the stop check does not treat as a failure

Two classes were false positives in real use (2026-09-20) and are now excepted
in the criteria, with live checks either way:

- **A next step blocked on the user** — "the roll PR needs your merge" — named
  once alongside what was finished. Naming the gate is a report of where the
  work stands, not a promise the assistant can keep.
- **An outcome expected from a check not yet run** — "the roll will show 200
  instead of 404". That is intent; claiming the check already passed is not.

Three more were measured 2026-09-21, over one long session whose stop check
blocked eleven times:

- **Work in flight** — eight of the eleven were `promise` blocks on honest
  status about a running subagent, a background command or a scheduled
  wake-up ("still building, not done yet"), each rephrased a turn later to
  get past the check. The Stop input carries `background_tasks` and
  `session_crons` for exactly this distinction, and the hook now reads them:
  with something in flight and the message saying the work is not finished,
  `promise` alone cannot block. A plain promise about something else still
  does, work in flight or not.
- **A command handed back after a refusal** — the assistant was denied
  permission to run it, so "run this yourself" is a gate, not a promise. The
  refusal is in the tool record; the hook names it in the state.
- **Describing what it just wrote** — a `Write` result says only "File created
  successfully", so the judge could not see the content and read any
  description of it as a claim from nowhere. The call line now carries the
  first 400 characters of what was written, for this check and the verifier.

And one input bug, measured 2026-09-22 by replaying a session at each block:
at 6 of 16 blocks, "the last user prompt" was harness text — the check's own
previous complaint (3), the empty-reply nudge (2), a subagent's hand-back (1)
— so `unanswered` was judging the reply against the hook. Those are skipped now.

Two changes were tried against the same replay and **not** shipped: widening
the tool-result window (8k to 32k) did not bring a restated number from
hundreds of calls earlier into view, so a block on it stands — re-verifying was
one query. Dropping loop_detect's `counting` records was also wrong: each is a
paid judgment with a score, not bookkeeping.

### Tuning the Bash gate on a mixed log

A first pass at the gate's prompt rate, 2026-09-21, found the log itself was the
problem. Sessions keep running the plugin copy they started with, so after an
update old and new code write the same file: of 201 judged prompts in the three
hours after a retune, 126 were bare `confirm` verdicts — the exact rule that
retune had removed — interleaved with current-rule prompts until the end of the
log. The apparent 28–37% prompt rate was mostly code that no longer ships.
Every record now carries `v`, the plugin version that made it, and
`bin/stats.py --v=0.15.0` counts one version alone.

The log did point at one cost clearly: 179 of 532 judged commands were
`cd <repo> && <something already read-only>`, each paying a call and ~300 ms.
`cd` now counts as read-only (a `cd $(...)` is still refused by the same check
that refuses any hidden command). Eight representative commands — patches to
tracked files in clean repos, `rm -rf`, a mainnet `cast send`, `git reset
--hard`, an overwrite outside any repo — gave the same verdicts before and
after, three runs each against the live service: silent on the four patches,
ask on the four destructive ones. A change to feed the judge git state was
built toward and dropped: the current gate already gets those cases right, and
the log entries that suggested otherwise could not be attributed to a version.

When the stop check blocks, the user now gets a `systemMessage` with the exact
`bin/wrong.py` command to dispute it. It is kept out of `reason`, which the
model reads: a label from the party being judged is not a label.

A plain promise ("I'll open the PR next"), a plan in place of results, and an
unevidenced past result ("all 41 checks pass") all still block.

## Narrowing output, live

Tool results dominate a session's context — in one real transcript ~1460 tool results
against ~750 assistant messages — and every turn re-sends all of it. The cheapest place
to attack that is the moment before the call, where one 300 ms judgment can keep a whole
log out of the transcript for good.

Run 2026-09-19 against the live service, three real calls with the goal taken from the
last user prompt:

| Command | Goal | bulky | needs_all | where | Decision | Latency |
|---|---|---|---|---|---|---|
| `cat /var/log/syslog` | "why did the service restart" | 0.96 | 0.15 | tail (0.87) | **narrowed** to the last 200 lines | 437 ms |
| `wc -l big.csv` | "how many rows" | — | — | — | fast path, **no call** | 135 ms |
| `grep -rn TODO .` | "count the TODOs" | 0.91 | 0.91 | tail (0.64) | silent — left alone | 499 ms |

The middle row costs nothing at all: `wc` already bounds its own output, so there is
nothing to decide. The third is the one the rails exist for — the output really is bulky
(0.91), but counting every match needs all of it, so narrowing would produce a confidently
wrong number. `cat big.csv` with the same "how many rows" goal scores bulky 0.96 and
needs_all 0.83 and is likewise left alone. Each call costs about 680 input tokens.

The narrowed command is rewritten so the truncation is visible in its own output:

```bash
_jh_f=$(mktemp)
{ cat /var/log/syslog
} > "$_jh_f"
_jh_rc=$?
tail -n 200 "$_jh_f"
_jh_n=$(wc -l < "$_jh_f")
[ "$_jh_n" -gt 200 ] && echo "[jev-hooks: showing last 200 of $_jh_n lines; re-run without narrowing for all]"
rm -f "$_jh_f"
(exit $_jh_rc)
```

It is not a pipe to `head`, and the reason matters. A pipe makes the tool's exit status
that of the *last* command in it, so a failing read-only command would report success — a
worse lie than the bulk it saves. Buffering stdout instead lets `$?` be captured
immediately and replayed by `(exit $_jh_rc)` in a subshell, which sets the status without
exiting the harness's persistent shell. Counting the whole output is also what lets the
marker state the **true total** ("showing last 200 of 4812 lines"), which a pipe can never
know, and it means the marker appears only when something was really cut. **stderr is
never redirected**: errors are short, important, and must not be narrowed or delayed
behind a buffer.

The tradeoff accepted: the producer now runs to completion rather than being killed early
by SIGPIPE. That is right here — the goal is keeping tokens out of context, not saving the
command work — and a read-only command that produces gigabytes is pathological.

Two details are load-bearing. The group closes on a **newline**, not `;`, because a command
ending in a trailing `# comment` would swallow a `;` and the rewrite would not parse. And
the shell variables are `_jh_`-prefixed because the harness reuses one shell across calls,
where a bare `f`, `rc` or `n` would clobber the caller's own.

A `systemMessage` names what was narrowed and why on every narrowing, so it is visible and
can be re-run unnarrowed.

### The rails, which are the point

1. **Never narrow anything not provably read-only.** It imports `bash_risk_gate.is_read_only`
   rather than copying it, so there is one copy of that predicate. A redirect, `$(`, a
   backtick, `-delete`, `-exec`, `xargs`, `tee` or any non-read-only verb is left alone.
2. **Only ever append.** A limiter on the end, `limit` on a Read, `head_limit` on a Grep.
   Never a path, a pattern, a flag's value or the command's meaning.
3. **The truncation is visible in the output itself**, so a later reader — human or model —
   is never misled into thinking they saw everything.
4. **`JEV_HOOKS_NARROW=off`** disables it entirely: exit 0, no call.
5. **Fails open** on no key, timeout, 429/529, malformed stdin. And with no goal in the
   transcript it does nothing at all: there is nothing for "bulky" to be relative to, and a
   judge asked to guess one would narrow on the command's looks alone.
6. **A small file is never judged.** Almost no `Read` arrives with a `limit`, so without a
   free fast path the most common call in a session would pay ~300 ms and ~680 tokens every
   time. Anything under 64 KB is skipped outright: a small file cannot be bulky, and there
   is nothing to decide.

### Why the verifier judges the record

Measured 2026-09-20: a fabricated hand-back — invented test names, a file path
with a line number, a PR number, a passing timing, **zero tool calls** — passed
this hook in silence. It is the same hole the graph's own verification had, and
it sat in the component vouching for every agent.

It now judges `work`, the recorded output of tools the agent actually invoked.
Five cases, live against jev-1.13.0:

| Case | Verdict |
|---|---|
| fabrication, zero tool calls | blocked, "made NO tool calls" |
| fabrication, unrelated work only | blocked on the criteria |
| honest "not done", zero tool calls | passes — it claims nothing |
| real work, matching output | passes |
| real work, report inflates it | blocked on invented specifics |

Two bugs the tests caught while writing it: the parent's spawning `Agent` call
appears in a fork's own transcript, so counting it made a do-nothing agent look
busy; and blocking an honest "not done" just repeats the instruction the agent
followed, which is a loop.

### Which transcript it judges

Versions through 0.12 read `transcript_path`, which on `SubagentStop` is the
**parent** session's transcript; the subagent's own is `agent_transcript_path`
(hooks reference). Measured 2026-09-21: four forks spawned in one turn were
every one judged against the task of the fork spawned last — the parent's most
recent `Agent` call — and against the parent's tool record, so their real work
read as invented (p 0.88–0.94) and all four were blocked. Over 297 recorded
verdicts the hook had blocked 93%; a good share of that was this. It now takes
the documented field, then the sibling file named by `agent_id`, then the
sibling whose final message is the report being judged, and if none matches it
stays silent and records why — a judgment against the wrong record is worse
than none. Every decision now records which transcript it judged and how the
task was found, so the block rate can be re-measured against real records.

Re-measured on 0.14.0, 2026-09-21: three probe subagents (a fork, a fresh
general-purpose agent, an Explore agent) with checkable tasks all passed, each
judged against its own transcript (`transcript=agent_transcript_path`), the
fork at 0.98/0.97 on its criteria — the case that was blocked before. The same
minutes surfaced one more wrong record: SubagentStop fired with no
`agent_transcript_path` and no `agent_type`, handing over a MAIN session's
transcript, and the fallback judged that session's own twelve-point prompt as
the task. Main-session records say `"isSidechain": false`; such a transcript is
now never judged as a subagent's (`transcript=main-session`, silent).

`bin/stats.py` and `bin/wrong.py` had a related blindness: run from a shell,
they read `~/.claude/jev-hooks/decisions.jsonl` (10 decisions) while the hooks,
run by the harness with `CLAUDE_PLUGIN_DATA` set, had written 3,450 to
`~/.claude/plugins/data/jev-hooks-jev-hooks/`. Not one dispute had reached the
real log. Both now find the newest plugin data log and print which file they read.

### What this cannot know

Three limits are real, are not fixed, and are written down here so the next person inherits
them rather than rediscovering them:

- **`is_read_only` now decides more than it used to.** In `bash_risk_gate` a mistake in that
  regex means the gate stays quiet and the normal permission flow decides; here it means a
  command gets rewritten. The blast radius is bounded — the rewrite only ever appends a
  limiter, so a misjudged command is not made destructive — but one regex is now load-bearing
  for two hooks that different people will edit.
- **The goal can be stale.** `goal` is the last thing the user typed, which in a long agentic
  stretch may be twenty tool calls and three subtasks old. Narrowing against a stale goal cuts
  the wrong end, and nothing at runtime can tell "old goal" from "current goal".
- **Two PreToolUse hooks now run on Bash.** They cannot both speak today, because anything
  `narrow_output` would rewrite is read-only and read-only takes `bash_risk_gate`'s silent
  fast path. That non-overlap is a property of the shared predicate, not something enforced
  anywhere, and if the two ever disagree a `deny` and an `updatedInput` would be emitted for
  the same call.

## Using Jev outside the hooks

The hooks are one caller. `bin/jev` is the handle for everything else — a
bounded judgment from the shell, no imports, usable by any agent or script:

```bash
bin/jev noul  "Does the report show every criterion met, with evidence?" --state-file report.md
bin/jev score "How risky is this if it is wrong?" --level contained --level spreads --level structural --state-file diff.txt
bin/jev ask   questions.json --state-file state.txt    # several questions, ONE call
```

Exit 3 means the judge could not be reached: no opinion, carry on. Every call
records its decision like a hook does.

**`skills/jev/SKILL.md` is the part that matters for a new agent.** It carries
what the code cannot say: that questions in one request are independent and
cheap so you should batch them, that code owns thresholds and ordering while
Jev only judges, that criteria must be written for the false positives, and the
two licence rules — never build a pass-through, never publish a comparison
against another model. An agent handed "use Jev for X" without it will get at
least one of those wrong.

## What the hooks decided, and tuning them with it

Every hook records its own verdict next to what the call cost, to
`${CLAUDE_PLUGIN_DATA}/decisions.jsonl` (or `JEV_HOOKS_LOG`; `off` disables
it). Local only, nothing leaves the machine. It is **on by default** on
purpose: a feedback loop that needs an env var set is one that never happens,
and without a record there is no way to measure a false positive rate — every
threshold then moves on whoever complains loudest.

```bash
python3 bin/stats.py                       # what each hook decided, what it cost,
                                           # and how many judgments sit near a threshold
python3 bin/wrong.py stop_check "why"      # mark its last spoken decision wrong
```

`stats.py` prints the number that matters for tuning: how many judgments landed
between 0.60 and 0.80, the band where a verdict flips on wording rather than
substance. `wrong.py` appends a dispute referring to that decision and its
probabilities, never editing history — the only labelled data a threshold can
honestly be moved against.

**Next, not built:** shipping the same decisions to `enforcer-governance` as
OTLP records under their own scope and chain (`client: jev-hooks`), which is
where the fleet's Claude Code telemetry and the governor's own receipts already
land. A receipt carries verdict, policy, rule, reason, tool, session and cost —
the same shape a hook decision has — with hash-chain integrity and dashboards
already built. The local file stays as the no-network fallback.

## Every hook fails open

No key, a timeout, a 429 or 529: the hook exits 0 with no output and the
session behaves exactly as it would without the plugin. A judgment service must
never be the reason a coding session stalls. The verify and stop hooks each block at most
once (`stop_hook_active`), and the gate never turns an allow into a
deny for read-only commands, which it short-circuits before any network call.

## Before making this repository public

See [LICENSING.md](LICENSING.md). It is a DRAFT and no lawyer has read it. The
open question is whether `bin/jev`, which forwards an arbitrary question to
Jev, is a client (each user brings their own key) or a standalone service (which
TypeSafe's agreement forbids offering). The hooks themselves are bounded
judgments and are not in question. A person decides this, not an agent.

## What leaves your machine

The gate sends the command, its description and the cwd. Triage sends user
prompts and assistant prose, clipped, never tool output or thinking. The model router sends the subagent's task, its
description and its agent type — never the parent's transcript. The session-model
hint sends the prompt it was already sending; the model it compares against is
read locally from the transcript and never leaves the machine. Verify
sends the subagent's task, its last message and up to 16k chars of its work
record — each tool call paired with what that call printed. The stop check
sends the last user prompt, the final message and up to 8k chars of the same
record. The narrowing
gate sends the command or path, its description and the last user prompt as the goal,
clipped to 1500 chars — never any tool output. The edit gate sends the least of
any of them: a path, git's one-word verdict on it and three byte counts, and
never a byte from inside the file — the ignored file most likely to reach it is
the one most likely to hold a secret, so shipping a preview to decide whether it
is precious would give away the thing being protected. TypeSafe stores inputs by
default with no stated retention period (US-hosted, not used for training); read
their terms before enabling this on a repository whose prompts are sensitive.

## Test

```bash
bash test/run.sh      # 203 checks against a local stub; no key, no network
```

The stub answers from markers in the request (`[qid=yes]`, `[qid=pick:block]`),
so each test steers Jev's answer and checks the hook's decision, not the model.

## Not here on purpose

No generic "ask Jev" tool, no summarisation, no memory store. The graph plugin
(`enforcer-graph/plugin`) is deliberately Jev-free; this plugin is the optional
layer for a developer who wants judgments over their own session.

## Loop detection, measured

Fast path (no repeat): 103 ms per call measured in `test/run.sh` on this machine,
no network. That is the cost on every tool call.

Live run, 2026-09-19, model `jev-1.13.0`: five identical `go test` calls, each
returning the same `--- FAIL: TestClaimRace ... expected 1 claim, got 2`. Calls
1 and 2 are below the repeat window and cost nothing. Calls 3, 4 and 5 each
scored 0.0 (level "no progress: the same failure again") and the fifth call
fired: `jev-hooks: the last three Bash calls made no progress (--- FAIL:
TestClaimRace ...)`, with additionalContext telling the model to change approach
and say what it will do differently. Strikes then reset, so the same loop will be
named again three calls later if it continues.
