"""Decoding of the rotating readings of a Casambi sensor platform.

The Sensor Platform V4 fixture declares, after presence (bits 0-1) and lux
(bits 2-13), a 4-bit packet header at bits 14-17 and a 16-bit value at bits
18-33; each state push carries one reading. The library decodes these
packets from fixed bytes instead (type from bits 14-15, value from bits
16-23), which keeps only the low six bits of the value, shifted left by two,
with the header's upper bits below them. That is where the documented
"wind / 4" and "rain 1 dry, 5 raining" came from, and it wraps every reading
at 64: a 70 km/h gust read as 6 km/h. The packets are therefore decoded here
from the fixture layout, which gives the same values below 64.
"""

from __future__ import annotations

from typing import Final

PACKET_WIND: Final = 1  # km/h
PACKET_SOLAR: Final = 2
PACKET_PIR: Final = 3  # 0 = no motion
PACKET_RAIN: Final = 4  # 0 = dry, 1 = raining

_HEADER_OFFSET: Final = 14
_HEADER_BITS: Final = 4
_VALUE_BITS: Final = 16


def decode_platform_packet(raw: bytes | None) -> tuple[int, int] | None:
    """Return the (packet, value) a sensor platform state carries, if any."""
    if raw is None or len(raw) * 8 < _HEADER_OFFSET + _HEADER_BITS + _VALUE_BITS:
        return None
    packed = int.from_bytes(raw, "little") >> _HEADER_OFFSET
    packet = packed & ((1 << _HEADER_BITS) - 1)
    if packet == 0:
        # An all-zero state (before the first reading) carries nothing.
        return None
    value = (packed >> _HEADER_BITS) & ((1 << _VALUE_BITS) - 1)
    return packet, value
