#!/usr/bin/env python3
"""Configuration parameters and events: the shipped table is whole, and it
agrees with the event catalogue the decoder uses."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from flevio import catalog, params


def check(cond, what):
    if not cond:
        raise AssertionError(what)
    print("  ok  " + what)


def test_table_is_whole():
    ps = params.PARAMS.values()
    check(len(params.PARAMS) > 100, "%d parameters loaded" % len(params.PARAMS))
    check(all(p.type in ("u8", "u16", "u32", "u64", "bool", "enum", "str") for p in ps),
          "every parameter has a known type")
    check(all(p.access in params.ACCESS_LEVELS for p in ps), "every access level is described")
    check(all(p.group in params.GROUPS for p in ps), "every group exists")
    check(all(p.choices for p in ps if p.type == "enum"), "every choice lists its values")
    check(len({p.key for p in ps}) == len(params.PARAMS), "keys are unique")


def test_defaults_pass_their_own_checks():
    bad = []
    for p in params.PARAMS.values():
        if p.access == "locked":
            continue
        try:
            params.check(p.id, p.default)
        except ValueError as e:
            bad.append(str(e))
    check(not bad, "every default is a valid value%s" % (": " + "; ".join(bad) if bad else ""))


def test_commands():
    check(params.setparams({"move_send_period_s": 300, 140: 30}) == "SETPARAMS 145=300;140=30;",
          "SETPARAMS by name and by id")
    check(params.setparams({32: "mqtt"}, password="pw") == "pw SETPARAMS 32=2;",
          "a choice by name goes out as its number, after the password")
    check(params.getparams([145, "move_min_period_s"]) == "GETPARAMS 145,140", "GETPARAMS")
    check(params.parse("140=30;145=300;junk;") == {140: "30", 145: "300"}, "an answer parses")
    for bad in ({140: 99999}, {32: "pigeon"}, {1: "x"}, {"nope": 1}, {39: "a;b"}):
        try:
            params.setparams(bad)
        except ValueError:
            continue
        raise AssertionError("accepted %r" % (bad,))
    check(True, "out of range, unknown choice, locked, unknown and ';' are refused")
    check("(hidden)" in params.describe(38, "secret"), "a password is never shown")


def test_events_agree_with_the_decoder():
    doc = params._DOC["events"]
    missing = [e["code"] for e in doc if e["code"] not in catalog.EVENTS]
    check(not missing, "the decoder knows every event%s" % (" - missing %s" % missing if missing else ""))
    wrong = [e["code"] for e in doc if e["code"] in catalog.EVENTS and (
        catalog.EVENTS[e["code"]].name != e["name"]
        or catalog.EVENTS[e["code"]].essential != e["essential"])]
    check(not wrong, "names and essential flags match")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            print(name)
            fn()
    print("parameters: all good")
