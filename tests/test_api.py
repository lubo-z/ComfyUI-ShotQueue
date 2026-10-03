import asyncio
import importlib.util
import mimetypes
import os
import pathlib
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


folder_paths = types.ModuleType("folder_paths")
folder_paths.get_input_directory = lambda: ""
folder_paths.filter_files_content_types = lambda files, kinds: [name for name in files if (mimetypes.guess_type(name)[0] or "").split("/")[0] in kinds]
folder_paths.is_within_directory = lambda root, path: os.path.commonpath((os.path.realpath(root), os.path.realpath(path))) == os.path.realpath(root)
aiohttp = types.ModuleType("aiohttp")
aiohttp.web = types.SimpleNamespace(json_response=lambda data: data)
server = types.ModuleType("server")
server.PromptServer = types.SimpleNamespace(instance=types.SimpleNamespace(routes=types.SimpleNamespace(get=lambda path: lambda fn: fn)))

spec = importlib.util.spec_from_file_location("shot_queue_api", pathlib.Path(__file__).parents[1] / "api.py")
with patch.dict(sys.modules, {"folder_paths": folder_paths, "aiohttp": aiohttp, "server": server}):
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)


class ImageListTests(unittest.TestCase):
    def test_lists_input_images_without_escaping_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp) / "input"
            queue = root / "shot_queue"
            queue.mkdir(parents=True)
            (root / "first.png").touch()
            (queue / "second.jpg").touch()
            (queue / "notes.txt").touch()
            outside = pathlib.Path(temp) / "outside.png"
            outside.touch()
            (queue / "outside.png").symlink_to(outside)
            with patch.object(folder_paths, "get_input_directory", return_value=str(root)):
                self.assertEqual(api.list_input_images(), ["first.png", "shot_queue/second.jpg"])
                self.assertEqual(asyncio.run(api.input_images(None)), {"files": ["first.png", "shot_queue/second.jpg"]})


if __name__ == "__main__":
    unittest.main()
