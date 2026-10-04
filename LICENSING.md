# Before this repository is made public

**Status: DRAFT, unreviewed. Written by an agent from a reading of TypeSafe's
Master Customer Agreement taken on 2026-09-19. It is not legal advice and no
lawyer has seen it. Nothing here authorises publication.**

This plugin has no value without TypeSafe's Jev API. Publishing it therefore
asks a question we have not answered in writing: does distributing a client
that calls Jev fall inside our agreement with TypeSafe, or outside it?

## The clause that matters

The agreement permits embedding the API in an application operated for your own
end users. It forbids offering the API as a standalone service. Our own note on
the server-side integration drew the line as: every call must be one of OUR
verbs — verify this report, dedupe this fact, rank this frontier — never a
generic "ask Jev anything" tool.

## Why publishing this repository is not obviously on the right side

`bin/jev` is a generic pass-through. It takes an arbitrary question from the
command line and forwards it to Jev:

    jev noul "Does the diff change behaviour, not just formatting?" --state-file diff.txt

That is the exact shape the note above says not to build. The eight hooks are
fine by that test — each one is a bounded judgment on a bounded input, which is
a verb. The CLI and the `jev` skill are not, and they are the surface a
community reader will reach for first.

## The argument on the other side, which may be the stronger one

Every user supplies their own `TYPESAFE_API_KEY` from their own account
(`lib/jev.py` reads the environment and fails open when the variable is unset;
there is no key in this repository and no proxy to one of ours). So we are not
reselling our access and we are not operating a service. We are publishing a
client, the way an SDK or an unofficial wrapper is a client. Each user's own
agreement with TypeSafe governs their use. That reading makes publication
straightforwardly fine, and it is how most API client libraries exist.

Which reading governs is a question for a person, not for the agent that wrote
this file.

## What a human needs to decide or confirm

- [ ] Is the TypeSafe account the company's, or an individual's? (Open since
      2026-09-19.)
- [ ] Does distributing a client that users drive with their own keys count as
      "offering the API as a standalone service"? Ask TypeSafe directly if the
      text does not settle it — a one-line answer in writing is worth more than
      any reading of ours.
- [ ] If the answer is no, does `bin/jev` and the `jev` skill need to go, or
      does the whole repository stay private? The hooks alone are defensible;
      the generic CLI is the part in question.
- [ ] Record the conclusion in a repository, not in a chat log or an agent's
      memory. That is the same item still open against enforcer-graph's
      NOTES.md.

## Two more publication items, unrelated to the agreement

- **Benchmarks.** The agreement forbids publishing performance comparisons
  against other models. Our own latency and cost figures are fine; "Jev vs
  <model>" is not. The README currently states our own numbers only. Keep it
  that way, and watch pull requests for anyone adding a comparison table.
- **Disclosure.** README's "What leaves your machine" names, per hook, what is
  sent, and says TypeSafe stores inputs by default with no stated retention
  period. That should stay at least as specific as it is now. If ZDR is ever
  agreed, update it rather than deleting the caveat.
- **Governor split (0.27.0).** With enforcer-governor installed, jev-hooks makes
  no allow/deny decisions; policy is the governor's, quality hooks stay here.
  Keep that line when describing either plugin.
