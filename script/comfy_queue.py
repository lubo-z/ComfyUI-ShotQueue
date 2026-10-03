#!/usr/bin/env python3
"""
Simple ComfyUI folder queue runner.

Folder layout:

production/episodes/EP01/S01/
├── EP01_S01_SH01/EXPORT/
│   ├── first_frame.png
│   ├── prompt.txt
│   └── duration.txt
└── EP01_S01_SH02/EXPORT/
    ├── first_frame.png
    ├── last_frame.png
    ├── prompt.txt
    └── duration.txt

EXPORT folders are found recursively. last_frame.png is optional; when absent,
the last-frame loader and its MiniMaxH3ImageToVideo.last_frame link are removed
from that job's API prompt.

Workflow preparation:
1. Export your ComfyUI workflow in "API Format".
2. Rename the four relevant nodes in ComfyUI to:
     QUEUE_START_IMAGE
     QUEUE_END_IMAGE
     QUEUE_PROMPT
     QUEUE_DURATION
3. Save/export that API workflow JSON.
4. Prompt/duration fields are detected for standard text and primitive nodes.
   Use --prompt-input / --duration-input for other nodes. Duration is seconds;
   keep your workflow's existing seconds-to-frame-count conversion.

Example:
    pip install requests

    python comfy_queue.py \
        --workflow workflow_api.json \
        --jobs ./production \
        --server http://127.0.0.1:8188

Optional:
    --free-memory      Clear execution caches and unload models between jobs.
    --unload-models    Unload models between jobs without clearing caches.
    --continue-on-error

This script runs independently of the Shot Queue custom node. Keep the script
running until the batch finishes; no browser is needed. Cleanup uses ComfyUI's
global flags, so do not submit other jobs to the same instance during cleanup.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import re
import sys
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

import requests


# ---------------------------------------------------------------------------
# Workflow node titles
# Rename the corresponding nodes in ComfyUI to these titles before exporting
# the workflow in API format.
# ---------------------------------------------------------------------------

START_IMAGE_NODE_TITLE = "QUEUE_START_IMAGE"
END_IMAGE_NODE_TITLE = "QUEUE_END_IMAGE"
PROMPT_NODE_TITLE = "QUEUE_PROMPT"
DURATION_NODE_TITLE = "QUEUE_DURATION"
TURBO_NODE_TITLE = "Enable Turbo LoRA"
MODEL_SWITCH_TITLE = "If/Else Switch (Model)"


# ---------------------------------------------------------------------------
# Input field names inside those nodes.
#
# Standard LoadImage uses: image
# Standard text nodes commonly use: text
# Use a seconds input such as PrimitiveFloat.value for duration, not a length
# input that expects a frame count.
# ---------------------------------------------------------------------------

START_IMAGE_INPUT = "image"
END_IMAGE_INPUT = "image"
PROMPT_INPUT = "text"
DURATION_INPUT = "duration"


DEFAULT_SERVER = "http://127.0.0.1:8188"
DEFAULT_POLL_INTERVAL = 2.0


class QueueRunnerError(RuntimeError):
    pass


class QueueServerError(QueueRunnerError):
    """Stop the batch when the server or a submitted job's state is unknown."""


def response_object(response: requests.Response, endpoint: str) -> dict[str, Any]:
    try:
        result = response.json()
    except ValueError as e:
        raise QueueServerError(f"Invalid JSON response from {endpoint}") from e
    if not isinstance(result, dict):
        raise QueueServerError(f"Expected a JSON object from {endpoint}")
    return result


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise QueueRunnerError(f"Cannot read workflow {path}: {e}") from e

    if not isinstance(data, dict):
        raise QueueRunnerError(f"Workflow JSON must be an object: {path}")

    # Some exports/wrappers may contain {"prompt": {...}}.
    # Pure ComfyUI API-format export is normally already the prompt dictionary.
    if "prompt" in data and isinstance(data["prompt"], dict):
        data = data["prompt"]

    if not data or any(
        not isinstance(node, dict)
        or not isinstance(node.get("class_type"), str)
        or not isinstance(node.get("inputs"), dict)
        for node in data.values()
    ):
        raise QueueRunnerError(
            f"{path}: export the workflow in API Format, not the canvas/workflow format."
        )

    return data


