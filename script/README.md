# Standalone folder runner

`comfy_queue.py` does not import or require the Shot Queue custom node. It uploads
frames and submits one job at a time through ComfyUI's API. The script must keep
running until the batch finishes; a browser does not need to stay open.

Point `--jobs` at your production folder, an episode, a scene, or a single shot.
The script recursively finds `EXPORT` folders and reads only their exported
files; character/location/prop assets and the shot's source prompts are ignored.

```text
production/episodes/EP01/S01/EP01_S01_SH01/EXPORT/
    first_frame.png
    last_frame.png  (optional)
    prompt.txt
    duration.txt
```

Duration is a positive number of **seconds**, including fractions. Values such as
`3`, `3 seconds`, `3 sec`, `3s`, and `3.5 seconds` are accepted. Export folders
run in alphabetical path order: episode, scene, then shot for padded names such
as `EP01/S01/EP01_S01_SH01`. An `EXPORT` folder missing a required file reports an
error instead of being silently skipped.

In your workflow, rename the two Load Image nodes to `QUEUE_START_IMAGE` and
`QUEUE_END_IMAGE`. Rename the prompt source to `QUEUE_PROMPT` and the duration
source to `QUEUE_DURATION`, then export in **API Format**. A normal canvas JSON
file, including this repository's example workflow, cannot be submitted directly.

The prepared `workflow/video_minimax_h3_i2v_api.json` can be used directly with
`--workflow`. The editable canvas workflow is `workflow/video_minimax_h3_i2v.json`;
the four titles are already set there (`QUEUE_PROMPT` and `QUEUE_DURATION` are
inside the H3 subgraph). Its settings may differ from the API template. If you
change models or other settings in ComfyUI, export that updated workflow using
**Export (API)** before running the script. Regular Export/Save produces canvas
JSON even when its filename ends with `_api.json`.

Keep both image loaders in the template. For a shot without `last_frame.png`, the
script removes `QUEUE_END_IMAGE` and its connection to the native
`MiniMaxH3ImageToVideo.last_frame` input from that job's copy. Shots with a last
frame keep the connection. The last-frame loader must connect directly to that
input only; other consumers cause an error when a shot has no last frame.

Standard `PrimitiveStringMultiline.value`, `CLIPTextEncode.text`, and
`PrimitiveFloat.value` inputs are detected automatically. For MiniMax H3, keep
the existing seconds-to-frame-count calculation after the duration source.
Other field names can be selected with `--prompt-input` and `--duration-input`.

```bash
python -m pip install requests
python script/comfy_queue.py --workflow workflow/video_minimax_h3_i2v_api.json --jobs "/path/to/test chapter/production" --server http://127.0.0.1:8188
```

Use `--turbo` or `--no-turbo` to enable or disable the turbo LoRA and select the
workflow's turbo or base step count. Omit both to keep the template setting.
Disabling turbo also removes its loader from the submitted prompt, so that run
does not require the turbo LoRA file. Custom API templates using these flags need
a boolean node titled `Enable Turbo LoRA` and, for disabling turbo, the model
switch titled `If/Else Switch (Model)` connected directly to the turbo loader.

Additional LoRAs can be added manually in ComfyUI and included in your API export,
or selected with `--lora-dir styles` for a folder under the **server's**
`models/loras/`. The script reads ComfyUI's filename list and chains every LoRA
in that folder and its subfolders in alphabetical path order after
`If/Else Switch (Model)`, before any manually added downstream loaders. It does
not copy or upload LoRA files. Use a folder containing only the additional LoRAs
you want to apply; keep the turbo LoRA outside it to avoid applying it twice.
`--lora-strength` sets the model strength for all directory LoRAs (default `1`).
Use manual loader nodes for individual strengths. These settings apply to every
shot in the batch.

```bash
python script/comfy_queue.py --workflow workflow/video_minimax_h3_i2v_api.json --jobs /path/to/production --no-turbo --lora-dir styles --lora-strength 0.7
```

`--free-memory` requests execution-cache cleanup and model unloading after each
job. `--unload-models` requests only model unloading. These are global ComfyUI
flags applied by its worker; use a dedicated instance while requesting cleanup.

`--continue-on-error` skips invalid folders and confirmed execution failures.
Connection failures or unknown job states always stop the batch, since an
accepted job may still be running. Check ComfyUI's queue before restarting after
a submission timeout. Ctrl+C stops the script but does not cancel submitted jobs.

Uploads use a unique `input/comfy_queue/<run-id>/<relative-export-path>/` directory for each
run so later runs cannot overwrite frames needed by earlier jobs. Uploaded files
remain in ComfyUI's input directory after completion.

Run the script checks without contacting a real ComfyUI instance:

```bash
python -m unittest discover -s script -v
```
