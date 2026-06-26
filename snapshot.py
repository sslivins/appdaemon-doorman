import requests
import json
import os
from PIL import Image
from io import BytesIO
import urllib3
import aiohttp
import asyncio

class SnapshotTaker:
    def __init__(self, camera_host):
        self.camera_host = camera_host

    def json_error(self, message, code):
        return json.dumps({"error": message, "http_code": code})

    def get_image_resolution(self, file_path):
        try:
            with Image.open(file_path) as img:
                return img.width, img.height
        except Exception as e:
            return None, str(e)

    def get_image_resolution_from_buffer(self, buffer):
        try:
            with Image.open(BytesIO(buffer)) as img:
                return img.width, img.height
        except Exception as e:
            return None, str(e)

    def capture_snapshot(self):
        url = f"https://{self.camera_host}/snap.jpeg"
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        try:
            response = requests.get(url, stream=True, verify=False)
            response.raise_for_status()  # Raise an HTTPError for bad responses
            return response.content, response.status_code, None

        except requests.exceptions.RequestException as e:
            return None, None, str(e)

    def take_snapshot_to_file(self, file_path):
        buffer, http_code, error = self.capture_snapshot()
        if error:
            return self.json_error(error, 500)

        # Save the image to the specified file path
        try:
            with open(file_path, 'wb') as out_file:
                out_file.write(buffer)
        except Exception as e:
            return self.json_error(str(e), 500)

        # Check if the JPEG file exists
        if not os.path.exists(file_path):
            return self.json_error(f"File '{file_path}' does not exist.", 404)

        # Get the resolution of the JPEG file
        width, height = self.get_image_resolution(file_path)
        if width is None:
            return self.json_error(height, 500)  # 'height' contains the error message in this case

        # Get the file name from the file path
        file_name = os.path.basename(file_path)

        # Construct the JSON object with image name, resolution, and HTTP status code
        json_response = json.dumps({
            "file_name": file_name,
            "resolution": {
                "width": width,
                "height": height
            },
            "http_code": http_code
        })

        return json_response

    def take_snapshot_to_buffer(self):
        buffer, http_code, error = self.capture_snapshot()
        if error:
            return None, self.json_error(error, 500)

        # Get the resolution of the JPEG buffer
        width, height = self.get_image_resolution_from_buffer(buffer)
        if width is None:
            return None, self.json_error(height, 500)  # 'height' contains the error message in this case

        # Construct the JSON object with resolution and HTTP status code
        json_response = json.dumps({
            "resolution": {
                "width": width,
                "height": height
            },
            "http_code": http_code
        })

        return buffer, json_response

class AsyncSnapshotTaker:
    def __init__(self, camera_host):
        self.camera_host = camera_host

    def json_error(self, message, code):
        return json.dumps({"error": message, "http_code": code})

    async def get_image_resolution(self, file_path):
        try:
            with Image.open(file_path) as img:
                return img.width, img.height
        except Exception as e:
            return None, str(e)

    async def get_image_resolution_from_buffer(self, buffer):
        try:
            with Image.open(BytesIO(buffer)) as img:
                return img.width, img.height
        except Exception as e:
            return None, str(e)

    async def capture_snapshot(self):
        url = f"https://{self.camera_host}/snap.jpeg"
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, ssl=False) as response:
                    response.raise_for_status()  # Raise an exception for HTTP errors
                    buffer = await response.read()
                    return buffer, response.status, None
        except aiohttp.ClientError as e:
            return None, None, str(e)

    async def take_snapshot_to_file(self, file_path):
        buffer, http_code, error = await self.capture_snapshot()
        if error:
            return self.json_error(error, 500)

        # Save the image to the specified file path
        try:
            with open(file_path, 'wb') as out_file:
                out_file.write(buffer)
        except Exception as e:
            return self.json_error(str(e), 500)

        # Check if the JPEG file exists
        if not os.path.exists(file_path):
            return self.json_error(f"File '{file_path}' does not exist.", 404)

        # Get the resolution of the JPEG file
        width, height = await self.get_image_resolution(file_path)
        if width is None:
            return self.json_error(height, 500)  # 'height' contains the error message in this case

        # Get the file name from the file path
        file_name = os.path.basename(file_path)

        # Construct the JSON object with image name, resolution, and HTTP status code
        json_response = json.dumps({
            "file_name": file_name,
            "resolution": {
                "width": width,
                "height": height
            },
            "http_code": http_code
        })

        return json_response

    async def take_snapshot_to_buffer(self):
        buffer, http_code, error = await self.capture_snapshot()
        if error:
            return None, self.json_error(error, 500)

        # Get the resolution of the JPEG buffer
        width, height = await self.get_image_resolution_from_buffer(buffer)
        if width is None:
            return None, self.json_error(height, 500)  # 'height' contains the error message in this case

        # Construct the JSON object with resolution and HTTP status code
        json_response = json.dumps({
            "resolution": {
                "width": width,
                "height": height
            },
            "http_code": http_code
        })

        return buffer, json_response
