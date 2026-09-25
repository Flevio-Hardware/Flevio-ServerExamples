#!/usr/bin/env python3
"""The command queue: what goes out when, and what counts as answered."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from flevio.cmdqueue import CommandQueue

IMEI = "865341041242696"
OTHER = "865341041238314"


def check(cond, what):
    if not cond:
        raise AssertionError(what)
    print("  ok  " + what)


def test_waits_for_the_device():
    q = CommandQueue(reply_timeout_s=90)
    a = q.add(IMEI, "GETSTATUS", now=0)
    check(q.due(now=1) == [], "nothing goes out while the device is offline")
    q.set_online(IMEI, True, now=2)
    due = q.due(now=3)
    check([c.id for c in due] == [a], "it goes out once the device is online")


def test_one_at_a_time_in_order():
    q = CommandQueue()
    q.set_online(IMEI, True, now=0)
    a = q.add(IMEI, "GETINFO", now=0)
    b = q.add(IMEI, "GETSTATUS", now=1)
    check([c.id for c in q.due(now=2)] == [a], "the oldest first")
    q.mark_sent(a, now=2)
    check(q.due(now=3) == [], "nothing else while one is in flight")
    done = q.on_reply(IMEI, a & 0xFFFF, "FE-OT100 fw=0.0.5", now=4)
    check(done and done.state == "done" and done.reply.startswith("FE-OT100"),
          "the reply completes it, matched by id")
    check([c.id for c in q.due(now=5)] == [b], "then the next one")


def test_devices_are_independent():
    q = CommandQueue()
    q.set_online(IMEI, True, now=0)
    q.set_online(OTHER, True, now=0)
    a = q.add(IMEI, "GETINFO", now=0)
    b = q.add(OTHER, "GETINFO", now=0)
    check(sorted(c.id for c in q.due(now=1)) == sorted([a, b]), "one per device, in parallel")


def test_retry_then_unanswered():
    q = CommandQueue(reply_timeout_s=90)
    q.set_online(IMEI, True, now=0)
    a = q.add(IMEI, "GETSTATUS", now=0)
    for attempt in range(3):
        due = q.due(now=100 * attempt + 1)
        check([c.id for c in due] == [a], "attempt %d goes out" % (attempt + 1))
        q.mark_sent(a, now=100 * attempt + 1)
    q.due(now=1000)
    check(q.get(a).state == "unanswered", "after three unanswered tries it stops")
    late = q.on_reply(IMEI, a, "ign=1", now=1001)
    check(late and late.state == "done", "a late answer still completes it")


def test_actions_are_not_repeated():
    q = CommandQueue(reply_timeout_s=90)
    q.set_online(IMEI, True, now=0)
    a = q.add(IMEI, "secret RESET", now=0)
    check(q.get(a).max_attempts == 1, "RESET is tried once")
    q.mark_sent(a, now=1)
    check(q.due(now=200) == [], "and not sent again when unanswered")
    check(q.get(a).state == "unanswered", "it is reported as unanswered instead")


def test_offline_while_in_flight():
    q = CommandQueue(reply_timeout_s=90)
    q.set_online(IMEI, True, now=0)
    a = q.add(IMEI, "GETSTATUS", now=0)
    q.mark_sent(a, now=1)
    q.device_gone(IMEI, now=2)
    check(q.due(now=200) == [], "nothing goes out to a device that is away")
    q.set_online(IMEI, True, now=300)
    check([c.id for c in q.due(now=301)] == [a], "the unanswered one is retried when it is back")


def test_expiry_and_cancel():
    q = CommandQueue(ttl_s=3600)
    a = q.add(IMEI, "GETSTATUS", now=0)
    b = q.add(IMEI, "GETINFO", now=0)
    check(q.cancel(b), "a pending command can be cancelled")
    q.set_online(IMEI, True, now=7200)
    check(q.due(now=7200) == [], "a command past its time to live is not sent")
    check(q.get(a).state == "expired", "it is marked expired")


def test_wrong_reply_is_ignored():
    q = CommandQueue()
    q.set_online(IMEI, True, now=0)
    a = q.add(IMEI, "GETSTATUS", now=0)
    q.mark_sent(a, now=1)
    check(q.on_reply(IMEI, a + 1, "?", now=2) is None, "a reply with another id matches nothing")
    check(q.on_reply(OTHER, a, "?", now=2) is None, "nor does one from another device")
    check(q.get(a).state == "sent", "the command is still waiting")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            print(name)
            fn()
    print("command queue: all good")
