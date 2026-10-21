"""Tests for decoding wall switch events from input-stream-only units."""

from CasambiBt import _client
from CasambiBt._switch import ButtonEventType

from custom_components.casambi_bt.switch_decoder import (
    InputStreamSwitchEventDecoder,
    install,
)

# Decrypted type-7 payloads (without the packet type byte) captured on a
# LEDsGO 24VDC_4CH_PB_Winsol (unit 4) while pressing buttons 1 to 4.
CAPTURE = [
    "10024004b4041200030100",  # button 1 press
    "10024004b4041200060100",  # retransmit (same origin)
    "10024004b6041200040200",  # button 1 release
    "10024004b7041200030100",
    "10024004b9041200040200",
    "10024104ba041200030101",  # button 2 press
    "10024104bd041200030101",
    "10024104bf041200050201",
    "10024104c2041200040201",
    "10024204c3041200030102",  # button 3 press
    "10024204c3041200040102",  # retransmit
    "10024204c6041200030102",
    "10024204c6041200040102",  # retransmit
    "10024204c80412000d0202",
    "10024204cb0412000d0202",
    "10024304cc041200020103",  # button 4 press
    "10024304cc041200030103",  # retransmit
    "10024304ce041200040203",
    "10024304cf041200030103",
    "10024304d1041200060203",
    "10024304d2041200050103",
    "10024304d4041200060203",
]


def _frame(opcode: int, origin: int, target_type: int, payload: str) -> str:
    """Build one INVOCATION frame for unit 7."""
    data = bytes.fromhex(payload)
    flags = len(data)
    return f"{flags:04x}{opcode:02x}{origin:04x}07{target_type:02x}0003" + payload


def _decode(decoder: InputStreamSwitchEventDecoder, *packets: str) -> list:
    events = []
    for seq, packet in enumerate(packets):
        events.extend(decoder.decode(bytes.fromhex(packet), seq))
    return events


def test_input_only_unit_presses_and_releases() -> None:
    """Test that every captured action is reported once, retransmits dropped."""
    events = _decode(InputStreamSwitchEventDecoder(), *CAPTURE)

    assert [(e.unit_id, e.button, e.event) for e in events] == [
        (4, 1, ButtonEventType.PRESS),
        (4, 1, ButtonEventType.RELEASE),
        (4, 1, ButtonEventType.PRESS),
        (4, 1, ButtonEventType.RELEASE),
        (4, 2, ButtonEventType.PRESS),
        (4, 2, ButtonEventType.PRESS),
        (4, 2, ButtonEventType.RELEASE),
        (4, 2, ButtonEventType.RELEASE),
        (4, 3, ButtonEventType.PRESS),
        (4, 3, ButtonEventType.PRESS),
        (4, 3, ButtonEventType.RELEASE),
        (4, 3, ButtonEventType.RELEASE),
        (4, 4, ButtonEventType.PRESS),
        (4, 4, ButtonEventType.RELEASE),
        (4, 4, ButtonEventType.PRESS),
        (4, 4, ButtonEventType.RELEASE),
        (4, 4, ButtonEventType.PRESS),
        (4, 4, ButtonEventType.RELEASE),
    ]
    assert all(e.target_type == 0x12 for e in events)


def test_input_only_unit_hold_still_decoded() -> None:
    """Test that HOLD and RELEASE_AFTER_HOLD keep coming from the library."""
    events = _decode(
        InputStreamSwitchEventDecoder(),
        "10024004e0041200030100",  # press
        "10024004e1041200030900",  # hold
        "10024004e2041200030c00",  # release after hold
    )
    assert [e.event for e in events] == [
        ButtonEventType.PRESS,
        ButtonEventType.HOLD,
        ButtonEventType.RELEASE_AFTER_HOLD,
    ]


def test_enocean_switch_not_doubled() -> None:
    """Test that a unit on both streams keeps the library behaviour.

    An EnOcean switch sends the same press on the 0x06 and 0x12 streams in
    one packet; it must be reported once, from 0x06 only.
    """
    press = _frame(29, 0x0100, 0x06, "00") + _frame(64, 0x0101, 0x12, "0100")
    release = _frame(29, 0x0102, 0x06, "00") + _frame(64, 0x0103, 0x12, "0200")
    events = _decode(InputStreamSwitchEventDecoder(), press, release)

    assert [(e.unit_id, e.event, e.target_type) for e in events] == [
        (7, ButtonEventType.PRESS, 0x06),
        (7, ButtonEventType.RELEASE, 0x06),
    ]


def test_install_replaces_client_decoder() -> None:
    """Test that new library clients build the input-aware decoder."""
    install()
    install()
    assert _client.SwitchEventDecoder is InputStreamSwitchEventDecoder
