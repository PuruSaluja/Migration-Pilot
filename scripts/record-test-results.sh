#!/usr/bin/env bash
# Records the JUnit XML and log fixtures in tests/fixtures/test_results/ (P2-14) by running the
# real test tools in the sandbox images. Each sample project is written to a temp directory,
# the tools are installed into it with network (this script only; the recordings themselves run
# with --network none, as in the work container), and every project is mounted at /workspace.
#
#   scripts/record-test-results.sh            # needs mpilot/node:22 and mpilot/python:3.12
#
# Tool versions are pinned below. Re-recording overwrites the fixtures; the parser tests then
# say what changed. Exit codes are written next to each log as <name>.exit.
set -euo pipefail

JEST=29.7.0 JEST_JUNIT=16.0.0 VITEST=3.2.7 MOCHA=11.8.0 MOCHA_JUNIT=2.2.1 PYTEST=8.4.2
OUT="$(cd "$(dirname "$0")/.." && pwd)/tests/fixtures/test_results"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$OUT"
U="$(id -u):$(id -g)"

# ------------------------------------------------------------------ sample projects
mkdir -p "$WORK"/jest/tests/sub "$WORK"/vitest/tests/sub "$WORK"/mocha/test \
    "$WORK"/py/tests/pkg "$WORK"/pycoll/tests "$WORK"/unit/tests

# The same cases for jest and vitest: pass, fail, nested describe, XML-special characters,
# skip, todo, parametrized (each), a top-level test, unicode, a thrown error, a suite that
# cannot load, and a green file.
js_math() { # $1: import line
cat <<EOF
$1
describe('math', () => {
  test('adds', () => { expect(1 + 1).toBe(2); });
  describe('nested', () => {
    test('subtracts & <compares> "quotes"', () => { expect(2 - 1).toBe(3); });
  });
  test.skip('skipped one', () => {});
  test.todo('todo one');
  test.each([[1, 2], [2, 4]])('inc %i -> %i', (a, b) => { expect(a + 1).toBe(b); });
});
test('top level', () => {});
EOF
}
js_math "" > "$WORK"/jest/tests/math.test.js
js_math "import { describe, test, expect } from 'vitest';" > "$WORK"/vitest/tests/math.test.js
cat > "$WORK"/jest/tests/broken.test.js <<'EOF'
const x = require('./does-not-exist');
test('never runs', () => {});
EOF
cat > "$WORK"/vitest/tests/broken.test.js <<'EOF'
import x from './does-not-exist.js';
import { test } from 'vitest';
test('never runs', () => {});
EOF
for d in jest vitest; do
  imp=""; [ "$d" = vitest ] && imp="import { describe, test, expect } from 'vitest';"
  cat > "$WORK/$d/tests/sub/unicode.test.js" <<EOF
$imp
test('ünïcödé ✓', () => { expect(true).toBe(true); });
test('throws error', () => { throw new TypeError('boom'); });
EOF
  cat > "$WORK/$d/tests/green.test.js" <<EOF
$imp
describe('green', () => {
  test('one', () => { expect(1).toBe(1); });
  test('two', () => { expect(2).toBe(2); });
});
EOF
done
echo '{"name":"rec-jest","version":"1.0.0","private":true,"scripts":{"test":"jest"}}' \
    > "$WORK"/jest/package.json
echo '{"name":"rec-vitest","version":"1.0.0","private":true,"type":"module","scripts":{"test":"vitest run"}}' \
    > "$WORK"/vitest/package.json
echo '{"name":"rec-mocha","version":"1.0.0","private":true,"scripts":{"test":"mocha"}}' \
    > "$WORK"/mocha/package.json
cat > "$WORK"/mocha/test/math.spec.js <<'EOF'
const assert = require('assert');
describe('math', () => {
  it('adds', () => { assert.strictEqual(1 + 1, 2); });
  describe('nested', () => {
    it('subtracts & <compares> "quotes"', () => { assert.strictEqual(2 - 1, 3); });
  });
  it.skip('skipped one', () => {});
  it('pending one');
  [[1, 2], [2, 4]].forEach(([a, b]) => it(`inc ${a} -> ${b}`, () => assert.strictEqual(a + 1, b)));
  describe('hooks', () => {
    before(() => { throw new Error('hook boom'); });
    it('after failing hook', () => {});
  });
});
it('top level', () => {});
it('ünïcödé ✓', () => {});
EOF

cat > "$WORK"/py/tests/test_sample.py <<'EOF'
import unittest

