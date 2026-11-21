# Agentic Journaling Skill Specification

Nov 21, 2026 · @Mike

## Scope

The skill finds agent requests in an Obsidian journal, carries out each one against a named target, writes status and result back to the request, and logs every step. Any agent harness with file access, `rg` and `uv` can run it. No running Obsidian app is required.

The skill has two parts:

- **Generic mechanics**, done by Python scripts shipped with the skill: configuration loading, discovery, scope, ids, state, feedback, writes and logging.
- **Target steps**, done by the agent following the target's instruction section in the config note.

## Configuration

Configuration lives in two files. Every setting exists in exactly one of them; neither overrides the other.

### Machine file

Path: `$XDG_CONFIG_HOME/agentic-journal/config.yaml` (default `~/.config/agentic-journal/config.yaml`). It is read first. If it is missing, the run stops and points to `init`.

```yaml
version: 1
journals:
  main: ~/Notes/Main/Journal/Agentic Journaling.md
```

| Key | Default | Meaning |
| --- | --- | --- |
| `version` | `1` | schema version |
| `journals.<name>` | required | path to that journal's config note; `<name>` is logged as journal= on run-start |

### Config note

One Markdown note per journal, at the path the machine file names. Convention: `Agentic Journaling.md` at the top of the journal folder.

```markdown
---
agentic-journal: 1
---
# Agentic Journaling

%% agentic-journal:docs start (maintained by the agent, edits here are replaced) %%
## What this note is

...
%% agentic-journal:docs end %%

## Targets

### <name>

Aliases: ...

<instructions>
```

1. Frontmatter holds exactly one flat property, `agentic-journal: 1`. It marks the note as the config and gives the schema version.
2. The note starts with an H1 heading.
3. A docs block between the two `%% agentic-journal:docs … %%` markers follows. A script renders it from a template shipped with the skill. It explains the note and the request markup, lists the target names and links to `## Targets`. The script rewrites only this block, and only when the rendered text changes.
4. `## Targets` follows, with one `### <name>` section per target. Scripts never write into it.
5. Every heading the scripts generate is followed by a blank line.
6. The note holds no transient state: no counters, timestamps, run ids or run results. Links are allowed.
7. Secrets never appear in the note. A target section names only the env var or CLI login to use.
8. `#agent` examples appear only inside code blocks. Discovery skips the config note.

### Derived values

- **Vault root:** the nearest parent folder of the config note that contains `.obsidian/`.
- **Journal folder:** the Daily Notes folder from `.obsidian/daily-notes.json`. Only this folder is scanned.

## Journal markup

A request is a task list item tagged `#agent`, written as the last line of the content it covers:

```markdown
- Obsidian Journal View shows daily notes as one rolling page
    - Settings: folder Journal, newest first
- It reads the Daily Notes folder setting
- [ ] #agent add this to the KB
```

### Syntax

- The line is a task item: `- [<status>] #agent <instruction in plain words>`. The checkbox is required.
- The instruction names the target by its name or an alias, and may say what to do.
- `#agent` inside fenced code, inline code or `%% %%` comments is ignored.

### Scope

The request covers the items above it at the same list level, with their children. Collection stops at the first of:

- the start of the parent item,
- a heading or a non-list paragraph,
- an earlier `#agent` request at the same level.

The parent item and the nearest heading are context, not content. The request line and its sub-items are never content. A request with no sibling items above it is an error.

### Feedback

The skill writes feedback as indented sub-items of the request, in plain sentences, without inline fields. Each names the result link or the error and the run id. On a retry, the skill replaces its earlier feedback sub-items.

```markdown
- [x] #agent add this to the KB ^agent-7f3a
    - Updated [Obsidian Journal View](KB/tools/obsidian-journal-view.md): added the folder setting. Run r7k2.
```

The `#agent` tag stays on the line after processing.

## Identity and state

State lives in the journal, on the request line. No external state store exists.

- **Identity:** a block id `^agent-xxxx` (4 characters) at the end of the request line. The skill reuses an existing id or assigns a new one.
- **Status:** the checkbox character.

| Checkbox | Status | Discovered |
| --- | --- | --- |
| `[ ]` | open | yes |
| `[/]` | in progress | no |
| `[x]` | done | no |
| `[!]` | error | no |
| `[-]` | cancelled | no |

Only `[ ]` requests are discovered, so setting a request back to `[ ]` makes the next run process it again.

## Targets

A target is a `### <name>` section under `## Targets` in the config note. The heading text is the target's name. The section runs to the next `###` or `##` heading. There are no target types and no target code in the skill. A new target is a new section.

The section is written by the user, in plain language, like an agent instructions file. It holds:

- an aliases line, e.g. `Aliases: KB, knowledge base, wiki`,
- where the target lives,
- which spec, tools or helper scripts to use, and which env var or CLI login for credentials,
- when to create and when to update,
- what to return.

### Matching

The agent matches a request's instruction against target names and the aliases in each section. No match, or more than one, sets the request to `[!]` with a feedback sentence saying why.

### Target step contract

The agent carries out the request by following the target's section. It returns either:

- a result link and a one-line summary, or
- an error in one sentence.

The scripts write that result to the journal and the log.

### Example

The skill ships no target templates. The generated docs show this example section inside a code block:

