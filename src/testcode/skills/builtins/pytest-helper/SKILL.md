---
name: pytest-helper
description: A focused pytest workflow with on-demand project-aware test execution.
triggers:
  - "run tests"
  - "python test"
  - "pytest"
version: 1.3.0
---

# Pytest Helper Skill

Use this toolbox as a focused test loop:

1. Discover the project contract.
   - Read the nearest project instructions and pytest configuration before selecting a command.
   - Prefer `run_tests` without an explicit command when project detection can choose the environment and
     working directory safely.
2. Reproduce narrowly.
   - Start with the failing test or smallest affected node selection.
   - Read the failure, test, and production boundary before editing; fix the cause rather than weakening
     assertions merely to make the suite green.
3. Write durable tests.
   - Prefer plain pytest assertions, fixtures with narrow scope, and deterministic inputs.
   - Avoid sleeps, uncontrolled network access, real credentials, and order-dependent global state.
   - Patch dependencies where they are looked up and assert externally meaningful behavior rather than
     implementation trivia.
4. Expand verification.
   - After the focused case passes, use `run_tests` for the broader affected suite, then the full suite when
     proportional to the change.
   - Treat timeout, collection error, and environment failure as distinct from an assertion failure.
5. Report evidence.
   - State the exact test scope, pass/fail/timeout result, duration when available, and anything not run.
