import asyncio
import sys
import unittest

import helpers  # noqa: F401
from dsd.streams import LineStream
from dsd.util import run


class SubprocessTimeout(unittest.IsolatedAsyncioTestCase):
    async def test_run_kills_and_reaps_timed_out_command(self):
        rc, output = await run([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.05)
        self.assertEqual(rc, -9)
        self.assertIn("timeout", output)


class LineStreamStop(unittest.IsolatedAsyncioTestCase):
    async def test_stop_waits_for_pipe_process_to_exit(self):
        stream = LineStream("test", [sys.executable, "-c", "import time; time.sleep(10)"], lambda _line: None)
        stream.start()
        for _ in range(100):
            if stream.proc is not None:
                break
            await asyncio.sleep(0.01)
        self.assertIsNotNone(stream.proc)
        proc = stream.proc
        await stream.stop()
        self.assertIsNotNone(proc.returncode)
        self.assertTrue(stream.task.done())


if __name__ == "__main__":
    unittest.main()
