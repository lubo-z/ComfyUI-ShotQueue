"""Metadata-only queue. No torch/PIL imports or image reads in ShotQueue."""
import json
import math

DEFAULT_SHOTS = '[{"start_frame":"","end_frame":"","prompt":"","duration":5}]'


def current_shot(shots_json, shot_index):
    try:
        shots = json.loads(shots_json)
    except (TypeError, ValueError) as exc:
        raise ValueError("Shot Queue: invalid task JSON") from exc
    if not isinstance(shots, list) or not shots:
        raise ValueError("Shot Queue: add at least one shot")
    if isinstance(shot_index, bool) or not isinstance(shot_index, int) or not 0 <= shot_index < len(shots):
        raise ValueError("Shot Queue: current shot index is out of range (zero-based)")
    shot = shots[shot_index]
    if not isinstance(shot, dict):
        raise ValueError("Shot Queue: current shot must be an object")
    for field in ("start_frame", "end_frame", "prompt"):
        if not isinstance(shot.get(field), str):
            raise ValueError(f"Shot Queue: {field} must be text")
    duration = shot.get("duration")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        raise ValueError("Shot Queue: duration must be a positive finite number of seconds")
    return (shot["start_frame"].strip(), shot["end_frame"].strip(), shot["prompt"], float(duration), shot_index, f"shot_{shot_index + 1:03d}")


class ShotQueue:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "shots_json": ("STRING", {"default": DEFAULT_SHOTS, "multiline": True}),
            "shot_index": ("INT", {"default": 0, "min": 0, "max": 2147483647, "control_after_generate": False}),
        }}

    RETURN_TYPES = ("STRING", "STRING", "STRING", "FLOAT", "INT", "STRING")
    RETURN_NAMES = ("start_frame_path", "end_frame_path", "prompt", "duration", "shot_index", "shot_name")
    FUNCTION = "select"
    CATEGORY = "Shot Queue"

    def select(self, shots_json, shot_index):
        return current_shot(shots_json, shot_index)


class ShotQueueLoadImage:
    """Delegates decoding of one selected image to ComfyUI's built-in loader."""
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"path": ("STRING", {"default": "", "forceInput": True})}}

    RETURN_TYPES = ("IMAGE", "MASK")
    FUNCTION = "load"
    CATEGORY = "Shot Queue"

    def load(self, path):
        if not path.strip():
            return (None, None)
        from nodes import LoadImage
        validation = LoadImage.VALIDATE_INPUTS(path)
        if validation is not True:
            raise ValueError(str(validation))
        return LoadImage().load_image(path)

    @classmethod
    def IS_CHANGED(cls, path):
        from nodes import LoadImage
        return LoadImage.IS_CHANGED(path) if path.strip() else ""