import pytest


def test_pass():
    assert True


def test_fail():
    assert 1 == 2, "one is not two"


@pytest.fixture
def broken():
    raise RuntimeError("fixture boom")


def test_setup_error(broken):
    pass


@pytest.fixture
def bad_teardown():
    yield
    raise RuntimeError("teardown boom")


def test_teardown_error(bad_teardown):
    pass


@pytest.mark.skip(reason="not today")
def test_skipped():
    pass


@pytest.mark.xfail(reason="known bug")
def test_xfail():
    assert False


@pytest.mark.xfail(reason="fixed?")
def test_xpass():
    assert True


@pytest.mark.xfail(reason="strict", strict=True)
def test_xpass_strict():
    assert True


@pytest.mark.parametrize(
    "value",
    [1, "a/b", "x::y", "with space", "ünï", "br[ack]et"],
)
def test_param(value):
    assert value != "x::y"


class TestKlass:
    def test_method(self):
        assert True

    @pytest.mark.parametrize("n", [1, 2])
    def test_method_param(self, n):
        assert n == 1

    class TestInner:
        def test_deep(self):
            assert True


class LegacyCase(unittest.TestCase):
    def test_legacy(self):
        self.assertEqual(1, 1)

    @unittest.skip("legacy skip")
    def test_legacy_skip(self):
        pass
EOF
touch "$WORK"/py/tests/__init__.py "$WORK"/py/tests/pkg/__init__.py
cat > "$WORK"/py/tests/pkg/test_mod.py <<'EOF'
def test_in_package():
    assert "<&>" == "<&>\""
EOF
printf '[pytest]\n' > "$WORK"/py/pytest.ini
printf 'def test_ok():\n    pass\n' > "$WORK"/pycoll/tests/test_ok.py
printf 'import does_not_exist\n\n\ndef test_never():\n    pass\n' > "$WORK"/pycoll/tests/test_broken.py
printf '[pytest]\n' > "$WORK"/pycoll/pytest.ini
touch "$WORK"/unit/tests/__init__.py
cat > "$WORK"/unit/tests/test_things.py <<'EOF'
import unittest


class Things(unittest.TestCase):
    def test_pass(self):
        self.assertTrue(True)

    def test_fail(self):
        self.assertEqual(1, 2)

    def test_error(self):
        raise ValueError("boom")

    @unittest.skip("later")
    def test_skip(self):
        pass

    @unittest.expectedFailure
    def test_expected_failure(self):
        self.assertEqual(1, 2)

    def test_subtests(self):
        for i in range(3):
            with self.subTest(i=i):
                self.assertNotEqual(i, 1)


class Other(unittest.TestCase):
    def test_other(self):
        pass
EOF
printf 'import does_not_exist_either\n' > "$WORK"/unit/tests/test_import_error.py

# ------------------------------------------------------------------ install (network)
node_run() { docker run --rm -u "$U" -e HOME=/tmp "$@"; }
node_run -v "$WORK"/jest:/workspace -w /workspace mpilot/node:22 \
    npm install -s --no-audit --no-fund --save-dev "jest@$JEST" "jest-junit@$JEST_JUNIT"
node_run -v "$WORK"/vitest:/workspace -w /workspace mpilot/node:22 \
    npm install -s --no-audit --no-fund --save-dev "vitest@$VITEST"
node_run -v "$WORK"/mocha:/workspace -w /workspace mpilot/node:22 \
    npm install -s --no-audit --no-fund --save-dev "mocha@$MOCHA" "mocha-junit-reporter@$MOCHA_JUNIT"
for p in py pycoll unit; do
  docker run --rm -u "$U" -e HOME=/tmp -v "$WORK/$p":/workspace -w /workspace mpilot/python:3.12 \
      sh -c "python -m venv .venv && .venv/bin/pip install -q --disable-pip-version-check pytest==$PYTEST"
done

