from .nodes import ShotQueue, ShotQueueLoadImage
from . import api

NODE_CLASS_MAPPINGS = {"ShotQueue": ShotQueue, "ShotQueueLoadImage": ShotQueueLoadImage}
NODE_DISPLAY_NAME_MAPPINGS = {"ShotQueue": "Shot Queue", "ShotQueueLoadImage": "Shot Queue • Load Current Image"}
WEB_DIRECTORY = "./web"
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
