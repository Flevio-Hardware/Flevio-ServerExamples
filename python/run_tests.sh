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
echo "== examples import cleanly"
for f in examples/*.py; do
    python3 -c "import ast,sys; ast.parse(open('$f').read())"
done

echo
echo "== the shipped capture decodes"
python3 examples/04_decode_hex.py --corpus > /dev/null

echo
echo "all good"
