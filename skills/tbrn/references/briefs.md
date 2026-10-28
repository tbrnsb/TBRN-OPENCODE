# Brief templates

A child session starts with no history. Everything it needs goes in the brief.

## Skeleton

```
GOAL: one paragraph. What done looks like, not the backstory.
ACCEPTANCE: observable outcomes and the exact commands that prove them.
CONTEXT: decisions from earlier roles, in one or two lines.
FILES: paths to read, paths to change, paths not to touch.
CONSTRAINTS: conventions, limits, scope boundaries.
LOAD: the process skill to load before working.
RETURN: what to report, in what shape.
```

Rules:

- No conversation history. No "as discussed".
- Exact paths, not "the auth file".
- Say what not to touch when the blast radius matters.
- Demand evidence: commands run and their output, not a summary of intent.
- One role per brief. If a task needs two roles, send two briefs.

## Roles

### explorer
Use when the area is unfamiliar and a wrong edit would be expensive.
Load: nothing.
Brief: the question, the directories to search, the depth (quick / medium / thorough).
Return: file map, entry points, data flow, constraints found, open questions. No edits.

### planner
Use when the work has more than two steps or touches more than two files.
Load: `writing-plans`.
Brief: the agreed outcome, the files known so far, the constraints, where to save the
plan (`docs/superpowers/plans/` in a repo).
Return: ordered tasks, files per task, acceptance per task, risks, open questions.
The planner does not write product code.

### coder
Use for implementation, refactors, and fixes whose root cause is known.
Load: `test-driven-development` for features and fixes; `receiving-code-review` when
acting on review feedback.
Brief: goal, acceptance commands, files, constraints, the test command, and the
existing conventions to follow.
Return: files changed, tests run with output, deviations from the brief, open questions.
The coder does not expand scope.

### tester
Use after every implementation, before a critic.
Load: `verification-before-completion`.
Brief: what changed, the commands to run (tests, lint, typecheck, build), and the
acceptance criteria.
Return: each command, its exit code, the relevant output, and pass/fail. Test files may
be edited; product code may not.

### debugger
Use for any bug, crash, or failing test before a fix is proposed.
Load: `systematic-debugging`.
Brief: the symptom, the reproduction command, the last known good state, and the files
involved.
Return: reproduction, root cause with evidence (file:line and the mechanism), the
smallest fix, and what would regress. No guess without evidence.

### critic
Use after implementation and testing, or when a score is required.
Load: `requesting-code-review`.
Brief: the diff or the files, the acceptance criteria, and the author models.
Return: score out of 10, findings with file:line, what would raise the score. Read-only.
Refuse if the critic shares a model family with any author model.

### designer
Use when the visual design is open, or a screen needs a spec before implementation.
Load: nothing.
Brief: the screen or flow, the platform, the user goal, existing design tokens, and the
states that must be covered (empty, loading, error, success).
Return: layout, spacing, type scale, colour, component states, accessibility notes.
No implementation code.

### copywriter
Use for README, changelog, docs, in-app strings, and any user-facing prose.
Load: `stop-slop`.
Brief: audience, voice, what the text must accomplish, length limit, files to edit, and
the facts that must not change.
Return: the edited files, a one-line summary of each change, and any claim that needs
verification. No code.

### code-reviewer
Use when the user wants an independent review of existing work.
Load: `requesting-code-review`.
Brief: the diff or commit range, the requirements, and the author models.
Return: regressions, architecture violations, missing requirements, unsafe behaviour,
maintainability problems, each with file:line. Read-only.

## Automatic roles

`archiver`, `security-reviewer`, `final-verifier`, `ui-reviewer` and `general` use the
same skeleton. Their role prompt already states the return shape:

- `archiver`: conventional commit, secret scan, no force-push.
- `security-reviewer`: severity-ranked findings with file:line.
- `final-verifier`: IMPLEMENTED / VERIFIED / NOT VERIFIED / REMAINING ISSUES.
- `ui-reviewer`: screen-by-screen deltas against the spec.
- `general`: the task result plus evidence.
