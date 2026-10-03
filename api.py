import asyncio
import os

from aiohttp import web
import folder_paths
from server import PromptServer


def list_input_images():
    input_dir = folder_paths.get_input_directory()
    images = []
    for root, dirs, files in os.walk(input_dir):
        dirs[:] = [name for name in dirs if folder_paths.is_within_directory(input_dir, os.path.join(root, name))]
        for name in folder_paths.filter_files_content_types(files, ["image"]):
            path = os.path.join(root, name)
            if os.path.isfile(path) and folder_paths.is_within_directory(input_dir, path):
                images.append(os.path.relpath(path, input_dir).replace(os.sep, "/"))
    return sorted(images, key=str.casefold)


@PromptServer.instance.routes.get("/shot_queue/images")
async def input_images(request):
    return web.json_response({"files": await asyncio.to_thread(list_input_images)})
