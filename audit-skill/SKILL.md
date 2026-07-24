---
name: audit
description: "Audit a project for compliance with project standards — files AND GitHub repository settings. Use this skill whenever the user asks to audit, check, or validate a project against standards — including phrases like 'does this project comply', 'check project standards', 'what's missing', 'run the audit', or '/audit'."
---

# Project Standards Audit

Systematically check a project against all standards and produce a structured compliance report.

## Invocation

- `/audit` — audit the current working directory
- `/audit <path>` — audit the project at the given path

## Audit Process

### Step 1 — Resolve the target

If a path was given, use it. Otherwise use the current working directory. State the project being audited at the top of your report.

Detect the GitHub repo identity by running:
```sh
git -C <path> remote get-url origin
```
Parse `owner/repo` from the URL. If no remote exists, skip all GitHub settings checks and note it in the report.

### Step 2 — Check each area

#### README.md

- File exists
- Has project name and one-sentence description
- Lists prerequisites (runtime versions, required environment variables)
- Has a quickstart command (`just dev` or equivalent)
- Links to `docs/PURPOSE.md`
- States a license

#### docs/PURPOSE.md

- File exists
- Has a "problem being solved" section (1–3 paragraphs)
- Explicitly lists non-goals
- States the intended audience or users

#### .gitignore

- File exists
- Covers language/runtime artifacts relevant to this project's stack (build output, `__pycache__`, `node_modules`, `target/`, etc.) — check what languages the project uses (package.json, go.mod, Cargo.toml, etc.) and verify relevant entries are present
- Ignores `.env` and `.env.*` variants
- Ignores IDE directories (`.idea/`, `.vscode/`)
- Ignores OS files (`.DS_Store`, `Thumbs.db`)
- Ignores local-only config (e.g., `settings.local.json`)

#### Justfile

- File exists
- Has a header comment `# <project-name> — <one-line description>`
- `default` recipe is first and uses exactly `@just --list`
- Has all required recipes: `install`, `check`, `lint`, `fix`, `typecheck`, `test`, `clean`, `fresh`
- For runnable apps (not libraries): also has `run` and `dev`
- For containerized apps: also has `docker-build`, `docker-run`, `docker-push`
- For deployable apps: also has `deploy` (and `deploy-staging` if staging exists)
- `check` depends on `lint typecheck test` (may omit `test` only if no tests exist)
- `lint` is read-only; `fix` is write-mode — not a single combined recipe
- No hyphens in recipe names (`typecheck` not `type-check`)
- `fresh` (not `reinstall` or `reset`) depends on `clean install`
- Each recipe has a comment on the line immediately above it (no blank line between comment and recipe)
- One blank line between recipes
- No `set` declarations or variables unless clearly needed

#### .github/workflows/ci.yml

- File exists (check for any `.github/workflows/*.yml` if the exact name differs)
- Triggers on `pull_request` events (PR open and update) — this is the critical gate
- Also triggers on `push` to `main`
- Has at least two jobs covering lint/typecheck and tests
- All action versions are pinned to a specific tag (not `@latest`)
- `actions/checkout` is v6 or newer
- Has a multi-platform or multi-version test matrix where the runtime warrants it

#### GitHub repository settings

Fetch base settings and social preview in one batch:
```sh
gh api repos/{owner}/{repo}
gh api graphql -f query='{ repository(owner: "{owner}", name: "{repo}") { usesCustomOpenGraphImage } }'
```

Check:

| Source | Field | Expected |
|--------|-------|----------|
| REST | `allow_merge_commit` | `false` |
| REST | `allow_rebase_merge` | `false` |
| REST | `squash_merge_commit_title` | `"PR_TITLE"` |
| REST | `allow_update_branch` | `true` |
| REST | `delete_branch_on_merge` | `true` |
| GraphQL | `usesCustomOpenGraphImage` | `true` |

#### Labels

