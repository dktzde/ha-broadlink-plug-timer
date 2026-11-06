# Broadlink Plug Timer

A Home Assistant integration that writes timers **directly into Broadlink SP3S smart plugs**, the way the Broadlink app does it, over the local network and without the cloud.

The plug runs these timers **by itself**: it keeps switching even when Home Assistant, Wi-Fi or the cloud are down. The main use is a **dead-man switch**. Before Home Assistant switches a load on, it writes a one-time "off" into the plug. If the regular "off" from Home Assistant never comes, because of a crash, a restart, a Wi-Fi outage or an aborted script, the plug switches itself off.

> This repository is mainly the maintainer's own backup of a working setup (a hot-water circulation pump). It is public so that the Home Assistant Marketplace can install it, and in case it helps others. Only one plug model has been tested; see [Open points](#open-points).

## What it does

| Action | What it does |
|---|---|
| `broadlink_plug_timer.set_deadman` | Writes a one-time "off" at *plug clock + minutes* and replaces the previous dead-man entry. |
| `broadlink_plug_timer.clear_deadman` | Removes the dead-man entry and expired one-time entries. |
| `broadlink_plug_timer.set_once` | Adds a one-time "on" plus "off" in local time. |
| `broadlink_plug_timer.write_weekly` | Replaces the weekly table (like the app's timer list). It writes only when the plug differs; `dry_run` only shows the result. |
| `broadlink_plug_timer.read` | Returns state, plug clock and all timers, in plug time and in local time (response only). |
| `broadlink_plug_timer.backup` / `restore` | Saves the raw timer memory, or writes the timer tables of a backup back. |

There are no entities. Each action names the plug by its **switch entity from the core [Broadlink integration](https://www.home-assistant.io/integrations/broadlink/)**. Host, MAC address and device type come from that integration, so there is nothing to enter twice.

Every action except `read` returns its result when called with `response_variable`; `read` always returns its result.

## Supported devices

| Device | Type | Status |
|---|---|---|
| Broadlink SP3S-EU | `0x947A` | tested on a real plug (November 2026) |
| Broadlink SP3S-US | `0x9479` | same firmware family, **not tested** |

Other Broadlink devices are refused with an error, because their memory layout is unknown. The layout was worked out for this plug; see [docs/protocol.md](docs/protocol.md).

## Installation

Requirement: the plug is already set up in the core **Broadlink** integration and has a switch entity.

### Home Assistant Marketplace (Home Assistant 2026.11 and later)

The Marketplace replaced HACS in Home Assistant 2026.11. This repository is not in its catalog, so add it as a custom repository:

1. Go to **Settings › Marketplace**.
2. In the upper-right corner, select **Add from link**.
3. Under **GitHub link**, enter `dktzde/ha-broadlink-plug-timer` (or the full URL `https://github.com/dktzde/ha-broadlink-plug-timer`) and select **Add**. If the Marketplace asks for the type, choose **Integration** and select **Add** again.
4. Adding a custom repository needs a GitHub connection. If none is set up yet, the Marketplace asks you to connect GitHub.
5. Open **Broadlink Plug Timer** in the Marketplace and install it. The latest release is offered by default.
6. **Restart Home Assistant.** A new integration is loaded only at start-up.
7. Go to **Settings › Devices & services › Add integration › Broadlink Plug Timer**. There is nothing to fill in; one entry serves all plugs.

A repository added earlier through HACS moves to the Marketplace automatically when Home Assistant is updated to 2026.11.

### HACS (before Home Assistant 2026.11)

1. **HACS › ⋮ (top right) › Custom repositories**.
2. Repository `https://github.com/dktzde/ha-broadlink-plug-timer`, type **Integration**, then **Add**.
3. Open **Broadlink Plug Timer**, select **Download**, restart Home Assistant, then add the integration as in step 7 above.

### Manual

Copy `custom_components/broadlink_plug_timer/` into `/config/custom_components/`, restart Home Assistant and add the integration as in step 7 above.

## Usage

### Dead-man switch before every run

Call `set_deadman` right before switching on. Set `minutes` to the planned run time plus a reserve. Do not wait for it to finish (`script.turn_on` of a helper script), so a plug that does not answer never delays the switch-on:

```yaml
- action: broadlink_plug_timer.set_deadman
  data:
    entity_id: switch.circulation_pump
    minutes: 8          # 2 min run + 6 min reserve
  response_variable: deadman
  continue_on_error: true
- action: switch.turn_on
  target:
    entity_id: switch.circulation_pump
```

In normal operation Home Assistant switches off after 2 minutes. The plug's "off" comes later and does nothing, because the plug is already off. If Home Assistant is gone, the plug switches off after 8 minutes. The response holds `off_local`, the time the plug will switch off at the latest, as an ISO timestamp.

[examples/circulation_pump.yaml](examples/circulation_pump.yaml) is a complete package. It has a helper script with a second attempt and a notification, a timestamp sensor "switches off at the latest", and the daily weekly-table sync.

### One-time switching

```yaml
action: broadlink_plug_timer.set_once
data:
  entity_id: switch.circulation_pump
  on_time: "2026-11-07 10:00:00"
  off_time: "2026-11-07 10:05:00"
```

The times are local times. They are right even after a daylight saving change. A local time that does not exist or exists twice on the day of a change (for example 02:30) is refused; give it with its UTC offset instead (`2026-10-25 02:30:00+01:00`).

### Weekly schedule

```yaml
action: broadlink_plug_timer.write_weekly
data:
  entity_id: switch.circulation_pump
  entries:
    - days: mon-fri          # or "sat,sun", "fri-mon", or a list [mon, wed]
      on_time: "06:30"
      off_time: "06:35"
    - days: [sat, sun]
      on_time: "08:00"
      off_time: "08:05"
      enabled: false         # kept in the plug, but switched off
  dry_run: true              # only show what would be written
```

- The list **replaces the plug's whole weekly table**. To keep entries made in the app, include them; `read` shows them in local time.
- **Every `on_time` needs an `off_time`.** The plug has no run-time limit of its own. An `off_time` earlier than the `on_time` belongs to the next day (`23:00`–`00:05` on `mon` switches off on Tuesday at 00:05).
- The plug has 16 slots. An entry whose "on" and "off" fall on different plug days takes 2 slots (see below).
- **Daylight saving time:** the plug clock runs on UTC+8 and never changes. A weekly time written in winter is one hour off in summer. Call `write_weekly` every day with the same entries, for example at 04:05. It writes only when something changed, so only after a daylight saving change or after an edit in the app.

### Command line

`plug.py` has no Home Assistant imports and also works on its own. It needs Python 3.11+ and `cryptography`:

```bash
python3 custom_components/broadlink_plug_timer/plug.py --host 192.0.2.10 --mac a1b2c3d4e5f6 --tz Europe/Berlin read
python3 custom_components/broadlink_plug_timer/plug.py --host 192.0.2.10 --mac a1b2c3d4e5f6 deadman 8
python3 custom_components/broadlink_plug_timer/plug.py --host 192.0.2.10 --mac a1b2c3d4e5f6 --backup-dir ./backups weekly plan.json --dry-run
```

Commands: `read`, `deadman MINUTES`, `clear-deadman`, `once ON OFF`, `weekly FILE [--dry-run]` (JSON `{"entries": [...]}`), `backup DIR`, `restore FILE`. The output is JSON. The exit code is 0 for ok and 1 for an error.

## How it works

- The plug keeps a 512-byte timer memory: 16 weekly entries, 8 one-time entries, name, clock and some reserved bytes. Command 1 reads it, command 3 writes the timer part back. Every change is *read → change → write → read back → compare*. The reserved bytes, which include the app's cycle timer, are written back unchanged.
- **Plug time is UTC+8**, the factory time zone set by the cloud. Weekly weekdays are the weekdays of the plug clock. The integration converts from and to the Home Assistant time zone. The dead-man is relative to the plug clock, so it does not depend on time zones.
- **A dead-man entry** is a one-time entry with an "off" and no "on". `set_once` always writes an "on", so dead-man updates never touch your own one-time entries. The plug keeps entries after they fire; expired entries are removed on the next write.
- `write_weekly`, `set_once` and `restore` first save the raw memory to `/config/broadlink_plug_timer/<mac>_<time>.bin`. `set_deadman` makes no backup, because it runs before every switch-on.
- The integration logs in to the plug on its own, next to the core Broadlink integration. Both work at the same time.

Details, the way the layout was found, and the test log are in [docs/protocol.md](docs/protocol.md).

## Limits and safety notes

- **Timers in the plug bypass Home Assistant.** A weekly or one-time "on" switches the load even if Home Assistant is down. That is the point, but nothing in Home Assistant watches those runs.
- There is no dead-man for switching outside Home Assistant (app, button on the plug). It only covers runs that call `set_deadman` first.
- Every `set_deadman` is one write to the plug's flash, so 10–20 a day for a circulation pump. How many writes the flash can take is unknown.
- If the plug clock is not set (year before 2020, for example after a power cut without internet), the dead-man is refused with an error. The plug sets its clock from the Broadlink cloud. If the clock is more than 2 minutes off UTC+8, `write_weekly` and `set_once` are refused, because local times would be wrong; the dead-man still works, as it is relative to the plug clock.
- The plug has 8 one-time slots. If they are all taken by pending entries, `set_deadman` is refused. It never drops a pending switch-off to make room.
- If you save timers in the Broadlink app, the app writes the whole table. The next `set_deadman` brings the dead-man back; the daily `write_weekly` resets the weekly table.
- Home Assistant polls the plug about once a minute, so it sees switching done by the plug itself late, or not at all if it was very short.

## Open points

- **Weekday bits:** bit 5 = Friday was proven on the plug (a mask with only Friday switched on a Friday). That bit 0 = Sunday … bit 6 = Saturday is derived from the natural order and from the clock's weekday byte (Friday = 5). It has not been checked on a Sunday. `read` shows `plug_clock_weekday_byte`; on a plug-time Sunday it should be 0. "Every day" schedules are not affected.
- **Daylight saving change** (the daily `write_weekly` moving the weekly times by one hour) is tested offline only. It will be checked for real on the next change.
- **SP3S-US (`0x9479`)** and other plug types are not tested. SP2 plugs probably use an older layout with 8-byte weekly entries (see docs/protocol.md) and are refused.
- **Flash endurance** under 10–20 writes a day is unknown.
- **Reserved bytes 0x1D0–0x1EF** (probably the app's cycle timer) are not decoded. They are written back 1:1.
- **No entities:** a sensor "plug switches off at the latest" is built in the example package from the action response, not by the integration.
- **Brand icon:** there is no entry in `home-assistant/brands`, so Home Assistant shows a placeholder icon.

## Credits

- Packet format, encryption and authentication as in [python-broadlink](https://github.com/mjg59/python-broadlink) (mjg59). The code here is a separate, minimal implementation without that dependency.
- Timer commands for the SP2/SP3 family, discussed in [python-broadlink issue #81](https://github.com/mjg59/python-broadlink/issues/81).
- Broadlink's own timer documentation: [tx.ibroadlink.com/public/openproxy/定时](https://tx.ibroadlink.com/public/openproxy/%E5%AE%9A%E6%97%B6/), [docs.ibroadlink.com app SDK](https://docs.ibroadlink.com/public/appsdk_en/appsdk_05/).
- The memory layout was found by analysing the old Broadlink network SDK (`libBLNetwork.so`, functions `sp2_refresh` and `sp2_task`) in [torinnguyen/broadlink-android](https://github.com/torinnguyen/broadlink-android), plus read and write tests on the plug. No code was copied.

## License

[MIT](LICENSE)
