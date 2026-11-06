"""Read and write the timers of Broadlink SP3S smart plugs.

The plug runs its own timers (weekly schedule and one-time switching), without Home Assistant,
Wi-Fi or the Broadlink cloud. That makes it a good dead-man switch: before Home Assistant switches
a load on, it writes a one-time "off" into the plug. If the regular "off" from Home Assistant never
comes (crash, restart, Wi-Fi outage, aborted script), the plug switches itself off at that time.

This module has no Home Assistant imports on purpose. The integration (__init__.py) uses it, and it
also works on its own as a command line tool (see main() at the bottom, `python3 plug.py --help`).
It needs only the Python standard library and `cryptography`, which every Home Assistant ships.

Overview (details and how the layout was found: docs/protocol.md in the repository):

* Transport: UDP port 80, the same packet format and AES-128-CBC encryption as python-broadlink.
  Authentication is packet type 0x65, commands are packet type 0x6A.
* Command 1 reads the whole timer memory (512 bytes). Command 3 writes the timer part back
  (bytes 0x08-0x1EF). The plug always stores the complete tables, so every change is
  read - modify - write - read back - compare.
* The plug clock runs on UTC+8 (factory setting from the cloud). All timer times are in plug time,
  and weekdays are the weekdays of the plug clock. This module converts from and to local time.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta, tzinfo
import json
import os
import random
import socket
import sys
import time
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# --------------------------------------------------------------------------------------------------
# Devices
# --------------------------------------------------------------------------------------------------

#: Device types (as stored by the Home Assistant Broadlink integration) with this memory layout.
SUPPORTED_TYPES: dict[int, str] = {0x947A: "SP3S-EU", 0x9479: "SP3S-US"}
#: Device types that were tested on a real plug. The others share the firmware family.
TESTED_TYPES: frozenset[int] = frozenset({0x947A})

DEFAULT_TIMEOUT = 6.0  # seconds per command; the packet is repeated every second until then
PORT = 80

# Default key and IV of all Broadlink devices; the key is replaced by the session key after auth.
DEFAULT_KEY = bytes.fromhex("097628343fe99e23765c1513accf8b02")
DEFAULT_IV = bytes.fromhex("562e17996d093d28ddb3ba695a2e6f58")

PACKET_MAGIC = bytes.fromhex("5aa5aa555aa5aa55")
PACKET_AUTH = 0x65
PACKET_COMMAND = 0x6A
CHECKSUM_SEED = 0xBEAF

CMD_READ = 1
CMD_WRITE = 3

# --------------------------------------------------------------------------------------------------
# Memory layout of command 1 (decrypted payload, 512 bytes). See docs/protocol.md.
# --------------------------------------------------------------------------------------------------

MEMORY_SIZE = 0x200
OFS_STATE = 0x04  # bit 0 = relay on, bit 1 = night light
OFS_WEEKLY_COUNT = 0x08  # u32: number of weekly entries in use
OFS_WEEKLY = 0x0C  # 16 weekly entries of 12 bytes
WEEKLY_SIZE = 12
WEEKLY_MAX = 16
OFS_NAME = 0xCC  # device name, 63 bytes, zero padded
NAME_LENGTH = 63
OFS_LOCK = 0x10B  # "lock" flag of the app (not used here)
OFS_ONCE_COUNT = 0x10C  # u32: number of one-time entries in use
OFS_ONCE = 0x110  # 8 one-time entries of 24 bytes
ONCE_SIZE = 24
ONCE_MAX = 8
OFS_RESERVED = 0x1D0  # 32 bytes, among them the cycle timer of the app: never changed, written back 1:1
OFS_WRITE_END = 0x1F0  # command 3 writes bytes 0x08 .. 0x1EF
OFS_CLOCK = 0x1F0  # plug clock, 8-byte timestamp (read only)

# A weekly entry without an "on" or without an "off" time stores 25:61.
NO_HOUR, NO_MINUTE = 25, 61

#: Weekday names; the index is the bit in the weekday mask (bit 0 = Sunday ... bit 6 = Saturday).
DAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
#: The same names in the order used for output (Monday first).
DAYS_FROM_MONDAY = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
ALL_DAYS = 0x7F

MAX_DEADMAN_MINUTES = 24 * 60


class PlugError(Exception):
    """The plug did not answer, answered with an error, or its memory looks wrong."""


class PlugInputError(PlugError):
    """The request itself is invalid (bad time, too many entries, ...)."""


# --------------------------------------------------------------------------------------------------
# Protocol
# --------------------------------------------------------------------------------------------------


class Plug:
    """One Broadlink SP3S plug on the local network.

    All methods block (UDP with timeout). Call them from an executor in Home Assistant.
    """

    def __init__(self, host: str, mac: str, devtype: int, timeout: float = DEFAULT_TIMEOUT) -> None:
        mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
        if len(mac_bytes) != 6:
            raise PlugInputError(f"MAC address must have 6 bytes: {mac}")
        self.host = host
        self.mac = mac_bytes.hex()
        self.devtype = devtype
        self.timeout = timeout
        self._mac = mac_bytes
        self._count = random.randint(0x8000, 0xFFFF)  # packet counter, like python-broadlink
        self._session_id = 0
        self._key = DEFAULT_KEY

    # -- encryption ------------------------------------------------------------------------------

    def _cipher(self) -> Cipher:
        return Cipher(algorithms.AES(self._key), modes.CBC(DEFAULT_IV))

    def encrypt(self, data: bytes) -> bytes:
        encryptor = self._cipher().encryptor()
        return encryptor.update(data) + encryptor.finalize()

    def decrypt(self, data: bytes) -> bytes:
        decryptor = self._cipher().decryptor()
        return decryptor.update(data) + decryptor.finalize()

    # -- packets ---------------------------------------------------------------------------------

    def build_packet(self, packet_type: int, payload: bytes) -> bytes:
        """Build one request packet: 0x38-byte header + encrypted payload.

        Header: 0x00 magic | 0x20 checksum of the whole packet | 0x24 device type | 0x26 packet type
        | 0x28 counter | 0x2A MAC (reversed) | 0x30 session id | 0x34 checksum of the plain payload.
        """
        self._count = ((self._count + 1) | 0x8000) & 0xFFFF
        packet = bytearray(0x38)
        packet[0x00:0x08] = PACKET_MAGIC
        packet[0x24:0x26] = self.devtype.to_bytes(2, "little")
        packet[0x26:0x28] = packet_type.to_bytes(2, "little")
        packet[0x28:0x2A] = self._count.to_bytes(2, "little")
        packet[0x2A:0x30] = self._mac[::-1]
        packet[0x30:0x34] = self._session_id.to_bytes(4, "little")
        packet[0x34:0x36] = (sum(payload, CHECKSUM_SEED) & 0xFFFF).to_bytes(2, "little")
        padded = bytes(payload) + bytes((16 - len(payload)) % 16)  # AES needs blocks of 16 bytes
        packet += self.encrypt(padded)
        packet[0x20:0x22] = (sum(packet, CHECKSUM_SEED) & 0xFFFF).to_bytes(2, "little")
        return bytes(packet)

    def parse_response(self, response: bytes) -> bytes:
        """Check a response packet and return its decrypted payload."""
        if len(response) < 0x38:
            raise PlugError(f"Response too short ({len(response)} bytes)")
        expected = int.from_bytes(response[0x20:0x22], "little")
        # The checksum covers the whole packet with the checksum field itself counted as zero.
        actual = (sum(response, CHECKSUM_SEED) - sum(response[0x20:0x22])) & 0xFFFF
        if expected != actual:
            raise PlugError("Wrong checksum in the response")
        error = int.from_bytes(response[0x22:0x24], "little", signed=True)
        if error:
            raise PlugError(f"The plug answered with error {error}")
        encrypted = response[0x38:]
        if len(encrypted) % 16:  # AES works on whole blocks; anything else is a broken packet
            raise PlugError(f"Encrypted payload is not a multiple of 16 bytes ({len(encrypted)} bytes)")
        return self.decrypt(encrypted)

    def send(self, packet_type: int, payload: bytes) -> bytes:
        """Send one request and wait for its response (repeated every second until the timeout)."""
        packet = self.build_packet(packet_type, payload)
        address = socket.gethostbyname(self.host)  # replies from any other address are ignored
        start = time.monotonic()
        response = None
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            while response is None:
                remaining = self.timeout - (time.monotonic() - start)
                if remaining <= 0:
                    raise PlugError(f"No answer from the plug at {self.host} within {self.timeout:g} s")
                sock.sendto(packet, (address, PORT))
                slot_end = time.monotonic() + min(1.0, remaining)  # wait up to 1 s, then send again
                while (wait := slot_end - time.monotonic()) > 0:
                    sock.settimeout(wait)
                    try:
                        data, sender = sock.recvfrom(2048)
                    except TimeoutError:  # socket.timeout is an alias of TimeoutError
                        break
                    if sender[0] == address:
                        response = data
                        break
        return self.parse_response(response)

    # -- commands --------------------------------------------------------------------------------

    def auth(self) -> None:
        """Get a session id and key (same request as python-broadlink)."""
        self._session_id, self._key = 0, DEFAULT_KEY
        payload = bytearray(0x50)
        payload[0x04:0x14] = b"\x31" * 16
        payload[0x1E] = 0x01
        payload[0x2D] = 0x01
        payload[0x30:0x36] = b"Test 1"
        answer = self.send(PACKET_AUTH, bytes(payload))
        if len(answer) < 20:
            raise PlugError("Authentication answer too short")
        self._session_id = int.from_bytes(answer[0:4], "little")
        self._key = bytes(answer[4:20])

    def read_memory(self) -> bytes:
        """Command 1: return the 512-byte timer memory."""
        answer = self.send(PACKET_COMMAND, bytes([CMD_READ] + [0] * 15))
        if len(answer) < MEMORY_SIZE or answer[0] != CMD_READ:
            raise PlugError(
                f"Unexpected answer to read (length {len(answer)}, command {answer[0] if answer else None})"
            )
        memory = bytes(answer[:MEMORY_SIZE])
        check_memory(memory)
        return memory

    def write_memory(self, memory: bytes) -> bytes:
        """Command 3: write the timer part of `memory`, read it back and compare.

        Returns the memory as read back after the write.
        """
        check_memory(memory)
        answer = self.send(PACKET_COMMAND, bytes([CMD_WRITE, 0, 0, 0]) + bytes(memory[OFS_WEEKLY_COUNT:OFS_WRITE_END]))
        if not answer or answer[0] != CMD_WRITE:
            raise PlugError("Unexpected answer to write")
        stored = self.read_memory()
        if stored[OFS_WEEKLY_COUNT:OFS_WRITE_END] != bytes(memory[OFS_WEEKLY_COUNT:OFS_WRITE_END]):
            raise PlugError("Read back differs from what was written: the plug did not take the change")
        return stored


# --------------------------------------------------------------------------------------------------
# Memory: decode and encode
# --------------------------------------------------------------------------------------------------


def _u32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "little")


def check_memory(memory: bytes) -> None:
    """Refuse memory that is too short or has implausible entry counts."""
    if len(memory) < OFS_WRITE_END:
        raise PlugError(f"Timer memory too short ({len(memory)} bytes)")
    weekly, once = _u32(memory, OFS_WEEKLY_COUNT), _u32(memory, OFS_ONCE_COUNT)
    if weekly > WEEKLY_MAX or once > ONCE_MAX:
        raise PlugError(f"Implausible entry counts (weekly {weekly}, one-time {once}): unknown memory layout")


def decode_timestamp(data: bytes) -> datetime | None:
    """8 bytes: year (u16), second, minute, hour, weekday, day, month. Naive datetime in plug time."""
    year = int.from_bytes(data[0:2], "little")
    second, minute, hour, _weekday, day, month = data[2:8]
    try:
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


def encode_timestamp(moment: datetime) -> bytes:
    """Inverse of decode_timestamp. The weekday byte is 0 = Sunday; the plug ignores it in timers."""
    return moment.year.to_bytes(2, "little") + bytes(
        [moment.second, moment.minute, moment.hour, moment.isoweekday() % 7, moment.day, moment.month]
    )


def plug_clock(memory: bytes) -> datetime | None:
    """Current time of the plug clock (naive, plug time = UTC+8)."""
    return decode_timestamp(memory[OFS_CLOCK : OFS_CLOCK + 8])


def _valid_clock(memory: bytes) -> datetime:
    clock = plug_clock(memory)
    if clock is None or clock.year < 2020:
        raise PlugError(
            f"Plug clock is not set ({memory[OFS_CLOCK : OFS_CLOCK + 8].hex()}). "
            "The plug sets it from the Broadlink cloud; check its internet access."
        )
    return clock


def _hms(hour: int, minute: int, second: int = 0) -> str | None:
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}" + (f":{second:02d}" if second else "")


def mask_to_days(mask: int) -> list[str]:
    """Weekday mask -> ["mon", "tue", ...] (Monday first)."""
    return [day for day in DAYS_FROM_MONDAY if mask >> DAYS.index(day) & 1]


def parse_days(value: str | Iterable[str] | None) -> int:
    """Weekday mask from "mon-sun", "mon-fri", "sat,sun", "fri-mon" or a list like ["mon", "wed"].

    Only the first three letters count, case does not matter. None means every day.
    """
    if value is None:
        return ALL_DAYS
    parts = value.replace(" ", "").split(",") if isinstance(value, str) else [str(part) for part in value]
    mask = 0
    for part in parts:
        names = [name.strip().lower()[:3] for name in part.split("-")]
        if not names or not all(names) or any(name not in DAYS for name in names) or len(names) > 2:
            raise PlugInputError(f"Unknown weekday(s): {part!r} (use mon, tue, wed, thu, fri, sat, sun)")
        index, last = DAYS.index(names[0]), DAYS.index(names[-1])
        while True:  # a range may wrap around the week, e.g. fri-mon
            mask |= 1 << index
            if index == last:
                break
            index = (index + 1) % 7
    if not mask:
        raise PlugInputError(f"No weekday given: {value!r}")
    return mask


def rotate_mask(mask: int, days: int) -> int:
    """Shift a weekday mask by `days` (positive = later). Saturday + 1 day = Sunday."""
    days %= 7
    return ((mask << days) | (mask >> (7 - days))) & ALL_DAYS


def parse_time(value: str) -> tuple[int, int, int]:
    """ "HH:MM" or "HH:MM:SS" -> (hour, minute, second)."""
    try:
        parts = [int(part) for part in str(value).strip().split(":")]
    except ValueError:
        parts = []
    if len(parts) == 2:
        parts.append(0)
    if len(parts) != 3 or not (0 <= parts[0] <= 23 and 0 <= parts[1] <= 59 and 0 <= parts[2] <= 59):
        raise PlugInputError(f"Invalid time {value!r} (use HH:MM or HH:MM:SS)")
    return parts[0], parts[1], parts[2]


def weekly_entries(memory: bytes) -> list[dict[str, Any]]:
    """Weekly table in plug time.

    Entry (12 bytes): enabled, weekday mask, 0, 0, on hour, on minute, off hour, off minute,
    on second, off second, 0, 0.
    """
    entries = []
    for index in range(min(_u32(memory, OFS_WEEKLY_COUNT), WEEKLY_MAX)):
        raw = memory[OFS_WEEKLY + index * WEEKLY_SIZE : OFS_WEEKLY + (index + 1) * WEEKLY_SIZE]
        entries.append(
            {
                "index": index,
                "enabled": bool(raw[0]),
                "mask": raw[1],
                "days": mask_to_days(raw[1]),
                "on_time": _hms(raw[4], raw[5], raw[8]),
                "off_time": _hms(raw[6], raw[7], raw[9]),
                "raw": raw.hex(),
            }
        )
    return entries


def encode_weekly(
    enabled: bool,
    mask: int,
    on: tuple[int, int, int] | None,
    off: tuple[int, int, int] | None,
) -> bytes:
    """One 12-byte weekly entry. `on`/`off` are (hour, minute, second) in plug time or None."""
    on_h, on_m, on_s = on if on else (NO_HOUR, NO_MINUTE, 0)
    off_h, off_m, off_s = off if off else (NO_HOUR, NO_MINUTE, 0)
    return bytes([1 if enabled else 0, mask & ALL_DAYS, 0, 0, on_h, on_m, off_h, off_m, on_s, off_s, 0, 0])


def set_weekly_table(memory: bytearray, entries: list[bytes]) -> None:
    """Replace the whole weekly table (unused slots are zeroed)."""
    if len(entries) > WEEKLY_MAX:
        raise PlugInputError(f"Too many weekly entries for the plug ({len(entries)} > {WEEKLY_MAX})")
    if any(len(entry) != WEEKLY_SIZE for entry in entries):
        raise PlugInputError(f"Every weekly entry must have {WEEKLY_SIZE} bytes")
    memory[OFS_WEEKLY_COUNT : OFS_WEEKLY_COUNT + 4] = len(entries).to_bytes(4, "little")
    memory[OFS_WEEKLY : OFS_WEEKLY + WEEKLY_MAX * WEEKLY_SIZE] = b"".join(entries) + bytes(
        (WEEKLY_MAX - len(entries)) * WEEKLY_SIZE
    )


def once_entries(memory: bytes) -> list[dict[str, Any]]:
    """One-time table in plug time.

    Entry (24 bytes): on timestamp (8), on enabled (u32), off timestamp (8), off enabled (u32).
    The plug keeps an entry after it fired; this module removes expired entries on the next write.
    """
    entries = []
    for index in range(min(_u32(memory, OFS_ONCE_COUNT), ONCE_MAX)):
        raw = memory[OFS_ONCE + index * ONCE_SIZE : OFS_ONCE + (index + 1) * ONCE_SIZE]
        on, off = decode_timestamp(raw[0:8]), decode_timestamp(raw[12:20])
        entries.append(
            {
                "index": index,
                "on_enabled": bool(_u32(raw, 8)),
                "on": on.isoformat(" ") if on else None,
                "off_enabled": bool(_u32(raw, 20)),
                "off": off.isoformat(" ") if off else None,
                "deadman": is_deadman(raw),
                "raw": raw.hex(),
            }
        )
    return entries


def encode_once(on: datetime | None, off: datetime | None) -> bytes:
    """One 24-byte one-time entry; times are naive datetimes in plug time."""
    empty = bytes(8)
    return (
        (encode_timestamp(on) if on else empty)
        + (1 if on else 0).to_bytes(4, "little")
        + (encode_timestamp(off) if off else empty)
        + (1 if off else 0).to_bytes(4, "little")
    )


def set_once_table(memory: bytearray, entries: list[bytes]) -> None:
    """Replace the whole one-time table (unused slots are zeroed)."""
    if len(entries) > ONCE_MAX:
        raise PlugInputError(f"Too many one-time entries for the plug ({len(entries)} > {ONCE_MAX})")
    if any(len(entry) != ONCE_SIZE for entry in entries):
        raise PlugInputError(f"Every one-time entry must have {ONCE_SIZE} bytes")
    memory[OFS_ONCE_COUNT : OFS_ONCE_COUNT + 4] = len(entries).to_bytes(4, "little")
    memory[OFS_ONCE : OFS_ONCE + ONCE_MAX * ONCE_SIZE] = b"".join(entries) + bytes(
        (ONCE_MAX - len(entries)) * ONCE_SIZE
    )


def is_deadman(raw: bytes) -> bool:
    """A dead-man entry is a one-time entry with an "off" and no "on".

    set_once always needs an "on", so entries written by this module never get mixed up.
    """
    return not _u32(raw, 8) and bool(_u32(raw, 20))


def _latest_enabled_time(raw: bytes) -> datetime | None:
    times = [
        moment
        for moment, enabled in (
            (decode_timestamp(raw[0:8]), _u32(raw, 8)),
            (decode_timestamp(raw[12:20]), _u32(raw, 20)),
        )
        if enabled and moment
    ]
    return max(times) if times else None


def _once_raw(memory: bytes) -> list[bytes]:
    return [
        bytes(memory[OFS_ONCE + i * ONCE_SIZE : OFS_ONCE + (i + 1) * ONCE_SIZE])
        for i in range(min(_u32(memory, OFS_ONCE_COUNT), ONCE_MAX))
    ]


# --------------------------------------------------------------------------------------------------
# Time: local time <-> plug time
# --------------------------------------------------------------------------------------------------


def _round_quarter(seconds: float) -> timedelta:
    return timedelta(minutes=round(seconds / 900) * 15)


def local_offset(memory: bytes, now: datetime) -> tuple[timedelta, float]:
    """Plug clock minus local wall-clock time, rounded to 15 minutes, and the clock deviation in s.

    Example: plug UTC+8, Central Europe: +7 h in winter, +6 h in summer.
    `now` must be timezone aware (local time zone).
    """
    clock = _valid_clock(memory)
    difference = (clock - now.replace(tzinfo=None)).total_seconds()
    offset = _round_quarter(difference)
    return offset, difference - offset.total_seconds()


def utc_offset(memory: bytes, now: datetime) -> timedelta:
    """Plug clock minus UTC, rounded to 15 minutes (+8 h). Valid across daylight saving changes."""
    clock = _valid_clock(memory)
    return _round_quarter((clock - now.astimezone(UTC).replace(tzinfo=None)).total_seconds())


def _plug_to_local(moment: datetime, offset_utc: timedelta, zone: tzinfo | None) -> datetime:
    return (moment - offset_utc).replace(tzinfo=UTC).astimezone(zone)


def _local_to_plug(moment: datetime, offset_utc: timedelta) -> datetime:
    return moment.astimezone(UTC).replace(tzinfo=None) + offset_utc


def shift_time(hour: int, minute: int, offset: timedelta) -> tuple[int, int, int]:
    """hour:minute + offset -> (hour, minute, day shift). The offset is a multiple of 15 minutes."""
    minutes = hour * 60 + minute + int(offset.total_seconds() // 60)
    return (minutes % 1440) // 60, minutes % 60, minutes // 1440


def _time_to_plug(text: str, offset: timedelta) -> tuple[tuple[int, int, int], int]:
    """Local "HH:MM[:SS]" -> ((hour, minute, second) in plug time, day shift)."""
    hour, minute, second = parse_time(text)
    plug_hour, plug_minute, day_shift = shift_time(hour, minute, offset)
    return (plug_hour, plug_minute, second), day_shift


def weekly_to_plug(entries: Iterable[dict[str, Any]], offset: timedelta) -> list[bytes]:
    """Weekly entries in local time -> encoded entries in plug time.

    Entry: {"enabled": bool (default True), "days": "mon-fri" or ["mon", ...] (default every day),
            "on_time": "HH:MM[:SS]" or None, "off_time": "HH:MM[:SS]" or None}.
    The weekdays are local weekdays; they are shifted when the plug time is on another day.
    If "on" and "off" end up on different plug days, the entry is split into two.
    """
    encoded: list[bytes] = []
    for number, entry in enumerate(entries, start=1):
        enabled = bool(entry.get("enabled", True))
        mask = parse_days(entry.get("days"))
        on_text, off_text = entry.get("on_time"), entry.get("off_time")
        if not on_text and not off_text:
            raise PlugInputError(f"Weekly entry {number}: needs on_time and/or off_time")
        if on_text and not off_text:
            raise PlugInputError(
                f"Weekly entry {number}: on_time without off_time. The plug has no run-time limit, "
                "so every switch-on needs a switch-off."
            )
        on = _time_to_plug(on_text, offset) if on_text else None
        off = _time_to_plug(off_text, offset) if off_text else None
        if on and off and on[1] != off[1]:
            encoded.append(encode_weekly(enabled, rotate_mask(mask, on[1]), on[0], None))
            encoded.append(encode_weekly(enabled, rotate_mask(mask, off[1]), None, off[0]))
        else:
            day_shift = (on or off)[1]
            encoded.append(
                encode_weekly(enabled, rotate_mask(mask, day_shift), on[0] if on else None, off[0] if off else None)
            )
    return encoded


def weekly_in_local_time(memory: bytes, offset: timedelta) -> list[dict[str, Any]]:
    """Weekly table converted to local time (each switch time with its own local weekdays)."""
    result = []
    for entry in weekly_entries(memory):
        raw = bytes.fromhex(entry["raw"])
        item: dict[str, Any] = {"index": entry["index"], "enabled": entry["enabled"]}
        for name, hour, minute, second in (("on", raw[4], raw[5], raw[8]), ("off", raw[6], raw[7], raw[9])):
            if hour <= 23 and minute <= 59:
                local_hour, local_minute, day_shift = shift_time(hour, minute, -offset)
                item[f"{name}_time"] = _hms(local_hour, local_minute, second)
                item[f"{name}_days"] = mask_to_days(rotate_mask(raw[1], day_shift))
        result.append(item)
    return result


# --------------------------------------------------------------------------------------------------
# Changes (pure functions: memory in, new memory out; easy to test without a plug)
# --------------------------------------------------------------------------------------------------


def with_deadman(memory: bytes, minutes: float | None, now: datetime) -> tuple[bytearray, dict[str, Any]]:
    """Replace the dead-man entry: one-time "off" at plug clock + `minutes` (None = only remove it).

    Expired one-time entries are removed, future entries with an "on" are kept. If the table is
    full, the oldest entries are dropped so the dead-man always fits.
    """
    if minutes is not None and not 1 <= minutes <= MAX_DEADMAN_MINUTES:
        raise PlugInputError(f"Dead-man minutes must be 1 ... {MAX_DEADMAN_MINUTES}, not {minutes}")
    clock = _valid_clock(memory)
    keep = []
    for raw in _once_raw(memory):
        latest = _latest_enabled_time(raw)
        if is_deadman(raw) or latest is None or latest < clock:
            continue  # old dead-man (replaced or removed), empty or expired entry
        keep.append(raw)
    info: dict[str, Any] = {"plug_clock": clock.isoformat(" ")}
    if minutes is not None:
        off = clock + timedelta(seconds=round(minutes * 60))
        keep.append(encode_once(None, off))
        info["off_plug_time"] = off.isoformat(" ")
        info["off_local"] = _plug_to_local(off, utc_offset(memory, now), now.tzinfo).isoformat()
    new = bytearray(memory)
    set_once_table(new, keep[-ONCE_MAX:])
    return new, info


def with_once(memory: bytes, on: datetime, off: datetime, now: datetime) -> tuple[bytearray, dict[str, Any]]:
    """Add a one-time switching: on at `on`, off at `off` (timezone aware local datetimes).

    Uses the plug's UTC offset, so times after a daylight saving change are right as well.
    Expired entries are removed. Entries with an "on" survive every dead-man update.
    """
    if off is None:
        raise PlugInputError("A one-time switch-on needs a switch-off: the plug has no run-time limit of its own")
    if on.tzinfo is None or off.tzinfo is None:
        raise PlugInputError("Times must be timezone aware")
    clock = _valid_clock(memory)
    offset = utc_offset(memory, now)
    on_plug = _local_to_plug(on, offset)
    off_plug = _local_to_plug(off, offset)
    if on_plug <= clock:
        raise PlugInputError(f"The switch-on time {on.isoformat()} is not in the future")
    if off_plug <= on_plug:
        raise PlugInputError("The switch-off time must be after the switch-on time")
    keep = []
    for raw in _once_raw(memory):
        latest = _latest_enabled_time(raw)
        if latest is not None and latest >= clock:
            keep.append(raw)
    if len(keep) >= ONCE_MAX:
        raise PlugInputError(f"The plug already has {ONCE_MAX} one-time entries")
    keep.append(encode_once(on_plug, off_plug))
    new = bytearray(memory)
    set_once_table(new, keep)
    return new, {"on_plug_time": on_plug.isoformat(" "), "off_plug_time": off_plug.isoformat(" ")}


def with_weekly(memory: bytes, entries: Iterable[dict[str, Any]], now: datetime) -> tuple[bytearray, dict[str, Any]]:
    """Replace the whole weekly table with `entries` (local time, see weekly_to_plug).

    info["changed"] tells whether the plug differs. Because the plug clock has a fixed UTC offset,
    the local times move by one hour at every daylight saving change: write the table again
    every day (only a changed table is written).
    """
    offset, _deviation = local_offset(memory, now)
    new = bytearray(memory)
    set_weekly_table(new, weekly_to_plug(entries, offset))
    changed = new[OFS_WEEKLY_COUNT:OFS_NAME] != memory[OFS_WEEKLY_COUNT:OFS_NAME]
    return new, {
        "changed": changed,
        "offset_hours": offset.total_seconds() / 3600,
        "weekly_plug_time": weekly_entries(new),
        "weekly_local_time": weekly_in_local_time(new, offset),
    }


def with_restored(memory: bytes, backup: bytes) -> bytearray:
    """Copy the weekly and one-time tables from a backup. Name, lock and reserved bytes stay."""
    check_memory(backup)
    new = bytearray(memory)
    new[OFS_WEEKLY_COUNT:OFS_NAME] = backup[OFS_WEEKLY_COUNT:OFS_NAME]
    new[OFS_ONCE_COUNT:OFS_RESERVED] = backup[OFS_ONCE_COUNT:OFS_RESERVED]
    return new


def report(memory: bytes, now: datetime) -> dict[str, Any]:
    """Everything readable, in plug time and (if the plug clock is set) in local time."""
    if len(memory) < MEMORY_SIZE:
        raise PlugError(f"Timer memory too short for a report ({len(memory)} of {MEMORY_SIZE} bytes)")
    check_memory(memory)
    clock = plug_clock(memory)
    result: dict[str, Any] = {
        "name": memory[OFS_NAME : OFS_NAME + NAME_LENGTH].split(b"\0")[0].decode("utf-8", "replace"),
        "is_on": bool(memory[OFS_STATE] & 1),
        "plug_clock": clock.isoformat(" ") if clock else None,
        "plug_clock_weekday_byte": memory[OFS_CLOCK + 5],
        "weekly_plug_time": weekly_entries(memory),
        "once_plug_time": once_entries(memory),
        "reserved_hex": memory[OFS_RESERVED:OFS_CLOCK].hex(),
    }
    try:
        offset, deviation = local_offset(memory, now)
        offset_utc = utc_offset(memory, now)
    except PlugError as err:
        result["clock_error"] = str(err)
        return result
    result["offset_hours"] = offset.total_seconds() / 3600
    result["clock_deviation_s"] = round(deviation)
    result["weekly_local_time"] = weekly_in_local_time(memory, offset)
    result["once_local_time"] = [
        {
            "index": entry["index"],
            "deadman": entry["deadman"],
            "on": _plug_to_local(datetime.fromisoformat(entry["on"]), offset_utc, now.tzinfo).isoformat()
            if entry["on_enabled"] and entry["on"]
            else None,
            "off": _plug_to_local(datetime.fromisoformat(entry["off"]), offset_utc, now.tzinfo).isoformat()
            if entry["off_enabled"] and entry["off"]
            else None,
        }
        for entry in once_entries(memory)
    ]
    return result


# --------------------------------------------------------------------------------------------------
# Operations on a plug (blocking: connect, read, change, write, read back)
# --------------------------------------------------------------------------------------------------


def _connect(plug: Plug) -> bytes:
    plug.auth()
    return plug.read_memory()


def backup_file_name(plug: Plug, moment: datetime) -> str:
    return f"{plug.mac}_{moment.strftime('%Y%m%d_%H%M%S')}.bin"


def save_backup(plug: Plug, directory: str, memory: bytes | None = None) -> str:
    """Save the raw 512-byte memory to `directory`; returns the file path."""
    if memory is None:
        memory = _connect(plug)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, backup_file_name(plug, datetime.now()))
    with open(path, "wb") as file:
        file.write(memory)
    return path


def read_report(plug: Plug, now: datetime) -> dict[str, Any]:
    return report(_connect(plug), now)


def write_deadman(plug: Plug, minutes: float | None, now: datetime) -> dict[str, Any]:
    """Set (minutes) or remove (None) the dead-man entry. No backup: this runs before every switch-on."""
    memory = _connect(plug)
    new, info = with_deadman(memory, minutes, now)
    stored = plug.write_memory(new)
    info["once_plug_time"] = once_entries(stored)
    return info


def write_once(plug: Plug, on: datetime, off: datetime, now: datetime, backup_dir: str | None = None) -> dict[str, Any]:
    """Add a one-time switching (local times). Saves a backup first if `backup_dir` is given."""
    memory = _connect(plug)
    new, info = with_once(memory, on, off, now)
    if backup_dir:
        info["backup"] = save_backup(plug, backup_dir, memory)
    stored = plug.write_memory(new)
    info["once_plug_time"] = once_entries(stored)
    return info


def write_weekly(
    plug: Plug,
    entries: Iterable[dict[str, Any]],
    now: datetime,
    dry_run: bool = False,
    backup_dir: str | None = None,
) -> dict[str, Any]:
    """Write the weekly table if it differs from the plug (or only show it with dry_run)."""
    memory = _connect(plug)
    new, info = with_weekly(memory, list(entries), now)
    info["written"] = False
    if info["changed"] and not dry_run:
        if backup_dir:
            info["backup"] = save_backup(plug, backup_dir, memory)
        plug.write_memory(new)
        info["written"] = True
    return info


def restore_backup(plug: Plug, path: str, now: datetime, backup_dir: str | None = None) -> dict[str, Any]:
    """Write the weekly and one-time tables of a backup file back into the plug."""
    with open(path, "rb") as file:
        backup = file.read()
    memory = _connect(plug)
    new = with_restored(memory, backup)
    saved = save_backup(plug, backup_dir, memory) if backup_dir else None
    result = report(plug.write_memory(new), now)
    if saved:
        result["backup"] = saved
    return result


# --------------------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------------------


def _parse_local(text: str, zone: tzinfo | None) -> datetime:
    try:
        moment = datetime.fromisoformat(text.strip())
    except ValueError as err:
        raise PlugInputError(f'Invalid date/time {text!r} (use "YYYY-MM-DD HH:MM")') from err
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone) if zone else moment.astimezone()
    return moment


def _zone(name: str | None) -> tzinfo | None:
    if not name:
        return None
    from zoneinfo import ZoneInfo  # noqa: PLC0415  (only needed on the command line)

    return ZoneInfo(name)


def main(argv: list[str] | None = None) -> int:
    """Command line tool. Prints JSON; exit code 0 = ok, 1 = error."""
    parser = argparse.ArgumentParser(
        prog="plug.py",
        description="Read and write the timers of a Broadlink SP3S plug (local network, no cloud).",
    )
    parser.add_argument("--host", required=True, help="IP address of the plug")
    parser.add_argument("--mac", required=True, help="MAC address, e.g. a1b2c3d4e5f6")
    parser.add_argument("--type", default="0x947A", type=lambda text: int(text, 0), help="device type (0x947A)")
    parser.add_argument("--tz", help="IANA time zone for local times (default: system time zone)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds per command")
    parser.add_argument("--backup-dir", help="save the memory here before weekly/once/restore")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("read", help="show state, clock and all timers")
    command = commands.add_parser("deadman", help="one-time off in MINUTES from now (replaces the old one)")
    command.add_argument("minutes", type=float)
    commands.add_parser("clear-deadman", help="remove the dead-man entry")
    command = commands.add_parser("once", help='one-time switching, e.g. once "2026-11-07 10:00" "2026-11-07 10:05"')
    command.add_argument("on")
    command.add_argument("off")
    command = commands.add_parser("weekly", help='weekly table from a JSON file {"entries": [...]}')
    command.add_argument("file")
    command.add_argument("--dry-run", action="store_true", help="only show what would be written")
    command = commands.add_parser("backup", help="save the raw memory into DIRECTORY")
    command.add_argument("directory")
    command = commands.add_parser("restore", help="write the timer tables of a backup FILE back")
    command.add_argument("file")
    args = parser.parse_args(argv)

    try:
        zone = _zone(args.tz)
        now = datetime.now(zone) if zone else datetime.now().astimezone()
        if args.type not in SUPPORTED_TYPES:
            raise PlugInputError(f"Device type {args.type:#06x} is not supported (only SP3S: 0x947A, 0x9479)")
        plug = Plug(args.host, args.mac, args.type, args.timeout)
        if args.command == "read":
            result = read_report(plug, now)
        elif args.command == "deadman":
            result = write_deadman(plug, args.minutes, now)
        elif args.command == "clear-deadman":
            result = write_deadman(plug, None, now)
        elif args.command == "once":
            result = write_once(plug, _parse_local(args.on, zone), _parse_local(args.off, zone), now, args.backup_dir)
        elif args.command == "weekly":
            with open(args.file, encoding="utf-8") as file:
                plan = json.load(file)
            result = write_weekly(plug, plan.get("entries", []), now, args.dry_run, args.backup_dir)
        elif args.command == "backup":
            result = {"backup": save_backup(plug, args.directory)}
        else:
            result = restore_backup(plug, args.file, now, args.backup_dir)
    except (PlugError, OSError, ValueError) as err:
        print(json.dumps({"ok": False, "error": f"{type(err).__name__}: {err}"}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
