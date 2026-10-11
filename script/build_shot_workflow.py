#!/usr/bin/env python3
"""Fill a canvas workflow's Shot Queue from configurable shot folders, without an API."""
import argparse
import json
import math
from pathlib import Path
import re
import shutil
import sys
import uuid


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_TEMPLATE = SCRIPT_DIR.parent / "workflow/video_minimax_h3_i2v_shot_queue.json"
DEFAULT_INPUT_DIR = SCRIPT_DIR.parents[2] / "input"
# File paths are relative to each matched shot folder. Set end_frame to None to omit it.
DEFAULT_LAYOUT = {
    "jobs": None,
    "job_glob": "**/EXPORT",
    "start_frame": "first_frame.png",
    "end_frame": "last_frame.png",
    "prompt": "prompt.txt",
    "duration": "duration.txt",
}


def load_layout(args):
    layout = DEFAULT_LAYOUT.copy()
    if args.config:
        config_path = Path(args.config).expanduser().resolve()
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        if not isinstance(config, dict):
            raise ValueError("Config must be a JSON object.")
        unknown = config.keys() - layout.keys()
        if unknown:
            raise ValueError(f"Unknown config fields: {', '.join(sorted(unknown))}")
        layout.update(config)
        if isinstance(config.get("jobs"), str) and config["jobs"]:
            layout["jobs"] = str(config_path.parent / Path(config["jobs"]).expanduser())
    if args.jobs:
        layout["jobs"] = args.jobs
    for field in ("jobs", "job_glob", "start_frame", "prompt", "duration"):
        if not isinstance(layout[field], str) or not layout[field].strip():
            raise ValueError(f"Set {field} to a nonempty string; use --jobs or config for the jobs directory.")
    if layout["end_frame"] is not None and (
        not isinstance(layout["end_frame"], str) or not layout["end_frame"].strip()
    ):
        raise ValueError("end_frame must be a nonempty path string or null.")
    pattern = Path(layout["job_glob"])
    if pattern.is_absolute() or ".." in pattern.parts:
        raise ValueError("job_glob must be relative to the jobs directory, without '..'.")
    return layout


def discover_jobs(root, pattern):
    if not root.is_dir():
        raise ValueError(f"Production directory not found: {root}")
    folder_name = pattern.removeprefix("**/")
    matches_root = pattern == "." or (root.name == folder_name and not any(c in folder_name for c in "*?[]/"))
    jobs = [root] if matches_root else sorted(
        path for path in root.glob(pattern)
        if path.is_dir() and not any(part.startswith(".") for part in path.relative_to(root).parts)
    )
    if not jobs:
        raise ValueError(f"No shot folders matching {pattern!r} found in: {root}")
    return jobs


def read_job(directory, layout):
    paths = {field: directory / Path(layout[field]).expanduser()
             for field in ("start_frame", "prompt", "duration")}
    for field, path in paths.items():
        if not path.is_file():
            raise ValueError(f"{directory}: missing required {field} file: {path}")
    last = directory / Path(layout["end_frame"]).expanduser() if layout["end_frame"] else None
    if last is not None and last.exists() and not last.is_file():
        raise ValueError(f"Last frame is not a file: {last}")
    prompt = paths["prompt"].read_text(encoding="utf-8-sig").strip()
    if not prompt:
        raise ValueError(f"{directory}: prompt file is empty: {paths['prompt']}")
    text = paths["duration"].read_text(encoding="utf-8-sig").strip()
    value = re.sub(r"\s*(?:seconds?|secs?|s)\s*$", "", text, flags=re.IGNORECASE)
    try:
        duration = float(value)
    except ValueError as error:
        raise ValueError(f"{directory}: invalid duration in seconds: {text!r}") from error
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f"{directory}: duration must be positive finite seconds")
    return {"start_frame": paths["start_frame"], "end_frame": last if last is not None and last.is_file() else None,
            "prompt": prompt, "duration": duration}


def build(args):
    layout = load_layout(args)
    root = Path(layout["jobs"]).expanduser().resolve()
    template = Path(args.template).expanduser().resolve()
    output = Path(args.out).expanduser().resolve()
    input_dir = Path(args.input_dir).expanduser().resolve()
    if output == template:
        raise ValueError("Choose an output path different from the template.")
    if output.exists():
        raise ValueError(f"Output already exists: {output}; choose another --out path.")
    workflow = json.loads(template.read_text(encoding="utf-8-sig"))
    if (not isinstance(workflow, dict) or not isinstance(workflow.get("nodes"), list)
            or any(not isinstance(node, dict) for node in workflow["nodes"])):
        raise ValueError("Template must be a canvas workflow, not an API-format workflow.")
    queues = [node for node in workflow["nodes"] if node.get("type") == "ShotQueue"]
    if len(queues) != 1:
        raise ValueError("Template must contain exactly one Shot Queue node.")
    queue = queues[0]
    widgets = queue.get("widgets_values")
    if not isinstance(widgets, list) or len(widgets) < 2:
        raise ValueError("Shot Queue must have shots_json and shot_index widget values.")
    # Validate all shots before copying images or writing the workflow.
    jobs = [(directory, read_job(directory, layout)) for directory in discover_jobs(root, layout["job_glob"])]
    batch_dir = input_dir / "shot_queue" / f"production_{uuid.uuid4().hex}"
    shots = []
    output_created = False
    try:
        for index, (directory, job) in enumerate(jobs, 1):
            shot = {"start_frame": "", "end_frame": "", "prompt": job["prompt"], "duration": job["duration"]}
            for field in ("start_frame", "end_frame"):
                source = job[field]
                if source is None:
                    continue
                destination = batch_dir / f"shot_{index:03d}" / field / source.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                shot[field] = destination.relative_to(input_dir).as_posix()
            shots.append(shot)
            print(f"[{index}/{len(jobs)}] {directory} — {shot['duration']:g}s")
        raw = json.dumps(shots, ensure_ascii=False, separators=(",", ":"))
        widgets[0], widgets[1] = raw, 0
        named = queue.get("widgets_values_named")
        if isinstance(named, dict):
            named.update(shots_json=raw, shot_index=0)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as stream:
            output_created = True
            json.dump(workflow, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except BaseException:
        shutil.rmtree(batch_dir, ignore_errors=True)
        if output_created:
            output.unlink(missing_ok=True)
        raise
    print(f"Saved {len(shots)} shots: {output}\nImages: {batch_dir}\nLoad the workflow in ComfyUI to review, edit, and Run Queue.")
    return output


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", help="Shot discovery root; overrides config and DEFAULT_LAYOUT")
    parser.add_argument("--config", help="JSON configuration for shot folders and file paths")
    parser.add_argument("--template", default=str(DEFAULT_TEMPLATE), help="Canvas workflow containing one Shot Queue")
    parser.add_argument("--out", default="production_shot_queue.json", help="New canvas workflow JSON to save")
    parser.add_argument("--input-dir", default=str(DEFAULT_INPUT_DIR), help="Target ComfyUI input directory for copied frames")
    return parser


if __name__ == "__main__":
    try:
        build(build_parser().parse_args())
    except (ValueError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