```markdown
### kb

Aliases: KB, knowledge base, wiki

The knowledge base is the folder `KB/` in this vault. It is an OKF bundle and follows the Open Knowledge Format v0.2 spec. Every `.md` file in it is a concept, except `index.md` and `log.md`.

- Update an existing concept when it covers the same subject. Otherwise create one.
- New concept: file name from the title, frontmatter `type`, `title`, `description`, `tags`.
- Add a `sources` entry with `id` = the request's block id, linking back to the journal block.
- Use relative Markdown links.
- Update the folder's `index.md` and `log.md`.

Return the concept's vault path, created or updated, and one sentence on what was added.
```

## Propagation run

A run processes every journal in the machine file, or one with `--journal <name>`. Per journal:

1. Load the machine file and the journal's config note; collect the target sections.
2. Derive the vault root and the journal folder.
3. Start the run: write `run-start`.
4. Re-render the config note's docs block if its text changed.
5. Discover open requests: `rg` finds candidate lines in the journal folder, then a Markdown pass drops matches in code or comments and computes each request's scope. Files modified in the last 30 s are skipped.
6. For each request, in one write: reuse or assign `^agent-xxxx` and set `[/]`. Log `discover`. If the write fails, log `skipped` with file and line; the request stays `[ ]` for the next run.
7. Match the target. On a scope error or a failed match, set `[!]` with feedback and go to the next request.
8. Carry out the target step per the target's section. Log `action`.
9. Write the result: `[x]` with feedback on success, `[!]` with feedback on error. Log `state`.
10. Write `run-end`.

### Writes

Every write to a vault file is a compare-and-swap:

1. Write a temp file `.<note>.md.agent-tmp` in the same folder.
2. Re-check the original's mtime, size and SHA-256 against what was read.
3. Unchanged: rename the temp file over the original atomically. Changed: re-read, re-locate the request by its block id, re-apply, and retry, up to 3 times with backoff from 0.5 s, doubling.
4. Re-read 3 s after writing to verify the change is still there.

Writes use direct file access, never the Obsidian app or CLI.

## Run log

- **Output:** stdout only; the skill writes no log files and has no log settings. Under the systemd service, journald stores and rotates the lines. A manual run prints them to the terminal.
- **Format:** one line per event: an ISO 8601 timestamp, then logfmt `key=value` pairs. Values with spaces are quoted.
- **Required keys:** `run=<run id>` and `ev=<event>` on every line; `id=agent-xxxx` on every line about a request; journal=\<name> on run-start.

| `ev` | Written when | Extra keys |
| --- | --- | --- |
| `run-start` | a run starts for a journal |  |
| `discover` | a request got its id and `[/]` | source file, request text |
| `action` | the target step finished | target, result link or error |
| `state` | the final status was written | new status |
| `error` | a step failed | message |
| `skipped` | a request could not be claimed | file, line |
| `run-end` | the run ends | duration, `found`, `completed`, `failed`, `skipped` |

```
2026-11-21T14:03:12+01:00 run=r7k2 ev=discover id=agent-7f3a file="Journal/2026-11-21.md" text="add this to the KB"
```

- `journalctl --user -u agentic-journal -g agent-7f3a` returns one request's full history; `-g run=<id>` returns one run.
- `run-end` counts only this run's transitions: `found = completed + failed + skipped`. A run with no requests writes only a `run-end found=0` line.
- **Retention:** journald's own settings. The skill does no retention.

## Scripts

Generic mechanics are deterministic Python scripts shipped with the skill. Each declares its dependencies inline (PEP 723) and runs with `uv run`, or `uvx` once packaged. `rg` is taken from `PATH`. The agent does only what needs judgment: matching targets and carrying out target steps.

| Done by scripts | Done by the agent |
| --- | --- |
| loading and validating configuration | matching an instruction to a target |
| rendering the config note's docs block | following the target's section |
| discovery and scope | returning result link and summary, or an error |
| id assignment and status changes |  |
| feedback sub-items |  |
| compare-and-swap writes |  |
| log lines on stdout |  |

Commands besides the run itself:

- `init`: creates the machine file and a config note with an empty `## Targets` section in a given journal folder.
- `check`: validates both files and prints what a run would use. It reports a missing `## Targets` section, an empty target section, and duplicate target names or aliases.

Constants fixed in the scripts: the `#agent` tag, the checkbox characters, the `^agent-` id shape, the CAS timings, the log fields.

## Deployment

- **Repository:** the skill, its scripts and the systemd units live in one Git repository on GitHub. A checkout on the machine that holds the vault is the installation. Updating is `git pull`.
- **Skill loading:** the skill sits in the repository's repo-local skill directory. A harness started with the checkout as working directory loads it; there is no install step.
- **Units** in `deploy/`, linked into `~/.config/systemd/user/` and enabled with `systemctl --user enable --now agentic-journal.timer`:
  - `agentic-journal.timer`: `OnCalendar=<schedule>`, `Persistent=true` (a run missed during sleep starts on wake).
  - `agentic-journal.service`: `Type=oneshot`, `WorkingDirectory=<checkout>`, `ExecStart=omp -p "<run prompt>"`.
- **User:** runs execute as the user under the systemd user manager. `loginctl enable-linger <user>` lets runs happen while the user is logged out. The service sets `PATH` and any variables the harness needs, because it does not read shell profiles.
- **Logs:** stdout and stderr go to journald (see Run log).
- **Manual run:** the same `omp -p "<run prompt>"` from the checkout; log lines print to the terminal.
