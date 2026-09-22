---
name: git-conflict-resolution
description: Systematic approach to resolving git merge conflicts
triggers: git merge conflict
category: devops
---

A step-by-step guide for resolving merge conflicts:
1. Run `git status` to identify which files have conflicts
2. Open conflicted files and find conflict markers (<<<<<<<, =======, >>>>>>>)
3. Edit the file to keep the version you want (or combine both)
4. Remove all conflict markers, save the file
5. Run `git add <filename>` to stage the resolved file
6. Test your code still works
7. Commit the resolution with `git commit` (git automatically uses the merge message)

Example: If main branch and feature-branch both modified the same function,
review which changes are newer/more correct, keep the best version, remove
conflict markers, stage and commit.