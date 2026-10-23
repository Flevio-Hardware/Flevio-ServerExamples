"""Fault code list (IO 253): unpacking and the panel shape."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from flevio import catalog  # noqa: E402


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


# The bytes a real tractor's engine sent (DM1, SPN 2791, FMI 13, twice) and
# its body controller (SPN 6893, FMI 4, 126 times), as the device lists them.
d = catalog.decode_dtc_list("0:2:033006893041260 000002791130020")
check(not d["engine_light"] and d["count"] == 2, "light/count")
check(d["codes"][1]["spn"] == 2791 and d["codes"][1]["fmi"] == 13 and d["codes"][1]["occurrences"] == 2, "engine code")
check(d["codes"][0]["sa"] == 33 and d["codes"][0]["source"] == "Body controller", "source")

# The hex form must be the vehicle's own DM1 bytes: E7 0A 0D 02 for the
# engine code, read little-endian.
check(d["codes"][1]["hex"] == "0x020D0AE7", d["codes"][1]["hex"])
check(catalog.dtc_hex(3216, 10, 105) == "0x690A0C90", catalog.dtc_hex(3216, 10, 105))
check(catalog.dtc_hex(520192, 5, 10) == "0x0AE5F000", "SPN above 16 bits")

p = catalog.dtc_for_panel(catalog.decode_dtc_list("1:3:000003216101050 000000100010030 P0420"))
check(p[0] == {"bus": "j1939", "code": "0x690A0C90",
               "description": "SPN: 3216 \u2013 Aftertreatment 1 Intake NOx FMI: 10 Count: 105"}, p[0])
check(p[1]["description"].startswith("SPN: 100 \u2013 Engine oil pressure FMI: 1 Count: 3"), p[1])
check(p[2] == {"bus": "obdii", "code": "P0420", "description": "P0420"}, p[2])

# An SPN with no name gets no name part.
p = catalog.dtc_for_panel(catalog.decode_dtc_list("0:1:033006893041260"))
check(p[0]["description"] == "SPN: 6893 FMI: 4 Count: 126", p[0])

# No faults is an empty list, not an error.
check(catalog.dtc_for_panel(catalog.decode_dtc_list("0:0:")) == [], "empty")
print("dtc list: all good")
