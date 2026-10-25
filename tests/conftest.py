"""Fixtures shared across test modules; the fakes they are built from live in `fakes.py`."""

from typing import TYPE_CHECKING, cast

import pytest
from fakes import FakeOutput, FakeTouchscreen

from armada_trackpad import devices
from armada_trackpad.output import Availability
from armada_trackpad.trackpad import Trackpad


if TYPE_CHECKING:
    from armada_trackpad.evdev import Touchscreen


@pytest.fixture
def started_trackpad() -> Trackpad:
    """A Nova trackpad on fake devices as startup leaves it: both pads created, available, mode off.

    The availability is set by hand, since no check task runs, so mode on is allowed from the start.
    """
    # The fake has every `Touchscreen` method the trackpad calls.
    trackpad = Trackpad(devices.RETROID_POCKET_NOVA, cast("Touchscreen", FakeTouchscreen()), FakeOutput())
    trackpad.apply(trackpad.settings)
    trackpad.set_availability(Availability(True))
    return trackpad
