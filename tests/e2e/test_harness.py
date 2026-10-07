"""Harness regression checks without Docker: python3 tests/e2e/test_harness.py."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

RUN = Path(__file__).with_name('run.sh')
# Load the actual setup/functions without starting infrastructure.
SETUP = RUN.read_text().split('step "kind cluster"')[0]


class HarnessChecks(unittest.TestCase):
    def run_shell(self, body, keep='0'):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'kind.log'
            result = subprocess.run(
                ['bash', '-c', SETUP + '\nkind() { echo "$*" >> "$REVIEW_LOG"; };\n'
                 'python3() { echo 12345; };\n' + body],
                env={**os.environ, 'KEEP': keep, 'REVIEW_LOG': str(log)},
                capture_output=True, text=True, timeout=10,
            )
            # KEEP deliberately retains the work directory; remove this test's copy.
            for line in result.stdout.splitlines():
                if line.startswith('WORK='):
                    import shutil
                    shutil.rmtree(line.removeprefix('WORK='), ignore_errors=True)
            return result, log.read_text() if log.exists() else ''

    def test_failed_creation_does_not_delete_cluster(self):
        # The failing mock still logs, so a delete issued by cleanup would show up.
        result, log = self.run_shell('kind() { echo "$*" >> "$REVIEW_LOG"; [ "$1" != create ]; }; kind create cluster; created=1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(log, 'create cluster\n')

    def test_owned_cluster_deleted_on_failure(self):
        result, log = self.run_shell('created=1; false')
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(log, r'^delete cluster --name opencloud-e2e-')

    def test_keep_stops_forward_but_retains_cluster(self):
        result, log = self.run_shell('''
created=1
echo "WORK=$work"
kubectl() { exec sleep 30; }
curl() { return 0; }
forward idp svc/keycloak 8080
[ "${#pids[@]}" = 1 ]
[ "${pids[0]}" = "$FORWARD_PID" ]
pid=$FORWARD_PID
cleanup
! kill -0 "$pid" 2>/dev/null
trap - EXIT
''', keep='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(log, '')
        self.assertIn('kept:', result.stdout)

    def test_checks_preserves_failure_and_reaps_forward(self):
        result, _ = self.run_shell('''
kubectl() { exec sleep 30; }
curl() { return 0; }
python3() {
  if [ "$1" = -c ]; then echo 12345; else return 7; fi
}
KC=http://keycloak
status=0
checks oc write marker || status=$?
[ "$status" = 7 ]
[ "${#pids[@]}" = 0 ]
! kill -0 "$FORWARD_PID" 2>/dev/null
''')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_forward_timeout_fails(self):
        result, _ = self.run_shell('''
kubectl() { exec sleep 30; }
curl() { return 1; }
sleep() { :; }
forward idp svc/keycloak 8080
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('did not come up', result.stderr)

    def test_runtime_assertions_fail_closed(self):
        assertions = RUN.read_text().split('kubectl -n oc get pods -o json |', 1)[1].split('checks oc write fresh', 1)[0]
        assertions = 'kubectl -n oc get pods -o json |' + assertions
        # The first case is the positive control: without it, a broken snippet
        # would fail every case and the test would pass vacuously.
        for readonly, get_status, exec_status in [('true', 0, 0), ('false', 0, 0), ('true', 1, 0), ('true', 0, 1)]:
            with self.subTest(readonly=readonly, get_status=get_status, exec_status=exec_status):
                body = '''
unset -f python3
kubectl() {
  if [ "$3" = get ]; then
    echo '{"items":[{"metadata":{"name":"test"},"spec":{"containers":[{"name":"server","securityContext":{"readOnlyRootFilesystem":READONLY}}]}}]}'
    return GET_STATUS
  fi
  return EXEC_STATUS
}
'''.replace('READONLY', readonly).replace('GET_STATUS', str(get_status)).replace('EXEC_STATUS', str(exec_status))
                # In run.sh more commands follow these lines; a trailing ':' mimics
                # that, so a failure swallowed by `cmd && echo` cannot pass as the
                # shell's final exit status.
                result, _ = self.run_shell(body + assertions + '\n:')
                if (readonly, get_status, exec_status) == ('true', 0, 0):
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('ok   every container has a read-only root filesystem', result.stdout)
                    self.assertIn('ok   no generated opencloud.yaml', result.stdout)
                else:
                    self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
