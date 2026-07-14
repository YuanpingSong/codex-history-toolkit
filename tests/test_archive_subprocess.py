import fcntl
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

from codex_history.archive import _default_command_runner


def wait_for_lock_release(path, timeout=2.0):
    deadline = time.monotonic() + timeout
    with path.open("r+b") as handle:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.01)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                return True


def create_interrupt_fixture(codex_home):
    helper = codex_home / "interrupt-fake-codex"
    metadata = codex_home / "interrupt-processes"
    ready = codex_home / "interrupt-grandchild-ready"
    liveness = codex_home / "interrupt-grandchild-alive"
    trigger = codex_home / "interrupt-allow-late-mutation"
    mutation = codex_home / "interrupt-late-mutation"
    helper.write_text(
        textwrap.dedent(
            '''\
            #!/usr/bin/env python3
            import os
            from pathlib import Path
            import subprocess
            import sys
            import time

            home = Path(os.environ["CODEX_HOME"])
            grandchild_source = """
            import fcntl
            import os
            from pathlib import Path
            import time

            home = Path(os.environ["CODEX_HOME"])
            liveness = (home / "interrupt-grandchild-alive").open("w")
            fcntl.flock(liveness.fileno(), fcntl.LOCK_EX)
            (home / "interrupt-grandchild-ready").write_text(
                "ready\\\\n", encoding="utf-8"
            )
            trigger = home / "interrupt-allow-late-mutation"
            deadline = time.monotonic() + 10.0
            while not trigger.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            if trigger.exists():
                (home / "interrupt-late-mutation").write_text(
                    "late mutation\\\\n", encoding="utf-8"
                )
            time.sleep(10.0)
            """
            grandchild = subprocess.Popen(
                [sys.executable, "-c", grandchild_source],
                stdin=subprocess.DEVNULL,
            )
            ready = home / "interrupt-grandchild-ready"
            deadline = time.monotonic() + 10.0
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not ready.exists():
                raise RuntimeError("grandchild did not become ready")
            (home / "interrupt-processes").write_text(
                "%d %d %d\\n" % (os.getpid(), os.getpgrp(), grandchild.pid),
                encoding="utf-8",
            )
            time.sleep(10.0)
            '''
        ),
        encoding="utf-8",
    )
    helper.chmod(helper.stat().st_mode | stat.S_IXUSR)
    return helper, metadata, ready, liveness, trigger, mutation


