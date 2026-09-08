from __future__ import annotations

import math
from datetime import datetime, timezone


def view_geometry(latitude: float, longitude: float, satellite_longitude: float = -75.0,
                  semi_major: float = 6378137.0, semi_minor: float = 6356752.31414,
                  perspective_height: float = 35786023.0) -> tuple[float, float]:
    """Scalar translation of notebook Cell 2 calculateViewGeometry."""
    lat, lon, sat_lon = map(math.radians, (latitude, longitude, satellite_longitude))
    e2 = 1 - semi_minor**2 / semi_major**2
    n = semi_major / math.sqrt(1 - e2 * math.sin(lat)**2)
    site = (n*math.cos(lat)*math.cos(lon), n*math.cos(lat)*math.sin(lon), n*(1-e2)*math.sin(lat))
    distance = semi_major + perspective_height
    delta = (distance*math.cos(sat_lon)-site[0], distance*math.sin(sat_lon)-site[1], -site[2])
    east = -math.sin(lon)*delta[0] + math.cos(lon)*delta[1]
    north = -math.sin(lat)*math.cos(lon)*delta[0] - math.sin(lat)*math.sin(lon)*delta[1] + math.cos(lat)*delta[2]
    up = math.cos(lat)*math.cos(lon)*delta[0] + math.cos(lat)*math.sin(lon)*delta[1] + math.sin(lat)*delta[2]
    slant = math.sqrt(sum(value*value for value in delta))
    return math.degrees(math.acos(max(-1, min(1, up/slant)))), (math.degrees(math.atan2(east, north))+360)%360


def solar_angles(latitude: float, longitude: float, when: datetime) -> tuple[float, float]:
    """NOAA solar-position approximation; exact research helper comparison remains pending."""
    when = when.astimezone(timezone.utc)
    day = when.timetuple().tm_yday
    hour = when.hour + when.minute/60 + when.second/3600 + when.microsecond/3.6e9
    gamma = 2*math.pi/365 * (day-1 + (hour-12)/24)
    eqtime = 229.18*(0.000075 + 0.001868*math.cos(gamma) - 0.032077*math.sin(gamma)
                     - 0.014615*math.cos(2*gamma) - 0.040849*math.sin(2*gamma))
    decl = (0.006918 - 0.399912*math.cos(gamma) + 0.070257*math.sin(gamma)
            - 0.006758*math.cos(2*gamma) + 0.000907*math.sin(2*gamma)
            - 0.002697*math.cos(3*gamma) + 0.00148*math.sin(3*gamma))
    hour_angle = math.radians((hour*60 + eqtime + 4*longitude)/4 - 180)
    lat = math.radians(latitude)
    cos_zenith = math.sin(lat)*math.sin(decl) + math.cos(lat)*math.cos(decl)*math.cos(hour_angle)
    zenith = math.degrees(math.acos(max(-1, min(1, cos_zenith))))
    azimuth = (math.degrees(math.atan2(math.sin(hour_angle), math.cos(hour_angle)*math.sin(lat)-math.tan(decl)*math.cos(lat))) + 180) % 360
    return zenith, azimuth


def angle_difference(a: float, b: float) -> float:
    return abs((a-b+180)%360-180)
