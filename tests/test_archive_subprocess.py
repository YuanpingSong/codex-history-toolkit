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

from codex_history.archive import _default_command_runner


def process_group_exists(process_group):
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    return True


def create_interrupt_fixture(codex_home):
    helper = codex_home / "interrupt-fake-codex"
    metadata = codex_home / "interrupt-processes"
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
            import os
            from pathlib import Path
            import time

            home = Path(os.environ["CODEX_HOME"])
            trigger = home / "interrupt-allow-late-mutation"
            deadline = time.monotonic() + 30.0
            while not trigger.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            if trigger.exists():
                (home / "interrupt-late-mutation").write_text(
                    "late mutation\\\\n", encoding="utf-8"
                )
            time.sleep(30.0)
            """
            grandchild = subprocess.Popen(
                [sys.executable, "-c", grandchild_source],
                stdin=subprocess.DEVNULL,
            )
            (home / "interrupt-processes").write_text(
                "%d %d %d\\n" % (os.getpid(), os.getpgrp(), grandchild.pid),
                encoding="utf-8",
            )
            time.sleep(30.0)
            '''
        ),
        encoding="utf-8",
    )
    helper.chmod(helper.stat().st_mode | stat.S_IXUSR)
    return helper, metadata, trigger, mutation


class DefaultCommandRunnerTests(unittest.TestCase):
    def test_timeout_kills_descendant_process_group_before_returning(self):
        with tempfile.TemporaryDirectory() as temporary:
            codex_home = Path(temporary)
            helper = codex_home / "fake-codex"
            metadata = codex_home / "processes"
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
                    import os
                    from pathlib import Path
                    import time

                    home = Path(os.environ["CODEX_HOME"])
                    trigger = home / "allow-late-mutation"
                    deadline = time.monotonic() + 30.0
                    while not trigger.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    if trigger.exists():
                        (home / "late-mutation").write_text(
                            "mutation after runner returned\\\\n", encoding="utf-8"
                        )
                    time.sleep(30.0)
                    """
                    grandchild = subprocess.Popen(
                        [sys.executable, "-c", grandchild_source],
                        stdin=subprocess.DEVNULL,
                    )
                    (home / "processes").write_text(
                        "%d %d %d\\n" % (os.getpid(), os.getpgrp(), grandchild.pid),
                        encoding="utf-8",
                    )
                    time.sleep(30.0)
                    '''
                ),
                encoding="utf-8",
            )
            helper.chmod(helper.stat().st_mode | stat.S_IXUSR)

            helper_pid = None
            process_group = None
            grandchild_pid = None
            try:
                outcome = _default_command_runner(
                    str(helper),
                    "019e1000-0000-7000-8000-000000000001",
                    codex_home,
                    timeout=2.0,
                )
                self.assertTrue(outcome.timed_out)
                self.assertIsNone(outcome.exit_code)
                self.assertTrue(metadata.is_file(), "fake Codex process did not start")
                helper_pid, process_group, grandchild_pid = (
                    int(value) for value in metadata.read_text(encoding="utf-8").split()
                )

                deadline = time.monotonic() + 2.0
                while (
                    process_group != os.getpgrp()
                    and process_group_exists(process_group)
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                group_is_gone = (
                    process_group != os.getpgrp()
                    and not process_group_exists(process_group)
                )

                # A surviving grandchild now has an explicit opportunity to mutate.
                trigger.touch()
                deadline = time.monotonic() + 1.0
                while not mutation.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)

                with self.subTest("dedicated process group"):
                    self.assertNotEqual(process_group, os.getpgrp())
                with self.subTest("process group reaped"):
                    self.assertTrue(group_is_gone)
                with self.subTest("no mutation after timeout"):
                    self.assertFalse(mutation.exists())
            finally:
                if (
                    process_group is not None
                    and process_group != os.getpgrp()
                    and process_group_exists(process_group)
                ):
                    try:
                        os.killpg(process_group, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                elif process_group == os.getpgrp():
                    # The vulnerable implementation shares this test's group, so
                    # clean up its surviving grandchild without signaling the group.
                    for process_id in (helper_pid, grandchild_pid):
                        if process_id is not None:
                            try:
                                os.kill(process_id, signal.SIGKILL)
                            except ProcessLookupError:
                                pass

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
                    helper, metadata, trigger, mutation = create_interrupt_fixture(
                        codex_home
                    )
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
                    process_group = None
                    try:
                        deadline = time.monotonic() + 5.0
                        while not metadata.exists() and time.monotonic() < deadline:
                            time.sleep(0.01)
                        self.assertTrue(metadata.is_file(), "fake Codex process did not start")
                        _, process_group, _ = (
                            int(value)
                            for value in metadata.read_text(encoding="utf-8").split()
                        )

                        os.kill(runner.pid, signum)
                        runner.wait(timeout=5.0)
                        self.assertTrue(interrupted.is_file())

                        deadline = time.monotonic() + 2.0
                        while (
                            process_group_exists(process_group)
                            and time.monotonic() < deadline
                        ):
                            time.sleep(0.01)
                        self.assertFalse(process_group_exists(process_group))

                        trigger.touch()
                        time.sleep(0.25)
                        self.assertFalse(mutation.exists())
                    finally:
                        if runner.poll() is None:
                            runner.kill()
                            runner.wait(timeout=5.0)
                        if process_group is not None and process_group_exists(
                            process_group
                        ):
                            try:
                                os.killpg(process_group, signal.SIGKILL)
                            except ProcessLookupError:
                                pass


if __name__ == "__main__":
    unittest.main()
