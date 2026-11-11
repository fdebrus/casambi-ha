"""Decoding of the sensor channels Winsol motors multiplex into their state.

A Winsol motor's 5-byte state starts with a 4-bit channel header (bits 0-3)
followed by a 24-bit value (bits 4-27); each push carries one channel. The
Lamel Standard V4.1 (fixture 38915) names its four channels by tag, channel
= tag + 1: Temperatuur (°C), #Overcurrent, Travel Distance and Module State.
The SO! screen (fixture 27814) reports its travel times the same way.
"""

from __future__ import annotations

from typing import Final

HEADER_BITS: Final = 4
VALUE_BITS: Final = 24

# A louvre motor's own temperature: outside this range a reading is not
# a temperature but an encoding the captures haven't shown yet.
TEMPERATURE_RANGE: Final = (-40, 85)


def decode_channel(raw: bytes | None) -> tuple[int, int] | None:
    """Return the (channel, value) multiplexed into a motor state."""
    if raw is None or len(raw) * 8 < HEADER_BITS + VALUE_BITS:
        return None
    packed = int.from_bytes(raw, "little")
    channel = packed & ((1 << HEADER_BITS) - 1)
    value = (packed >> HEADER_BITS) & ((1 << VALUE_BITS) - 1)
    return channel, value


def louvre_temperature(value: int) -> int | None:
    """Return the °C a louvre's 24-bit temperature channel carries.

    Only readings above 0 °C have been captured. Below zero the value is
    read as two's complement; anything else implausible is dropped rather
    than recorded (and taken by the frost lock) as hundreds of degrees.
    """
    if value & 1 << (VALUE_BITS - 1):
        value -= 1 << VALUE_BITS
    low, high = TEMPERATURE_RANGE
    return value if low <= value <= high else None


def read_bit(raw: bytes | None, offset: int) -> bool | None:
    """Read one bit of a raw state, or None if the state is too short."""
    if raw is None:
        return None
    byte_index, bit_index = divmod(offset, 8)
    if byte_index >= len(raw):
        return None
    return bool(raw[byte_index] >> bit_index & 1)