def find_node_by_title(workflow: dict[str, Any], title: str) -> tuple[str, dict[str, Any]]:
    """
    Find an API-format workflow node by _meta.title.

    Typical node:
    {
      "12": {
        "inputs": {...},
        "class_type": "LoadImage",
        "_meta": {"title": "QUEUE_START_IMAGE"}
      }
    }
    """
    matches = []

    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue

        meta = node.get("_meta", {})
        if isinstance(meta, dict) and meta.get("title") == title:
            matches.append((str(node_id), node))

    if not matches:
        raise QueueRunnerError(
            f'Cannot find node titled "{title}" in workflow.\n'
            f'Rename that node in ComfyUI to "{title}" and export again in API format.'
        )

    if len(matches) > 1:
        ids = ", ".join(node_id for node_id, _ in matches)
        raise QueueRunnerError(
            f'Multiple nodes use title "{title}" (IDs: {ids}). Titles must be unique.'
        )

    return matches[0]


def set_node_input(
    workflow: dict[str, Any],
    title: str,
    input_name: str,
    value: Any,
) -> None:
    node_id, node = find_node_by_title(workflow, title)

    inputs = node.get("inputs")
    if not isinstance(inputs, dict):
        raise QueueRunnerError(f'Node "{title}" (ID {node_id}) has no inputs object.')

    if input_name not in inputs:
        available = ", ".join(sorted(inputs.keys()))
        raise QueueRunnerError(
            f'Node "{title}" (ID {node_id}) has no input "{input_name}". '
            f"Available inputs: {available or '(none)'}"
        )

    inputs[input_name] = value


def resolve_input_name(
    workflow: dict[str, Any], title: str, explicit: str | None, candidates: tuple[str, ...]
) -> str:
    node_id, node = find_node_by_title(workflow, title)
    inputs = node.get("inputs")
    if not isinstance(inputs, dict):
        raise QueueRunnerError(f'Node "{title}" (ID {node_id}) has no inputs object.')
    if explicit is not None:
        if explicit not in inputs:
            raise QueueRunnerError(f'Node "{title}" has no input "{explicit}".')
        return explicit
    matches = [name for name in candidates if name in inputs]
    if len(matches) != 1:
        available = ", ".join(inputs)
        raise QueueRunnerError(
            f'Cannot select the input for "{title}". Available inputs: {available}. '
            "Specify --prompt-input or --duration-input; duration must be in seconds."
        )
    return matches[0]


def validate_server(session: requests.Session, server: str) -> None:
    try:
        r = session.get(f"{server}/system_stats", timeout=10)
        r.raise_for_status()
    except requests.RequestException as e:
        raise QueueServerError(
            f"Cannot connect to ComfyUI at {server}\n"
            f"Make sure ComfyUI is running and the address is correct.\n"
            f"Details: {e}"
        ) from e


def upload_image(
    session: requests.Session,
    server: str,
    image_path: Path,
    subfolder: str,
) -> str:
    """
    Upload a local image into ComfyUI's input directory via /upload/image.

    Returns the value that should be placed into a LoadImage node's "image"
    input, e.g.:
        comfy_queue/<run-id>/episodes/EP01/S01/EP01_S01_SH01/EXPORT/first_frame.png
    """
    if not image_path.is_file():
        raise QueueRunnerError(f"Image not found: {image_path}")

    with image_path.open("rb") as f:
        files = {
            "image": (image_path.name, f),
        }
        data = {
            "type": "input",
            "subfolder": subfolder,
            "overwrite": "false",
        }

        try:
            r = session.post(
                f"{server}/upload/image",
                files=files,
                data=data,
                timeout=120,
            )
            r.raise_for_status()
        except requests.RequestException as e:
            raise QueueServerError(f"Failed to upload {image_path}: {e}") from e

    result = response_object(r, "/upload/image")
    name = result.get("name")
    returned_subfolder = result.get("subfolder", "")

    if not isinstance(name, str) or not name or not isinstance(returned_subfolder, str):
        raise QueueServerError(
            f"Unexpected /upload/image response for {image_path}: {result}"
        )

    if returned_subfolder:
        # ComfyUI LoadImage uses a path relative to its input directory.
        return f"{returned_subfolder.rstrip('/')}/{name}"

    return name


