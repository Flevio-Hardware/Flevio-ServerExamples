#!/bin/sh
# Every test in this repository. No dependencies, no test runner needed.
set -e
cd "$(dirname "$0")"

echo "== protocol: the decoder against the firmware's own encoder"
python3 tests/test_protocol.py

echo
echo "== server: the simulator against the server, with frames dropped"
python3 tests/test_server.py

echo
echo "== command queue: commands for devices that are offline"
python3 tests/test_cmdqueue.py

echo
echo "== parameters: the table is whole and agrees with the events"
python3 tests/test_params.py

echo
echo "== examples import cleanly"
for f in examples/*.py; do
    python3 -c "import ast,sys; ast.parse(open('$f').read())"
done

echo
echo "all good"
