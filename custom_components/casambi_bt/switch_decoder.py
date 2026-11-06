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
stream and decodes the whole 0x12 stream itself for units that never use
it. The 0x06 frames of a packet are noted before its 0x12 frames are
looked at, so an EnOcean switch is recognised from its first packet on.

Two things the mesh does to the frames, seen on the same module:

* **Late copies.** A frame can be relayed again well after the first copy:
  a button 2 release came back 1.35 s later with the same origin (its
  age field, in 10 ms, read 0x08 and then 0x8f). The library only drops a
  copy within 0.5 s, so it was reported twice. A new action always gets a
  new origin, so copies are dropped for DEDUP_WINDOW_SECONDS instead.
* **Lost frames.** A long press on button 4 came as press then release
  after hold, its hold frame lost. A release after hold that follows no
  hold gets the hold added in front of it, and a hold arriving after its
  release after hold is dropped.
"""

from __future__ import annotations

import logging
from time import monotonic
from typing import Any, Final

from CasambiBt import _client
from CasambiBt._invocation import InvocationFrame, parse_invocation_stream
from CasambiBt._switch import ButtonEventType, SwitchEvent, SwitchEventDecoder

_LOGGER = logging.getLogger(__name__)

_TARGET_TYPE_BUTTON: Final = 0x06
_TARGET_TYPE_INPUT: Final = 0x12
_BUTTON_EVENT_MIN: Final = 29  # FunctionButtonEvent0
_BUTTON_EVENT_MAX: Final = 36  # FunctionButtonEvent7
_INPUT_EVENT_MIN: Final = 64  # FunctionNotifyInput0
_INPUT_EVENT_MAX: Final = 71  # FunctionNotifyInput7


# A copy of a frame (same unit, button and origin) relayed within this
# long is dropped; 1.75 s late copies were seen.
DEDUP_WINDOW_SECONDS: Final = 5.0
# Forget origins older than the window once this many are kept.
_SEEN_ORIGINS_PRUNE_AT: Final = 64


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
        # The last event of each button of an input-only unit, to put back
        # a hold the mesh lost.
        self._phases: dict[tuple[int, int], ButtonEventType] = {}

    def reset(self) -> None:
        """Clear all cached state."""
        super().reset()
        self._phases.clear()

    def _is_retransmit(self, unit_id: int, button_index: int, origin: int) -> bool:
        """Return True if a frame is a copy of one accepted recently."""
        ts = self._seen_origins.get((unit_id, button_index, origin))
        return ts is not None and monotonic() - ts < DEDUP_WINDOW_SECONDS

    def _prune_origins(self) -> None:
        if len(self._seen_origins) < _SEEN_ORIGINS_PRUNE_AT:
            return
        now = monotonic()
        for key, ts in list(self._seen_origins.items()):
            if now - ts >= DEDUP_WINDOW_SECONDS:
                del self._seen_origins[key]

    def decode(self, data: bytes, packet_seq: int) -> list[SwitchEvent]:
        """Decode a type-7 packet, the 0x12 stream of input-only units included."""
        self._prune_origins()
        frames = parse_invocation_stream(data)
        self._button_stream_units.update(
            frame.target >> 8
            for frame in frames
            if frame.target & 0xFF == _TARGET_TYPE_BUTTON
            and _BUTTON_EVENT_MIN <= frame.opcode <= _BUTTON_EVENT_MAX
        )

        # Input-only units first: their accepted origins make the library
        # pass over the same frames below.
        own: list[SwitchEvent] = []
        for frame in frames:
            unit_id = frame.target >> 8
            if (
                frame.target & 0xFF != _TARGET_TYPE_INPUT
                or not _INPUT_EVENT_MIN <= frame.opcode <= _INPUT_EVENT_MAX
                or not frame.payload
                or unit_id in self._button_stream_units
            ):
                continue
            button_event_index = frame.opcode - _INPUT_EVENT_MIN
            if self._is_retransmit(unit_id, button_event_index, frame.origin):
                continue
            self._seen_origins[(unit_id, button_event_index, frame.origin)] = (
                monotonic()
            )
            try:
                event = ButtonEventType(frame.payload[0])
            except ValueError:
                event = ButtonEventType.UNKNOWN
            self._logger.debug(
                "Accepted 0x12 %s from input-only unit %d button_index=%d",
                event.name,
                unit_id,
                button_event_index,
            )
            own.extend(self._in_sequence(event, frame, unit_id, button_event_index))

        return super().decode(data, packet_seq) + own

    def _in_sequence(
        self,
        event: ButtonEventType,
        frame: InvocationFrame,
        unit_id: int,
        button_event_index: int,
    ) -> list[SwitchEvent]:
        """Return the events one accepted frame stands for.

        A release after hold with no hold since the press gets its lost hold
        back in front of it; a hold coming after its release after hold is a
        late copy of one already made up.
        """
        key = (unit_id, button_event_index)
        phase = self._phases.get(key)
        events: list[ButtonEventType] = [event]
        if (
            event is ButtonEventType.HOLD
            and phase is ButtonEventType.RELEASE_AFTER_HOLD
        ):
            self._logger.debug(
                "Dropped a HOLD after its RELEASE_AFTER_HOLD: unit %d button_index=%d",
                unit_id,
                button_event_index,
            )
            return []
        if (
            event is ButtonEventType.RELEASE_AFTER_HOLD
            and phase is not ButtonEventType.HOLD
        ):
            self._logger.debug(
                "Added the HOLD lost in the mesh: unit %d button_index=%d",
                unit_id,
                button_event_index,
            )
            events.insert(0, ButtonEventType.HOLD)
        self._phases[key] = event
        return [
            SwitchEvent(
                button_event_index=button_event_index,
                button=button_event_index + 1,
                unit_id=unit_id,
                target_type=_TARGET_TYPE_INPUT,
                event=item,
                flags=frame.flags,
                extra_data=frame.payload[1:],
            )
            for item in events
        ]


def install() -> None:
    """Make new library clients use the input-stream-aware decoder.

    The client creates its decoder when it is constructed, which happens on
    every connect, so replacing the class the client module refers to is
    enough. It is idempotent.
    """
    if _client.SwitchEventDecoder is not InputStreamSwitchEventDecoder:
        setattr(_client, "SwitchEventDecoder", InputStreamSwitchEventDecoder)
        _LOGGER.debug("Installed the input-stream-aware switch event decoder")
