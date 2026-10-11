# Production workflow builder

## Generate a workflow for review

`build_shot_workflow.py` reads shot folders (by default, production `EXPORT` folders) and writes
**one canvas workflow containing every shot in Shot Queue**.
It uses only Python's standard library; no server or API connection is needed.

```bash
python3 script/build_shot_workflow.py --jobs "/path/to/production" --out "/path/to/production_review.json"
```

The default template is `workflow/video_minimax_h3_i2v_shot_queue.json`. Use
`--template /path/to/template.json` to select another normal canvas workflow
containing exactly one Shot Queue node. The script replaces its shot list and
selects the first shot; other nodes, connections, settings, and queue properties
are preserved. Both positional and named widget values are updated.

## Configure the folder layout

Edit `DEFAULT_LAYOUT` near the top of the script, or copy
`script/shot_layout.example.json` and use:

```bash
python3 script/build_shot_workflow.py --config /path/to/shot_layout.json
```

The JSON accepts these fields; omitted fields use `DEFAULT_LAYOUT`:

| Field | Meaning | Default |
|---|---|---|
| `jobs` | Directory to search; `--jobs` overrides it | Must be supplied |
| `job_glob` | Pattern selecting shot directories under `jobs` | `**/EXPORT` |
| `start_frame` | First-frame file path per shot | `first_frame.png` |
| `end_frame` | Optional last-frame path; `null` disables it | `last_frame.png` |
| `prompt` | Prompt text file path per shot | `prompt.txt` |
| `duration` | Duration text file path per shot | `duration.txt` |

For example, use `job_glob: "episodes/*/scenes/*/shots/*"` if files are stored
inside shot directories rather than EXPORT folders. File paths may include
subdirectories, such as `frames/start.png` or `metadata/duration.txt`.
Paths are relative to each matched shot directory; absolute paths are also
accepted and refer to a shared file for every shot. Relative `jobs` in a JSON
config is resolved against the config file's directory; `--jobs` and script
defaults are resolved against the current working directory. Use `job_glob: "*"`
for immediate child directories or `"."` to treat `jobs` itself as one shot.
Missing optional last frames remain empty; other configured files are required.

## Default production layout

Point `--jobs` at a production folder, an episode, a scene, a single shot, or an
`EXPORT` folder. The expected layout is:

```text
production/episodes/EP01/S01/EP01_S01_SH01/EXPORT/
    first_frame.png
    last_frame.png  (optional)
    prompt.txt
    duration.txt
```

Duration is a positive number of seconds, including fractions. Values such as
`3`, `3 seconds`, `3 sec`, `3s`, and `3.5 seconds` are accepted. Missing required
files or an empty prompt cause an error instead of silently skipping the shot.

Matched shot folders run in alphabetical path order, with hidden directories
ignored. All shots are validated before copying images.
Frames are copied into a unique `input/shot_queue/production_<id>/` folder so
ComfyUI can load and preview them. Original exports and the template are untouched.
By default the input directory belongs to the ComfyUI installation containing
this extension; use `--input-dir /path/to/ComfyUI/input` for another installation.
If `--out` is omitted, the workflow is saved as `production_shot_queue.json` in
the current working directory. An existing output workflow is never overwritten;
choose a new `--out` path. Failed exports remove their copied batch and any
partially written output.

Load the generated JSON in ComfyUI, review the paths, prompts, and durations,
make any edits, then click **Run Queue**. The usual ComfyUI Queue button runs
only the selected shot. If moving the workflow to another machine, copy its
`shot_queue/production_<id>/` image folder into that machine's `input/` too.

The default template includes LBH second-pass refinement. Sampling steps,
denoise, resolution, models, and LoRAs are preserved from the template. Edit
them in the template before generating, or in the generated workflow before
running. The script only updates the Shot Queue task list and selection.
