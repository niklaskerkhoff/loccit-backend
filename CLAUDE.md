# loccit backend – notes for Claude Code

## Git: never merge into `main`

`main` changes only through pull requests that the owner merges on GitHub.

- Work on a branch of your own, push it, and open a pull request against
  `main` (`gh pr create` if `gh` is installed and logged in; otherwise give
  the owner the link to open one).
- Never run `git merge`, `git rebase`, `git push` or `git commit` on `main`,
  not even locally, not after an approval, and not for small fixes.
- Do not delete branches; the owner decides after merging.

## Conventions

- Talk to the owner in German. Code, comments, tests, docs and commit
  messages are in English; texts shown in the UI or printed by the CLI are
  German.
- Python runs in the conda env `loccit-backend`. Dependencies go into
  `requirements.txt` and that env; install nothing globally.
- Run `ruff format`, `ruff check` and `pytest` before committing.
- Everything loccit stores lives in `~/.loccit/`. Tests and manual tries use a
  temporary folder (for the server: `HOME=<temp dir> python -m loccit`), never
  the real one, and never the real system keychain.
