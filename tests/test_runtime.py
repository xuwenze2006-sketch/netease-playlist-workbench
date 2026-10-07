import threading
import unittest

from netease_organizer.runtime import OperationControl, OperationPaused


class RuntimeTests(unittest.TestCase):
    def test_request_from_another_thread_stays_set_until_explicit_reset(self):
        control = OperationControl()
        control.checkpoint()
        worker = threading.Thread(target=control.request_pause)
        worker.start()
        worker.join()
        self.assertTrue(control.pause_requested)
        with self.assertRaises(OperationPaused):
            control.checkpoint()
        control.reset()
        control.checkpoint()
        self.assertFalse(control.pause_requested)

    def test_pause_text_has_no_external_exception_data(self):
        self.assertEqual(str(OperationPaused()), '已请求暂停，本次不会开始下一项操作。')
