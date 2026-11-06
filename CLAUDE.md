# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**Read [STATUS.md](STATUS.md) first** (current state, next steps, decisions) and update it after every step.

## Project

- Home Assistant custom integration `broadlink_plug_timer`: actions that write timers (dead-man "off", one-time, weekly) directly into Broadlink SP3S plugs. No entities.
- `custom_components/broadlink_plug_timer/plug.py` = protocol + memory layout. **No Home Assistant imports**; it must stay usable as a command line tool (`python3 plug.py --help`). Pure functions `with_*` change a memory image; `write_*`/`read_report` talk to the plug.
- `__init__.py` = action schemas and handlers. Plug I/O runs in the executor, one lock per plug. Host, MAC and device type come from the config entry of the core Broadlink integration behind the given switch entity.
- Memory layout, how it was found, and the test log: `docs/protocol.md`. Change it there when the layout knowledge changes.
- Safety rules of the design: every switch-on needs a switch-off (weekly and one-time); bytes `0x1D0-0x1EF`, name and lock are written back unchanged; a one-time entry with "off" and no "on" is the dead-man.

## Tests

- `pip install -r requirements_test.txt`, then `python -m pytest -q` (offline: fake plug in `tests/common.py`; never talk to a real plug in tests).
- Lint and format: `ruff check . && ruff format --check .` (settings in `pyproject.toml`).
- A test on the real plug is done by hand with the command line tool (see README) and recorded in STATUS.md with date and time. Never put real IP or MAC addresses into the repository.

## Git, commits and pull requests

These rules apply to every session, also to Claude Code on the web.

- **Author:** commits and PRs must look as if the maintainer made them alone. Before the first commit run `git config user.name "dktzde"` and `git config user.email "dktzde@users.noreply.github.com"`. No `Co-Authored-By`, no "Generated with Claude" in commits, PR texts, issues, changelog or README.
- **Branches:** no `claude/` prefix where the environment allows it (Claude Code on the web may force `claude/…`; that is accepted).
- **One topic = one branch = one PR**, also for documentation. Open PRs with `gh pr create --repo dktzde/ha-broadlink-plug-timer --base main`. One commit per sub-topic, test after each commit. Plan the commit split before the first push. Never force-push a pushed branch; fix mistakes with `git revert`.
- **CodeRabbit:** this repository has fewer than 10 stars, so CodeRabbit does not review PRs automatically. Right after opening a PR that changes code (Python under `custom_components/`, translations, `manifest.json`, `services.yaml`, tests, workflows), comment `@coderabbitai review` (`gh pr comment <number> --repo dktzde/ha-broadlink-plug-timer --body "@coderabbitai review"`). Skip documentation-only PRs (`docs:`), drafts and pure version bumps without code changes. Before the merge, read the bot's comments and either address them or explain in the PR why not. Settings are in `.coderabbit.yaml`.
- **Merge** only when the maintainer explicitly asks, always with a merge commit (no squash), so every commit can be reverted on its own.
- **Versions:** bump only for user-facing changes and only on `main`; documentation-only changes get no new version. Change the version in `manifest.json` and `CHANGELOG.md` at the same time, then create a GitHub release with the tag `vX.Y.Z` (the Marketplace offers the latest release).
- **Changelog:** every version gets its date as `<sub>YYYY-MM-DD</sub>` under its heading (the day it landed on `main`).
- **Language:** everything in the repository is English (code, comments, docs, commits, PR and issue texts). Commit messages in the imperative, e.g. `feat(scope): …`, `fix: …`, `docs: …`, `chore: …`, `ci: …`, `test: …`.
- **No secrets or private data** (tokens, passwords, IP addresses, MAC addresses, serial numbers, entity IDs of the maintainer's setup) in the repository; use placeholders like `192.0.2.10`, `a1b2c3d4e5f6`, `switch.circulation_pump`.
- **README:** a credit line for Claude Code only after asking the maintainer.
- **Handover:** keep `STATUS.md` with the current state, next steps, known issues, decisions and key files. Record what runs on the device with date and time, what was checked and what not. Update it after every step.
