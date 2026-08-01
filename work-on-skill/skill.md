---
name: work-on
description: "Full issue workflow: verify clean main, pull, read issue, branch, plan (with tests), iterate on plan until approved, implement, validate (lint/typecheck/test), open PR. Usage: /work-on <issue-number>"
---

# Work On Issue

End-to-end workflow for picking up a GitHub issue, implementing it, and opening a PR.

## Invocation

```
/work-on <issue-number> [descriptor] [-- instructions]
```

Read the **leading integer** as the issue number, then split the remaining arguments on the
first `--` separator:

- **Before `--`** — an optional *descriptor*: a cosmetic scope/title hint (e.g. the addon or
  module the work touches). Launchers such as the artifact console's deep link append it purely
  to name the desktop session; it does **not** steer the work.
- **After `--`** — optional *instructions*: additional context or directives for this run.
  Honour them as real instructions that shape the plan (Step 5) and the implementation.

Examples:
- `/work-on 123 foo` — issue 123, descriptor `foo`, no instructions.
- `/work-on 123 -- and also include X` — issue 123, no descriptor, instruction "and also include X".
- `/work-on 123 bar -- but skip foo` — issue 123, descriptor `bar`, instruction "but skip foo".

Neither the descriptor nor the instructions ever feed the issue lookup or the naming mechanics:
the `gh` issue query, the branch name, and the PR title/number always derive from the issue
itself. Abort with a clear message only if no issue number is present.

## Step 1 — Verify clean main

Run:
```sh
git branch --show-current
```

If the current branch is not `main`, stop and tell the user:
> You're on branch `<branch>`. Switch to `main` before starting a new issue.

Do not proceed.

Run:
```sh
git status --short
```

If there are **uncommitted changes** (modified or deleted tracked files), stop and tell the user what's dirty. Do not proceed.

If there are **untracked files**, handle them before proceeding:

1. List the untracked files to the user.
2. Check the current `.gitignore` and look at the file names/paths to determine whether each file is a build artifact, tooling output, secret, or OS/editor noise that should be ignored rather than committed (e.g. `.env`, `node_modules/`, `dist/`, `.DS_Store`, `*.log`, `__pycache__/`).
3. If any untracked files clearly belong in `.gitignore`, add appropriate patterns to `.gitignore` and tell the user what you added.
4. Re-run `git status --short`. If the repo is now clean, continue. If untracked files remain (files that are legitimately new and should be committed, or whose disposition is unclear), stop and tell the user:
   > Untracked files remain. Either commit them, delete them, or add them to `.gitignore` before starting a new issue.

Do not proceed until `git status --short` shows a clean working tree.

## Step 2 — Update main

```sh
git pull --ff-only
```

If this fails (diverged or no remote), report it and stop. Do not force-pull or reset.

## Step 3 — Read the issue

```sh
gh issue view <issue-number> --json number,title,body,labels,assignees,milestone,url,comments
```

Display a compact summary:
- Issue number and title
- URL
- Labels and milestone (if any)
- Assignees (if any)
- Body (full text)

If the issue doesn't exist or `gh` errors, report and stop.

### Check the assignee before going any further

If `assignees` names anyone other than the current user, stop and tell the user:

> Issue #<number> is assigned to @<login>. I'm not starting work on an issue assigned to
> someone else — say so explicitly if you want me to proceed anyway.

Do not continue to the comments, do not plan, and do not create a branch. A collaborator who assigns themselves
has usually also commented with the shape they intend to build, so starting anyway duplicates
their effort and competes with a design they may already be coding against.

An **unassigned** issue is fair game. An issue assigned to the current user is fair game.
Resolve the current user with `gh api user --jq .login` if it is not already known.

This gate applies to *any* issue work reached through this skill — research and planning
included, not just implementation.

Read **every comment** on the issue before doing anything else — comments frequently carry
critical information that supersedes or refines the original body: scope changes, corrected
repro steps, decisions from discussion, or an explicit "actually, do X instead." Treat the issue
body plus its comment thread as a single source of truth, with later comments taking precedence
over the original body when they conflict.

Summarize any comments that materially affect scope, approach, or acceptance criteria (skip
comments that are just chatter, +1s, or bot noise). If a comment changes what the work should be,
call this out explicitly to the user before moving on — this must factor into the plan in Step 5.