def submit_workflow(
    session: requests.Session,
    server: str,
    workflow: dict[str, Any],
) -> str:
    try:
        r = session.post(
            f"{server}/prompt",
            json={"prompt": workflow},
            timeout=60,
        )
    except requests.RequestException as e:
        raise QueueServerError(
            f"Workflow submission failed; acceptance is unknown. Check ComfyUI's queue before restarting: {e}"
        ) from e

    if not r.ok:
        try:
            details = json.dumps(r.json(), indent=2, ensure_ascii=False)
        except ValueError:
            details = r.text

        if r.status_code >= 500:
            raise QueueServerError(
                f"ComfyUI returned HTTP {r.status_code}; acceptance is unknown. "
                f"Check its queue before restarting:\n{details}"
            )
        raise QueueRunnerError(
            f"ComfyUI rejected the workflow (HTTP {r.status_code}):\n{details}"
        )

    result = response_object(r, "/prompt")
    prompt_id = result.get("prompt_id")

    if not isinstance(prompt_id, str) or not prompt_id:
        raise QueueServerError(f"Unexpected /prompt response; acceptance is unknown: {result}")
    if result.get("node_errors"):
        raise QueueServerError(
            f"ComfyUI accepted job {prompt_id} with node validation errors: {result['node_errors']}. "
            "Check its queue before restarting."
        )

    return prompt_id


def wait_for_completion(
    session: requests.Session,
    server: str,
    prompt_id: str,
    poll_interval: float,
) -> dict[str, Any]:
    """Wait for explicit success; errors and removed jobs are terminal."""
    while True:
        history = get_server_object(session, server, f"/history/{prompt_id}")
        if prompt_id not in history:
            queue = get_server_object(session, server, "/queue")
            running, pending = queue.get("queue_running"), queue.get("queue_pending")
            if not isinstance(running, list) or not isinstance(pending, list):
                raise QueueServerError("Unexpected /queue response")
            if any(not isinstance(item, list) or len(item) < 2 for item in running + pending):
                raise QueueServerError("Unexpected job record in /queue response")
            if any(item[1] == prompt_id for item in running + pending):
                time.sleep(poll_interval)
                continue
            # The job may have completed between the history and queue reads.
            history = get_server_object(session, server, f"/history/{prompt_id}")
            if prompt_id not in history:
                raise QueueServerError(
                    f"Job {prompt_id} disappeared from the queue without history. "
                    "It may have been cancelled or ComfyUI restarted."
                )

        entry = history[prompt_id]
        if not isinstance(entry, dict) or not isinstance(entry.get("status"), dict):
            raise QueueServerError(f"Job {prompt_id} has invalid history status")
        status = entry["status"]
        if status.get("status_str") == "error":
            messages = status.get("messages", [])
            formatted = json.dumps(messages, indent=2, ensure_ascii=False)
            raise QueueRunnerError(
                f"ComfyUI job failed: {prompt_id}\n{formatted}"
            )

        if status.get("completed") is True and status.get("status_str") == "success":
            return entry

        raise QueueServerError(f"Job {prompt_id} ended without an explicit success status: {status}")


def get_server_object(session: requests.Session, server: str, endpoint: str) -> dict[str, Any]:
    try:
        response = session.get(f"{server}{endpoint}", timeout=30)
        response.raise_for_status()
    except requests.RequestException as e:
        raise QueueServerError(f"Cannot check {endpoint}; job state is unknown: {e}") from e
    return response_object(response, endpoint)


