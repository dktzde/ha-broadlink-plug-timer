# STATUS

Read this first, and update it after every step, so the next session can continue without the chat history.

Last updated: 2026-11-06 (0.1.0 released and running on the device)

## Current state

- **0.1.0 released on 2026-11-06** (GitHub release `v0.1.0`; PR #1 merged with a merge commit).
  - CodeRabbit reviewed it twice. The first review: 13 comments and 3 nitpicks; 14 fixed, 2 answered (each verdict is in its thread). The second review, of the fixes only: no findings.
- **On the device since 2026-11-06 12:59 (maintainer's Home Assistant 2026.10.4):**
  - installed through HACS as a custom repository at 12:47; the files are identical to the release;
  - restart at 12:57, integration added through the config flow;
  - the maintainer's circulation pump package now calls `set_deadman` before every run; the old stand-alone script is removed.
- **Checked 2026-11-06:**
  - 12:58 `read` through Home Assistant.
  - **12:59 normal 2-minute run:** the dead-man was written 1 s after the start (off at +8 min, first attempt), the plug was on, and Home Assistant switched off after 2 min as usual.
  - The checks of the code before the release (real plug, simulated Home Assistant failure at 11:24: the plug switched itself off 1 s after the planned time) are in the PR #1 description and below.
- **Before the release, checked 2026-11-06 on the real plug (SP3S-EU `0x947A`) with the command line tool:**
  - `read` identical to the predecessor script;
  - `weekly --dry-run` with the existing app entry: `changed: false`;
  - `deadman`, `once`, `clear-deadman`, `restore`: byte-identical afterwards;
  - after the review fixes (12:05/12:15): `read` with the sender/port check, `weekly --dry-run` unchanged, a non-existent local time refused.
- **Not checked yet:**
  - installation through the Marketplace (Home Assistant 2026.11);
  - a daylight saving change with `write_weekly` in use (the maintainer does not manage weekly timers yet);
  - a Sunday weekday mask;
  - SP3S-US.
- **Offline tests:** 84 passed; `ruff check` and `ruff format --check` are clean; CI (hassfest, HACS, tests) is green.

## Next steps

1. After the maintainer's update to Home Assistant 2026.11.x: check that the Marketplace took the repository over from HACS and that the integration still loads and sets the dead-man.
2. On a plug-time Sunday (Saturday 17:00 – Sunday 17:00 CET), check that `read` shows `plug_clock_weekday_byte` 0.

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
- An `off_time` earlier than the `on_time` in a weekly entry means the next day (overnight). Equal times are refused.
- `write_weekly` and `set_once` are refused when the plug clock deviates from UTC+8 by more than 120 s (measured directly, not via the rounded offset). The dead-man is not affected, because it is relative to the plug clock.
- `set_deadman` is refused when pending one-time entries fill all 8 slots. It never drops a pending switch-off.
- Naive local times that do not exist, or exist twice, at a daylight saving change are refused; times with a UTC offset are accepted.
- Only replies from the plug's address and port 80 are accepted.
- The example does not wait for the dead-man before switching on: a plug that does not answer must not block the run. The helper script warns instead. Raised by CodeRabbit; kept on purpose.
- The minimum Home Assistant version is 2025.1.0. All APIs used exist since 2024.x; there is no CI job for that version.
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
