import contextlib
import copy
import importlib.util
import io
import json
import pathlib
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests


spec = importlib.util.spec_from_file_location("comfy_queue", pathlib.Path(__file__).with_name("comfy_queue.py"))
queue = importlib.util.module_from_spec(spec)
spec.loader.exec_module(queue)


def response(payload, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload).encode()
    return result


def template():
    return {
        "1": {"class_type": "LoadImage", "inputs": {"image": "old_start.png"}, "_meta": {"title": "QUEUE_START_IMAGE"}},
        "2": {"class_type": "LoadImage", "inputs": {"image": "old_end.png"}, "_meta": {"title": "QUEUE_END_IMAGE"}},
        "3": {"class_type": "PrimitiveStringMultiline", "inputs": {"value": "old prompt"}, "_meta": {"title": "QUEUE_PROMPT"}},
        "4": {"class_type": "PrimitiveFloat", "inputs": {"value": 5}, "_meta": {"title": "QUEUE_DURATION"}},
        "5": {"class_type": "MiniMaxH3ImageToVideo", "inputs": {"first_frame": ["1", 0], "last_frame": ["2", 0], "prompt": ["3", 0], "length": ["6", 1]}},
        "6": {"class_type": "ComfyMathExpression", "inputs": {"values.a": ["4", 0], "expression": "round(a * 24)"}},
    }


SUCCESS = {"status": {"completed": True, "status_str": "success"}, "outputs": {}}
FAILURE = {"status": {"completed": False, "status_str": "error", "messages": [["execution_error", {"exception_message": "Out of memory"}]]}}


class ScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.jobs = self.root / "production"
        self.jobs.mkdir()
        self.exports = []
        for name in ("EP01_S01_SH01", "EP01_S01_SH02"):
            folder = self.jobs / "episodes" / "EP01" / "S01" / name / "EXPORT"
            folder.mkdir(parents=True)
            self.exports.append(folder)
            (folder / "first_frame.png").write_bytes(b"start")
            (folder / "last_frame.png").write_bytes(b"end")
            (folder / "prompt.txt").write_text(f"Prompt {name}", encoding="utf-8")
            (folder / "duration.txt").write_text("5.5", encoding="utf-8")
        self.workflow = self.root / "workflow.json"
        self.workflow.write_text(json.dumps(template()), encoding="utf-8")

    def args(self, *options):
        return queue.build_parser().parse_args([
            "--workflow", str(self.workflow), "--jobs", str(self.jobs), *options,
        ])

    def run_batch(self, *options, first_history=SUCCESS, monitor_error=None):
        session = Mock()
        events, prompts, uploads = [], [], []

        def post(url, **kwargs):
            endpoint = url.removeprefix(queue.DEFAULT_SERVER)
            events.append(endpoint)
            if endpoint == "/upload/image":
                uploads.append(kwargs["data"].copy())
                filename, stream = kwargs["files"]["image"]
                self.assertTrue(stream.read())
                return response({"name": filename, "subfolder": kwargs["data"]["subfolder"], "type": "input"})
            if endpoint == "/prompt":
                prompts.append(copy.deepcopy(kwargs["json"]["prompt"]))
                return response({"prompt_id": f"p{len(prompts)}", "node_errors": {}})
            if endpoint == "/free":
                self.assertEqual(kwargs["json"], {"free_memory": True, "unload_models": True})
                return response({})
            self.fail(f"Unexpected POST: {endpoint}")

        def get(url, **kwargs):
            endpoint = url.removeprefix(queue.DEFAULT_SERVER)
            events.append(endpoint)
            if endpoint == "/system_stats":
                return response({})
            if endpoint == "/object_info/LoraLoaderModelOnly":
                return response({"LoraLoaderModelOnly": {"input": {"required": {
                    "lora_name": [["styles/b.safetensors", "other.safetensors", "styles/a.safetensors"]],
                }}}})
            if monitor_error:
                raise monitor_error
            prompt_id = endpoint.split("/")[-1]
            return response({prompt_id: first_history if prompt_id == "p1" else SUCCESS})

        session.post.side_effect = post
        session.get.side_effect = get
        with patch.object(queue.requests, "Session") as factory:
            factory.return_value.__enter__.return_value = session
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                result = queue.run(self.args(*options))
            factory.return_value.__exit__.assert_called_once()
        return result, events, prompts, uploads

    def test_sequential_batch_uses_primitive_values_and_isolates_uploads(self):
        result, events, prompts, uploads = self.run_batch()
        self.assertEqual(result, 0)
        self.assertEqual(len(prompts), 2)
        self.assertLess(events.index("/history/p1"), events.index("/prompt", events.index("/prompt") + 1))
        self.assertEqual(prompts[0]["3"]["inputs"]["value"], "Prompt EP01_S01_SH01")
        self.assertEqual(prompts[1]["4"]["inputs"]["value"], 5.5)
        self.assertEqual(prompts[0]["5"]["inputs"]["last_frame"], ["2", 0])
        self.assertTrue(uploads[0]["subfolder"].endswith("episodes/EP01/S01/EP01_S01_SH01/EXPORT"))
        self.assertTrue(all(upload["overwrite"] == "false" for upload in uploads))
        self.assertNotIn("/free", events)
        _, _, _, next_uploads = self.run_batch()
        self.assertNotEqual(uploads[0]["subfolder"], next_uploads[0]["subfolder"])

    def test_cleanup_is_armed_before_completion(self):
        result, events, _, _ = self.run_batch("--free-memory")
        self.assertEqual(result, 0)
        self.assertLess(events.index("/free"), events.index("/history/p1"))

    def test_unload_models_does_not_request_cache_cleanup(self):
        session = Mock()
        session.post.return_value = response({})
        queue.free_memory(session, queue.DEFAULT_SERVER, free_memory_flag=False, unload_models_flag=True)
        self.assertEqual(session.post.call_args.kwargs["json"], {"free_memory": False, "unload_models": True})

    def test_confirmed_failure_stops_or_continues_as_requested(self):
        result, _, prompts, _ = self.run_batch(first_history=FAILURE)
        self.assertEqual((result, len(prompts)), (1, 1))
        result, _, prompts, _ = self.run_batch("--continue-on-error", first_history=FAILURE)
        self.assertEqual((result, len(prompts)), (2, 2))

    def test_unknown_job_state_stops_even_with_continue_on_error(self):
        result, _, prompts, _ = self.run_batch("--continue-on-error", monitor_error=requests.Timeout("offline"))
        self.assertEqual((result, len(prompts)), (1, 1))

    def test_completed_false_error_does_not_poll_forever(self):
        session = Mock()
        session.get.return_value = response({"p1": FAILURE})
        with patch.object(queue.time, "sleep") as sleep:
            with self.assertRaisesRegex(queue.QueueRunnerError, "Out of memory"):
                queue.wait_for_completion(session, queue.DEFAULT_SERVER, "p1", 1)
            sleep.assert_not_called()

    def test_pending_job_waits_for_history(self):
        session = Mock()
        session.get.side_effect = [response({}), response({"queue_running": [], "queue_pending": [[0, "p1"]]}), response({"p1": SUCCESS})]
        with patch.object(queue.time, "sleep") as sleep:
            self.assertEqual(queue.wait_for_completion(session, queue.DEFAULT_SERVER, "p1", 1), SUCCESS)
            sleep.assert_called_once_with(1)

    def test_completion_between_history_and_queue_reads(self):
        session = Mock()
        session.get.side_effect = [response({}), response({"queue_running": [], "queue_pending": []}), response({"p1": SUCCESS})]
        self.assertEqual(queue.wait_for_completion(session, queue.DEFAULT_SERVER, "p1", 1), SUCCESS)

    def test_removed_job_stops_without_history(self):
        session = Mock()
        session.get.side_effect = [response({}), response({"queue_running": [], "queue_pending": []}), response({})]
        with self.assertRaisesRegex(queue.QueueServerError, "disappeared"):
            queue.wait_for_completion(session, queue.DEFAULT_SERVER, "p1", 1)

    def test_outputs_do_not_imply_success(self):
        session = Mock()
        session.get.return_value = response({"p1": {"outputs": {}, "status": {}}})
        with self.assertRaisesRegex(queue.QueueServerError, "explicit success"):
            queue.wait_for_completion(session, queue.DEFAULT_SERVER, "p1", 1)

    def test_invalid_durations_and_utf8_bom(self):
        folder = self.exports[0]
        for value in ("nan", "inf", "0", "-1"):
            with self.subTest(value=value):
                (folder / "duration.txt").write_text(value, encoding="utf-8")
                with self.assertRaisesRegex(queue.QueueRunnerError, "positive finite"):
                    queue.read_job(folder)
        (folder / "duration.txt").write_text("6", encoding="utf-8-sig")
        (folder / "prompt.txt").write_text("Hello", encoding="utf-8-sig")
        job = queue.read_job(folder)
        self.assertEqual(job["prompt"], "Hello")
        self.assertIs(type(job["duration"]), int)

    def test_canvas_export_rejected_and_wrapped_api_export_accepted(self):
        self.workflow.write_text(json.dumps({"nodes": [], "links": []}), encoding="utf-8")
        with self.assertRaisesRegex(queue.QueueRunnerError, "API Format"):
            queue.load_json(self.workflow)
        self.workflow.write_text(json.dumps({"prompt": template()}), encoding="utf-8-sig")
        self.assertEqual(queue.load_json(self.workflow), template())

    def test_duration_accepts_seconds_labels(self):
        folder = self.exports[0]
        for text, expected in [("3", 3), ("3 seconds", 3), ("3 second", 3),
                ("3 sec", 3), ("3secs", 3), ("3s", 3), (" 3.5 Seconds ", 3.5), ("0.5 sec", 0.5)]:
            with self.subTest(text=text):
                (folder / "duration.txt").write_text(text, encoding="utf-8")
                self.assertEqual(queue.read_job(folder)["duration"], expected)

    def test_duration_does_not_ignore_other_units_or_extra_text(self):
        folder = self.exports[0]
        for text in ("3 minutes", "3 ms", "3 frames", "3 seconds extra", "3 seconds\n5 seconds"):
            with self.subTest(text=text):
                (folder / "duration.txt").write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(queue.QueueRunnerError, "must contain seconds"):
                    queue.read_job(folder)

    def test_invalid_mapping_fails_before_network(self):
        workflow = template()
        workflow["1"]["inputs"] = {"wrong": "start.png"}
        self.workflow.write_text(json.dumps(workflow), encoding="utf-8")
        with patch.object(queue.requests, "Session") as factory:
            with self.assertRaisesRegex(queue.QueueRunnerError, 'no input "image"'):
                queue.run(self.args())
            factory.assert_not_called()

    def test_duration_frames_not_silently_treated_as_seconds(self):
        workflow = template()
        workflow["4"]["inputs"] = {"length": 124}
        with self.assertRaisesRegex(queue.QueueRunnerError, "duration must be in seconds"):
            queue.resolve_input_name(workflow, "QUEUE_DURATION", None, ("duration", "seconds", "value"))

    def test_poll_interval_requires_positive_finite_number(self):
        for value in ("0", "-1", "nan", "inf"):
            with self.subTest(value=value), self.assertRaisesRegex(queue.QueueRunnerError, "poll-interval"):
                queue.run(self.args(f"--poll-interval={value}"))

    def test_submission_timeout_reports_unknown_acceptance(self):
        session = Mock()
        session.post.side_effect = requests.Timeout("timed out")
        with self.assertRaisesRegex(queue.QueueServerError, "acceptance is unknown"):
            queue.submit_workflow(session, queue.DEFAULT_SERVER, template())

    def test_submission_server_error_reports_unknown_acceptance(self):
        session = Mock()
        session.post.return_value = response({"error": "Internal error"}, status=500)
        with self.assertRaisesRegex(queue.QueueServerError, "acceptance is unknown"):
            queue.submit_workflow(session, queue.DEFAULT_SERVER, template())

    def test_discovers_only_exports_in_episode_scene_shot_order(self):
        for episode, scene, shot in [("EP02", "S01", "EP02_S01_SH01"), ("EP01", "S02", "EP01_S02_SH01")]:
            (self.jobs / "episodes" / episode / scene / shot / "EXPORT").mkdir(parents=True)
        (self.jobs / "characters" / "CHAR_CHEN_LIN").mkdir(parents=True)
        (self.jobs / ".backup" / "EP00_S00_SH00" / "EXPORT").mkdir(parents=True)
        jobs = queue.discover_jobs(self.jobs)
        self.assertEqual([p.parent.name for p in jobs], [
            "EP01_S01_SH01", "EP01_S01_SH02", "EP01_S02_SH01", "EP02_S01_SH01",
        ])
        self.assertEqual(queue.discover_jobs(self.exports[0].parent), [self.exports[0]])
        self.assertEqual(queue.discover_jobs(self.exports[0]), [self.exports[0]])

    def test_first_only_and_first_last_jobs_in_same_batch(self):
        (self.exports[0] / "last_frame.png").unlink()
        result, _, prompts, uploads = self.run_batch()
        self.assertEqual(result, 0)
        self.assertEqual(len(uploads), 3)
        self.assertEqual(len(prompts), 2)
        self.assertNotIn("2", prompts[0])
        self.assertNotIn("last_frame", prompts[0]["5"]["inputs"])
        self.assertEqual(prompts[0]["5"]["inputs"]["first_frame"], ["1", 0])
        self.assertTrue(prompts[0]["1"]["inputs"]["image"].endswith("/first_frame.png"))
        self.assertEqual(prompts[1]["5"]["inputs"]["last_frame"], ["2", 0])
        self.assertTrue(prompts[1]["2"]["inputs"]["image"].endswith("/last_frame.png"))
        self.assertEqual(queue.load_json(self.workflow), template(), "template must keep its last frame for later jobs")

    def test_last_frame_with_other_consumers_cannot_be_omitted(self):
        workflow = template()
        workflow["7"] = {"class_type": "GetImageSize", "inputs": {"image": ["2", 0]}}
        self.workflow.write_text(json.dumps(workflow), encoding="utf-8")
        (self.exports[0] / "last_frame.png").unlink()
        result, events, prompts, uploads = self.run_batch()
        self.assertEqual(result, 1)
        self.assertEqual(prompts, [])
        self.assertEqual(uploads, [])
        self.assertEqual(events, ["/system_stats"])

    def test_export_missing_required_files_is_not_silently_skipped(self):
        (self.exports[0] / "first_frame.png").unlink()
        self.assertEqual(queue.discover_jobs(self.jobs), self.exports)
        with self.assertRaisesRegex(queue.QueueRunnerError, "first_frame.png"):
            queue.read_job(self.exports[0])

    def test_turbo_modes_keep_step_switch_and_additional_loras(self):
        folder = pathlib.Path(__file__).parents[1] / "workflow"
        template = queue.load_json(folder / "video_minimax_h3_i2v_api.json")
        template["105_126"]["inputs"]["value"] = False
        original = copy.deepcopy(template)
        for turbo in (None, True, False):
            with self.subTest(turbo=turbo):
                workflow = copy.deepcopy(template)
                queue.configure_loras(workflow, turbo, ["styles/a.safetensors", "styles/b.safetensors"], 0.7)
                self.assertEqual(workflow["105_126"]["inputs"]["value"], turbo is True)
                self.assertEqual(workflow["105_123"]["inputs"]["switch"], ["105_126", 0])
                step_inputs = workflow["105_123"]["inputs"]
                self.assertEqual(workflow[step_inputs["on_false"][0]]["inputs"]["value"], 20)
                self.assertEqual(workflow[step_inputs["on_true"][0]]["inputs"]["value"], 8)
                self.assertEqual("105_121" in workflow, turbo is not False)
                if turbo is False:
                    self.assertEqual(workflow["105_122"]["inputs"]["on_true"], ["105_6", 0])
                self.assertEqual(workflow["queue_lora_1"]["inputs"], {
                    "model": ["105_122", 0], "lora_name": "styles/a.safetensors", "strength_model": 0.7,
                })
                self.assertEqual(workflow["queue_lora_2"]["inputs"]["model"], ["queue_lora_1", 0])
                for consumer in ("105_9", "105_16"):
                    self.assertEqual(workflow[consumer]["inputs"]["model"], ["queue_lora_2", 0])
                for node in workflow.values():
                    for value in node["inputs"].values():
                        if isinstance(value, list):
                            self.assertIn(value[0], workflow)
        self.assertEqual(template, original)

    def test_additional_loras_preserve_manual_loaders_and_node_ids(self):
        workflow = {
            "switch": {"class_type": "ComfySwitchNode", "inputs": {}, "_meta": {"title": queue.MODEL_SWITCH_TITLE}},
            "queue_lora_1": {"class_type": "LoraLoaderModelOnly", "inputs": {
                "model": ["switch", 0], "lora_name": "manual.safetensors", "strength_model": 0.5,
            }},
            "guider": {"class_type": "BasicGuider", "inputs": {"model": ["queue_lora_1", 0]}},
        }
        queue.configure_loras(workflow, None, ["new.safetensors"], -0.2)
        self.assertEqual(workflow["queue_lora_1"]["inputs"]["model"], ["queue_lora_2", 0])
        self.assertEqual(workflow["queue_lora_1"]["inputs"]["lora_name"], "manual.safetensors")
        self.assertEqual(workflow["guider"]["inputs"]["model"], ["queue_lora_1", 0])
        self.assertEqual(workflow["queue_lora_2"]["inputs"]["model"], ["switch", 0])

    def test_directory_loras_use_server_catalog_and_directory_boundaries(self):
        session = Mock()
        session.get.return_value = response({"LoraLoaderModelOnly": {"input": {"required": {
            "lora_name": [["styles/z.safetensors", "styles/sub/a.safetensors", "styles_extra/b.safetensors",
                "styles\\c.safetensors", "outside.safetensors"]],
        }}}})
        self.assertEqual(queue.directory_loras(session, queue.DEFAULT_SERVER, "styles/"),
            ["styles\\c.safetensors", "styles/sub/a.safetensors", "styles/z.safetensors"])
        with self.assertRaisesRegex(queue.QueueRunnerError, "No LoRAs found"):
            queue.directory_loras(session, queue.DEFAULT_SERVER, "missing")
        for path in ("../styles", "/tmp/styles", "styles/../../outside"):
            session.reset_mock()
            with self.subTest(path=path), self.assertRaisesRegex(queue.QueueRunnerError, "relative"):
                queue.directory_loras(session, queue.DEFAULT_SERVER, path)
            session.get.assert_not_called()

    def test_batch_disables_turbo_and_adds_directory_loras_before_uploads(self):
        folder = pathlib.Path(__file__).parents[1] / "workflow"
        self.workflow.write_text((folder / "video_minimax_h3_i2v_api.json").read_text())
        result, events, prompts, _ = self.run_batch("--no-turbo", "--lora-dir", "styles", "--lora-strength", "0.6")
        self.assertEqual(result, 0)
        self.assertLess(events.index("/object_info/LoraLoaderModelOnly"), events.index("/upload/image"))
        for workflow in prompts:
            self.assertNotIn("105_121", workflow)
            self.assertFalse(workflow["105_126"]["inputs"]["value"])
            self.assertEqual(workflow["queue_lora_1"]["inputs"]["lora_name"], "styles/a.safetensors")
            self.assertEqual(workflow["queue_lora_2"]["inputs"]["lora_name"], "styles/b.safetensors")
            self.assertEqual(workflow["queue_lora_2"]["inputs"]["strength_model"], 0.6)

    def test_canvas_workflows_have_only_optional_turbo_lora(self):
        folder = pathlib.Path(__file__).parents[1] / "workflow"
        for filename in ("video_minimax_h3_i2v.json", "video_minimax_h3_i2v_shot_queue.json"):
            with self.subTest(filename=filename):
                canvas = json.loads((folder / filename).read_text())
                subgraph = canvas["definitions"]["subgraphs"][0]
                loras = [node for node in subgraph["nodes"] if node["type"] == "LoraLoaderModelOnly"]
                self.assertEqual(len(loras), 1)
                toggle = next(node for node in subgraph["nodes"] if node.get("title") == queue.TURBO_NODE_TITLE)
                self.assertEqual(toggle["type"], "PrimitiveBoolean")
                node_ids = {node["id"] for node in subgraph["nodes"]} | {-10, -20}
                for link in subgraph["links"]:
                    self.assertIn(link["origin_id"], node_ids)
                    self.assertIn(link["target_id"], node_ids)
                model_switch = next(node for node in subgraph["nodes"] if node.get("title") == queue.MODEL_SWITCH_TITLE)
                self.assertEqual(model_switch["outputs"][0]["links"], [234, 241])

    def test_bundled_api_workflow_prepares_both_frame_modes(self):
        folder = pathlib.Path(__file__).parents[1] / "workflow"
        api = queue.load_json(folder / "video_minimax_h3_i2v_api.json")
        ui = json.loads((folder / "video_minimax_h3_i2v.json").read_text())
        ids = {node["class_type"]: node_id for node_id, node in api.items()}
        h3_id = ids["MiniMaxH3ImageToVideo"]
        duration_id = ids["PrimitiveFloat"]
        math_id = ids["ComfyMathExpression"]
        original = copy.deepcopy(api)
        prompt_input = queue.resolve_input_name(api, "QUEUE_PROMPT", None, ("text", "prompt", "value"))
        duration_input = queue.resolve_input_name(api, "QUEUE_DURATION", None, ("duration", "seconds", "value"))
        self.assertEqual((prompt_input, duration_input), ("prompt", "value"))
        canvas_nodes = ui["nodes"] + [
            node for subgraph in ui["definitions"]["subgraphs"] for node in subgraph["nodes"]
        ]
        for title in (queue.START_IMAGE_NODE_TITLE, queue.END_IMAGE_NODE_TITLE,
                queue.PROMPT_NODE_TITLE, queue.DURATION_NODE_TITLE):
            matches = [node for node in canvas_nodes if node.get("title") == title]
            self.assertEqual(len(matches), 1, f"canvas workflow must contain one {title}")
            _, api_node = queue.find_node_by_title(api, title)
            self.assertEqual(matches[0]["type"], api_node["class_type"])
        for last_frame in (None, "shot/last_frame.png"):
            with self.subTest(last_frame=last_frame):
                job = queue.prepare_workflow(api, start_image="shot/first_frame.png", end_image=last_frame,
                    prompt="Motion and dialogue", duration=5.5, prompt_input=prompt_input, duration_input=duration_input)
                h3 = job[h3_id]["inputs"]
                self.assertEqual(h3["prompt"], "Motion and dialogue")
                self.assertEqual(job[duration_id]["inputs"]["value"], 5.5)
                self.assertEqual(h3["length"], [math_id, 1])
                self.assertEqual(job[math_id]["inputs"]["values.a"], [duration_id, 0])
                self.assertEqual(h3.get("last_frame"), None if last_frame is None else ["127", 0])
                for node in job.values():
                    for value in node["inputs"].values():
                        if isinstance(value, list):
                            self.assertIn(value[0], job, "prepared workflow must not have dangling links")
        self.assertEqual(api, original)


if __name__ == "__main__":
    unittest.main()
