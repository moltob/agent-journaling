---
name: agentic-journaling
description: Run a propagation over Obsidian journals - find `#agent` task requests in daily notes, carry each out against a target defined in the journal's config note, and write status and result back. Use when asked to run, check or set up agentic journaling.
---

# Agentic journaling

Scripts do the mechanics: configuration, discovery, scope, ids, status, feedback, compare-and-swap writes and logging. You do only what needs judgment: matching a request to a target and carrying out the target's instructions.

The script is `scripts/aj.py` in this skill's directory. Run it with `uv run <skill-dir>/scripts/aj.py <command>`, each command on one line (your shell may be bash or PowerShell). It writes its log lines to the log file and prints the same lines on stdout; never re-log or summarize them elsewhere.

## Hard rules

- Never edit a request line, its checkbox, its block id or its feedback sub-items. Only `aj.py` writes those.
- Every request in the `begin` result gets exactly one `report` call, success or error. Then call `end` for every sweep that `begin` left open.
- Process requests one at a time, each in a fresh subagent, using your harness's subagent tool (omp and Copilot: `task`; Claude: `Agent`). Wait for each to finish before starting the next. Never run two at once.
- When in doubt, report an error in one sentence. Never guess a target.
- Never put secrets into any note. Use the env var or CLI login a target section names.

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
                   "instruction": "add this to the KB", "heading": "## Notes",
                   "parent": null, "content": "- Obsidian Journal View ...\n    - Settings: ..."}]}]
   ```

   `begin` has already claimed every listed request (`[/]` plus block id) and given each a run id. `content` is what the script collected above the request; it may be empty. An entry with `"ended": true` needs nothing more: it had no requests or failed to load (`error`).

2. For each request of each journal whose `ended` is false, start one subagent with the prompt below, filled in. Wait for it to finish before starting the next.

3. Turn the subagent's answer into one `report` call, with the entry's `journal` and `sweep` and the request's `run` and `block`:

   ```sh
   uv run <skill-dir>/scripts/aj.py report --journal main --sweep aj-sweep-3k9q --run aj-run-x9k2 --block aj-block-7f3a --target kb --change updated --link "KB/tools/obsidian-journal-view.md" --title "Obsidian Journal View" --summary "added the folder setting"
   uv run <skill-dir>/scripts/aj.py report --journal main --sweep aj-sweep-3k9q --run aj-run-x9k2 --block aj-block-7f3a --target kb --error "the KB folder does not exist"
   ```

   The first is a success, the second an error. Report an error also for no or ambiguous target match and for a subagent that failed or returned nothing usable. Omit `--target` when no target matched. `--change` is `created` or `updated`; omit it when neither fits. If `report` exits non-zero, note it and continue with the next request.

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
Request in: <file>
Instruction: <instruction>
Context heading: <heading>
Context parent item: <parent>
Content above the request (may be empty):
<content>

1. Read the "## Targets" section of the config note. Each "### <name>" section is a
   target, running to the next ### or ## heading; its "Aliases:" line lists other names.
2. Match the instruction against the target names and aliases. If no target or more than
   one target matches, stop and answer ERROR naming why.
3. Decide whether the instruction refers to the content above ("this", "the notes above",
   "the process described above") or stands alone. If it refers to it, work from that
   content. If it stands alone, ignore the content and work from the instruction. If it
   refers to content that is empty or does not fit, or it stands alone but does not say
   enough to act on, stop and answer ERROR naming why.
4. Follow the matched target's section exactly. Paths in it are relative to the vault root.
   Use only the credentials it names (env var or CLI login).
5. Never edit the journal note's request line or the lines under it. Never write block ids
   or block references (^…, #^…) into what you create or update.

Answer with exactly these lines and nothing after them:
TARGET: <matched target name, or none>
RESULT: OK or ERROR
CHANGE: created, updated or none        (OK only)
LINK: <vault-relative path or URL>       (OK only)
TITLE: <link text>                       (OK only)
SUMMARY: <one sentence on what was done> (OK only)
ERROR: <one sentence on why it failed>   (ERROR only)
```

## Other commands

- `aj.py check [--journal main]`: read-only. Validates the machine file and config notes, shows the log file, lists targets, shows whether the docs block would be re-rendered, previews each open request with its scope, and lists requests stuck at `[/]`.
- `aj.py rename "<absolute file path>" "<new name>"`: renames a vault file through the obsidian CLI so links are updated. Precondition: the file is inside the vault root reported by `obsidian vault`. It lists backlinks before and after the rename and exits 1 listing any that were lost. Use it for requests to rename a vault file; never rename with plain file operations.
- `aj.py init --name main "<journal folder>"`: registers a journal in the machine file and creates `Agentic Journaling.md` with an empty `## Targets` section in that folder.
