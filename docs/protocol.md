# Broadlink SP3S timer memory: protocol notes

How the plug stores its timers, how this was found out, and what was tested. The numbers come from an SP3S-EU (`0x947A`), checked in November 2026.

## How it was found

python-broadlink supports only on/off, night light and power for SP2/SP3S plugs, and nothing for timers. The old Broadlink network SDK `libBLNetwork.so` (2015, in the APK sources of [torinnguyen/broadlink-android](https://github.com/torinnguyen/broadlink-android)) has the functions `sp2_refresh` and `sp2_task`. Disassembling them showed:

- `sp2_refresh` sends command `1` and parses the timer memory.
- `sp2_task` sends command `3` with 332 bytes: the SP2 layout with 8 weekly entries of 8 bytes and 8 one-time entries of 24 bytes.

The SP3S answers command `1` with 512 bytes. It uses the same scheme, but with **12-byte weekly entries and 16 slots**. This was recognised by:

- the device name at `0xCC`;
- two old app timers on a second plug;
- write-and-read-back tests on the plug.

## Transport

Same as python-broadlink: UDP port 80, 0x38-byte header plus an AES-128-CBC encrypted payload.

| Header offset | Content |
|---|---|
| `0x00` | magic `5a a5 aa 55 5a a5 aa 55` |
| `0x20` | checksum of the whole packet (seed `0xBEAF`, the field itself counted as 0) |
| `0x22` | error code (responses; 0 = ok) |
| `0x24` | device type, e.g. `0x947A` |
| `0x26` | packet type: `0x65` authentication, `0x6A` command |
| `0x28` | packet counter |
| `0x2A` | MAC address, reversed |
| `0x30` | session id (from authentication) |
| `0x34` | checksum of the plain payload (seed `0xBEAF`) |
| `0x38` | encrypted payload, zero padded to 16 bytes |

Encryption uses the default key `097628343fe99e23765c1513accf8b02` and the IV `562e17996d093d28ddb3ba695a2e6f58`. After authentication (packet `0x65`) the plug returns a session id and a session key.

## Timer memory

Command `1` (payload `01 00 00 00`, padded) returns 512 bytes:

| Offset | Size | Content |
|---|---|---|
| `0x00` | 4 | echo of the command (`01`) |
| `0x04` | 4 | state: bit 0 relay on, bit 1 night light |
| `0x08` | 4 | number of weekly entries in use (u32, ≤ 16) |
| `0x0C` | 16 × 12 | weekly entries |
| `0xCC` | 63 | device name (UTF-8, zero padded) |
| `0x10B` | 1 | lock flag of the app |
| `0x10C` | 4 | number of one-time entries in use (u32, ≤ 8) |
| `0x110` | 8 × 24 | one-time entries |
| `0x1D0` | 32 | reserved. From `0x1D8` this is probably the app's cycle timer: on the test plug "07:59:59–12:09:59, 240/2400, every day, off". Never changed here; written back 1:1. |
| `0x1F0` | 8 | plug clock (timestamp, see below; read only) |

Command `3` writes `03 00 00 00` + memory bytes `0x08 … 0x1EF` (492 bytes; the state word is not part of it). The plug always takes the **complete tables**. Its answer echoes the command; the integration then reads the memory again and compares.

### Weekly entry (12 bytes)

| Byte | Content |
|---|---|
| 0 | enabled (1/0) |
| 1 | weekday mask: bit 0 Sunday, bit 1 Monday … bit 6 Saturday (days of the plug clock) |
| 2–3 | 0 |
| 4, 5 | on: hour, minute (25:61 = no "on") |
| 6, 7 | off: hour, minute (25:61 = no "off") |
| 8, 9 | on second, off second (the app writes 59) |
| 10–11 | 0 |

An entry may have only an "on" or only an "off". Each time is a separate switching event on the masked days.

### One-time entry (24 bytes)

| Bytes | Content |
|---|---|
| 0–7 | on: timestamp |
| 8–11 | on enabled (u32) |
| 12–19 | off: timestamp |
| 20–23 | off enabled (u32) |

The plug keeps an entry after it fired, still enabled.

### Timestamp (8 bytes)

Year (u16, little endian), second, minute, hour, weekday, day, month.

The weekday byte of the clock is 0 = Sunday … 6 = Saturday (Friday = 5 on the test day). The plug **ignores** the weekday byte in one-time entries: an entry with a wrong weekday byte still fired.

### Clock

The plug clock runs on **UTC+8**, the factory time zone set by the Broadlink cloud. It does not follow daylight saving time. All timer times and weekdays are in plug time.

- **Central Europe:** plug time = local time + 7 h in winter, + 6 h in summer.
- **Day change:** a local time from 17:00 (winter) or 18:00 (summer) is already the next day on the plug. That is why a weekly entry can need two slots.

## Tests on the real plug (5 November 2026, SP3S-EU)

All times are local times (CET) unless marked as plug time.

| Test | Result |
|---|---|
| Change the "on" of a disabled app entry from 10:14 to 10:15 (plug time) | taken over, no other byte changed |
| Weekly "on" without "off", one-time "off", weekly "on", weekly "off" without "on" (plug time 05:16–05:19) | on 05:16:01, off 05:17:02, on 05:18:01, off 05:19:02 |
| Weekday masks `0x0F`, `0x33`, `0x55` one after another on a plug-time Friday | only `0x33` switched, so Friday = bit 5 |
| Normal run through Home Assistant (2 min) | dead-man written (off at 22:41:56), Home Assistant switched off at 22:35:57 as usual |
| **Simulated Home Assistant failure:** 1-min run, the controlling scripts stopped 12 s after the start | the load ran; **the plug switched itself off at 22:43:59** (planned 22:43:58); Home Assistant noticed 45 s later |
| Weekly entry in local time through Home Assistant (Thu 22:47–22:48 = plug Fri 05:47–05:48) | on 22:46:59, off 22:47:59 |
| One-time "off" with a wrong weekday byte | fired anyway |
| One-time entry kept by a dead-man update, dead-man removed, tables restored from a backup | as expected; the plug was byte-identical to the original afterwards |
| Plug not reachable | error after 6 s |

The first version of this code was a stand-alone script. The integration uses the same protocol code. Its tests on the plug are recorded in STATUS.md.
