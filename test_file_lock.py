import multiprocessing
import queue
import tempfile
import threading
import unittest
from pathlib import Path

from core.file_lock import CrossProcessRLock


def _hold_lock(lock_path: str, events, release_event) -> None:
    with CrossProcessRLock(lock_path):
        events.put("first_acquired")
        release_event.wait(timeout=5)


def _try_lock(lock_path: str, events) -> None:
    with CrossProcessRLock(lock_path):
        events.put("second_acquired")


class CrossProcessRLockTests(unittest.TestCase):
    def test_reentrant_lock_creates_lock_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "nested" / "resource.lock"
            lock = CrossProcessRLock(lock_path)

            with lock:
                with lock:
                    self.assertTrue(lock_path.exists())

    def test_distinct_instances_are_reentrant_on_same_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "resource.lock"

            with CrossProcessRLock(lock_path):
                with CrossProcessRLock(lock_path):
                    self.assertTrue(lock_path.exists())

    def test_second_thread_waits_until_first_releases(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "resource.lock"
            events: queue.Queue[str] = queue.Queue()
            release_event = threading.Event()

            def hold_lock() -> None:
                with CrossProcessRLock(lock_path):
                    events.put("first_acquired")
                    release_event.wait(timeout=5)

            def try_lock() -> None:
                with CrossProcessRLock(lock_path):
                    events.put("second_acquired")

            first = threading.Thread(target=hold_lock)
            second = threading.Thread(target=try_lock)
            first.start()
            try:
                self.assertEqual(events.get(timeout=2), "first_acquired")
                second.start()
                with self.assertRaises(queue.Empty):
                    events.get(timeout=0.3)
                release_event.set()
                self.assertEqual(events.get(timeout=2), "second_acquired")
            finally:
                release_event.set()
                first.join(timeout=2)
                second.join(timeout=2)

    def test_second_process_waits_until_first_releases(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = str(Path(tmpdir) / "resource.lock")
            events = multiprocessing.Queue()
            release_event = multiprocessing.Event()
            first = multiprocessing.Process(target=_hold_lock, args=(lock_path, events, release_event))
            second = multiprocessing.Process(target=_try_lock, args=(lock_path, events))

            first.start()
            try:
                self.assertEqual(events.get(timeout=2), "first_acquired")
                second.start()
                with self.assertRaises(queue.Empty):
                    events.get(timeout=0.3)
                release_event.set()
                self.assertEqual(events.get(timeout=2), "second_acquired")
            finally:
                release_event.set()
                first.join(timeout=2)
                if second.pid is not None:
                    second.join(timeout=2)
                if first.is_alive():
                    first.terminate()
                    first.join(timeout=2)
                if second.is_alive():
                    second.terminate()
                    second.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
