import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("workflow_builder", PROJECT / "script/build_shot_workflow.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class WorkflowBuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def args(self, *extra):
        return builder.build_parser().parse_args([
            "--out", str(self.root / "review.json"),
            "--input-dir", str(self.root / "input"), *extra,
        ])

    def write_shot(self, folder, layout=None):
        layout = layout or builder.DEFAULT_LAYOUT
        for field, content in (("start_frame", b"first"), ("end_frame", b"last"),
                               ("prompt", b"An example shot"), ("duration", b"3.5 seconds")):
            if layout[field] is None:
                continue
            file = folder / layout[field]
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(content)

    def shots(self, result):
        workflow = json.loads(result.read_text())
        queue = next(n for n in workflow["nodes"] if n["type"] == "ShotQueue")
        return workflow, json.loads(queue["widgets_values"][0])

    def test_default_layout_preserves_two_pass_graph(self):
        folder = self.root / "production" / "shot01" / "EXPORT"
        self.write_shot(folder)
        workflow, shots = self.shots(builder.build(self.args("--jobs", str(self.root / "production"))))
        original = json.loads(builder.DEFAULT_TEMPLATE.read_text())
        self.assertEqual(len(shots), 1)
        self.assertEqual(shots[0]["duration"], 3.5)
        for before, after in zip(original["nodes"], workflow["nodes"]):
            if before["type"] == "ShotQueue":
                after["widgets_values"] = before["widgets_values"]
                if "widgets_values_named" in before:
                    after["widgets_values_named"] = before["widgets_values_named"]
        self.assertEqual(workflow, original)
        self.assertEqual(builder.discover_jobs(folder, "**/EXPORT"), [folder])

    def test_config_relative_root_and_same_named_frames(self):
        layout = {"jobs": "production", "job_glob": "*", "start_frame": "first/image.png",
                  "end_frame": "last/image.png", "prompt": "text/prompt.md", "duration": "text/seconds.txt"}
        self.write_shot(self.root / "production" / "shot01", layout)
        self.write_shot(self.root / "production" / ".hidden", layout)
        config = self.root / "layout.json"
        config.write_text(json.dumps(layout))
        _, shots = self.shots(builder.build(self.args("--config", str(config))))
        self.assertEqual(len(shots), 1)
        self.assertNotEqual(shots[0]["start_frame"], shots[0]["end_frame"])
        self.assertEqual((self.root / "input" / shots[0]["start_frame"]).read_bytes(), b"first")
        self.assertEqual((self.root / "input" / shots[0]["end_frame"]).read_bytes(), b"last")

    def test_jobs_override_and_disabled_end_frame(self):
        config = self.root / "layout.json"
        config.write_text(json.dumps({"jobs": "missing", "job_glob": ".", "end_frame": None}))
        layout = dict(builder.DEFAULT_LAYOUT, end_frame=None)
        self.write_shot(self.root / "single", layout)
        _, shots = self.shots(builder.build(self.args("--config", str(config), "--jobs", str(self.root / "single"))))
        self.assertEqual(shots[0]["end_frame"], "")

    def test_invalid_shot_validated_before_any_copy(self):
        self.write_shot(self.root / "production" / "a" / "EXPORT")
        self.write_shot(self.root / "production" / "b" / "EXPORT")
        (self.root / "production" / "b" / "EXPORT" / "duration.txt").unlink()
        with self.assertRaisesRegex(ValueError, "missing required duration"):
            builder.build(self.args("--jobs", str(self.root / "production")))
        self.assertFalse((self.root / "input").exists())
        self.assertFalse((self.root / "review.json").exists())

    def test_config_rejects_typos_and_invalid_types(self):
        config = self.root / "layout.json"
        for value in ({"duraton": "seconds.txt"}, {"job_glob": 3}, {"job_glob": "../*"}, {"end_frame": False}):
            with self.subTest(value=value):
                config.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    builder.load_layout(self.args("--config", str(config), "--jobs", str(self.root)))

    def test_failed_copy_removes_batch(self):
        self.write_shot(self.root / "production" / "EXPORT")
        copy = builder.shutil.copy2
        calls = 0

        def fail_second_copy(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("copy failed")
            return copy(source, destination)

        with patch.object(builder.shutil, "copy2", side_effect=fail_second_copy):
            with self.assertRaisesRegex(OSError, "copy failed"):
                builder.build(self.args("--jobs", str(self.root / "production")))
        self.assertEqual(list((self.root / "input" / "shot_queue").iterdir()), [])
        self.assertFalse((self.root / "review.json").exists())

    def test_failed_json_write_removes_output_and_batch(self):
        self.write_shot(self.root / "production" / "EXPORT")

        def fail_write(workflow, stream, **kwargs):
            stream.write('{"partial":')
            raise OSError("disk full")

        with patch.object(builder.json, "dump", side_effect=fail_write):
            with self.assertRaisesRegex(OSError, "disk full"):
                builder.build(self.args("--jobs", str(self.root / "production")))
        self.assertFalse((self.root / "review.json").exists())
        self.assertEqual(list((self.root / "input" / "shot_queue").iterdir()), [])

    def test_existing_output_is_preserved(self):
        output = self.root / "review.json"
        output.write_text("existing workflow")
        with self.assertRaisesRegex(ValueError, "already exists"):
            builder.build(self.args("--jobs", str(self.root)))
        self.assertEqual(output.read_text(), "existing workflow")
        self.assertFalse((self.root / "input").exists())


if __name__ == "__main__":
    unittest.main()
