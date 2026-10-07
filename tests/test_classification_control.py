import tempfile
import unittest
from pathlib import Path
from scripts.classification_control import ClassificationFileControl
from netease_organizer.runtime import OperationPaused


class ClassificationControlTests(unittest.TestCase):
    def test_normal_checkpoint_has_no_side_effect(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'pause';ClassificationFileControl(p).checkpoint()
            self.assertFalse(p.exists())

    def test_separate_local_pause_marker_stops_at_next_checkpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'pause';control=ClassificationFileControl(p)
            control.checkpoint();p.write_text('pause\n')
            with self.assertRaises(OperationPaused):control.checkpoint()

    def test_reset_cannot_silently_ignore_an_existing_marker(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'pause';control=ClassificationFileControl(p);p.touch();control.reset()
            with self.assertRaises(OperationPaused):control.checkpoint()
