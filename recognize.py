import requests
import json
import os

class ImageRecognizer:
    def __init__(self, recognizer_host, api_key):
        self.host = recognizer_host
        self.api_key = api_key

    def json_error(self, message, code):
        return json.dumps({"error": message, "http_code": code})

    def check_file_exists(self, file_path):
        if not os.path.isfile(file_path):
            return False
        return True

    def _recognize_image(self, files):
        url = f"http://{self.host}/api/v1/recognition/recognize"
        headers = {
            "x-api-key": self.api_key
        }

        try:
            # Make the REST call and capture the response
            response = requests.post(url, headers=headers, files=files)
            http_code = response.status_code
            http_response = response.json()

            # Construct the nested JSON object
            nested_response = {
                "http_response": http_response,
                "http_code": http_code
            }

            return json.dumps(nested_response)

        except requests.exceptions.RequestException as e:
            return self.json_error(str(e), 500)

    def recognize_image_from_file(self, jpeg_file):
        # Check if the JPEG file exists
        if not self.check_file_exists(jpeg_file):
            return self.json_error(f"File '{jpeg_file}' does not exist.", 404)

        files = {
            "file": open(jpeg_file, "rb")
        }

        return self._recognize_image(files)

    def recognize_image_from_buffer(self, buffer):
        files = {
            "file": ('image.jpg', buffer, 'image/jpeg')
        }

        return self._recognize_image(files)

# This part allows the script to be run independently for testing purposes
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Recognize an image using a REST API.")
    parser.add_argument("ip", help="The IP address of the server.")
    parser.add_argument("port", help="The port of the server.")
    parser.add_argument("api_key", help="The API key for authentication.")
    parser.add_argument("jpeg_file", help="The path to the JPEG file.")
    args = parser.parse_args()

    recognizer = ImageRecognizer(args.ip, args.port, args.api_key)
    result = recognizer.recognize_image_from_file(args.jpeg_file)
    print(result)
