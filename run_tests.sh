#!/bin/zsh
# Run the test suite in Docker (includes real LibreOffice end-to-end tests).
# Optional: PUB2PDF_SAMPLES_HOST=/path/to/pub/samples ./run_tests.sh
set -euo pipefail
cd "${0:A:h}"
if [[ -n "${PUB2PDF_SAMPLES_HOST:-}" ]]; then
  export PUB2PDF_SAMPLE_DIR=/samples
fi
docker-compose build test
docker-compose run --rm test
