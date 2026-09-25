"""
What the numbers mean: every event code and every IO element id.
(Configuration parameters - the ids in GETPARAMS and SETPARAMS - are in
:mod:`flevio.params`.)

The wire protocol in :mod:`flevio.protocol` carries integers - an event is a
byte, an IO element is an id and a value. This module is the dictionary that
turns them into something a person, or a database column, can use:

    >>> from flevio import catalog
    >>> catalog.event_name(1)
    'IGN_ON'
    >>> io = catalog.IO[66]
    >>> io.name, io.unit, io.scale
    ('ext_voltage', 'V', 0.001)
    >>> io.to_physical(13820)
    13.82

Two rules make this safe to build on:

* **An id never changes meaning.** A firmware release may add ids; it does
  not repurpose them. So a server that stores ``io[204]`` as engine RPM today
  stores engine RPM forever.
* **An unknown id is not an error.** The vehicle database can publish signals
  this table has not heard of yet - that is the point of the database. Keep
  the raw value under its numeric id and decode it later; :func:`describe`
  gives you a reasonable fallback name in the meantime.

Every value on the wire is an integer. ``scale`` and ``offset`` say how to get
back to the physical quantity::

    physical = raw * scale + offset

so ``IO[205]`` (coolant, ``degC + 40``) has ``scale=1, offset=-40``, and
``IO[201]`` (bus speed, ``km/h x1000``) has ``scale=0.001``. Values that are
already whole units have ``scale=1, offset=0``; :meth:`IoDef.to_physical`
returns an ``int`` for those, so you do not get ``13.0`` where you wanted 13.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

__all__ = [
    "Event",
    "EVENTS",
    "event_name",
    "event_by_name",
    "is_essential",
    "IoDef",
    "IO",
    "io_name",
    "io_by_name",
    "describe",
    "decode_io",
    "RESET_REASON",
    "HEALTH_REASON",
    "HEALTH_TASK",
    "reset_detail",
    "TIME_SOURCE",
    "BUS_PROTOCOL",
    "ODOMETER_SOURCE",
    "J1939_STATE",
    "PRIORITY",
]


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    """One entry in the event catalogue."""

    code: int
    name: str
    label: str
    essential: bool
    """An essential event cannot be masked off on the server channel.

    The fleet owner may switch off harsh-braking reports; nobody may switch
    off ignition, power or crash. If you are writing a server that offers an
    event editor, grey these out - the device will refuse them anyway.
    """


#: Every event the firmware can emit, by code. Codes not listed here are not
#: produced by any released firmware; treat one as a future event rather than
#: as corrupt data.
EVENTS: Dict[int, Event] = {
    e.code: e
    for e in (
        # --- power and lifecycle ---
        Event(0, "POWER_UP", "Device power up", True),
        Event(20, "POWER_CUT", "External power cut", True),
        Event(21, "POWER_RESTORE", "External power restored", True),
        Event(23, "POWER_OFF", "Device shutting down", True),
        Event(50, "CFG_CHANGED", "Configuration changed", True),
        Event(51, "FW_UPDATED", "Firmware updated", True),
        # --- ignition and motion ---
        Event(1, "IGN_ON", "Ignition ON", True),
        Event(2, "IGN_OFF", "Ignition OFF", True),
        Event(5, "TRIP_START", "Trip started", True),
        Event(6, "TRIP_STOP", "Trip ended", True),
        # Code 7 was END_STOP, which the device wrote in the same second as
        # the trip start and which said the same thing. It is retired rather
        # than reused: a server that still knows the old name sees the event
        # stop occurring, instead of seeing it come to mean something else.
        Event(19, "TOWING", "Movement with ignition off", False),
        # --- periodic and on demand ---
        Event(3, "ON_PERIODIC", "Periodic (ignition on)", False),
        Event(4, "OFF_PERIODIC", "Periodic (ignition off)", False),
        Event(24, "HEARTBEAT", "Heartbeat", False),
        Event(25, "POLL", "Response to server poll", False),
        Event(26, "LIVE", "On demand tracking point", False),
        Event(27, "TELEMETRY", "Vehicle telemetry", False),
        # --- driving behaviour ---
        Event(10, "SPEEDING", "Overspeeding started", False),
        Event(11, "SPEEDING_END", "Overspeeding ended", False),
        Event(12, "IDLING", "Excessive idling started", False),
        Event(13, "IDLING_END", "Idling ended", False),
        Event(14, "HARDACCEL", "Harsh acceleration", False),
        Event(15, "HARDBRAKE", "Harsh braking", False),
        Event(16, "HARDTURN", "Harsh cornering", False),
        Event(17, "SHOCK", "Crash / high-G impact", True),
        # --- positioning ---
        Event(29, "GNSS_LOST", "GNSS fix lost", False),
        Event(30, "GNSS_OK", "GNSS fix restored", False),
        # --- the vehicle bus ---
        Event(32, "VIN", "VIN read or changed", True),
        Event(33, "BUS_CONNECTED", "Vehicle bus connected", True),
        Event(34, "BUS_LOST", "Moving without ECU data", True),
        Event(35, "MIL_ON", "Check engine lamp ON", False),
        Event(36, "MIL_OFF", "Check engine lamp OFF", False),
        Event(37, "DTC_NEW", "New fault code", False),
        # --- the driver application ---
        Event(48, "BLE_CONN", "Driver app connected over BLE", False),
        Event(49, "BLE_DISCONN", "Driver app disconnected", False),
        # --- wired inputs and outputs ---
        # The record carries every input and output as extended elements
        # din<n> / dout<n>; these say when one moved.
        Event(52, "DOUT_CHANGED", "Output switched", False),
        Event(53, "DIN_CHANGED", "Input changed", False),
    )
}

_EVENTS_BY_NAME: Dict[str, Event] = {e.name: e for e in EVENTS.values()}


def event_name(code: int) -> str:
    """``1 -> 'IGN_ON'``; an unknown code comes back as ``'EVENT_63'``."""
    e = EVENTS.get(code)
    return e.name if e else "EVENT_%d" % code


def event_by_name(name: str) -> Optional[Event]:
    """``'IGN_ON' -> Event(...)``. Case-insensitive; ``None`` if unknown."""
    return _EVENTS_BY_NAME.get(name.strip().upper())


def is_essential(code: int) -> bool:
    """True when the device will refuse to have this event switched off."""
    e = EVENTS.get(code)
    return bool(e and e.essential)


#: Record priority, from the record header.
PRIORITY = {0: "low", 1: "high", 2: "panic"}


# --------------------------------------------------------------------------
# IO elements
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class IoDef:
    """One IO element id: what it is, and how to read its integer."""

    id: int
    name: str
    unit: str = ""
    scale: float = 1.0
    offset: float = 0.0
    desc: str = ""
    enum: Optional[Dict[int, str]] = None
    is_string: bool = False
    group: str = ""

    def to_physical(self, raw: int):
        """Turn the wire integer into the physical quantity.

        Returns the ``int`` unchanged when the element needs no conversion,
        so a database column typed ``integer`` stays that way.
        """
        if self.scale == 1.0 and self.offset == 0.0:
            return raw
        return raw * self.scale + self.offset

    def format(self, raw: int) -> str:
        """A one-line human rendering: ``'13.82 V'``, ``'on'``, ``'1FUJA6…'``."""
        if self.enum is not None:
            return self.enum.get(raw, str(raw))
        v = self.to_physical(raw)
        if isinstance(v, float):
            v = round(v, 4)
        return "%s %s" % (v, self.unit) if self.unit else str(v)


# Enumerations several elements share.
BUS_PROTOCOL = {0: "none", 1: "OBD2", 2: "J1939"}
ODOMETER_SOURCE = {1: "bus", 2: "GNSS"}
TIME_SOURCE = {
    0: "none (uptime, not a date)",
    1: "satellites",
    2: "cellular network",
}
RESET_REASON = {
    1: "power-on",
    2: "software (OTA or command)",
    3: "panic",
    4: "watchdog",
    5: "brownout",
    6: "external pin",
    7: "other",
}
#: The high nibble of ``reset_detail`` (IO 247): what the health monitor did.
#: 0 means the reset was not the firmware's own decision - read IO 246 then.
HEALTH_REASON = {
    0: "not our reset",
    1: "soft deadline overran",
    2: "scheduled self-reboot",
    3: "modem never registered",
    4: "low heap",
    5: "reset command",
    6: "rebooting into new firmware",
    7: "configuration rolled back",
    8: "new firmware not confirmed in time",
    9: "configuration reset to defaults",
}

#: The low nibble of ``reset_detail``: which task the monitor was watching
#: when it acted. Only meaningful for reason 1.
HEALTH_TASK = {
    0: "link",
    1: "dms",
    2: "sms",
    3: "ble",
    4: "vehicle",
    5: "gnss",
    6: "imu",
    7: "tracker",
}


def reset_detail(raw: int):
    """Split IO 247 into ``(reason, task)``, both as text.

    The firmware packs two things into one byte: ``reason << 4 | task``. The
    task half only means something when the reason is "soft deadline
    overran"; otherwise it is ``None``.

        >>> reset_detail(0x14)
        ('soft deadline overran', 'vehicle')
        >>> reset_detail(0x40)
        ('low heap', None)
    """
    reason = (raw >> 4) & 0x0F
    task = raw & 0x0F
    text = HEALTH_REASON.get(reason, "reason %d" % reason)
    if reason != 1:
        return text, None
    return text, HEALTH_TASK.get(task, "task %d" % task)
#: A J1939 two-bit status. The fourth value means "not available" and is
#: never sent: an element absent from a record is what "not available" looks
#: like on this protocol.
J1939_STATE = {0: "off", 1: "on", 2: "error"}
_ONOFF = {0: "off", 1: "on"}


def _d(
    id: int,
    name: str,
    unit: str = "",
    scale: float = 1.0,
    offset: float = 0.0,
    desc: str = "",
    enum: Optional[Dict[int, str]] = None,
    is_string: bool = False,
    group: str = "",
) -> IoDef:
    return IoDef(id, name, unit, scale, offset, desc, enum, is_string, group)


#: Every IO element id the firmware can send, by id.
#:
#: Groups: ``device`` (the tracker itself), ``bus`` (decoded from the
#: vehicle), ``meta`` (about the record rather than the vehicle).
IO: Dict[int, IoDef] = {
    d.id: d
    for d in (
        # --- the device itself -------------------------------------------
        _d(1, "ignition", enum=_ONOFF, group="device",
           desc="Ignition as the device sees it: the wire, the bus, or "
                "voltage, depending on ign_source."),
        _d(2, "movement", enum=_ONOFF, group="device",
           desc="The device considers the vehicle to be moving."),
        _d(21, "gsm_level", "", group="device",
           desc="Signal quality 0..5, from the modem's CSQ."),
        _d(66, "ext_voltage", "V", 0.001, group="device",
           desc="The vehicle supply at the connector, in millivolts on the "
                "wire."),
        _d(67, "batt_voltage", "V", 0.001, group="device",
           desc="The device's own backup cell, in millivolts on the wire."),
        _d(68, "batt_percent", "%", group="device"),
        _d(209, "accel_magnitude", "g", 0.001, group="device",
           desc="Peak acceleration behind a driving event, in milli-g."),
        _d(210, "hdop", "", 0.1, group="device",
           desc="Only on firmware before this codec; HDOP now travels in the "
                "record header. Kept so an old log still decodes."),
        # --- about the record --------------------------------------------
        _d(244, "pos_src", enum={0: "GNSS", 1: "dead reckoning"}, group="meta",
           desc="Where the position came from. Absent means the satellites. "
                "1 means the device had no fix and built the position from "
                "the distance the vehicle bus reported and a heading "
                "estimated from lateral acceleration - defensible for a "
                "minute or two in a tunnel, and never to be drawn on a map "
                "as though it were a fix. IO 243 says how wrong it may be."),
        _d(243, "pos_err_m", "m", group="meta",
           desc="Radius the device believes its reckoned position is good "
                "to. Only present with pos_src = 1. The device stops "
                "reckoning rather than let this grow without bound, so a "
                "record either carries a bounded estimate or no position "
                "at all."),
        _d(245, "cfg_revision", "", group="meta",
           desc="The configuration revision, on CFG_CHANGED. Line a change "
                "of behaviour up with the write that caused it."),
        _d(246, "reset_reason", enum=RESET_REASON, group="meta",
           desc="Why the device started, on POWER_UP."),
        _d(247, "reset_detail", group="meta",
           desc="What the health monitor did, packed as reason<<4 | task. "
                "Use catalog.reset_detail() to split it; 0 when the reset "
                "was not the firmware's own decision."),
        _d(248, "time_src", enum=TIME_SOURCE, group="meta",
           desc="Where this record's timestamp came from. ABSENT MEANS 1 "
                "(satellites). When it is 0 the header holds seconds since "
                "boot, not a date - re-date the record from its arrival."),
        _d(249, "simulated", enum=_ONOFF, group="meta",
           desc="This record was produced by the ELD Simulator. A server "
                "that ignores it will store invented mileage as real."),
        _d(252, "fw_version", is_string=True, group="meta",
           desc="Firmware version, on FW_UPDATED and POWER_UP."),
        _d(251, "serial", is_string=True, group="meta",
           desc="The serial number printed on the label."),
        # --- identity from the vehicle ------------------------------------
        _d(250, "vin", is_string=True, group="bus",
           desc="Vehicle identification number, as the bus reports it."),
        # --- the bus, firmware's own fixed set ----------------------------
        _d(200, "bus_protocol", enum=BUS_PROTOCOL, group="bus"),
        _d(201, "bus_speed", "km/h", 0.001, group="bus",
           desc="Road speed from the vehicle, x1000 on the wire."),
        _d(202, "odometer", "km", 0.001, group="bus",
           desc="Total distance in metres on the wire. Source in IO 207."),
        _d(203, "engine_hours", "h", 1.0 / 3600.0, group="bus",
           desc="Total engine time in seconds on the wire. Source in IO 208."),
        _d(204, "engine_rpm", "rpm", group="bus"),
        _d(205, "coolant_c", "degC", 1.0, -40.0, group="bus",
           desc="Coolant temperature, offset by 40 on the wire so it is "
                "never negative."),
        _d(206, "fuel_percent", "%", group="bus"),
        _d(207, "odometer_src", enum=ODOMETER_SOURCE, group="bus"),
        _d(208, "hours_src", enum=ODOMETER_SOURCE, group="bus"),
        _d(212, "park_brake", enum=J1939_STATE, group="bus"),
        _d(213, "demand_torque", "%", group="bus",
           desc="The driver's demand, as the pedal asks for it."),
        _d(214, "fuel_used", "L", 0.001, group="bus",
           desc="Total fuel, in millilitres on the wire."),
        _d(215, "engine_run_time", "s", group="bus",
           desc="Seconds since this engine start, not the lifetime total."),
        # --- engine (vehicle database) ------------------------------------
        _d(100, "engine_load", "%", group="bus", desc="Load at current speed."),
        _d(101, "actual_torque", "%", group="bus"),
        _d(102, "demanded_torque", "%", group="bus", desc="Engine demand."),
        _d(103, "accel_pedal", "%", 0.1, group="bus"),
        _d(104, "throttle_pos", "%", 0.1, group="bus"),
        _d(105, "oil_pressure", "kPa", group="bus"),
        _d(106, "oil_temp_c", "degC", group="bus"),
        _d(107, "fuel_temp_c", "degC", group="bus"),
        _d(108, "turbo_oil_temp_c", "degC", group="bus"),
        _d(109, "intercooler_c", "degC", group="bus"),
        _d(110, "boost_kpa", "kPa", group="bus"),
        _d(111, "intake_temp_c", "degC", group="bus"),
        _d(112, "barometric_kpa", "kPa", 0.1, group="bus"),
        _d(113, "ambient_temp_c", "degC", group="bus"),
        _d(114, "exhaust_temp_c", "degC", group="bus"),
        _d(115, "torque_mode", "", group="bus", desc="J1939 SPN 899."),
        # --- fuel ----------------------------------------------------------
        _d(120, "fuel_rate", "L/h", 0.001, group="bus",
           desc="Instantaneous consumption, millilitres per hour on the wire."),
        _d(121, "fuel_economy", "km/L", 0.001, group="bus",
           desc="Instantaneous economy, metres per litre on the wire."),
        _d(122, "trip_fuel", "L", 0.001, group="bus"),
        _d(123, "trip_distance", "km", 0.001, group="bus"),
        # --- driveline ------------------------------------------------------
        _d(130, "output_shaft_rpm", "rpm", group="bus"),
        _d(131, "input_shaft_rpm", "rpm", group="bus"),
        _d(132, "selected_gear", "", group="bus", desc="Negative is reverse."),
        _d(133, "current_gear", "", group="bus", desc="Negative is reverse."),
        _d(134, "gear_ratio", "", 0.001, group="bus"),
        _d(135, "clutch_slip", "%", 0.1, group="bus"),
        _d(136, "trans_oil_temp_c", "degC", group="bus"),
        _d(137, "driveline_engaged", enum=J1939_STATE, group="bus"),
        _d(138, "tc_lockup", enum=J1939_STATE, group="bus"),
        # --- switches and states ---------------------------------------------
        _d(140, "brake_switch", enum=J1939_STATE, group="bus"),
        _d(141, "clutch_switch", enum=J1939_STATE, group="bus"),
        _d(142, "cruise_active", enum=J1939_STATE, group="bus"),
        _d(143, "cruise_enabled", enum=J1939_STATE, group="bus"),
        _d(144, "cruise_set_kph", "km/h", group="bus"),
        _d(145, "pto_state", "", group="bus", desc="J1939 PTO governor state."),
        _d(146, "two_speed_axle", enum=J1939_STATE, group="bus"),
        _d(147, "low_idle_switch", enum=J1939_STATE, group="bus"),
        _d(148, "kickdown_switch", enum=J1939_STATE, group="bus"),
        _d(149, "vehicle_motion", enum=J1939_STATE, group="bus",
           desc="From the tachograph, where one is fitted."),
        # --- electrical --------------------------------------------------------
        _d(150, "battery_mv", "V", 0.001, group="bus",
           desc="Battery voltage as the BUS reports it - not the device's own "
                "measurement, which is IO 66/67."),
        # --- emissions aftertreatment -------------------------------------------
        _d(160, "def_level", "%", group="bus", desc="Diesel exhaust fluid."),
        _d(161, "def_temp_c", "degC", group="bus"),
        _d(162, "dpf_soot", "%", group="bus"),
        _d(163, "dpf_ash", "%", group="bus"),
        _d(164, "dpf_since_regen", "s", group="bus"),
        # --- dashboard lamps (J1939 DM1) -----------------------------------------
        _d(170, "lamp_mil", enum=J1939_STATE, group="bus",
           desc="Malfunction indicator - the check-engine light."),
        _d(171, "lamp_red_stop", enum=J1939_STATE, group="bus"),
        _d(172, "lamp_amber_warn", enum=J1939_STATE, group="bus"),
        _d(173, "lamp_protect", enum=J1939_STATE, group="bus"),
        _d(174, "dtc_spn", "", group="bus",
           desc="Suspect parameter number: which part. On DTC_NEW only."),
        _d(175, "dtc_fmi", "", group="bus",
           desc="Failure mode: how it is broken. On DTC_NEW only."),
        _d(176, "dtc_count", "", group="bus",
           desc="How many faults the vehicle reports active."),
        # --- engine fluids and the rest of the engine block ------------
        _d(116, "oil_level", "%", 0.1, group="bus", desc="SPN 98."),
        _d(117, "coolant_level", "%", 0.1, group="bus", desc="SPN 111."),
        _d(118, "coolant_pressure", "kPa", group="bus", desc="SPN 109."),
        _d(119, "crankcase_kpa", "kPa", 0.1, group="bus",
           desc="SPN 101. A rising crankcase pressure is worn rings."),
        _d(155, "turbo_rpm", "rpm", group="bus", desc="SPN 103."),
        _d(156, "rail_pressure", "kPa", group="bus",
           desc="SPN 157, injector metering rail."),
        _d(158, "intercool_thermo", "%", 0.1, group="bus", desc="SPN 1134."),
        _d(159, "ecu_temp_c", "degC", group="bus", desc="SPN 1136."),
        # --- fuel -------------------------------------------------------
        _d(124, "fuel_level_2", "%", 0.1, group="bus",
           desc="SPN 38, the second tank. Absent on a single-tank vehicle - "
                "which is not the same as zero, so do not sum it blindly."),
        _d(125, "fuel_deliv_kpa", "kPa", group="bus", desc="SPN 94."),
        _d(126, "fuel_filter_dp", "kPa", group="bus",
           desc="SPN 95. Climbs as the filter blocks."),
        _d(127, "avg_fuel_economy", "km/L", 0.001, group="bus", desc="SPN 185."),
        _d(128, "idle_fuel", "L", 0.001, group="bus",
           desc="SPN 236, lifetime fuel burned at idle."),
        _d(129, "idle_hours", "h", 1.0 / 3600.0, group="bus", desc="SPN 235."),
        # --- driveline and transmission ---------------------------------
        _d(139, "trans_oil_level", "%", 0.1, group="bus", desc="SPN 124."),
        _d(157, "trans_oil_kpa", "kPa", group="bus", desc="SPN 127."),
        _d(221, "retarder_mode", "", group="bus", desc="SPN 900."),
        _d(222, "retarder_pct", "%", group="bus", desc="SPN 520."),
        _d(223, "retarder_sel", "%", group="bus", desc="SPN 1716."),
        # --- electrical --------------------------------------------------
        _d(151, "battery_current", "A", group="bus",
           desc="SPN 114, net. Negative is discharge."),
        _d(152, "alternator_a", "A", group="bus", desc="SPN 115."),
        _d(153, "alternator_mv", "V", 0.001, group="bus", desc="SPN 167."),
        _d(154, "keyswitch_mv", "V", 0.001, group="bus", desc="SPN 158."),
        # --- aftertreatment ----------------------------------------------
        _d(165, "dpf_regen_state", "", group="bus", desc="SPN 3700."),
        _d(166, "dpf_diff_kpa", "kPa", 0.1, group="bus", desc="SPN 3251."),
        _d(167, "dpf_inlet_c", "degC", group="bus", desc="SPN 3242."),
        _d(168, "dpf_outlet_c", "degC", group="bus", desc="SPN 3246."),
        _d(169, "intake_nox", "ppm", group="bus", desc="SPN 3216."),
        # --- air brakes ---------------------------------------------------
        _d(177, "brake_primary_kpa", "kPa", group="bus", desc="SPN 1087."),
        _d(178, "brake_secondary_kpa", "kPa", group="bus", desc="SPN 1088."),
        _d(179, "brake_apply_kpa", "kPa", group="bus", desc="SPN 1086."),
        # --- OBD2 mixture control ------------------------------------------
        _d(194, "stft_b1", "%", 0.1, group="bus", desc="PID 06."),
        _d(195, "ltft_b1", "%", 0.1, group="bus", desc="PID 07."),
        _d(196, "stft_b2", "%", 0.1, group="bus", desc="PID 08."),
        _d(197, "ltft_b2", "%", 0.1, group="bus", desc="PID 09."),
        _d(198, "commanded_egr", "%", 0.1, group="bus", desc="PID 2C."),
        _d(199, "egr_error", "%", 0.1, group="bus", desc="PID 2D."),
        _d(232, "catalyst_c", "degC", 0.1, group="bus", desc="PID 3C."),
        _d(233, "evap_pa", "Pa", group="bus", desc="PID 32."),
        _d(234, "o2_s1_mv", "V", 0.001, group="bus", desc="PID 14."),
        _d(235, "fuel_rail_kpa", "kPa", group="bus", desc="PID 23."),
        _d(236, "lambda", "", 0.0001, group="bus",
           desc="PID 44, commanded air-fuel equivalence ratio."),
        _d(237, "rel_accel_pedal", "%", 0.1, group="bus", desc="PID 5A."),
        _d(238, "ref_torque_nm", "Nm", group="bus", desc="PID 63."),
        # --- air and ambient ------------------------------------------------
        _d(216, "air_inlet_c", "degC", group="bus",
           desc="SPN 172, measured at the filter. Not the same quantity as "
                "intake_temp_c, which is after the turbo and reads far hotter."),
        _d(217, "air_inlet_kpa", "kPa", group="bus", desc="SPN 106."),
        _d(218, "air_filter_dp", "kPa", 0.1, group="bus",
           desc="SPN 107. Climbs as the air filter blocks."),
        _d(219, "cab_temp_c", "degC", group="bus", desc="SPN 170."),
        _d(220, "road_surface_c", "degC", group="bus",
           desc="SPN 79, for a fleet that cares about ice."),
        # --- weight and tyres -------------------------------------------------
        _d(224, "axle_front_kg", "kg", group="bus", desc="SPN 582."),
        _d(225, "axle_rear_kg", "kg", group="bus", desc="The drive axles summed."),
        _d(226, "gross_weight_kg", "kg", group="bus", desc="SPN 181."),
        _d(227, "tyre_min_kpa", "kPa", group="bus",
           desc="SPN 241, the lowest wheel the device has seen reported."),
        _d(228, "tyre_max_c", "degC", group="bus", desc="SPN 242, the hottest."),
        # --- service ------------------------------------------------------------
        _d(229, "pto_hours", "h", 1.0 / 3600.0, group="bus", desc="SPN 248."),
        _d(230, "engine_revs", "krev", group="bus", desc="SPN 249, lifetime."),
        _d(231, "service_dist", "km", 0.001, group="bus",
           desc="SPN 914. Negative means the service is overdue."),
        _d(239, "washer_level", "%", 0.1, group="bus", desc="SPN 80."),
        # --- OBD2 only -------------------------------------------------------------
        _d(180, "maf_mgs", "mg/s", group="bus", desc="Mass air flow."),
        _d(181, "timing_advance", "deg", 0.1, group="bus", desc="Before TDC."),
        _d(182, "fuel_pressure", "kPa", group="bus"),
        _d(183, "map_kpa", "kPa", group="bus",
           desc="Intake manifold absolute pressure."),
        _d(184, "dist_mil_on", "km", 0.001, group="bus",
           desc="Distance travelled with the lamp on."),
        _d(185, "dist_since_clear", "km", 0.001, group="bus"),
        _d(186, "time_mil_on", "min", group="bus"),
        _d(187, "time_since_clear", "min", group="bus"),
        _d(188, "absolute_load", "%", group="bus"),
        _d(189, "rel_throttle", "%", 0.1, group="bus"),
        _d(190, "abs_throttle_b", "%", 0.1, group="bus"),
        _d(191, "cmd_throttle", "%", 0.1, group="bus"),
        _d(192, "module_voltage", "V", 0.001, group="bus"),
        _d(193, "tachograph_kph", "km/h", 0.001, group="bus"),
    )
}

_IO_BY_NAME: Dict[str, IoDef] = {d.name: d for d in IO.values()}


def io_name(io_id: int) -> str:
    """``66 -> 'ext_voltage'``; an unknown id comes back as ``'io_233'``."""
    d = IO.get(io_id)
    return d.name if d else "io_%d" % io_id


def io_by_name(name: str) -> Optional[IoDef]:
    """``'engine_rpm' -> IoDef(...)``; ``None`` when the name is unknown."""
    return _IO_BY_NAME.get(name.strip().lower())


def describe(io_id: int, raw) -> str:
    """One line for a log or a console: ``'engine_rpm = 1450 rpm'``.

    Works for ids this table has never heard of, because the device may be
    running a newer vehicle database than the server.
    """
    d = IO.get(io_id)
    if d is None:
        return "io_%d = %r" % (io_id, raw)
    if d.is_string:
        return "%s = %s" % (d.name, raw)
    return "%s = %s" % (d.name, d.format(int(raw)))


def decode_io(io: Dict[int, object]) -> Dict[str, object]:
    """Turn a record's ``{id: value}`` into ``{name: physical value}``.

    Unknown ids keep their raw value under ``io_<id>`` - never dropped. This
    is the one function most servers need: give it ``record.io`` and store
    the result.

        >>> decode_io({1: 1, 66: 13820, 204: 1450, 233: 7})
        {'ignition': 1, 'ext_voltage': 13.82, 'engine_rpm': 1450, 'io_233': 7}
    """
    out: Dict[str, object] = {}
    for io_id, value in io.items():
        d = IO.get(io_id)
        if d is None:
            out["io_%d" % io_id] = value
        elif d.is_string:
            out[d.name] = value
        else:
            out[d.name] = d.to_physical(int(value))
    return out


# --------------------------------------------------------------------------
# Extended elements
# --------------------------------------------------------------------------
#
# Ids that arrive inside element 254 - a 16-bit space for everything a
# tracker has that is not the vehicle bus: wired inputs and outputs, 1-Wire
# and RS-485 sensors, Bluetooth sensors, cell information, device health.
# The ranges are fixed in the firmware's proto.h; a new sensor takes the next
# id in its range and nothing else moves. Bit 15 set means the value is
# bytes. 60000 and up are a customer's own and are never assigned here.

EXT_BLOB = 0x8000

_EXT_FIXED = {
    1100: ("driver_id", ""), 1101: ("driver_id_kind", ""), 1102: ("driver_auth", ""),
    1240: ("tacho_state", ""), 1241: ("tacho_card", ""),
    1301: ("cell_mcc", ""), 1302: ("cell_mnc", ""), 1303: ("cell_tac", ""),
    1304: ("cell_id", ""), 1305: ("cell_rsrp", "dBm"), 1306: ("cell_rsrq", "dB x0.1"),
    1307: ("cell_rat", ""), 1310: ("cell_iccid", ""),
    1351: ("gnss_acc_m", "m"), 1352: ("gnss_fix_age_s", "s"), 1353: ("gnss_jamming", ""),
    1354: ("gnss_sats_view", ""), 1355: ("gnss_ttff_s", "s"), 1356: ("gnss_alt_acc_m", "m"),
    1401: ("dev_temp_c", "degC"), 1402: ("dev_heap_free", "B"), 1403: ("dev_modem_resets", ""),
    1404: ("dev_uptime_s", "s"), 1405: ("dev_queue", ""), 1410: ("dev_hw_rev", ""),
    1411: ("dev_model", ""),
    1451: ("geofence_id", ""), 1452: ("geofence_state", ""), 1461: ("trip_id", ""),
    1462: ("trip_distance_m", "m"), 1463: ("trip_duration_s", "s"), 1464: ("trip_idle_s", "s"),
    1465: ("trip_max_kph", "km/h"), 1501: ("media_id", ""), 1502: ("media_kind", ""),
}
_EXT_RANGES = (
    (1001, 8, "din", ""), (1011, 8, "dout", ""), (1021, 8, "ain", "mV"),
    (1031, 4, "pulse", ""), (1041, 2, "freq", "Hz x0.1"),
    (1111, 8, "temp", "degC x0.01"), (1121, 8, "temp_id", ""),
    (1201, 4, "fuel_level", "x0.1"), (1211, 4, "fuel_temp", "degC"),
    (1221, 4, "fuel_raw", ""), (1231, 4, "axle_load", "kg"),
)
_BLE_FIELDS = (
    ("mac", ""), ("rssi", "dBm"), ("batt_pct", "%"), ("batt_mv", "mV"),
    ("temp", "degC x0.01"), ("humidity", "% x0.1"), ("pressure", "hPa x0.1"),
    ("lux", "lx"), ("magnet", ""), ("moving", ""), ("move_count", ""),
    ("pitch", "deg"), ("roll", "deg"), ("flags", ""), ("custom", ""), ("adv", ""),
    ("name", ""), ("kind", ""), ("age_s", "s"), ("tpms_kpa", "kPa"), ("fuel_pct", "% x0.1"),
)

BLE_KIND = {1: "beacon", 2: "thermometer", 3: "door", 4: "fuel cap", 5: "tyre", 6: "custom"}
CELL_RAT = {1: "LTE-M", 2: "NB-IoT", 3: "GSM"}
DRIVER_ID_KIND = {1: "iButton", 2: "RFID", 3: "BLE card", 4: "keypad"}
DRIVER_AUTH = {0: "unknown", 1: "authorised", 2: "rejected"}
GEOFENCE_STATE = {1: "entered", 2: "left"}
MEDIA_KIND = {1: "photo", 2: "clip", 3: "bus capture", 4: "audio"}


def ext_name(ext_id: int) -> str:
    """``1351 -> 'gnss_acc_m'``, ``0x8000|2000 -> 'ble0_mac'``; unknown ids
    come back as ``'ext_<n>'``."""
    return _ext_def(ext_id)[0]


def _ext_def(ext_id: int):
    base = ext_id & ~EXT_BLOB
    if base in _EXT_FIXED:
        return _EXT_FIXED[base]
    for lo, n, name, unit in _EXT_RANGES:
        if lo <= base < lo + n:
            return ("%s%d" % (name, base - lo + 1), unit)
    if 2000 <= base < 2000 + 32 * 32:
        slot, fld = divmod(base - 2000, 32)
        name, unit = _BLE_FIELDS[fld] if fld < len(_BLE_FIELDS) else ("f%d" % fld, "")
        return ("ble%d_%s" % (slot, name), unit)
    if 4000 <= base <= 7999:
        return ("vdb_%d" % base, "")
    if base >= 60000:
        return ("private_%d" % base, "")
    return ("ext_%d" % base, "")


def describe_ext(ext_id: int, raw) -> str:
    """One line for a log: ``'ble0_temp = -1250 degC x0.01'``."""
    name, unit = _ext_def(ext_id)
    if isinstance(raw, (bytes, bytearray)):
        return "%s = %s" % (name, bytes(raw).hex())
    return "%s = %s%s" % (name, raw, (" " + unit) if unit else "")


def decode_ext(ext: Dict[int, object]) -> Dict[str, object]:
    """Turn a record's ``{ext_id: value}`` into ``{name: value}``. Bytes come
    out as hex strings, so the result is JSON-ready. Nothing is dropped."""
    return {_ext_def(k)[0]: (bytes(v).hex() if isinstance(v, (bytes, bytearray)) else v)
            for k, v in ext.items()}
