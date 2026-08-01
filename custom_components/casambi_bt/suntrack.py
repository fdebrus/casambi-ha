"""Sun tracking for louvre covers.

Replicates the "automatic" mode of intelligent louvre motors in the
integration: the louvre angle is computed from the current solar position
so that the slats stay perpendicular to the sun's rays, blocking direct
sunlight while letting in as much indirect light as possible.

The classic venetian-blind geometry is used: the profile angle phi is the
sun's elevation projected onto the vertical plane perpendicular to the
slat axis, tan(phi) = tan(elevation) / cos(azimuth difference). Slats
perpendicular to the rays are then tilted 90 - phi degrees from
horizontal. A user offset shifts the result toward more sun (positive)
or more shade (negative).
"""

from __future__ import annotations

from math import atan2, cos, degrees, radians, tan

from homeassistant.core import HomeAssistant
from homeassistant.helpers.sun import get_astral_location
from homeassistant.util import dt as dt_util

# Hardware range of the louvre slider in degrees (Winsol Lamel: 0-142).
LOUVRE_MAX_ANGLE = 142.0


def get_sun_position(hass: HomeAssistant) -> tuple[float, float]:
    """Return the current solar (elevation, azimuth) in degrees."""
    location, elevation = get_astral_location(hass)
    now = dt_util.utcnow()
    return (
        location.solar_elevation(now, elevation),
        location.solar_azimuth(now, elevation),
    )


def compute_louvre_angle(
    sun_elevation: float,
    sun_azimuth: float,
    louvre_azimuth: float,
    offset: float = 0.0,
) -> float | None:
    """Return the target louvre angle in degrees, or None to leave it alone.

    None is returned when the sun is below the horizon or behind the
    pergola (no direct sunlight to manage).

    :param sun_elevation: Solar elevation in degrees.
    :param sun_azimuth: Solar azimuth in degrees.
    :param louvre_azimuth: Compass direction the pergola faces in degrees.
    :param offset: User preference in degrees; positive lets in more sun.
    """
    if sun_elevation <= 0:
        return None

    rel = radians(sun_azimuth - louvre_azimuth)
    if cos(rel) <= 0:
        return None

    profile = degrees(atan2(tan(radians(sun_elevation)), cos(rel)))
    angle = 90.0 - profile + offset
    return min(max(angle, 0.0), LOUVRE_MAX_ANGLE)