# ------------------------------------------------------------------ record (no network)
# rec PROJECT IMAGE NAME 'COMMAND' [docker env flags...]: stdout, stderr and exit code of one run.
rec() {
  local project=$1 image=$2 name=$3 cmd=$4; shift 4
  local code=0
  docker run --rm --network none -u "$U" -e HOME=/tmp -e CI=true "$@" \
      -v "$WORK/$project":/workspace -w /workspace "$image" sh -c "$cmd" \
      >"$OUT/$name.stdout" 2>"$OUT/$name.stderr" || code=$?
  echo "$code" > "$OUT/$name.exit"
}
# The jest-junit settings P2-14a passes through the exec environment (detect/node.py).
JJ=(-e JEST_JUNIT_OUTPUT_DIR=.mpilot -e JEST_JUNIT_OUTPUT_NAME=junit.xml
    -e "JEST_JUNIT_CLASSNAME={filepath}" -e "JEST_JUNIT_TITLE={classname} › {title}"
    -e "JEST_JUNIT_ANCESTOR_SEPARATOR= › " -e JEST_JUNIT_ADD_FILE_ATTRIBUTE=true
    -e JEST_JUNIT_REPORT_TEST_SUITE_ERRORS=true)

rec jest mpilot/node:22 jest-junit-default 'npx jest --reporters=default --reporters=jest-junit'
mv "$WORK"/jest/junit.xml "$OUT"/jest-junit-default.xml
rec jest mpilot/node:22 jest-junit-configured 'npx jest --reporters=default --reporters=jest-junit' "${JJ[@]}"
mv "$WORK"/jest/.mpilot/junit.xml "$OUT"/jest-junit-configured.xml
# The env settings win over a repo's own "jest-junit" config in package.json.
python3 - "$WORK"/jest/package.json <<'EOF'
import json, sys
path = sys.argv[1]
pkg = json.load(open(path))
pkg["jest-junit"] = {"outputDirectory": ".", "outputName": "repo-chosen.xml",
                     "classNameTemplate": "REPO {title}", "titleTemplate": "REPO {title}",
                     "addFileAttribute": "false", "reportTestSuiteErrors": "false"}
json.dump(pkg, open(path, "w"), indent=2)
EOF
rec jest mpilot/node:22 jest-junit-repo-config 'npx jest --reporters=default --reporters=jest-junit' "${JJ[@]}"
mv "$WORK"/jest/.mpilot/junit.xml "$OUT"/jest-junit-repo-config.xml
test ! -e "$WORK"/jest/repo-chosen.xml
rec jest mpilot/node:22 jest-default 'npx jest'
rec jest mpilot/node:22 jest-verbose 'npx jest --verbose'
rec jest mpilot/node:22 jest-green 'npx jest tests/green.test.js tests/sub'

rec vitest mpilot/node:22 vitest-junit \
    'npx vitest run --reporter=default --reporter=junit --outputFile.junit=.mpilot/junit.xml'
mv "$WORK"/vitest/.mpilot/junit.xml "$OUT"/vitest-junit.xml
rec vitest mpilot/node:22 vitest-default 'npx vitest run'
rec vitest mpilot/node:22 vitest-verbose 'npx vitest run --reporter=verbose'

rec mocha mpilot/node:22 mocha-junit \
    'npx mocha --reporter mocha-junit-reporter --reporter-options mochaFile=.mpilot/junit.xml'
mv "$WORK"/mocha/.mpilot/junit.xml "$OUT"/mocha-junit.xml
rec mocha mpilot/node:22 mocha-spec 'npx mocha'

PY=.venv/bin/python
rec py mpilot/python:3.12 pytest-junit "$PY -m pytest -q --junitxml=.mpilot/junit.xml"
mv "$WORK"/py/.mpilot/junit.xml "$OUT"/pytest-junit.xml
rec py mpilot/python:3.12 pytest-xunit1 "$PY -m pytest -q -o junit_family=xunit1 --junitxml=.mpilot/junit.xml"
mv "$WORK"/py/.mpilot/junit.xml "$OUT"/pytest-xunit1.xml
rec py mpilot/python:3.12 pytest-v "$PY -m pytest -v"
rec py mpilot/python:3.12 pytest-ra "$PY -m pytest -q -rA"
rec pycoll mpilot/python:3.12 pytest-collection-error "$PY -m pytest -q --junitxml=.mpilot/junit.xml"
mv "$WORK"/pycoll/.mpilot/junit.xml "$OUT"/pytest-collection-error.xml
rec pycoll mpilot/python:3.12 pytest-green "$PY -m pytest -q tests/test_ok.py"

for v in 3.10 3.12; do
  rec unit "mpilot/python:$v" "unittest-py$v" 'python -m unittest discover'
  rec unit "mpilot/python:$v" "unittest-v-py$v" 'python -m unittest discover -v'
done

# Empty streams are not kept.
find "$OUT" -maxdepth 1 \( -name '*.stdout' -o -name '*.stderr' \) -empty -delete
echo "recorded into $OUT"