## Step 4 — Create branch

Derive a slug from the issue title: lowercase, words joined by hyphens, max 40 chars, no special characters. Format:

```
<issue-number>-<slug>
```

Examples: `142-add-user-avatar`, `88-fix-login-redirect-loop`

Run:
```sh
git checkout -b <branch-name>
```

Confirm the branch name to the user.

## Step 5 — Implementation plan

Analyze the issue, **any instructions passed after `--` in the invocation** (see Invocation), and the relevant codebase to produce an implementation plan that satisfies both. If those instructions add to, narrow, or override the issue, honour them and call out where they diverge from the issue. (The pre-`--` descriptor is only a cosmetic title hint — it does not shape the plan.) The plan must:

- Break the work into numbered, concrete steps
- Call out which files will be created or modified
- Include a testing strategy — enumerate every behaviour that should be verified, then classify each as **automated** (will be covered by a new or updated test) or **manual** (genuinely cannot be automated). Default to automated; manual is only valid for things like visual rendering, hardware interaction, or third-party integration with no test double available
- Flag any ambiguities or assumptions that need user confirmation
- Note any risks or non-obvious side effects

Present the plan clearly. Do not start implementing.

## Step 6 — Plan approval loop

Ask the user: **"Does this plan look good, or do you have changes?"**

Loop:
- If the user requests changes, revise the plan and re-present it
- If the user approves (e.g. "yes", "looks good", "go ahead"), proceed to Step 7
- If the user says to abort, check out `main` and delete the branch

Do not begin implementation until explicitly approved.

## Step 7 — Implement

Carry out the approved plan step by step. Follow the implementation order in the plan. Write the automated tests identified in the plan alongside the code they cover — do not leave tests until the end. After all steps are complete, verify the implementation matches the plan.

Commit logically cohesive chunks as you go rather than one giant commit at the end. Each commit message must follow Conventional Commits (`<type>(<scope>): <description>`).

## Step 8 — Validate loop

Detect the project's check command in order of preference:
1. `just check` (if a Justfile exists with a `check` recipe)
2. `npm run check` / `pnpm run check` (if `package.json` has a `check` script)
3. Run lint, typecheck, and test commands individually if no unified check exists

Run the check command. If it fails:
- Read the output
- Fix the issues
- Re-run
- Repeat until the check passes with no errors

Do not open a PR until all checks pass.

## Step 8b — Verify clean working tree

Run:
```sh
git status --short
```

If there are uncommitted changes or untracked files:

1. For **modified/deleted tracked files**: commit them. If they're part of the implementation, add to the most appropriate existing commit scope (or a new commit if logically distinct). Follow Conventional Commits.
2. For **untracked files**: determine whether each belongs in `.gitignore` (build artifacts, tooling output, secrets, OS/editor noise) or should be committed as part of the implementation.
   - If it belongs in `.gitignore`, add the pattern and commit the `.gitignore` change.
   - If it should be committed, stage and commit it with a proper message.

Re-run `git status --short` and repeat until the working tree is clean. Do not open a PR with a dirty working tree.

## Step 9 — Open PR

```sh
gh pr create \
  --title "<issue-title>" \
  --body "$(cat <<'EOF'
Closes #<issue-number>

## Summary
<3-5 bullet points describing what changed and why>

## Test plan

**Automated** (covered by tests added in this PR — verified by CI):
<bulleted list of behaviours covered by new/updated tests>

**Manual** (not automatable):
<bulleted list of anything that genuinely cannot be automated, or "None" if everything is covered>


🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

PR title and all commit messages must follow Conventional Commits:

```
<type>(<optional scope>): <description>
```

Types: `build`, `chore`, `ci`, `docs`, `feat`, `fix`, `perf`, `refactor`, `revert`, `style`, `test`

Choose the type that best describes the change — use the issue and the diff to decide, not the issue title verbatim. Include a scope when the change is clearly contained to one module or area (e.g. `fix(auth): ...`). Omit scope when the change is broad.

## Step 10 — Present changes

Show the user:
- PR URL
- Branch name
- Files changed (from `git diff --name-only main`)
- A one-paragraph plain-English summary of what was implemented