**This skill holds no label list.** The org-wide issue-label standard is owned by
`roshne/Tooling` ([Tooling#149](https://github.com/roshne/Tooling/issues/149)) in its
`labels.json`, and a second copy here is exactly the drift that file exists to prevent.
Ask the owning script instead — it is read-only in this mode:

```sh
python R:/repos/Tooling/sync_labels.py --repo {owner}/{repo} --dry-run --json
```

The whole of stdout is one JSON object. Read `results[0]`:

| Field | Meaning |
|-------|---------|
| `missing` | standard labels the repo doesn't have |
| `changed` | labels whose color/description has drifted from the standard |
| `forbidden` | labels the standard **rejects** that this repo carries, in the repo's own spelling |
| `ok` / `error` | `false` plus a message if the repo's labels couldn't be read |

Check:
- `missing` is empty
- `changed` is empty
- `forbidden` is empty

Report the names verbatim from those lists — do not restate them as a required-label
checklist, and never assert what the standard *should* contain. If the Tooling checkout
isn't at `R:\repos\Tooling` (or Python/`gh` fails), report Labels as `N/A` with the reason.
**Do not** fall back to a hardcoded list: a wrong list is worse than no check, because
acting on one renames or creates labels that contradict the real standard.

### Step 3 — Produce the report

Use this exact format:

```
## Project Standards Audit: <project-name>
Audited: <absolute path>

### README.md                    [PASS | FAIL | MISSING]
- OK   Has project name and description
- FAIL Missing prerequisites section
- OK   Quickstart command present (just dev)
- FAIL No link to docs/PURPOSE.md
- OK   License stated (MIT)

### docs/PURPOSE.md              [PASS | FAIL | MISSING]
...

### .gitignore                   [PASS | FAIL | MISSING]
...

### Justfile                     [PASS | FAIL | MISSING]
...

### CI workflow                  [PASS | FAIL | MISSING]
...

### GitHub settings              [PASS | FAIL | N/A]
- OK   No merge commits
- FAIL No rebase merging (currently enabled)
- OK   Commit message = PR title
- OK   Suggest branch updates
- FAIL Auto-delete head branches (disabled)
- FAIL Social preview image (not set)

### Labels                       [PASS | FAIL | N/A]
- FAIL Missing: effort: XS, effort: S
- FAIL Forbidden present: good first issue, help wanted
- OK   No color/description drift

---
Summary: X/7 areas passing
Critical gaps: <one-line list of the most important missing things, or "none">
```

Each item is `OK` or `FAIL`. Section header is `PASS` if all items OK, `FAIL` if any fail, `MISSING` if the file doesn't exist, and `N/A` if the check couldn't run at all — no GitHub remote was detected, or, for Labels specifically, no Tooling checkout was available to ask. Always state which.

### Step 4 — Offer to fix

After the report, ask: "Would you like me to fix any of these issues?"

If the user says yes (or gives a specific list), apply fixes:

**File-based gaps** — create missing files from scratch or edit existing ones to add missing content. If something requires human input (e.g., the actual purpose statement), scaffold a template with `<!-- TODO: fill in -->`.

**GitHub settings** — apply with a single PATCH:
```sh
gh api repos/{owner}/{repo} \
  --method PATCH \
  --field allow_merge_commit=false \
  --field allow_rebase_merge=false \
  --field squash_merge_commit_title=PR_TITLE \
  --field allow_update_branch=true \
  --field delete_branch_on_merge=true
```

**Labels** — re-run the owning script without `--dry-run`. It creates what's missing and aligns
what drifted, additively: it has no delete path and no rename path, so nothing already tagged on
an issue is disturbed.
```sh
python R:/repos/Tooling/sync_labels.py --repo {owner}/{repo}
```

**Never rename a label as a "fix."** A rename is a `PATCH` that rewrites the label on every issue
and PR already carrying it, and the next `sync_labels.py` sweep re-creates the original alongside
— which is how this skill used to leave a repo holding two competing taxonomies
([Tooling#149](https://github.com/roshne/Tooling/issues/149)). If a repo's label looks "wrong,"
the standard is what's right; change `labels.json` in Tooling, not the repo.

Forbidden labels are the one thing the script won't do for you, precisely because it has no
delete path. Delete only the names its `forbidden` list actually reported — one call per name,
taken verbatim from that list (it echoes the repo's own spelling) and URL-encoded, spaces as
`%20`. Do not type the names from memory; the standard, not this file, decides which they are:
```sh
gh api "repos/{owner}/{repo}/labels/{url-encoded-name-from-forbidden}" --method DELETE
```
Deleting a label strips it from every issue and PR carrying it, so confirm with the user before
running these, even inside an already-approved fix pass.

**Manual only:**
- Social preview — must be uploaded via GitHub Settings → Social preview

After fixing, re-audit only the changed areas and confirm they now pass.

## Judgment calls

- If a README has a prerequisites section but it's vague (e.g., "Node.js" with no version), mark it FAIL with a note.
- For .gitignore, look at what languages/tools the project actually uses and only flag entries relevant to the project.
- For CI: if the workflow file has a different name, still check it. If there are multiple workflow files, audit the most likely main CI gate.
- For Justfile app-type classification: look at whether the project has a start script, server code, or deployment config — if yes, treat it as an app.
- Labels are the one area with **no** judgment calls: report exactly what `sync_labels.py --repo ... --dry-run --json` returns, and nothing else. Color/description drift *is* reported (as `changed`) and *is* safe to align, because aligning changes no label's name.
- If the `main` branch doesn't exist, try `master` for branch protection. If neither, skip that check.
