---
name: git-helper
description: A safe Git inspection and change-delivery workflow with reusable core inspection and on-demand history.
triggers:
  - "git commit"
  - "git push"
  - "commit changes"
version: 1.2.0
---

# Git Helper Skill

Use this toolbox as a staged Git workflow:

1. Orient safely.
   - Read the nearest repository instructions before choosing a branch, commit, or pull-request workflow.
   - Use `git_status` to establish the branch and distinguish task changes from unrelated user work.
2. Inspect evidence.
   - Use `git_diff` for working-tree changes and `git_show` for committed history or `revision:path`.
   - Inspect only the paths relevant to the task; never infer ownership of unrelated changes.
3. Validate delivery scope.
   - Keep commits intentional and exclude secrets, generated artifacts, and unrelated files.
   - Run verification appropriate to the changed area before committing and report anything not run.
4. Perform mutations only when authorized.
   - The workbench deliberately exposes only read-only Git tools. Branch creation, staging, commit, push, PR,
     merge, history rewrite, and deletion remain explicit shell actions governed by normal approval rules.
   - Never rewrite shared history or discard work unless the user explicitly requested it and the exact
     target was verified.
   - Do not push, open a pull request, merge, or delete a branch unless the user requested that external
     state change.
5. Hand off clearly.
   - Report the branch, commit identifier when created, verification result, and remaining local changes.
