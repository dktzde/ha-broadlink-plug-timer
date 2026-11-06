"""Tests of plug.py without a device: memory layout, time conversion, changes, packets, CLI."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
from zoneinfo import ZoneInfo

import pytest

from custom_components.broadlink_plug_timer import plug

from .common import HOST, LEGACY_APP_ENTRY, MAC, FakeDevice, make_memory, plug_now

BERLIN = ZoneInfo("Europe/Berlin")
# Winter: 22:33:57 CET = 21:33:57 UTC = 05:33:57 next day on the plug (UTC+8)
WINTER_NOW = datetime(2026, 11, 5, 22, 33, 57, tzinfo=BERLIN)
WINTER_CLOCK = datetime(2026, 11, 6, 5, 33, 57)
# Summer: 12:00 CEST = 10:00 UTC = 18:00 on the plug
SUMMER_NOW = datetime(2026, 7, 1, 12, 0, tzinfo=BERLIN)
SUMMER_CLOCK = datetime(2026, 7, 1, 18, 0)


# -- weekdays and masks -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("days", "mask"),
    [
        (None, 0x7F),
        ("mon-sun", 0x7F),
        ("mon-fri", 0x3E),
        ("sat,sun", 0x41),
        ("Sat, Sun", 0x41),
        (["fri"], 0x20),  # Friday = bit 5, found on the real plug
        ("fri-mon", 0x63),  # range across the end of the week
        (["monday", "wednesday"], 0x0A),
    ],
)
def test_parse_days(days, mask) -> None:
    assert plug.parse_days(days) == mask


@pytest.mark.parametrize("days", ["", "xyz", "mon-", "mon-tue-wed", []])
def test_parse_days_invalid(days) -> None:
    with pytest.raises(plug.PlugInputError):
        plug.parse_days(days)


def test_mask_to_days_starts_monday() -> None:
    assert plug.mask_to_days(0x41) == ["sat", "sun"]
    assert plug.mask_to_days(0x7F) == ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def test_rotate_mask() -> None:
    assert plug.rotate_mask(0x40, 1) == 0x01  # Saturday + 1 day = Sunday
    assert plug.rotate_mask(0x01, -1) == 0x40  # Sunday - 1 day = Saturday
    assert plug.rotate_mask(0x7F, 3) == 0x7F


@pytest.mark.parametrize(("text", "parts"), [("06:30", (6, 30, 0)), ("23:59:59", (23, 59, 59)), (" 7:05 ", (7, 5, 0))])
def test_parse_time(text, parts) -> None:
    assert plug.parse_time(text) == parts


@pytest.mark.parametrize("text", ["24:00", "12:60", "12", "a:b", "12:00:61"])
def test_parse_time_invalid(text) -> None:
    with pytest.raises(plug.PlugInputError):
        plug.parse_time(text)


# -- timestamps and tables -------------------------------------------------------------------------------


def test_timestamp_round_trip() -> None:
    moment = datetime(2026, 11, 8, 10, 15, 30)  # a Sunday
    raw = plug.encode_timestamp(moment)
    assert raw[5] == 0  # weekday byte: Sunday = 0
    assert plug.decode_timestamp(raw) == moment
    assert plug.decode_timestamp(bytes(8)) is None


def test_weekly_entry_layout() -> None:
    raw = plug.encode_weekly(True, 0x3E, (13, 30, 0), None)
    assert raw == bytes([1, 0x3E, 0, 0, 13, 30, 25, 61, 0, 0, 0, 0])


def test_legacy_app_entry_is_read() -> None:
    memory = make_memory(WINTER_CLOCK, weekly=[LEGACY_APP_ENTRY])
    [entry] = plug.weekly_entries(memory)
    assert entry["enabled"] is False
    assert entry["days"] == list(plug.DAYS_FROM_MONDAY)
    assert (entry["on_time"], entry["off_time"]) == ("10:14:59", "14:19:59")
    offset, _ = plug.local_offset(memory, WINTER_NOW)
    [local] = plug.weekly_in_local_time(memory, offset)
    assert (local["on_time"], local["off_time"]) == ("03:14:59", "07:19:59")


def test_too_many_entries() -> None:
    memory = make_memory(WINTER_CLOCK)
    with pytest.raises(plug.PlugInputError):
        plug.set_weekly_table(memory, [plug.encode_weekly(True, 1, (1, 0, 0), (2, 0, 0))] * 17)
    with pytest.raises(plug.PlugInputError):
        plug.set_once_table(memory, [plug.encode_once(None, WINTER_CLOCK)] * 9)


def test_check_memory_refuses_unknown_layout() -> None:
    memory = make_memory(WINTER_CLOCK)
    memory[plug.OFS_WEEKLY_COUNT] = 40
    with pytest.raises(plug.PlugError):
        plug.check_memory(memory)
    with pytest.raises(plug.PlugError):
        plug.check_memory(bytes(100))


# -- time offsets ------------------------------------------------------------------------------------------


def test_offsets_winter_and_summer() -> None:
    offset, deviation = plug.local_offset(make_memory(WINTER_CLOCK), WINTER_NOW)
    assert offset == timedelta(hours=7)
    assert deviation == 0
    assert plug.local_offset(make_memory(SUMMER_CLOCK), SUMMER_NOW)[0] == timedelta(hours=6)
    assert plug.utc_offset(make_memory(WINTER_CLOCK), WINTER_NOW) == timedelta(hours=8)
    assert plug.utc_offset(make_memory(SUMMER_CLOCK), SUMMER_NOW) == timedelta(hours=8)


def test_offset_rounds_clock_drift() -> None:
    memory = make_memory(WINTER_CLOCK + timedelta(seconds=40))
    offset, deviation = plug.local_offset(memory, WINTER_NOW)
    assert offset == timedelta(hours=7)
    assert deviation == 40


def test_clock_not_set() -> None:
    memory = make_memory(datetime(2000, 1, 1))
    with pytest.raises(plug.PlugError):
        plug.with_deadman(memory, 8, WINTER_NOW)
    assert "clock_error" in plug.report(memory, WINTER_NOW)


# -- weekly conversion -------------------------------------------------------------------------------------


def test_weekly_to_plug_same_day() -> None:
    [raw] = plug.weekly_to_plug([{"days": "mon-fri", "on_time": "06:30", "off_time": "06:35"}], timedelta(hours=7))
    assert raw == plug.encode_weekly(True, 0x3E, (13, 30, 0), (13, 35, 0))


def test_weekly_to_plug_next_plug_day() -> None:
    # 17:00 CET = 00:00 next day on the plug: Saturday/Sunday become Sunday/Monday
    [raw] = plug.weekly_to_plug([{"days": "sat,sun", "on_time": "17:00", "off_time": "17:05"}], timedelta(hours=7))
    assert raw == plug.encode_weekly(True, 0x03, (0, 0, 0), (0, 5, 0))


def test_weekly_to_plug_split_across_plug_midnight() -> None:
    raws = plug.weekly_to_plug([{"days": ["fri"], "on_time": "16:50", "off_time": "17:10"}], timedelta(hours=7))
    assert raws == [
        plug.encode_weekly(True, 0x20, (23, 50, 0), None),  # Friday 23:50
        plug.encode_weekly(True, 0x40, None, (0, 10, 0)),  # Saturday 00:10
    ]


def test_weekly_overnight_entry() -> None:
    # Monday 23:00 on, 00:05 off = Tuesday morning; CET: plug Tuesday 06:00-07:05
    [raw] = plug.weekly_to_plug([{"days": "mon", "on_time": "23:00", "off_time": "00:05"}], timedelta(hours=7))
    assert raw == plug.encode_weekly(True, 0x04, (6, 0, 0), (7, 5, 0))
    # Without offset the off moves to the next plug day: split into two entries
    raws = plug.weekly_to_plug([{"days": "sat", "on_time": "23:00", "off_time": "00:05"}], timedelta(0))
    assert raws == [
        plug.encode_weekly(True, 0x40, (23, 0, 0), None),  # Saturday 23:00
        plug.encode_weekly(True, 0x01, None, (0, 5, 0)),  # Sunday 00:05
    ]


def test_weekly_on_equals_off() -> None:
    with pytest.raises(plug.PlugInputError, match="same"):
        plug.weekly_to_plug([{"on_time": "06:30", "off_time": "06:30"}], timedelta(hours=7))


def test_weekly_and_once_refused_with_clock_drift() -> None:
    memory = make_memory(WINTER_CLOCK + timedelta(seconds=200))
    with pytest.raises(plug.PlugError, match="clock"):
        plug.with_weekly(memory, [{"on_time": "06:30", "off_time": "06:35"}], WINTER_NOW)
    on = datetime(2026, 11, 7, 10, 0, tzinfo=BERLIN)
    with pytest.raises(plug.PlugError, match="clock"):
        plug.with_once(memory, on, on + timedelta(minutes=5), WINTER_NOW)
    plug.with_deadman(memory, 8, WINTER_NOW)  # relative to the plug clock: drift does not matter


def test_weekly_needs_off_for_every_on() -> None:
    with pytest.raises(plug.PlugInputError, match="off_time"):
        plug.weekly_to_plug([{"on_time": "06:30"}], timedelta(hours=7))
    with pytest.raises(plug.PlugInputError):
        plug.weekly_to_plug([{"days": "mon"}], timedelta(hours=7))
    # an "off" alone is fine
    assert len(plug.weekly_to_plug([{"off_time": "06:30"}], timedelta(hours=7))) == 1


def test_with_weekly_unchanged_and_changed() -> None:
    memory = make_memory(WINTER_CLOCK, weekly=[LEGACY_APP_ENTRY])
    same = [{"enabled": False, "days": "mon-sun", "on_time": "03:14:59", "off_time": "07:19:59"}]
    new, info = plug.with_weekly(memory, same, WINTER_NOW)
    assert info["changed"] is False
    assert new == memory

    new, info = plug.with_weekly(
        memory, [*same, {"days": "mon-fri", "on_time": "06:30", "off_time": "06:35"}], WINTER_NOW
    )
    assert info["changed"] is True
    assert info["offset_hours"] == 7
    assert info["weekly_local_time"][1] == {
        "index": 1,
        "enabled": True,
        "on_time": "06:30",
        "on_days": ["mon", "tue", "wed", "thu", "fri"],
        "off_time": "06:35",
        "off_days": ["mon", "tue", "wed", "thu", "fri"],
    }
    assert new[plug.OFS_NAME :] == memory[plug.OFS_NAME :]  # name, one-time table, reserved, clock untouched


def test_weekly_summer_offset() -> None:
    memory = make_memory(SUMMER_CLOCK)
    new, _ = plug.with_weekly(memory, [{"days": "mon-fri", "on_time": "06:30", "off_time": "06:35"}], SUMMER_NOW)
    assert plug.weekly_entries(new)[0]["on_time"] == "12:30"


# -- dead-man -------------------------------------------------------------------------------------------------


def test_deadman_set_replace_and_clear() -> None:
    user_entry = plug.encode_once(WINTER_CLOCK + timedelta(hours=5), WINTER_CLOCK + timedelta(hours=5, minutes=5))
    expired = plug.encode_once(WINTER_CLOCK - timedelta(hours=2), WINTER_CLOCK - timedelta(hours=1))
    old_deadman = plug.encode_once(None, WINTER_CLOCK + timedelta(minutes=3))
    memory = make_memory(WINTER_CLOCK, once=[expired, old_deadman, user_entry])

    new, info = plug.with_deadman(memory, 8, WINTER_NOW)
    entries = plug.once_entries(new)
    assert [entry["deadman"] for entry in entries] == [False, True]  # expired + old dead-man gone
    assert entries[0]["raw"] == user_entry.hex()
    assert entries[1]["off"] == "2026-11-06 05:41:57"
    assert entries[1]["on_enabled"] is False
    assert info["off_local"] == "2026-11-05T22:41:57+01:00"
    assert new[: plug.OFS_ONCE_COUNT] == memory[: plug.OFS_ONCE_COUNT]  # weekly table untouched
    assert new[plug.OFS_RESERVED :] == memory[plug.OFS_RESERVED :]

    cleared, info = plug.with_deadman(new, None, WINTER_NOW)
    assert [entry["raw"] for entry in plug.once_entries(cleared)] == [user_entry.hex()]
    assert "off_local" not in info


def test_deadman_fits_into_full_table() -> None:
    future = [
        plug.encode_once(WINTER_CLOCK + timedelta(hours=h), WINTER_CLOCK + timedelta(hours=h, minutes=1))
        for h in range(1, 9)
    ]
    new, _ = plug.with_deadman(make_memory(WINTER_CLOCK, once=future), 8, WINTER_NOW)
    entries = plug.once_entries(new)
    assert len(entries) == plug.ONCE_MAX
    assert entries[-1]["deadman"] is True


@pytest.mark.parametrize("minutes", [0, 0.5, 1441])
def test_deadman_minutes_range(minutes) -> None:
    with pytest.raises(plug.PlugInputError):
        plug.with_deadman(make_memory(WINTER_CLOCK), minutes, WINTER_NOW)


# -- one-time ----------------------------------------------------------------------------------------------


def test_once_winter() -> None:
    on, off = datetime(2026, 11, 7, 10, 0, tzinfo=BERLIN), datetime(2026, 11, 7, 10, 5, tzinfo=BERLIN)
    new, info = plug.with_once(make_memory(WINTER_CLOCK), on, off, WINTER_NOW)
    assert info == {"on_plug_time": "2026-11-07 17:00:00", "off_plug_time": "2026-11-07 17:05:00"}
    [entry] = plug.once_entries(new)
    assert entry["on_enabled"] and entry["off_enabled"] and not entry["deadman"]
    local = plug.report(new, WINTER_NOW)["once_local_time"][0]
    assert local["on"] == "2026-11-07T10:00:00+01:00"


def test_once_across_daylight_saving_change() -> None:
    now = datetime(2027, 3, 20, 12, 0, tzinfo=BERLIN)  # CET, plug 19:00
    memory = make_memory(datetime(2027, 3, 20, 19, 0))
    on, off = datetime(2027, 4, 1, 10, 0, tzinfo=BERLIN), datetime(2027, 4, 1, 10, 5, tzinfo=BERLIN)  # CEST
    _, info = plug.with_once(memory, on, off, now)
    assert info["on_plug_time"] == "2027-04-01 16:00:00"


def test_once_is_kept_by_deadman() -> None:
    on, off = datetime(2026, 11, 7, 10, 0, tzinfo=BERLIN), datetime(2026, 11, 7, 10, 5, tzinfo=BERLIN)
    memory, _ = plug.with_once(make_memory(WINTER_CLOCK), on, off, WINTER_NOW)
    memory, _ = plug.with_deadman(memory, 8, WINTER_NOW)
    assert [entry["deadman"] for entry in plug.once_entries(memory)] == [False, True]


def test_once_invalid() -> None:
    memory = make_memory(WINTER_CLOCK)
    past = datetime(2026, 11, 5, 20, 0, tzinfo=BERLIN)
    later = datetime(2026, 11, 7, 10, 0, tzinfo=BERLIN)
    with pytest.raises(plug.PlugInputError, match="future"):
        plug.with_once(memory, past, later, WINTER_NOW)
    with pytest.raises(plug.PlugInputError, match="after"):
        plug.with_once(memory, later, later, WINTER_NOW)
    with pytest.raises(plug.PlugInputError, match="switch-off"):
        plug.with_once(memory, later, None, WINTER_NOW)
    with pytest.raises(plug.PlugInputError, match="aware"):
        plug.with_once(memory, later.replace(tzinfo=None), later.replace(tzinfo=None), WINTER_NOW)
    full = make_memory(
        WINTER_CLOCK, once=[plug.encode_once(WINTER_CLOCK + timedelta(days=2), WINTER_CLOCK + timedelta(days=3))] * 8
    )
    with pytest.raises(plug.PlugInputError, match="already"):
        plug.with_once(full, later, later + timedelta(minutes=5), WINTER_NOW)


# -- restore and report ------------------------------------------------------------------------------------


def test_restore_copies_only_timer_tables() -> None:
    backup = make_memory(
        WINTER_CLOCK, name="Old name", weekly=[LEGACY_APP_ENTRY], once=[plug.encode_once(None, WINTER_CLOCK)]
    )
    memory = make_memory(SUMMER_CLOCK, name="Current")
    memory[plug.OFS_LOCK] = 1
    new = plug.with_restored(memory, backup)
    assert plug.weekly_entries(new) == plug.weekly_entries(backup)
    assert plug.once_entries(new) == plug.once_entries(backup)
    assert new[plug.OFS_NAME : plug.OFS_NAME + 7] == b"Current"
    assert new[plug.OFS_LOCK] == 1
    assert new[plug.OFS_RESERVED :] == memory[plug.OFS_RESERVED :]


def test_report() -> None:
    result = plug.report(make_memory(WINTER_CLOCK, is_on=True, weekly=[LEGACY_APP_ENTRY]), WINTER_NOW)
    assert result["name"] == "Test plug"
    assert result["is_on"] is True
    assert result["plug_clock"] == "2026-11-06 05:33:57"
    assert result["plug_clock_weekday_byte"] == 5  # Friday
    assert result["offset_hours"] == 7
    json.dumps(result)  # must be JSON serialisable (action response)


# -- packets ----------------------------------------------------------------------------------------------------


def _checksum(packet: bytes) -> int:
    return (sum(packet, plug.CHECKSUM_SEED) - sum(packet[0x20:0x22])) & 0xFFFF


def test_build_packet() -> None:
    device = plug.Plug(HOST, "a1:b2:c3:d4:e5:f6", 0x947A)
    payload = bytes([1] + [0] * 15)
    packet = device.build_packet(plug.PACKET_COMMAND, payload)
    assert packet[:8] == plug.PACKET_MAGIC
    assert int.from_bytes(packet[0x20:0x22], "little") == _checksum(packet)
    assert packet[0x24:0x26] == (0x947A).to_bytes(2, "little")
    assert packet[0x26:0x28] == (0x6A).to_bytes(2, "little")
    assert packet[0x2A:0x30] == bytes.fromhex(MAC)[::-1]
    assert int.from_bytes(packet[0x34:0x36], "little") == (sum(payload, 0xBEAF) & 0xFFFF)
    assert device.decrypt(packet[0x38:]) == payload
    assert device.parse_response(packet) == payload  # same framing in both directions


def test_parse_response_errors() -> None:
    device = plug.Plug(HOST, MAC, 0x947A)
    packet = bytearray(device.build_packet(plug.PACKET_COMMAND, bytes(16)))
    with pytest.raises(plug.PlugError, match="short"):
        device.parse_response(bytes(packet[:0x30]))
    broken = bytearray(packet)
    broken[0x40] ^= 0xFF
    with pytest.raises(plug.PlugError, match="checksum"):
        device.parse_response(bytes(broken))
    packet[0x22:0x24] = (-7).to_bytes(2, "little", signed=True)
    packet[0x20:0x22] = _checksum(packet).to_bytes(2, "little")
    with pytest.raises(plug.PlugError, match="error -7"):
        device.parse_response(bytes(packet))


def test_parse_response_incomplete_block() -> None:
    device = plug.Plug(HOST, MAC, 0x947A)
    packet = bytearray(device.build_packet(plug.PACKET_COMMAND, bytes(16)) + bytes(5))
    packet[0x20:0x22] = _checksum(packet).to_bytes(2, "little")
    with pytest.raises(plug.PlugError, match="multiple of 16"):
        device.parse_response(bytes(packet))


class _FakeSocket:
    """UDP socket replacement: returns the queued datagrams, then times out."""

    def __init__(self, datagrams: list[tuple[bytes, tuple[str, int]]]) -> None:
        self.datagrams = datagrams
        self.sent = 0

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def settimeout(self, value: float) -> None:
        return None

    def sendto(self, data: bytes, address) -> None:
        self.sent += 1

    def recvfrom(self, size: int):
        if not self.datagrams:
            raise TimeoutError
        return self.datagrams.pop(0)


def test_send_ignores_other_senders(monkeypatch: pytest.MonkeyPatch) -> None:
    device = plug.Plug(HOST, MAC, 0x947A, timeout=1.5)
    payload = bytes([1] + [0] * 15)
    good = plug.Plug(HOST, MAC, 0x947A).build_packet(plug.PACKET_COMMAND, payload)
    forged = plug.Plug(HOST, MAC, 0x947A).build_packet(plug.PACKET_COMMAND, bytes([9] * 16))
    fake = _FakeSocket([(forged, ("192.0.2.99", 80)), (good, (HOST, 80))])
    monkeypatch.setattr(plug.socket, "socket", lambda *args: fake)
    monkeypatch.setattr(plug.socket, "gethostbyname", lambda host: HOST)
    assert device.send(plug.PACKET_COMMAND, payload) == payload
    assert fake.sent == 1


def test_send_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    device = plug.Plug(HOST, MAC, 0x947A, timeout=0.2)
    monkeypatch.setattr(plug.socket, "socket", lambda *args: _FakeSocket([]))
    monkeypatch.setattr(plug.socket, "gethostbyname", lambda host: HOST)
    with pytest.raises(plug.PlugError, match="No answer"):
        device.send(plug.PACKET_COMMAND, bytes(16))


def test_write_memory_detects_read_back_difference(monkeypatch: pytest.MonkeyPatch) -> None:
    device = plug.Plug(HOST, MAC, 0x947A)
    stored = make_memory(WINTER_CLOCK)
    wanted, _ = plug.with_deadman(stored, 8, WINTER_NOW)

    def send(packet_type: int, payload: bytes) -> bytes:
        if payload[0] == plug.CMD_WRITE:
            return bytes(payload)  # acknowledged ...
        return bytes(stored)  # ... but the plug still has the old table

    monkeypatch.setattr(device, "send", send)
    with pytest.raises(plug.PlugError, match="Read back differs"):
        device.write_memory(wanted)


def test_report_and_setters_refuse_bad_sizes() -> None:
    with pytest.raises(plug.PlugError, match="too short"):
        plug.report(make_memory(WINTER_CLOCK)[:0x1F0], WINTER_NOW)
    memory = make_memory(WINTER_CLOCK)
    with pytest.raises(plug.PlugInputError, match="12 bytes"):
        plug.set_weekly_table(memory, [bytes(11)])
    with pytest.raises(plug.PlugInputError, match="24 bytes"):
        plug.set_once_table(memory, [bytes(23)])
    assert memory == make_memory(WINTER_CLOCK)


def test_invalid_mac() -> None:
    with pytest.raises(plug.PlugInputError):
        plug.Plug(HOST, "a1b2c3", 0x947A)


# -- command line ---------------------------------------------------------------------------------------------


@pytest.fixture
def device(monkeypatch: pytest.MonkeyPatch) -> FakeDevice:
    fake = FakeDevice(make_memory(plug_now(), weekly=[LEGACY_APP_ENTRY]))
    monkeypatch.setattr(plug, "Plug", fake.factory)
    return fake


def _cli(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, dict]:
    code = plug.main(["--host", HOST, "--mac", MAC, "--tz", "Europe/Berlin", *args])
    return code, json.loads(capsys.readouterr().out)


def test_cli_read(device: FakeDevice, capsys: pytest.CaptureFixture[str]) -> None:
    code, result = _cli(capsys, "read")
    assert code == 0
    assert result["ok"] is True
    assert result["name"] == "Test plug"
    assert device.writes == 0


def test_cli_deadman_and_clear(device: FakeDevice, capsys: pytest.CaptureFixture[str]) -> None:
    code, result = _cli(capsys, "deadman", "8")
    assert code == 0
    assert result["once_plug_time"][0]["deadman"] is True
    code, result = _cli(capsys, "clear-deadman")
    assert code == 0
    assert result["once_plug_time"] == []
    assert device.writes == 2


def test_cli_weekly_dry_run_and_backup(device: FakeDevice, capsys, tmp_path) -> None:
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"entries": [{"days": "mon-fri", "on_time": "06:30", "off_time": "06:35"}]}))
    code, result = _cli(capsys, "weekly", str(plan), "--dry-run")
    assert (code, result["changed"], result["written"], device.writes) == (0, True, False, 0)
    code, result = _cli(capsys, "--backup-dir", str(tmp_path / "backups"), "weekly", str(plan))
    assert (code, result["written"], device.writes) == (0, True, 1)
    assert (tmp_path / "backups").is_dir() and result["backup"].endswith(".bin")


def test_cli_error(device: FakeDevice, capsys: pytest.CaptureFixture[str]) -> None:
    device.fail = True
    code, result = _cli(capsys, "read")
    assert code == 1
    assert result["ok"] is False
    assert "No answer" in result["error"]


def test_cli_unsupported_type(device: FakeDevice, capsys: pytest.CaptureFixture[str]) -> None:
    code, result = _cli(capsys, "--type", "0x7547", "read")
    assert code == 1
    assert "not supported" in result["error"]