def free_memory(
    session: requests.Session,
    server: str,
    *,
    free_memory_flag: bool,
    unload_models_flag: bool,
) -> None:
    if not free_memory_flag and not unload_models_flag:
        return

    payload = {
        "free_memory": free_memory_flag,
        "unload_models": unload_models_flag or free_memory_flag,
    }

    try:
        r = session.post(f"{server}/free", json=payload, timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        raise QueueServerError(f"Failed to request memory cleanup: {e}") from e


def read_job(job_dir: Path) -> dict[str, Any]:
    start = job_dir / "first_frame.png"
    end = job_dir / "last_frame.png"
    prompt_file = job_dir / "prompt.txt"
    duration_file = job_dir / "duration.txt"

    missing = [
        p.name
        for p in (start, prompt_file, duration_file)
        if not p.is_file()
    ]

    if missing:
        raise QueueRunnerError(
            f"{job_dir}: missing required file(s): {', '.join(missing)}"
        )

    if end.exists() and not end.is_file():
        raise QueueRunnerError(f"Last frame is not a file: {end}")

    prompt = prompt_file.read_text(encoding="utf-8-sig").strip()
    if not prompt:
        raise QueueRunnerError(f"{job_dir}: prompt.txt is empty")

    duration_text = duration_file.read_text(encoding="utf-8-sig").strip()
    duration_value = re.sub(r"\s*(?:seconds?|secs?|s)\s*$", "", duration_text, flags=re.IGNORECASE)

    try:
        duration_num = float(duration_value)
    except ValueError as e:
        raise QueueRunnerError(
            f"{job_dir}: duration.txt must contain seconds such as '3', '3 seconds' or '3s', got: {duration_text!r}"
        ) from e

    if not math.isfinite(duration_num) or duration_num <= 0:
        raise QueueRunnerError(f"{job_dir}: duration must be a positive finite number of seconds")

    # Preserve integer values as ints because some ComfyUI nodes validate
    # INT and will reject 6.0 even if conceptually equivalent to 6.
    duration: int | float
    if duration_num.is_integer():
        duration = int(duration_num)
    else:
        duration = duration_num

    return {
        "start": start,
        "end": end if end.is_file() else None,
        "prompt": prompt,
        "duration": duration,
    }


def discover_jobs(root: Path) -> list[Path]:
    if not root.is_dir():
        raise QueueRunnerError(f"Jobs directory not found: {root}")

    jobs = [root] if root.name == "EXPORT" else sorted(
        p for p in root.rglob("EXPORT")
        if p.is_dir() and not any(part.startswith(".") for part in p.relative_to(root).parts)
    )

    if not jobs:
        raise QueueRunnerError(f"No EXPORT folders found in: {root}")

    return jobs


def remove_last_frame(workflow: dict[str, Any]) -> None:
    node_id, _ = find_node_by_title(workflow, END_IMAGE_NODE_TITLE)
    for consumer_id, node in workflow.items():
        for input_name, value in list(node["inputs"].items()):
            if not isinstance(value, list) or len(value) != 2 or value[0] != node_id:
                continue
            if node["class_type"] != "MiniMaxH3ImageToVideo" or input_name != "last_frame" or value[1] != 0:
                raise QueueRunnerError(
                    f"Cannot omit last_frame.png: {END_IMAGE_NODE_TITLE} also feeds "
                    f"{node['class_type']} (ID {consumer_id}), input {input_name}. "
                    "Connect it only to MiniMaxH3ImageToVideo.last_frame for first-frame-only jobs."
                )
            del node["inputs"][input_name]
    del workflow[node_id]


def prepare_workflow(
    template: dict[str, Any],
    *,
    start_image: str,
    end_image: str | None,
    prompt: str,
    duration: int | float,
    prompt_input: str = PROMPT_INPUT,
    duration_input: str = DURATION_INPUT,
) -> dict[str, Any]:
    workflow = copy.deepcopy(template)

    set_node_input(
        workflow,
        START_IMAGE_NODE_TITLE,
        START_IMAGE_INPUT,
        start_image,
    )
    if end_image is None:
        remove_last_frame(workflow)
    else:
        set_node_input(workflow, END_IMAGE_NODE_TITLE, END_IMAGE_INPUT, end_image)
    set_node_input(
        workflow,
        PROMPT_NODE_TITLE,
        prompt_input,
        prompt,
    )
    set_node_input(
        workflow,
        DURATION_NODE_TITLE,
        duration_input,
        duration,
    )

    return workflow


def configure_loras(workflow: dict[str, Any], turbo: bool | None, loras: list[str], strength: float) -> None:
    if turbo is not None:
        set_node_input(workflow, TURBO_NODE_TITLE, "value", turbo)
    if turbo is not False and not loras:
        return

    model_id, model_switch = find_node_by_title(workflow, MODEL_SWITCH_TITLE)
    if model_switch["class_type"] != "ComfySwitchNode":
        raise QueueRunnerError(f'{MODEL_SWITCH_TITLE} must be a ComfySwitchNode.')
    if turbo is False:
        turbo_link = model_switch["inputs"]["on_true"]
        turbo_loader = workflow[turbo_link[0]]
        if turbo_loader["class_type"] != "LoraLoaderModelOnly":
            raise QueueRunnerError("The turbo model switch must connect directly to a LoraLoaderModelOnly node.")
        base_model = turbo_loader["inputs"]["model"]
        for node in workflow.values():
            for name, value in node["inputs"].items():
                if value == turbo_link:
                    node["inputs"][name] = copy.deepcopy(base_model)
        del workflow[turbo_link[0]]

    if not loras:
        return
    consumers = [(node, name) for node in workflow.values()
        for name, value in node["inputs"].items() if value == [model_id, 0]]
    model = [model_id, 0]
    next_id = 1
    for lora in loras:
        while f"queue_lora_{next_id}" in workflow:
            next_id += 1
        node_id = f"queue_lora_{next_id}"
        workflow[node_id] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {"model": model, "lora_name": lora, "strength_model": strength},
            "_meta": {"title": f"Additional LoRA: {lora}"},
        }
        model = [node_id, 0]
    for node, name in consumers:
        node["inputs"][name] = model.copy()


