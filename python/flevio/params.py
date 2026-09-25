"""
Configuration parameters: every id a device answers GETPARAMS with and
accepts in SETPARAMS - what it is called, its unit, range and default, and
who may change it.

The table is ``params.json`` next to this file. It is generated from the
firmware's own parameter registry, the same one the device's settings and
the Configurator are generated from, so it cannot describe a parameter the
firmware does not have. Do not edit it by hand; take the new one with a new
firmware release.

    >>> from flevio import params
    >>> p = params.get("move_send_period_s")          # or params.get(145)
    >>> p.id, p.title, p.unit, p.default
    (145, 'Send period', 's', 120)
    >>> params.setparams({"move_send_period_s": 300, 140: 30})
    'SETPARAMS 145=300;140=30;'
    >>> params.parse("140=30;145=300;")
    {140: '30', 145: '300'}
    >>> print(params.describe(32, "2"))
    32 srv1_protocol = mqtt (MQTT (the customer's broker))   [Protocol]

Text format, both directions: ``id=value`` pairs, each ended by ``;``. A
value is always a number, except for text: yes/no is ``1`` / ``0`` and a
choice is its number (``32=2`` is MQTT). The device does not take names;
:func:`check` and :func:`setparams` do, and send the number. Secret parameters (passwords) are never sent back unless the channel
presented the configuration password.

Access, per parameter:

* ``user``       - any channel may change it: console, driver app, server, SMS
* ``protected``  - the command must start with the configuration password
* ``locked``     - only the device management service (identity, keys)

A device refuses a value out of range and says so in its answer
(``OK applied=1 rejected=1 denied=0``); :func:`check` catches the same
mistakes before anything is sent.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Tuple, Union

__all__ = ["Param", "PARAMS", "GROUPS", "ACCESS_LEVELS", "SCHEMA_VERSION",
           "get", "find", "parse", "describe", "check", "setparams", "getparams"]

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "params.json")

_INT_RANGE = {"u8": (0, 0xFF), "u16": (0, 0xFFFF), "u32": (0, 0xFFFFFFFF),
              "u64": (0, 0xFFFFFFFFFFFFFFFF), "bool": (0, 1)}


@dataclass(frozen=True)
class Param:
    id: int
    key: str
    group: str
    type: str                                  # u8 u16 u32 u64 bool enum str
    default: object
    title: str
    access: str                                # user / protected / locked
    help: str = ""
    unit: str = ""
    min: Optional[int] = None
    max: Optional[int] = None
    length: Optional[int] = None               # str: the longest value
    secret: bool = False
    choices: Tuple[Tuple[int, str, str], ...] = ()   # enum: (value, name, label)
    depends: Optional[Tuple[int, int]] = None  # only meaningful when param == value
    models: Tuple[str, ...] = ()               # empty: every model

    @property
    def is_text(self) -> bool:
        return self.type == "str"

    def choice(self, value) -> Optional[Tuple[int, str, str]]:
        """The enum entry for a number or a name, ``None`` if there is none."""
        for c in self.choices:
            if str(value).strip().lower() in (str(c[0]), c[1].lower()):
                return c
        return None

    def show(self, value) -> str:
        """A value as a person reads it: ``'mqtt (MQTT ...)'``, ``'300 s'``."""
        if self.secret:
            return "(hidden)" if value not in ("", None) else "(empty)"
        if self.type == "enum":
            c = self.choice(value)
            return "%s (%s)" % (c[1], c[2]) if c else "%s (unknown choice)" % value
        if self.type == "bool":
            return "yes" if str(value).strip() in ("1", "true", "yes", "on") else "no"
        if self.is_text:
            return '"%s"' % value
        return "%s %s" % (value, self.unit) if self.unit else str(value)


def _load() -> dict:
    with open(_PATH, encoding="utf-8") as f:
        return json.load(f)


def _param(d: dict) -> Param:
    dep = d.get("depends")
    return Param(
        id=d["id"], key=d["key"], group=d["group"], type=d["type"], default=d["default"],
        title=d["title"], access=d["access"], help=d.get("help", ""), unit=d.get("unit", ""),
        min=d.get("min"), max=d.get("max"), length=d.get("len"), secret=bool(d.get("secret")),
        choices=tuple((c["v"], c["name"], c.get("label", c["name"])) for c in d.get("enum", ())),
        depends=(dep["param"], dep["value"]) if dep and "value" in dep else None,
        models=tuple(d.get("models", ())),
    )


_DOC = _load()

#: Every parameter, by id.
PARAMS: Dict[int, Param] = {d["id"]: _param(d) for d in _DOC["params"]}
_BY_KEY: Dict[str, Param] = {p.key: p for p in PARAMS.values()}

#: ``{group id: title}``, in the Configurator's order.
GROUPS: Dict[str, str] = {g["id"]: g["title"]
                          for g in sorted(_DOC["groups"], key=lambda g: g.get("order", 0))}
ACCESS_LEVELS: Dict[str, str] = dict(_DOC["access_levels"])
SCHEMA_VERSION: int = _DOC["schema_version"]


def get(which: Union[int, str]) -> Optional[Param]:
    """A parameter by id (``145``, ``"145"``) or key (``"move_send_period_s"``)."""
    if isinstance(which, int) or str(which).strip().isdigit():
        return PARAMS.get(int(which))
    return _BY_KEY.get(str(which).strip().lower())


def find(words: str) -> List[Param]:
    """Parameters whose key, title or help mention every word: ``find("period")``."""
    ws = words.lower().split()
    return [p for p in PARAMS.values()
            if all(w in ("%s %s %s" % (p.key, p.title, p.help)).lower() for w in ws)]


def parse(text: str) -> Dict[int, str]:
    """``"140=30;145=300;"`` -> ``{140: '30', 145: '300'}``: a GETPARAMS answer,
    or the body of a SETPARAMS. Anything that is not ``id=value`` is skipped."""
    out: Dict[int, str] = {}
    for part in text.split(";"):
        k, sep, v = part.strip().partition("=")
        if sep and k.strip().isdigit():
            out[int(k)] = v
    return out


def describe(pid: int, value) -> str:
    """One line: ``'145 move_send_period_s = 300 s   [Send period]'``."""
    p = PARAMS.get(int(pid))
    if p is None:
        return "%s = %s (a parameter this table does not know)" % (pid, value)
    return "%d %s = %s   [%s]" % (p.id, p.key, p.show(value), p.title)


def check(which: Union[int, str], value) -> Tuple[int, str]:
    """Validate one value; returns ``(id, text for the wire)`` or raises
    ``ValueError`` saying what is wrong. Names are accepted for choices
    (``"mqtt"``) and ``True``/``False`` for yes/no."""
    p = get(which)
    if p is None:
        raise ValueError("no parameter %r" % (which,))
    if p.access == "locked":
        raise ValueError("%d %s is locked: only device management sets it" % (p.id, p.key))
    if p.is_text:
        text = str(value)
        if ";" in text:
            raise ValueError("%d %s: a value cannot contain ';'" % (p.id, p.key))
        if p.length is not None and len(text.encode()) > p.length:
            raise ValueError("%d %s: at most %d bytes" % (p.id, p.key, p.length))
        return p.id, text
    if p.type == "enum":
        c = p.choice(value)
        if c is None:
            raise ValueError("%d %s: one of %s" % (
                p.id, p.key, ", ".join("%d=%s" % (v, n) for v, n, _ in p.choices)))
        return p.id, str(c[0])
    if isinstance(value, bool):
        value = int(value)
    try:
        n = int(str(value).strip())
    except ValueError:
        raise ValueError("%d %s: a whole number, not %r" % (p.id, p.key, value)) from None
    lo, hi = _INT_RANGE.get(p.type, (None, None))
    lo = p.min if p.min is not None else lo
    hi = p.max if p.max is not None else hi
    if (lo is not None and n < lo) or (hi is not None and n > hi):
        raise ValueError("%d %s: %s..%s%s" % (p.id, p.key, lo, hi, " " + p.unit if p.unit else ""))
    return p.id, str(n)


def setparams(values: Mapping[Union[int, str], object], password: str = "") -> str:
    """A checked SETPARAMS command. Keys are ids or names::

        setparams({"move_send_period_s": 300})             -> 'SETPARAMS 145=300;'
        setparams({32: "mqtt"}, password="secret")    -> 'secret SETPARAMS 32=2;'

    ``protected`` parameters need the configuration password in front.
    """
    body = "".join("%d=%s;" % check(k, v) for k, v in values.items())
    return ("%s SETPARAMS %s" % (password, body)) if password else "SETPARAMS " + body


def getparams(which: Iterable[Union[int, str]]) -> str:
    """``getparams(["move_send_period_s", 140])`` -> ``'GETPARAMS 145,140'``."""
    ids = []
    for w in which:
        p = get(w)
        if p is None:
            raise ValueError("no parameter %r" % (w,))
        ids.append(str(p.id))
    return "GETPARAMS " + ",".join(ids)
