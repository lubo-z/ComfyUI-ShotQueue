import importlib.util
import pathlib
import unittest

spec = importlib.util.spec_from_file_location("shot_queue_nodes", pathlib.Path(__file__).parents[1] / "nodes.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class QueueTests(unittest.TestCase):
    def test_only_current_task_is_validated_and_returned(self):
        raw = '[null,{"start_frame":" first.png ","end_frame":"last.png","prompt":"a\\nb","duration":5.5}]'
        self.assertEqual(m.ShotQueue().select(raw, 1), ("first.png", "last.png", "a\nb", 5.5, 1, "shot_002"))

    def test_reject_invalid_current_tasks(self):
        for raw, index in [("bad", 0), ("[]", 0), (m.DEFAULT_SHOTS, -1), (m.DEFAULT_SHOTS, 1), (m.DEFAULT_SHOTS, True), ('[{"start_frame":"","end_frame":"","prompt":"","duration":0}]', 0), ('[{"start_frame":"","end_frame":"","prompt":"","duration":NaN}]', 0), ('[{"start_frame":[],"end_frame":"","prompt":"","duration":5}]', 0)]:
            with self.subTest(raw=raw, index=index), self.assertRaises(ValueError):
                m.current_shot(raw, index)

    def test_empty_optional_frames_and_fractional_duration(self):
        self.assertEqual(m.current_shot(m.DEFAULT_SHOTS, 0), ("", "", "", 5.0, 0, "shot_001"))

    def test_empty_image_path_for_optional_frame(self):
        self.assertEqual(m.ShotQueueLoadImage().load("  "), (None, None))


if __name__ == "__main__":
    unittest.main()
