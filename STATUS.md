# STATUS

Read this first, and update it after every step, so the next session can continue without the chat history.

Last updated: 2026-11-06 (first version, PR #1)

## Current state

- **0.1.0 is in PR #1 (branch `initial-integration`), not merged yet.** CodeRabbit review requested; waiting for it.
- **On the device:** the maintainer's Home Assistant (2026.10.4) still runs the predecessor of this code since 2026-11-05 22:33: a stand-alone script with the same protocol code plus a YAML package, dead-man before every circulation pump run. The integration itself is **not installed** there yet, because a new integration needs a restart. The switch is planned together with the update to Home Assistant 2026.11.1.
- **Checked 2026-11-06 on the real plug (SP3S-EU `0x947A`) with the command line tool of this version:**
  - 11:23 `read`: identical to the predecessor script (weekly and one-time tables, reserved bytes, offset +7 h).
  - 11:23 `backup` worked.
  - 11:23 `weekly --dry-run`, with the plug's existing disabled app entry given in local time (03:14:59–07:19:59): `changed: false`. This shows that the conversion reproduces the app's bytes exactly.
  - 11:23 `deadman`, `once` (kept by the next `deadman`), `clear-deadman` (keeps `once`) and `restore` all worked; afterwards bytes 0x08–0x1EF were identical to the backup.
  - **11:24 end-to-end dead-man with a simulated Home Assistant failure:**
    - The pump was started through the maintainer's pump script (1 min); the controlling scripts were stopped 4 s later.
    - `deadman 1` of this version replaced the old dead-man entry (off at 11:25:38).
    - **The plug switched itself off at 11:25:39**; Home Assistant saw it at 11:26:16 (poll). Afterwards `clear-deadman` was run.
- **Checked in a Home Assistant test instance (pytest harness, HA 2026.10.4) against the real plug:** 11:26. The actions `read`, `backup`, `set_deadman`, `write_weekly` (dry run, `changed: false`), `clear_deadman` and `restore` worked; the plug looked the same as before afterwards. The test file is not in the repository, because it contains the real address.
- **Not checked yet:**
  - installation through the Marketplace or HACS on a real Home Assistant, and the config flow there;
  - the CI run on GitHub (hassfest and HACS validation could not be run locally);
  - a daylight saving change;
  - a Sunday weekday mask;
  - SP3S-US.
- Offline tests: 67 passed (`python -m pytest -q`); `ruff check` and `ruff format --check` are clean.

## Next steps

1. Read CodeRabbit's comments on PR #1. Address them or explain in the PR why not, then ask the maintainer about the merge.
2. After the merge: GitHub release `v0.1.0`, then check that HACS (and the Marketplace after the 2026.11 update) offers it.
3. Maintainer's setup, together with the restart for Home Assistant 2026.11.1:
   - install the integration and add it;
   - point the YAML package at `broadlink_plug_timer.set_deadman` and stop using the shell script;
   - run one real pump run and one simulated failure, and record the results here.
4. On a plug-time Sunday (Saturday 17:00 – Sunday 17:00 CET), check that `read` shows `plug_clock_weekday_byte` 0.

## Known issues and backlog

- See README › Open points (weekday bits, daylight saving change, other models, flash endurance, reserved bytes, no entities, brand icon).
- Possible later: a sensor entity per plug ("switches off at the latest"), storing the weekly plan in the integration with a built-in daily sync, an options flow.

## Decisions and conventions

- **Own protocol code, no python-broadlink dependency and no use of the core Broadlink integration's internals.** Only the config entry data (`host`, `mac`, `type`) of the switch entity's Broadlink entry is read. Those keys have been stable for years; the internals of the core integration are not.
- **Actions only, no entities** (version 0.1.0). The example package builds the timestamp sensor from the action response.
- **Every switch-on needs a switch-off** (weekly and one-time). The plug has no run-time limit, and this integration exists for safety.
- **Dead-man = one-time entry with "off" and no "on".** `set_once` always writes both, so dead-man updates never remove user entries.
- **Backups before** `write_weekly`, `set_once` and `restore` (to `/config/broadlink_plug_timer/`). There is no backup before `set_deadman`, because it runs before every switch-on.
- Only SP3S device types (`0x947A`, `0x9479`) are accepted. Other types are refused, because their layout is unknown.
- `write_weekly` replaces the whole table and writes only when something changed. It is meant to be called daily, because of daylight saving time.
- Repository texts are English; private data appears only as placeholders.

## Key files

| File | What |
|---|---|
| `custom_components/broadlink_plug_timer/plug.py` | protocol, memory layout, pure `with_*` changes, `write_*` operations, command line tool |
| `custom_components/broadlink_plug_timer/__init__.py` | action schemas and handlers, plug lookup through the entity registry |
| `custom_components/broadlink_plug_timer/services.yaml`, `translations/en.json` | action fields, names and descriptions |
| `docs/protocol.md` | memory layout, how it was found, test log of 2026-11-05 |
| `examples/circulation_pump.yaml` | complete example package |
| `tests/common.py` | memory builder and fake plug used by all tests |
