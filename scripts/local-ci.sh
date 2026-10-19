#!/usr/bin/env bash
set -euo pipefail
cd /work
export HOME=/tmp/fixture-home
mkdir -p "$HOME"
git init -q
git config user.name Fixture
git config user.email fixture@example.com
git add .
git commit -qm 'Synthetic verification snapshot'

python3 -m venv /tmp/check-env
export PATH="/tmp/check-env/bin:$PATH"
python3 -m pip install setuptools pillow
python3 -c 'import importlib.util; assert importlib.util.find_spec("sqlite_vec") is None'
make check
python3 -m pip install sqlite-vec
make check
scripts/proof.sh
scripts/wheel-install.sh

python3 -m venv /tmp/e2e-build
/tmp/e2e-build/bin/python -m pip install 'setuptools>=77' wheel
/tmp/e2e-build/bin/python -m pip wheel --no-deps -w /tmp/wheel /work
python3 -m venv /tmp/e2e-run
/tmp/e2e-run/bin/python -m pip install --no-index --find-links /tmp/wheel mailcrawl
/tmp/e2e-run/bin/python -m pip install sqlite-vec
export PATH="/tmp/e2e-run/bin:$PATH"
export REPO=/work
cd /tmp
bash "$REPO/scripts/e2e-journey.sh"
