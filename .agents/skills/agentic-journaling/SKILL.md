---
name: agentic-journaling
description: Run a propagation over Obsidian journals - find `#agent` task requests in daily notes, carry each out (following a target from the journal's config note where one matches), and write status and result back. Use when asked to run, check or set up agentic journaling.
disable-model-invocation: true
---

# Agentic journaling

Scripts do the mechanics: configuration, discovery, ids, status, feedback, compare-and-swap writes and logging. You do only what needs judgment: matching a request to a target where one fits and carrying out the request.

The script is `scripts/aj.py` in this skill's directory. Run it with `uv run <skill-dir>/scripts/aj.py <command>`, each command on one line (your shell may be bash or PowerShell). It writes its log lines to the log file and prints the same lines on stdout; never re-log or summarize them elsewhere.

## Hard rules

- Never edit a request line, its checkbox, its block id or its feedback sub-items. Only `aj.py` writes those.
- Every request in the `begin` result gets exactly one `report` call, success or error. Then call `end` for every sweep that `begin` left open.
- Process requests one at a time, each in a fresh subagent, using your harness's subagent tool (omp and Copilot: `task`; Claude: `Agent`). Wait for each to finish before starting the next. Never run two at once.
- When in doubt, report an error in one sentence. Never guess between targets.
- Never put secrets into any note. Use the env var or CLI login a target section or the instruction names.

## Propagation

1. Start one sweep per journal:

   ```sh
   uv run <skill-dir>/scripts/aj.py begin
   uv run <skill-dir>/scripts/aj.py begin --journal main
   ```

   The first form covers all journals, the second one journal. After the log lines comes `== begin result ==` and a JSON array, one entry per journal:

   ```json
   [{"journal": "main", "sweep": "aj-sweep-3k9q", "ended": false,
     "vault": "/home/me/Notes/Main", "config_note": "/home/me/Notes/Main/Journal/Agentic Journaling.md",
     "targets": [{"name": "kb", "aliases": ["KB", "knowledge base", "wiki"]}],
     "requests": [{"run": "aj-run-x9k2", "block": "aj-block-7f3a", "file": "Journal/2026-11-21.md", "line": 14,
                   "instruction": "add the two Journal View items above to the KB"}]}]
   ```

   `begin` has already claimed every listed request (`[/]` plus block id) and given each a run id. An entry with `"ended": true` needs nothing more: it had no requests or failed to load (`error`).

2. For each request of each journal whose `ended` is false, start one subagent with the prompt below, filled in. Wait for it to finish before starting the next.

3. Turn the subagent's answer into one `report` call, with the entry's `journal` and `sweep` and the request's `run` and `block`:

   ```sh
   uv run <skill-dir>/scripts/aj.py report --journal main --sweep aj-sweep-3k9q --run aj-run-x9k2 --block aj-block-7f3a --target kb --change updated --link "KB/tools/obsidian-journal-view.md" --title "Obsidian Journal View" --summary "added the folder setting"
   uv run <skill-dir>/scripts/aj.py report --journal main --sweep aj-sweep-3k9q --run aj-run-x9k2 --block aj-block-7f3a --target kb --error "the KB folder does not exist"
   ```

   The first is a success, the second an error. Report an error also for an ambiguous target match and for a subagent that failed or returned nothing usable. Omit `--target` when no target matched. `--change` is `created` or `updated`; omit it when neither fits. If `report` exits non-zero, note it and continue with the next request.

4. After all requests of a journal are reported:

   ```sh
   uv run <skill-dir>/scripts/aj.py end --sweep aj-sweep-3k9q
   ```

5. Reply with one line per journal: sweep id, how many requests were done and how many failed.

## Subagent prompt

```text
You carry out one agentic-journaling request. Work only on this request.

Vault root: <vault>
Config note: <config_note>
Skill dir: <skill-dir>
Request in: <file> (vault-relative), line <line>, block ^<block>
Instruction: <instruction>

1. Read the "## Targets" section of the config note. Each "### <name>" section is a
   target, running to the next ### or ## heading; its "Aliases:" line lists other names.
2. Match the instruction against the target names and aliases. If more than one target
   matches, stop and answer ERROR naming them. If none matches, work without a target.
3. Read the request's note and find the request by its block id; the line number may be
   stale. Work out from the instruction which content it is about: parts of that note,
   other notes, or none. If the instruction does not say clearly enough which content to
   use or what to do, stop and answer ERROR naming what is unclear.
4. With a matched target, follow its section exactly. Without one, follow the instruction.
   Paths are relative to the vault root. Use only the credentials the target section or
   the instruction names (env var or CLI login).
5. Never edit the journal note's request line or the lines under it. Never write block ids
   or block references (^…, #^…) into what you create or update.
6. Rename or move vault files only with
   `uv run <skill-dir>/scripts/aj.py rename "<absolute file path>" "<new name>"`, which
   updates links through the obsidian CLI. If it exits non-zero, stop and answer ERROR
   with its message.

Answer with exactly these lines and nothing after them:
TARGET: <matched target name, or none>
RESULT: OK or ERROR
CHANGE: created, updated or none        (OK only)
LINK: <vault-relative path or URL>       (OK only)
TITLE: <link text>                       (OK only)
SUMMARY: <one sentence on what was done, naming the content used> (OK only)
ERROR: <one sentence on why it failed>   (ERROR only)
In SUMMARY and ERROR, wrap technical terms (file names, paths, commands, ids, target
names) in backticks.
```
