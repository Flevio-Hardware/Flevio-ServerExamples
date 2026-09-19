#!/usr/bin/env python3
"""
Forward records to an HTTP endpoint - and only acknowledge what it took.

    python3 examples/03_forward_to_http.py https://example.com/ingest

This is the shape most integrations end up with: the tracker speaks a binary
protocol, and the rest of your company speaks JSON over HTTP. The one thing
worth getting right is in `on_records`:

    status = await post(...)
    return len(records) if 200 <= status < 300 else 0

Returning zero on a failed POST leaves every record on the device, which
holds tens of thousands of them in flash and will offer them again in a few
seconds. Your endpoint can be down for an afternoon and lose nothing. The
temptation to return `len(records)` regardless - because the device is
"probably fine" and the queue is filling - is how fleets end up with holes in
their history.

The retry also means your endpoint will see the same record twice from time
to time. Make the write idempotent on `imei + ts + event`.
"""

import asyncio
import json
import logging
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from flevio.server import Handler, Server
from flevio.sinks import record_to_dict

log = logging.getLogger("forward")


class HttpForwarder(Handler):
    def __init__(self, url: str, token: str = "", timeout: float = 20.0) -> None:
        self.url = url
        self.token = token
        self.timeout = timeout

    async def on_records(self, session, records) -> int:
        body = json.dumps({
            "imei": session.imei,
            "records": [record_to_dict(session.imei, r) for r in records],
        }).encode()

        try:
            status = await asyncio.get_running_loop().run_in_executor(None, self._post, body)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            log.warning("POST failed (%s); the device keeps its copy", e)
            return 0

        if not 200 <= status < 300:
            log.warning("endpoint answered %d; the device keeps its copy", status)
            return 0
        return len(records)

    def _post(self, body: bytes) -> int:
        req = urllib.request.Request(self.url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code          # a 4xx/5xx is an answer, not a transport failure


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__.strip())
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    handler = HttpForwarder(sys.argv[1], token=os.environ.get("INGEST_TOKEN", ""))
    try:
        asyncio.run(Server(handler).serve())
    except KeyboardInterrupt:
        pass
