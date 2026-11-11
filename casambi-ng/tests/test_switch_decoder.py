"""Tests for decoding wall switch events from input-stream-only units."""

from unittest.mock import patch

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


# Decrypted type-7 payloads with the time they arrived (s), captured on the
# same module: one short press on buttons 1 to 4, then one long press on
# buttons 1 and 4. The 1.35 s late copy of the button 2 release (age 0x8f
# against 0x08) is batched with a copy of the button 3 press, and the hold
# of the button 4 long press never came.
TIMED_CAPTURE = [
    (37.094, "1002400426041200020100"),  # button 1 press
    (37.213, "1002400426041200050100"),  # copy
    (37.625, "1002400428041200110200"),  # button 1 release
    (39.366, "1002410429041200030101"),  # button 2 press
    (39.756, "100241042b041200080201"),  # button 2 release
    (40.911, "100242042c041200040102"),  # button 3 press
    (40.917, "100242042c041200050102"),  # copy
    (41.106, "100241042b0412008f0201100242042c041200180102"),  # late copies
    (41.310, "100242042e041200050202"),  # button 3 release
    (41.414, "100242042e0412000e0202"),  # copy
    (41.743, "100242042e0412002f0202"),  # copy
    (42.765, "100243042f041200060103"),  # button 4 press
    (42.829, "100243042f0412000d0103"),  # copy
    (43.156, "1002430431041200030203"),  # button 4 release
    (43.359, "1002430431041200140203"),  # copy
    (51.958, "1002400432041200020100"),  # button 1 press
    (52.575, "1002400433041200050900"),  # button 1 hold
    (53.838, "10024004b2041200040c00"),  # button 1 release after hold
    (61.691, "100243044b041200020103"),  # button 4 press
    (63.633, "10024304dd041200070c03"),  # button 4 release after hold
]


def _decode_timed(packets: list[tuple[float, str]]) -> list:
    clock = [0.0]
    decoder = InputStreamSwitchEventDecoder()
    events = []
    with (
        patch(
            "custom_components.casambi_bt.switch_decoder.monotonic",
            side_effect=lambda: clock[0],
        ),
        patch("CasambiBt._switch.monotonic", side_effect=lambda: clock[0]),
    ):
        for seq, (at, packet) in enumerate(packets):
            clock[0] = at
            events.extend(decoder.decode(bytes.fromhex(packet), seq))
    return [(e.button, e.event) for e in events]


def test_late_copies_and_a_lost_hold() -> None:
    """Test one event per action despite late copies and a lost hold.

    The late copy of the button 2 release passed the library's 0.5 s
    window and was reported twice; the button 4 long press had no hold.
    """
    assert _decode_timed(TIMED_CAPTURE) == [
        (1, ButtonEventType.PRESS),
        (1, ButtonEventType.RELEASE),
        (2, ButtonEventType.PRESS),
        (2, ButtonEventType.RELEASE),
        (3, ButtonEventType.PRESS),
        (3, ButtonEventType.RELEASE),
        (4, ButtonEventType.PRESS),
        (4, ButtonEventType.RELEASE),
        (1, ButtonEventType.PRESS),
        (1, ButtonEventType.HOLD),
        (1, ButtonEventType.RELEASE_AFTER_HOLD),
        (4, ButtonEventType.PRESS),
        (4, ButtonEventType.HOLD),
        (4, ButtonEventType.RELEASE_AFTER_HOLD),
    ]


def test_a_late_hold_after_its_release_is_dropped() -> None:
    """Test a hold relayed after its release after hold, not seen twice."""
    assert _decode_timed(
        [
            (0.0, "1002400432041200020100"),  # press
            (2.0, "10024004b2041200040c00"),  # release after hold
            (2.4, "1002400433041200c80900"),  # the hold, 2 s late
        ]
    ) == [
        (1, ButtonEventType.PRESS),
        (1, ButtonEventType.HOLD),
        (1, ButtonEventType.RELEASE_AFTER_HOLD),
    ]


def test_the_same_origin_after_the_window_is_a_new_action() -> None:
    """Test that a reused origin long after counts again."""
    assert _decode_timed(
        [
            (0.0, "1002400426041200020100"),
            (60.0, "1002400426041200020100"),
        ]
    ) == [(1, ButtonEventType.PRESS), (1, ButtonEventType.PRESS)]