class DefaultCommandRunnerTests(unittest.TestCase):
    def test_timeout_kills_descendant_process_group_before_returning(self):
        with tempfile.TemporaryDirectory() as temporary:
            codex_home = Path(temporary)
            helper = codex_home / "fake-codex"
            metadata = codex_home / "processes"
            ready = codex_home / "grandchild-ready"
            liveness = codex_home / "grandchild-alive"
            trigger = codex_home / "allow-late-mutation"
            mutation = codex_home / "late-mutation"
            helper.write_text(
                textwrap.dedent(
                    '''\
                    #!/usr/bin/env python3
                    import os
                    from pathlib import Path
                    import subprocess
                    import sys
                    import time

                    home = Path(os.environ["CODEX_HOME"])
                    grandchild_source = """
                    import fcntl
                    import os
                    from pathlib import Path
                    import time

                    home = Path(os.environ["CODEX_HOME"])
                    liveness = (home / "grandchild-alive").open("w")
                    fcntl.flock(liveness.fileno(), fcntl.LOCK_EX)
                    (home / "grandchild-ready").write_text(
                        "ready\\\\n", encoding="utf-8"
                    )
                    trigger = home / "allow-late-mutation"
                    deadline = time.monotonic() + 10.0
                    while not trigger.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    if trigger.exists():
                        (home / "late-mutation").write_text(
                            "mutation after runner returned\\\\n", encoding="utf-8"
                        )
                    time.sleep(10.0)
                    """
                    grandchild = subprocess.Popen(
                        [sys.executable, "-c", grandchild_source],
                        stdin=subprocess.DEVNULL,
                    )
                    ready = home / "grandchild-ready"
                    deadline = time.monotonic() + 10.0
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    if not ready.exists():
                        raise RuntimeError("grandchild did not become ready")
                    (home / "processes").write_text(
                        "%d %d %d\\n" % (os.getpid(), os.getpgrp(), grandchild.pid),
                        encoding="utf-8",
                    )
                    time.sleep(10.0)
                    '''
                ),
                encoding="utf-8",
            )
            helper.chmod(helper.stat().st_mode | stat.S_IXUSR)

            outcome = _default_command_runner(
                str(helper),
                "019e1000-0000-7000-8000-000000000001",
                codex_home,
                timeout=2.0,
            )
            self.assertTrue(outcome.timed_out)
            self.assertIsNone(outcome.exit_code)
            self.assertTrue(metadata.is_file(), "fake Codex process did not start")
            self.assertTrue(ready.is_file(), "fake Codex grandchild was not ready")
            self.assertTrue(liveness.is_file())
            _, process_group, _ = (
                int(value) for value in metadata.read_text(encoding="utf-8").split()
            )

            with self.subTest("dedicated process group"):
                self.assertNotEqual(process_group, os.getpgrp())
            with self.subTest("descendant stopped before return"):
                self.assertTrue(wait_for_lock_release(liveness))

            # A surviving grandchild now has an explicit opportunity to mutate.
            trigger.touch()
            deadline = time.monotonic() + 1.0
            while not mutation.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            with self.subTest("no mutation after timeout"):
                self.assertFalse(mutation.exists())

    def test_interrupt_signals_reap_descendants_before_propagating(self):
        for signum in (
            signal.SIGINT,
            signal.SIGTERM,
            signal.SIGHUP,
            signal.SIGQUIT,
            signal.SIGTSTP,
        ):
            with self.subTest(signal=signal.Signals(signum).name):
                with tempfile.TemporaryDirectory() as temporary:
                    codex_home = Path(temporary)
                    (
                        helper,
                        metadata,
                        ready,
                        liveness,
                        trigger,
                        mutation,
                    ) = create_interrupt_fixture(codex_home)
                    interrupted = codex_home / "runner-interrupted"
                    wrapper = codex_home / "invoke-runner.py"
                    wrapper.write_text(
                        textwrap.dedent(
                            '''\
                            import sys
                            from pathlib import Path
                            from codex_history.archive import _default_command_runner

                            helper = sys.argv[1]
                            home = Path(sys.argv[2])
                            try:
                                _default_command_runner(
                                    helper,
                                    "019e1000-0000-7000-8000-000000000001",
                                    home,
                                    timeout=30.0,
                                )
                            except KeyboardInterrupt:
                                (home / "runner-interrupted").write_text(
                                    "child group reaped before propagation\\n",
                                    encoding="utf-8",
                                )
                            '''
                        ),
                        encoding="utf-8",
                    )
                    environment = os.environ.copy()
                    source_root = Path(__file__).resolve().parents[1] / "src"
                    environment["PYTHONPATH"] = str(source_root)
                    runner = subprocess.Popen(
                        [sys.executable, str(wrapper), str(helper), str(codex_home)],
                        env=environment,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    try:
                        deadline = time.monotonic() + 5.0
                        while not metadata.exists() and time.monotonic() < deadline:
                            time.sleep(0.01)
                        self.assertTrue(metadata.is_file(), "fake Codex process did not start")
                        self.assertTrue(
                            ready.is_file(), "fake Codex grandchild was not ready"
                        )
                        self.assertTrue(liveness.is_file())
                        _, process_group, _ = (
                            int(value)
                            for value in metadata.read_text(encoding="utf-8").split()
                        )
                        self.assertNotEqual(process_group, os.getpgrp())

                        os.kill(runner.pid, signum)
                        runner.wait(timeout=5.0)
                        self.assertTrue(interrupted.is_file())
                        self.assertTrue(wait_for_lock_release(liveness))

                        trigger.touch()
                        time.sleep(0.25)
                        self.assertFalse(mutation.exists())
                    finally:
                        if runner.poll() is None:
                            runner.kill()
                            runner.wait(timeout=5.0)

    def test_missing_process_group_during_timeout_cleanup_is_tolerated(self):
        process = mock.Mock()
        process.pid = 12345
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(["fake-codex", "archive"], 1.0),
            (b"stdout", b"stderr"),
        ]

        with mock.patch(
            "codex_history.archive.subprocess.Popen", return_value=process
        ) as popen, mock.patch(
            "codex_history.archive.os.killpg", side_effect=ProcessLookupError
        ) as killpg:
            outcome = _default_command_runner(
                "fake-codex",
                "019e1000-0000-7000-8000-000000000001",
                Path("/tmp/codex-home"),
                timeout=1.0,
            )

        self.assertTrue(outcome.timed_out)
        self.assertIsNone(outcome.exit_code)
        self.assertEqual(outcome.stdout, b"stdout")
        self.assertEqual(outcome.stderr, b"stderr")
        self.assertEqual(process.communicate.call_count, 2)
        killpg.assert_called_once_with(process.pid, signal.SIGKILL)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_permission_error_during_timeout_cleanup_is_propagated(self):
        process = mock.Mock()
        process.pid = 12345
        process.communicate.side_effect = subprocess.TimeoutExpired(
            ["fake-codex", "archive"], 1.0
        )
        termination_signals = (
            signal.SIGINT,
            signal.SIGHUP,
            signal.SIGTERM,
            signal.SIGQUIT,
            signal.SIGTSTP,
        )
        previous_handlers = {
            signum: signal.getsignal(signum) for signum in termination_signals
        }

        with mock.patch(
            "codex_history.archive.subprocess.Popen", return_value=process
        ), mock.patch(
            "codex_history.archive.os.killpg",
            side_effect=PermissionError("process group is not signalable"),
        ) as killpg:
            with self.assertRaises(PermissionError):
                _default_command_runner(
                    "fake-codex",
                    "019e1000-0000-7000-8000-000000000001",
                    Path("/tmp/codex-home"),
                    timeout=1.0,
                )

        self.assertEqual(process.communicate.call_count, 1)
        killpg.assert_called_once_with(process.pid, signal.SIGKILL)
        self.assertEqual(
            {signum: signal.getsignal(signum) for signum in termination_signals},
            previous_handlers,
        )


if __name__ == "__main__":
    unittest.main()
