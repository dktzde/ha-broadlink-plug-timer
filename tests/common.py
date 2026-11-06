"""Test helpers: build a plug memory image and a fake plug that keeps it in RAM."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from custom_components.broadlink_plug_timer import plug

MAC = "a1b2c3d4e5f6"  # placeholder, not a real device
HOST = "192.0.2.10"  # TEST-NET-1, not a real device
LEGACY_APP_ENTRY = bytes.fromhex("007f00000a0e0e133b3b0000")  # disabled, every day, 10:14:59-14:19:59 plug time


def make_memory(
    clock: datetime,
    *,
    name: str = "Test plug",
    is_on: bool = False,
    weekly: list[bytes] | None = None,
    once: list[bytes] | None = None,
) -> bytearray:
    """512 bytes as returned by command 1, with the plug clock set to `clock` (naive, plug time)."""
    memory = bytearray(plug.MEMORY_SIZE)
    memory[0] = plug.CMD_READ
    memory[plug.OFS_STATE] = 1 if is_on else 0
    plug.set_weekly_table(memory, weekly or [])
    encoded = name.encode()[: plug.NAME_LENGTH]  # UTF-8 bytes, cut to the 63-byte field
    memory[plug.OFS_NAME : plug.OFS_NAME + len(encoded)] = encoded
    plug.set_once_table(memory, once or [])
    memory[plug.OFS_RESERVED : plug.OFS_CLOCK] = bytes(range(32))  # recognisable, must never change
    memory[plug.OFS_CLOCK : plug.OFS_CLOCK + 8] = plug.encode_timestamp(clock)
    return memory


def plug_now() -> datetime:
    """Plug clock for "now": UTC+8, naive."""
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0) + timedelta(hours=8)


@dataclass
class FakeDevice:
    """State of one fake plug: its memory, the number of writes and an optional failure."""

    memory: bytearray
    writes: int = 0
    fail: bool = False
    connections: list[tuple[str, str, int]] = field(default_factory=list)

    def factory(self, host: str, mac: str, devtype: int, timeout: float = plug.DEFAULT_TIMEOUT) -> FakePlug:
        """Use as replacement for plug.Plug."""
        self.connections.append((host, mac, devtype))
        return FakePlug(self, mac)


class FakePlug:
    """Same interface as plug.Plug, without network."""

    def __init__(self, device: FakeDevice, mac: str) -> None:
        self.device = device
        self.mac = mac.replace(":", "").lower()
        self.host = HOST

    def auth(self) -> None:
        if self.device.fail:
            raise plug.PlugError(f"No answer from the plug at {HOST} within 6 s")

    def read_memory(self) -> bytes:
        return bytes(self.device.memory)

    def write_memory(self, memory: bytes) -> bytes:
        plug.check_memory(memory)
        self.device.memory[plug.OFS_WEEKLY_COUNT : plug.OFS_WRITE_END] = memory[
            plug.OFS_WEEKLY_COUNT : plug.OFS_WRITE_END
        ]
        self.device.writes += 1
        return bytes(self.device.memory)
