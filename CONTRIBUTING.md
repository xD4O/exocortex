# Contributing to Exocortex

Thanks for your interest. Exocortex is a small, opinionated codebase — these
notes will get you set up and patching in the same direction the rest of the
project moves.

## Ground rules

- **`core/` and `coordination/` import no adapter-specific code.** Provider
  quirks live in `agents/bridge/*` (how a specific agent binary speaks) or
  `agents/runner/*` (how a specific in-process model is driven).
- **`Bridges` and `Runners` are different interfaces.** Don't unify them.
- **Every `MemoryRecord` write carries provenance** (`source`, `confidence`,
  `timestamp`, `scope`). There is no "quick write" path.
- **Every `ToolInvocation` passes through policy before execution.** Policy is
  middleware, not optional.
- **Workspace isolation is `git worktree add`,** not optimistic conflict
  detection after the fact.
- **Every contract has `schema_version`.** Additive changes only within a
  major version; breaking changes need a documented migration.
- **Event bus + memory store are append-only and fully timestamped.** That
  invariant is what makes `precog trace` reconstructible.

If a change you're proposing touches one of these rules, open an issue first
so we can talk it through before you write code.

## Dev environment

We use [`uv`](https://github.com/astral-sh/uv) for everything. From the repo
root:

```bash
uv sync --all-extras            # install runtime + dev deps (pytest, ruff, mypy)
uv run pytest                   # run the full suite
uv run pytest tests/unit -x     # fast-fail unit tests
uv run pytest -k <name>         # filter by test name
uv run ruff check .             # lint
uv run ruff format .            # format
uv run mypy src                 # type check
uv run precog --help            # CLI smoke test
```

`pip install -e ".[dev]"` works as a fallback if `uv` is unavailable.

### Real-binary integration tests

Most tests run hermetically. The few that exec real agent binaries are gated
behind environment flags so they never run accidentally:

- `EXOCORTEX_RUN_HERMES=1` — exec the local `hermes` binary.
- `EXOCORTEX_RUN_CODEX=1` — exec the local `codex` binary.

CI runs the gated tests; local development normally doesn't need to.

## Submitting a change

1. **Open an issue first** for anything beyond a small, obvious fix —
   especially if it touches the load-bearing rules above. A 5-minute
   conversation saves a 50-minute back-and-forth on review.
2. **Branch from `main`.** Use a descriptive branch name (`fix/codex-cwd`,
   `feature/dispatch-batch`, etc.).
3. **Write a test.** Bug fixes should have a regression test that fails before
   your change and passes after. Features should have unit + (where
   relevant) contract or e2e tests.
4. **Run the full check list before pushing:**
   ```bash
   uv run pytest
   uv run ruff check .
   uv run ruff format --check .
   uv run mypy src
   ```
5. **Open a PR.** Describe the *why* in the body, not just the *what*. The
   diff already shows the what.

## Filing bugs

Useful bug reports include:

- What you ran (exact command).
- What you expected.
- What happened, with relevant log output. The audit log
  (`data/audit.jsonl`) is usually the first place to look — paste the
  surrounding events, redacted as needed.
- Your `uv --version`, `python --version`, OS, and any non-default env vars
  from `.env`.

## Memory rules of thumb

When you're modifying memory, retrieval, or conversation code:

- **Provenance on every write.** `confidence` defaults to `OBSERVED` for
  agents, `ASSERTED` for operator-authored facts.
- **Scope matters.** Use `scope="task"` for a unit-of-work, `scope="user"` for
  facts about the operator (USER scope), `scope="project"` for repo-wide
  decisions.
- **Soft-delete via audit events.** Don't mutate the durable store in place.
  Hide records by appending a delete event and filtering on read.

## Style

Ruff handles formatting + most lint. A few things ruff doesn't enforce:

- Avoid abbreviations in identifiers unless the abbreviation is canonical
  (`cfg`, `id`, `db`, `ts`).
- Comments should explain *why*, not *what*. The code shows the what.
- Docstrings on public functions; module-level docstring on every file in
  `src/exocortex/`.

## Code of Conduct

Be kind, be specific, be concise. Disagree on technical merits, not on
people. If you see a problem, raise it; if you can't raise it directly,
email the maintainers.

Thanks for contributing.
