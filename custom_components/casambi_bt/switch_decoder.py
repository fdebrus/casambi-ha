"""Switch event decoding for wired push-button interfaces.

The library's ``SwitchEventDecoder`` was written for EnOcean switches
(e.g. PTM215B), which report every action on two streams in the same
packet: the 0x06 button stream (PRESS / RELEASE) and the 0x12 input
stream (PRESS / RELEASE / HOLD / RELEASE_AFTER_HOLD). It therefore drops
PRESS and RELEASE from the 0x12 stream and relies on 0x06 for them.

Wired push-button interfaces such as the LEDsGO ``24VDC_4CH_PB_Winsol``
only emit the 0x12 stream, so every short press was silently dropped.
Captured on such a module (unit 4, decrypted type-7 payload)::

    1002 40 04b4 0412 0003 0100    button 1 PRESS   (opcode 0x40 + index)
    1002 40 04b4 0412 0006 0100    same frame retransmitted (same origin)
    1002 40 04b6 0412 0004 0200    button 1 RELEASE
    1002 43 04cc 0412 0002 0103    button 4 PRESS

This decoder keeps the library behaviour for units seen on the 0x06
stream and additionally accepts 0x12 PRESS / RELEASE from units that
never use it. The 0x06 frames of a packet are noted before its 0x12
frames are looked at, so an EnOcean switch is recognised from its first
packet on.
"""

from __future__ import annotations

import logging
from time import monotonic
from typing import Any, Final

from CasambiBt import _client
from CasambiBt._invocation import parse_invocation_stream
from CasambiBt._switch import ButtonEventType, SwitchEvent, SwitchEventDecoder

_LOGGER = logging.getLogger(__name__)

_TARGET_TYPE_BUTTON: Final = 0x06
_TARGET_TYPE_INPUT: Final = 0x12
_BUTTON_EVENT_MIN: Final = 29  # FunctionButtonEvent0
_BUTTON_EVENT_MAX: Final = 36  # FunctionButtonEvent7
_INPUT_EVENT_MIN: Final = 64  # FunctionNotifyInput0
_INPUT_EVENT_MAX: Final = 71  # FunctionNotifyInput7

_INPUT_ONLY_EVENTS: Final = {
    ButtonEventType.PRESS.value: ButtonEventType.PRESS,
    ButtonEventType.RELEASE.value: ButtonEventType.RELEASE,
}


class InputStreamSwitchEventDecoder(SwitchEventDecoder):
    """Switch event decoder that also serves input-stream-only units."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the decoder.

        Arguments are passed through unchanged: casambi-bt builds the decoder
        without arguments, casambi-bt-skk passes the client's logger.
        """
        super().__init__(*args, **kwargs)
        # Units seen on the 0x06 button stream; their 0x12 PRESS / RELEASE
        # stay ignored as in the library. The client builds a new decoder
        # on every connect, so this is learnt again after a reconnect.
        self._button_stream_units: set[int] = set()

    def decode(self, data: bytes, packet_seq: int) -> list[SwitchEvent]:
        """Decode a type-7 packet, including input-only PRESS / RELEASE."""
        frames = parse_invocation_stream(data)
        self._button_stream_units.update(
            frame.target >> 8
            for frame in frames
            if frame.target & 0xFF == _TARGET_TYPE_BUTTON
            and _BUTTON_EVENT_MIN <= frame.opcode <= _BUTTON_EVENT_MAX
        )

        events = super().decode(data, packet_seq)

        for frame in frames:
            unit_id = frame.target >> 8
            if (
                frame.target & 0xFF != _TARGET_TYPE_INPUT
                or not _INPUT_EVENT_MIN <= frame.opcode <= _INPUT_EVENT_MAX
                or not frame.payload
                or frame.payload[0] not in _INPUT_ONLY_EVENTS
                or unit_id in self._button_stream_units
            ):
                continue
            button_event_index = frame.opcode - _INPUT_EVENT_MIN
            event = _INPUT_ONLY_EVENTS[frame.payload[0]]
            if self._is_retransmit(unit_id, button_event_index, frame.origin):
                continue
            self._seen_origins[(unit_id, button_event_index, frame.origin)] = (
                monotonic()
            )
            self._logger.debug(
                "Accepted 0x12 %s from input-only unit %d button_index=%d",
                event.name,
                unit_id,
                button_event_index,
            )
            events.append(
                SwitchEvent(
                    button_event_index=button_event_index,
                    button=button_event_index + 1,
                    unit_id=unit_id,
                    target_type=_TARGET_TYPE_INPUT,
                    event=event,
                    flags=frame.flags,
                    extra_data=frame.payload[1:],
                )
            )

        return events


def install() -> None:
    """Make new library clients use the input-stream-aware decoder.

    The client creates its decoder when it is constructed, which happens on
    every connect, so replacing the class the client module refers to is
    enough. It is idempotent.
    """
    if _client.SwitchEventDecoder is not InputStreamSwitchEventDecoder:
        setattr(_client, "SwitchEventDecoder", InputStreamSwitchEventDecoder)
        _LOGGER.debug("Installed the input-stream-aware switch event decoder")