def directory_loras(session: requests.Session, server: str, directory: str) -> list[str]:
    folder = PurePosixPath(directory.replace("\\", "/"))
    if folder.is_absolute() or ".." in folder.parts:
        raise QueueRunnerError("--lora-dir must be relative to the ComfyUI server's models/loras directory.")
    info = get_server_object(session, server, "/object_info/LoraLoaderModelOnly")
    try:
        names = info["LoraLoaderModelOnly"]["input"]["required"]["lora_name"][0]
    except (KeyError, IndexError, TypeError) as e:
        raise QueueServerError("ComfyUI did not provide the LoRA filename list.") from e
    if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
        raise QueueServerError("ComfyUI returned an invalid LoRA filename list.")
    prefix = "" if str(folder) == "." else f"{folder.as_posix()}/"
    loras = sorted((name for name in names if name.replace("\\", "/").startswith(prefix)),
        key=lambda name: name.replace("\\", "/"))
    if not loras:
        raise QueueRunnerError(f"No LoRAs found in the server's models/loras/{folder} directory.")
    return loras


def run(args: argparse.Namespace) -> int:
    workflow_path = Path(args.workflow).resolve()
    jobs_root = Path(args.jobs).resolve()
    server = args.server.rstrip("/")

    if not math.isfinite(args.poll_interval) or args.poll_interval <= 0:
        raise QueueRunnerError("--poll-interval must be a positive finite number")

    if not workflow_path.is_file():
        raise QueueRunnerError(f"Workflow file not found: {workflow_path}")

    workflow_template = load_json(workflow_path)
    job_dirs = discover_jobs(jobs_root)
    if not math.isfinite(args.lora_strength):
        raise QueueRunnerError("--lora-strength must be finite")
    configure_loras(workflow_template, args.turbo, [], args.lora_strength)
    if args.lora_dir is not None:
        find_node_by_title(workflow_template, MODEL_SWITCH_TITLE)

    prompt_input = resolve_input_name(
        workflow_template, PROMPT_NODE_TITLE, args.prompt_input, ("text", "prompt", "value")
    )
    duration_input = resolve_input_name(
        workflow_template, DURATION_NODE_TITLE, args.duration_input, ("duration", "seconds", "value")
    )
    # Check every mapped input before uploading files or contacting the server.
    prepare_workflow(
        workflow_template, start_image="", end_image="", prompt="", duration=1,
        prompt_input=prompt_input, duration_input=duration_input,
    )

    print(f"ComfyUI: {server}")
    print(f"Workflow: {workflow_path}")
    print(f"Jobs: {jobs_root}")
    print(f"Found {len(job_dirs)} job(s).")
    print()

    completed_count = 0
    failed_count = 0
    upload_run = uuid.uuid4().hex

    with requests.Session() as session:
        validate_server(session, server)
        if args.lora_dir is not None:
            loras = directory_loras(session, server, args.lora_dir)
            configure_loras(workflow_template, None, loras, args.lora_strength)
        for index, job_dir in enumerate(job_dirs, start=1):
            job_path = job_dir.relative_to(jobs_root).as_posix()
            if job_path == ".":
                job_path = f"{job_dir.parent.name}/EXPORT"
            print("=" * 72)
            print(f"[{index}/{len(job_dirs)}] {job_path}")

            try:
                job = read_job(job_dir)

                print(f"  duration: {job['duration']}")
                print(f"  prompt:   {job['prompt'][:100]}{'...' if len(job['prompt']) > 100 else ''}")
                upload_subfolder = f"comfy_queue/{upload_run}/{job_path}"

                workflow = prepare_workflow(
                    workflow_template,
                    start_image="",
                    end_image="" if job["end"] is not None else None,
                    prompt=job["prompt"],
                    duration=job["duration"],
                    prompt_input=prompt_input,
                    duration_input=duration_input,
                )

                print("  uploading first frame...")
                start_uploaded = upload_image(session, server, job["start"], upload_subfolder)
                set_node_input(workflow, START_IMAGE_NODE_TITLE, START_IMAGE_INPUT, start_uploaded)
                if job["end"] is not None:
                    print("  uploading last frame...")
                    end_uploaded = upload_image(session, server, job["end"], upload_subfolder)
                    set_node_input(workflow, END_IMAGE_NODE_TITLE, END_IMAGE_INPUT, end_uploaded)
                else:
                    print("  no last frame; generating from first frame only")

                print("  submitting workflow...")
                prompt_id = submit_workflow(session, server, workflow)
                print(f"  prompt_id: {prompt_id}")

                # Arm cleanup while this job is running, before the worker moves on.
                free_memory(
                    session,
                    server,
                    free_memory_flag=args.free_memory,
                    unload_models_flag=args.unload_models,
                )
                print("  running...")
                wait_for_completion(session, server, prompt_id, args.poll_interval)
                print("  DONE")
                completed_count += 1

            except QueueServerError as e:
                print(f"  FAILED: {e}\nStopping; submitted jobs may still be running.", file=sys.stderr)
                return 1
            except (QueueRunnerError, OSError, UnicodeError) as e:
                failed_count += 1
                print(f"  FAILED: {e}", file=sys.stderr)
                if not args.continue_on_error:
                    print("\nStopping because --continue-on-error was not specified.", file=sys.stderr)
                    return 1

    print()
    print("=" * 72)
    print(
        f"Finished. Successful: {completed_count}, "
        f"Failed: {failed_count}, Total: {len(job_dirs)}"
    )

    return 0 if failed_count == 0 else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run ComfyUI workflows sequentially from job folders."
    )

    parser.add_argument(
        "--workflow",
        required=True,
        help="ComfyUI workflow exported in API format.",
    )
    parser.add_argument(
        "--jobs",
        required=True,
        help="Production, episode, scene or shot directory; recursively discovers EXPORT folders.",
    )
    parser.add_argument(
        "--server",
        default=DEFAULT_SERVER,
        help=f"ComfyUI server URL (default: {DEFAULT_SERVER}).",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL,
        help=f"Seconds between history checks (default: {DEFAULT_POLL_INTERVAL}).",
    )
    parser.add_argument(
        "--prompt-input",
        help="Input field on QUEUE_PROMPT (auto-detects text, prompt or value).",
    )
    parser.add_argument(
        "--duration-input",
        help="Seconds input field on QUEUE_DURATION (auto-detects duration, seconds or value).",
    )
    parser.add_argument(
        "--turbo",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable the turbo LoRA and turbo step count (default: keep workflow setting).",
    )
    parser.add_argument(
        "--lora-dir",
        help="Folder relative to the server's models/loras; apply its LoRAs recursively in alphabetical order.",
    )
    parser.add_argument(
        "--lora-strength",
        type=float,
        default=1.0,
        help="Model strength for LoRAs added by --lora-dir (default: 1).",
    )
    parser.add_argument(
        "--free-memory",
        action="store_true",
        help="Request cache cleanup and model unloading after each job (global to ComfyUI).",
    )
    parser.add_argument(
        "--unload-models",
        action="store_true",
        help="Request model unloading after each job without clearing execution caches.",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue after invalid folders or confirmed job failures; connection errors always stop.",
    )

    return parser


if __name__ == "__main__":
    try:
        sys.exit(run(build_parser().parse_args()))
    except (QueueRunnerError, OSError, UnicodeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted. Already submitted jobs may still run in ComfyUI.", file=sys.stderr)
        sys.exit(130)
