#!/bin/sh
#
# Run Vector unit tests for the Postfix grok patterns.
#
# The tests are generated from the same YAML test fixtures used by the
# Logstash tests (test/*.yaml) and executed by Vector's built-in unit-test
# runner.
#
# References:
#   Vector:              https://vector.dev/
#   Vector unit tests:   https://vector.dev/docs/reference/configuration/unit-tests/
#   VRL parse_groks:     https://vector.dev/docs/reference/vrl/functions/#parse_groks
#
# Usage:
#   ./test_vector.sh
#
set -eu

DOCKERIMAGE="postfix-grok-patterns-vector"
VOLUMEPATH="/runtests"

# Step 1: generate the Vector unit-test config from the Logstash test fixtures.
# Requires Python 3 on the host (no Docker needed for this step).
echo "Generating vector/vector_tests.yaml ..."
python3 vector/generate_vector_tests.py

# Step 2: build the Vector Docker image (only needs the Vector binary).
echo "Building Docker image ..."
docker build --tag "${DOCKERIMAGE}" -f Dockerfile.vector .

# Step 3: run the Vector unit tests inside the container.
echo "Running Vector unit tests ..."
docker run \
  --rm \
  --volume "$(pwd)":"${VOLUMEPATH}" \
  --workdir "${VOLUMEPATH}" \
  "${DOCKERIMAGE}" \
  vector test --config vector/vector_tests.yaml
