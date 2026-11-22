## What this note is

This note configures agentic journaling for this journal. A scheduled agent run scans the Daily Notes folder for requests, carries out each one against a target described under [[#Targets]], and writes the result back below the request.

The scripts rewrite the block between the two `agentic-journal:docs` comment markers. Everything else in this note is yours. Keep secrets out of it: a target names the env var or CLI login to use, never the secret itself.

## Writing a request

Write a task item with the agent tag as the last item of the content it covers, at the same list level. Name a target, or one of its aliases, and say what to do:

```markdown
- Obsidian Journal View shows daily notes as one rolling page
    - Settings: folder Journal, newest first
- It reads the Daily Notes folder setting
- [ ] #agent add this to the KB
```

The request covers the items above it at the same level, with their children. Collection stops at the parent item, a heading, a plain paragraph, or an earlier request at the same level. The parent item and the nearest heading are context, not content. A request with nothing above it at its level is an error.

## Status

| Checkbox | Status | Picked up by a run |
| --- | --- | --- |
| `[ ]` | open | yes |
| `[/]` | in progress | no |
| `[x]` | done | no |
| `[!]` | error | no |
| `[-]` | cancelled | no |

After a run, the request carries a block id and a feedback sub-item:

```markdown
- [x] #agent add this to the KB ^agent-7f3a
    - Updated [Obsidian Journal View](KB/tools/obsidian-journal-view.md): added the folder setting. Run agent-7f3a-x9k2.
```

Set a request back to `[ ]` to run it again; the next run keeps the block id, gets a new run id (`^agent-7f3a` → run `agent-7f3a-p4m1`) and replaces its feedback. A request left at `[/]` by an interrupted run stays there until you set it back to `[ ]`.

## Configured targets

{{targets}}

A target is a `### <name>` section under [[#Targets]], written in plain language like an agent instructions file: an aliases line, where the target lives, which spec, tools and credentials (env var or CLI login) to use, when to create and when to update, and what to return. Example:

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
