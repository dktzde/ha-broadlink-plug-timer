# Changelog

## 0.1.0
<sub>2026-11-06</sub>

First release.

- Actions `set_deadman`, `clear_deadman`, `set_once`, `write_weekly` (with `dry_run`), `read`, `backup` and `restore` for Broadlink SP3S plugs.
- The plug is selected by its switch entity from the core Broadlink integration.
- `plug.py` works on its own as a command line tool.
- Memory layout and test log in `docs/protocol.md`; example package in `examples/circulation_pump.yaml`.
