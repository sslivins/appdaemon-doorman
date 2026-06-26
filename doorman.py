import hassapi as hass
from openai import AsyncOpenAI
import os
import json
from recognize import ImageRecognizer
from snapshot import AsyncSnapshotTaker
from datetime import datetime, timezone
import time
import aiofiles
import asyncio
import aiohttp

#this app will put the office into 'theatre mode' when the tv is turned on

DEFAULT_MESSAGE = "Welcome Home!"

class Doorman(hass.Hass):

    def _require_arg(self, *keys):
        """Fetch a (possibly nested) required value from self.args.

        Required configuration must be present in doorman.yaml; if it is missing
        or empty we raise so the app fails loudly at startup instead of silently
        falling back to a bogus default that hides the misconfiguration.
        """
        value = self.args
        for key in keys:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)
        if value in (None, "", [], {}):
            raise ValueError(
                f"Doorman config error: required parameter '{'.'.join(keys)}' "
                "is missing or empty in doorman.yaml"
            )
        return value

    def initialize(self):

        # Logging (file, level, rotation) is configured natively in
        # appdaemon.yaml under `logs: doorman_log` and referenced by this app
        # via `log: doorman_log` in apps.yaml. Everything here uses self.log().

        # Initialize class variables for YAML parameters
        self.allowed_faces = self._require_arg("allowed_faces")
        # `entity` may be a single entity id or a list of entity ids. When a
        # list is given, doorman wakes on whichever fires first and keeps
        # recognizing while ANY of them reports the detected state (motion OR
        # person). detection_entity stays as the first one for any singular
        # references / fallbacks.
        _detection_entity = self._require_arg("detection_sensor", "entity")
        if isinstance(_detection_entity, (list, tuple)):
            self.detection_entities = list(_detection_entity)
        else:
            self.detection_entities = [_detection_entity]
        self.detection_entity = self.detection_entities[0]
        self.detected_state = self.args.get("detection_sensor", {}).get("detected_state", "on")
        self.not_detected_state = self.args.get("detection_sensor", {}).get("not_detected_state", "off")

        # Door open/closed sensor (face recognition only runs while the door is closed)
        self.door_sensor_entity = self._require_arg("door_sensor", "entity")
        self.door_closed_state = self.args.get("door_sensor", {}).get("closed_state", "off")
        # How often to re-check the door sensor while the door is not yet closed
        # (person present but door open). Tuning param -> safe default.
        self.door_poll_interval = self.args.get("door_sensor", {}).get("poll_interval", 0.2)

        # Electric strike lock that gets unlocked on an accepted face
        self.lock_entity = self._require_arg("lock", "entity")

        # Handle face_storage arguments 
        face_storage = self.args.get("face_storage", {})
        self.retention_days = face_storage.get("retention_days", 7)
        self.all_faces_directory = face_storage.get("all_faces_directory", None)
        self.accepted_faces_directory = face_storage.get("accepted_faces_directory", None)

        self.camera_host = self._require_arg("camera", "host")
        self.nvr_host = self._require_arg("g4_doorbell_pro", "nvr_host")
        self.nvr_api_key = self._require_arg("g4_doorbell_pro", "nvr_api_key")
        self.g4_camera_id = self._require_arg("g4_doorbell_pro", "camera_id")
        self.compreface_host = self._require_arg("compreface", "host")
        self.compreface_api_key = self._require_arg("compreface", "api_key")
        self.box_threshold = self.args.get("match_accuracy", {}).get("box", 0.95)
        self.box_size_ratio = self.args.get("match_accuracy", {}).get("box_ratio", 0.005)
        self.face_threshold = self.args.get("match_accuracy", {}).get("face", 0.99)

        self.snapshot_taker = AsyncSnapshotTaker(self.camera_host)
        self.recognizer = ImageRecognizer(self.compreface_host, self.compreface_api_key)

        api_key = self._require_arg("open_ai", "openai_key")

        # Initialize OpenAI client
        self.open_ai_client = AsyncOpenAI(api_key=api_key)
        
        self.electric_strike_event = asyncio.Event()
        self._recognition_active = False
    
        for _ent in self.detection_entities:
            self.listen_state(self.detected_person, _ent, new=self.detected_state)
        
        #listen for when the door transitions from unlocked to locked
        self.listen_state(self._door_locked_event, self.lock_entity, new="locked")
        self.listen_state(self._door_unlocked_event, self.lock_entity, new="unlocked")
        
        # run daily to clean up old images
        if self.all_faces_directory:
            self.run_daily(self.cleanup_old_images, "00:00:00", image_path=self.all_faces_directory, days_to_keep=self.retention_days)

        if self.accepted_faces_directory:
            self.run_daily(self.cleanup_old_images, "00:00:00", image_path=self.accepted_faces_directory, days_to_keep=self.retention_days)
            
        self.log(f"{self.__class__.__name__} initialized successfully.")

    def detected_person(self, entity=None, attribute=None, old=None, new=None, kwargs=None):
        # Schedule the async function using self.create_task()
        self.create_task(self.async_detected_person(entity, attribute, old, new, kwargs))

    async def _any_detected(self):
        # True while ANY configured detection sensor reports the detected state
        # (supports motion OR person triggering simultaneously).
        for ent in self.detection_entities:
            if await self.get_state(ent) == self.detected_state:
                return True
        return False

    async def async_detected_person(self, entity=None, attribute=None, old=None, new=None, kwargs=None):
        # Guard against concurrent recognition loops. When listening to multiple
        # sensors (motion OR person), both can flip at nearly the same instant
        # and spawn two overlapping loops; only allow one at a time.
        if getattr(self, "_recognition_active", False):
            self.log(f"recognition already active; ignoring extra trigger from {entity}.",
                     level="DEBUG")
            return
        self._recognition_active = True
        try:
            await self._run_recognition(entity, attribute, old, new, kwargs)
        finally:
            self._recognition_active = False

    async def _run_recognition(self, entity=None, attribute=None, old=None, new=None, kwargs=None):
        # Log the detection event
        
        # Initialize profiling data
        profiling_data = {
            "async_get_snapshot": {"total_time": 0, "count": 0},
            "async_save_image_to_file": {"total_time": 0, "count": 0},
            "async_get_faces": {"total_time": 0, "count": 0},
            "set_doorbell_message": {"total_time": 0, "count": 0},
            "overall_duration": {"total_time": 0, "count": 0}
        }

        loops = 0

        detection_start = time.time()
        accepted_ever = False
        best_sim = 0.0
        best_subject = None

        # One concise INFO marker per detection event; the per-frame detail
        # below is DEBUG so a normal visit is only a couple of INFO lines.
        # Include which sensor fired (useful when listening to motion OR person).
        trigger_name = (entity.split(".")[-1].replace("front_door_camera_", "")
                        if entity else "manual")
        self.log(f"Person detected ({trigger_name}) - starting face recognition.",
                 level="INFO")

        # (a) detection-sensor latency (DEBUG instrumentation): how long ago the
        #     triggering sensor actually flipped vs. when this handler started.
        try:
            latency_entity = entity or self.detection_entity
            last_changed = await self.get_state(latency_entity, attribute="last_changed")
            if last_changed:
                sensor_ts = datetime.fromisoformat(last_changed).timestamp()
                self.log(f"[timing] detection latency (sensor flip -> doorman start): "
                         f"{detection_start - sensor_ts:.3f}s", level="DEBUG")
        except Exception as e:
            self.log(f"could not compute detection latency: {e}", level="DEBUG")

        while await self._any_detected():
            #front door should be closed
            if await self.get_state(self.door_sensor_entity) == self.door_closed_state:
                overall_start_time = time.time()
                
                loops += 1
                self.log(f"attempt {loops}: taking snapshot from camera.", level="DEBUG")

                # Profile async_get_snapshot
                start_time = time.time()
                image_buffer, image_width, image_height = await self.async_get_snapshot()
                elapsed_time = time.time() - start_time
                snap_elapsed = elapsed_time
                profiling_data["async_get_snapshot"]["total_time"] += elapsed_time
                profiling_data["async_get_snapshot"]["count"] += 1

                if image_buffer is None:
                    self.log("Failed to take snapshot.", level="ERROR")

                # Profile async_get_faces
                start_time = time.time()
                faces = await self.async_get_faces(image_buffer, image_width, image_height)
                elapsed_time = time.time() - start_time
                recog_elapsed = elapsed_time
                profiling_data["async_get_faces"]["total_time"] += elapsed_time
                profiling_data["async_get_faces"]["count"] += 1

                # (d) live per-attempt timing + best allowed-face score this frame
                top = None
                if faces:
                    allowed = [f for f in faces if f.get("subject") in self.allowed_faces]
                    if allowed:
                        top = max(allowed, key=lambda f: f.get("similarity", 0))
                top_str = f"{top['subject']}:{top['similarity']:.4f}" if top else "none"
                self.log(f"[timing] attempt {loops}: snapshot={snap_elapsed:.3f}s "
                         f"recognize={recog_elapsed:.3f}s top={top_str} "
                         f"(elapsed since detect={time.time() - detection_start:.3f}s)", level="DEBUG")
                
                if faces is None:
                    self.log("No face found in this frame.", level="DEBUG")
                else:
                    accepted_faces = []
                    if self.allowed_faces:
                    # Check if any of the recognized faces match the allowed faces
                        for face in faces:
                            if face['subject'] in self.allowed_faces:
                                sim = face['similarity']
                                if sim > best_sim:
                                    best_sim = sim
                                    best_subject = face['subject']
                                self.log(f"Recognized allowed face: {face['subject']} ({sim:.4f})", level="DEBUG")
                                
                                current_time = datetime.now()
                                image_name = f"{current_time.strftime('%Y%m%d_%H%M%S')}_{face['subject']}_{sim:.2f}.jpg"
                                
                                # Profile async_save_image_to_file
                                if image_buffer and self.all_faces_directory:
                                    start_time = time.time()
                                    await self.async_save_image_to_file(image_buffer, self.all_faces_directory, image_name)
                                    elapsed_time = time.time() - start_time
                                    profiling_data["async_save_image_to_file"]["total_time"] += elapsed_time
                                    profiling_data["async_save_image_to_file"]["count"] += 1
                                    
                                #face must be above the face threshold to be considered 'accepted'
                                if sim >= self.face_threshold:
                                    #create list of all accepted faces
                                    accepted_faces.append(face["subject"])
                                    
                                    if image_buffer and self.accepted_faces_directory:
                                        start_time = time.time()
                                        await self.async_save_image_to_file(image_buffer, self.accepted_faces_directory, image_name)
                                        elapsed_time = time.time() - start_time
                                        profiling_data["async_save_image_to_file"]["total_time"] += elapsed_time
                                        profiling_data["async_save_image_to_file"]["count"] += 1
                                else:
                                    self.log(f"Face of {face['subject']} ({sim:.4f}) is below the threshold of {self.face_threshold}.", level="DEBUG")

                    if accepted_faces:
                        accept_elapsed = time.time() - detection_start
                        if not accepted_ever:
                            # The single INFO summary line for a successful visit.
                            self.log(f"Door opening for {accepted_faces} "
                                     f"(best {best_sim:.4f}, {loops} frame(s), {accept_elapsed:.2f}s).",
                                     level="INFO")
                        accepted_ever = True
                        self.log(f"[timing] time to accept: {accept_elapsed:.3f}s "
                                 f"over {loops} attempt(s)", level="DEBUG")
                        # Unlock the door if a recognized face is found
                        self._unlock_door()
                        
                        start_time = time.time()
                        await self.set_doorbell_message(accepted_faces)
                        elapsed_time = time.time() - start_time
                        profiling_data["set_doorbell_message"]["total_time"] += elapsed_time
                        profiling_data["set_doorbell_message"]["count"] += 1
                        
                        got_event = await self._wait_for_door_to_relock(timeout=5)
                        
                        if got_event:
                            self.log("Door relocked.", level="DEBUG")
                        else:
                            self.log("Door did not relock within the timeout period.", level="WARNING")
                
                overall_duration = time.time() - overall_start_time
                profiling_data["overall_duration"]["total_time"] += overall_duration
                profiling_data["overall_duration"]["count"] += 1
                
            else:
                await self.sleep(self.door_poll_interval)

        # Per-visit summary: if the person was present but never cleared the
        # threshold, log one INFO line (otherwise the "Door opening" line above
        # already summarised the visit).
        if loops > 0 and not accepted_ever:
            self.log(f"No allowed face accepted after {loops} frame(s) / "
                     f"{time.time() - detection_start:.1f}s "
                     f"(best {best_subject or 'none'} {best_sim:.4f}).", level="INFO")

        # Detailed per-function profiling (DEBUG only).
        if loops > 0:
            self.log("Profiling Summary:", level="DEBUG")
            for func, data in profiling_data.items():
                total_time = data["total_time"]
                count = data["count"]
                average_time = total_time / count if count > 0 else 0
                self.log(f"{func}: {total_time:.3f} seconds total, {count} calls, {average_time:.3f} seconds per call", level="DEBUG")
                
    async def async_get_snapshot(self):
            # Log the snapshot event
            self.log("Taking snapshot from camera.", level="DEBUG")
            
            # Take a snapshot using the camera
            snapshot_image, result = await self.snapshot_taker.take_snapshot_to_buffer()
            
            if not snapshot_image:
                self.log("Failed to take snapshot.", level="WARNING")
                return None
            
            snapshot_response = json.loads(result)
            if snapshot_response["http_code"] != 200:
                self.log(f"Error taking snapshot: {snapshot_response['http_response']}", level="ERROR")
                return None
            
            # Get the resolution of the snapshot
            snapshot_width = snapshot_response["resolution"]["width"]
            snapshot_height = snapshot_response["resolution"]["height"]
            
            return snapshot_image, snapshot_width, snapshot_height
    
    async def async_save_image_to_file(self, image_buffer, image_folder="saved_images", image_name=None):
        """
        Saves an image buffer to a file asynchronously.

        Args:
            image_buffer (bytes): The image data to be saved.
            image_folder (str): The folder where the image will be saved. If a single name is provided, 
                                it is assumed to be relative to the current file path. The folder will 
                                be created if it does not exist.
            image_name (str, optional): The name of the file to save the image as. If not provided, 
                                        the file will be named using the format 'snapshot_<timestamp>.jpg'.

        Returns:
            str: The full path of the saved image file.
        """
        # Get the directory of the current file
        script_dir = os.path.dirname(os.path.realpath(__file__))

        # Resolve the image folder path
        if not os.path.isabs(image_folder):
            image_folder = os.path.join(script_dir, image_folder)

        # Ensure the directory exists
        if not os.path.exists(image_folder):
            os.makedirs(image_folder)

        # Determine the image name
        if not image_name:
            current_time = datetime.now()
            image_name = f"snapshot_{current_time.strftime('%Y%m%d_%H%M%S')}.jpg"

        # Construct the full image path
        image_path = os.path.join(image_folder, image_name)

        # Save the image to a file asynchronously
        async with aiofiles.open(image_path, 'wb') as out_file:
            await out_file.write(image_buffer)

        return image_path
            
    def cleanup_old_images(self, kwargs=None, **other_kwargs): 

        if kwargs is None:
            kwargs = {}
            
        kwargs.update(other_kwargs)           
        
        # Retrieve parameters from kwargs
        image_path = kwargs.get("image_path")
        days_to_keep = kwargs.get("days_to_keep")
        
        self.log(f"Starting cleanup of old images in {image_path} older than {days_to_keep} days.", level="INFO")

        # Exit early if either kwarg is missing
        if image_path is None or days_to_keep is None:
            self.log("Missing required kwargs: image_path or days_to_keep. Exiting cleanup.", level="WARNING")
            return

        # Calculate the cutoff time
        cutoff_time = time.time() - (days_to_keep * 86400)  # 86400 seconds in a day

        # Get the directory of the current file
        script_dir = os.path.dirname(os.path.realpath(__file__))

        # Resolve the image_path relative to the script directory if not absolute
        if not os.path.isabs(image_path):
            image_path = os.path.join(script_dir, image_path)

        # Ensure the directory exists
        if not os.path.exists(image_path):
            self.log(f"Directory {image_path} does not exist. Skipping cleanup.", level="WARNING")
            return

        # Iterate through files in the directory
        for filename in os.listdir(image_path):
            file_path = os.path.join(image_path, filename)

            # Check if it's a file, has a .jpg extension, and not a directory
            if os.path.isfile(file_path) and filename.lower().endswith(".jpg"):
                file_mod_time = os.path.getmtime(file_path)

                # Remove files older than the cutoff time
                if file_mod_time < cutoff_time:
                    try:
                        os.remove(file_path)
                        self.log(f"Removed old image: {file_path}", level="DEBUG")
                    except Exception as e:
                        self.log(f"Error removing file {file_path}: {e}", level="ERROR")

    async def async_get_faces(self, image_buffer, image_width, image_height):
        
        # matched_subjects = {}
        # matched_subjects["matches"] = []
        # find faces in the image
        result = self.recognizer.recognize_image_from_buffer(image_buffer)
            
        self.log(f"Recognition Result: {result}", level="DEBUG")
        
        recognizer_response = None
        
        try:
            recognizer_response = json.loads(result)
            if recognizer_response["http_code"] != 200:
                resp = recognizer_response["http_response"]
                # code 28 == "No face is found in the given image": a normal,
                # expected outcome for many frames, so keep it at DEBUG. Any
                # other non-200 is a real problem -> WARNING.
                if isinstance(resp, dict) and resp.get("code") == 28:
                    self.log(f"recognizer: {resp.get('message', resp)}", level="DEBUG")
                else:
                    self.log(f"problem recognizing image: {resp}", level="WARNING")
                return None
        except json.JSONDecodeError as e:
            self.log(f"Error decoding recognition response: {e}", level="ERROR")
            return None
        except KeyError as e:
            self.log(f"Error parsing recognition response: {e}", level="ERROR")
            return None
        
        target_subjects = []
        
        #only keep boxes that have a high accuracy
        for box in recognizer_response["http_response"]["result"]:
            self.log(f"Box: {box}", level="DEBUG")
            if box["box"]["probability"] >= self.box_threshold:
                target_subjects.append(box)
            
        self.log(f"Target Subjects: {target_subjects}", level="DEBUG")
            
        #discard all boxes that are too small relative to the size of the image meaning they are far from the the camera
        ##get area of image
        image_area = image_width * image_height
        
        for box in target_subjects:
            ##get area of box
            box_area = (box["box"]["x_max"] - box["box"]["x_min"]) * (box["box"]["y_max"] - box["box"]["y_min"])
            
            if (box_area / image_area < self.box_size_ratio):
                self.log(f"Discarding - face too small: {box}", level="DEBUG")
                target_subjects.remove(box)
            
        #output a json with the name of all subjects as a json array
        faces = []
        for box in target_subjects:
            for subject in box["subjects"]:
                faces.append(subject)
            
        self.log(f"Faces: {json.dumps(faces)}", level="DEBUG")
        
        return faces
    
    def _unlock_door(self):
        # Implement the logic to unlock the door
        self._unlock_requested_at = time.time()
        self.log("Unlocking the door.", level="DEBUG")
        self.call_service("lock/unlock", entity_id=self.lock_entity)
        
    async def _wait_for_door_to_relock(self, timeout=5):
        # Wait for the door to be relocked
        self.log("Waiting for the door to relock.", level="DEBUG")
        try:
            await asyncio.wait_for(self.electric_strike_event.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False
        
    def _door_unlocked_event(self, entity=None, attribute=None, old=None, new=None, kwargs=None):
        # Log the event
        self.log("Front door unlocked.")

        # (c) strike actuation lag: unlock command -> HA reports physically unlocked
        requested_at = getattr(self, "_unlock_requested_at", None)
        if requested_at is not None:
            self.log(f"[timing] strike actuation lag (unlock command -> unlocked): "
                     f"{time.time() - requested_at:.3f}s", level="DEBUG")
            self._unlock_requested_at = None

        self.electric_strike_event.clear()
                
    def _door_locked_event(self, entity=None, attribute=None, old=None, new=None, kwargs=None):
        # Log the event
        self.log("Front door locked")
        
        self.electric_strike_event.set()

    async def get_chatgpt_response(self, prompt: str, model: str = "gpt-4.1") -> dict:
        # System prompt
        instructions = (
            "This GPT is a virtual doorman designed to greet residents and visitors of a household. "
            "When given a prompt containing the names of the people at the front door, it generates a "
            "short, unique greeting that is fun, quirky, or witty. It always responds in JSON format "
            "with a single key named 'message'. The value of 'message' is a greeting string that absolutely "
            "cannot exceed 25 characters in length and cannot include any special characters—only "
            "alphanumeric characters and spaces. The GPT should avoid repeating greetings and strive "
            "for variety and creativity in each response. It treats each name or group of names with a "
            "warm, playful tone that fits a friendly, imaginative household environment.\n\n"
            "The household includes:\n"
            "- Stefan (the dad)\n"
            "- Megan (the mom)\n"
            "- Mackenzie (a twin, also goes by Kenzie or 'Ken Ken')\n"
            "- Nadia (the other twin, affectionately called 'Nana Boo')\n"
            "- Natalie (the middle child, affectionately called 'Nat Nat'\n"
            "- Eric (also known as the 'Baby Boy'\n\n"
            "The greeting message should be directed to the individual or individuals approaching the "
            "door, similar to how a real doorman might address someone personally. It should sound "
            "welcoming and personal, like 'Welcome home' or 'Come on in'. The door is assumed to be "
            "unlocked for them automatically after the greeting."
        )

        response = await self.open_ai_client.responses.create(
            model=model,
            instructions=instructions,
            input=prompt,
        )

        content = response.output_text

        try:
            # Check if the response is valid JSON
            json_response = json.loads(content)
            message = json_response.get("message", DEFAULT_MESSAGE)
            
            #message cannot exceed 30 characters
            if len(message) > 30:
                self.log(f"Message exceeds 30 characters: {message}, using default", level="WARNING")
                return DEFAULT_MESSAGE
            
        except json.JSONDecodeError:
            # If not, log the error and return a default message
            self.log(f"Invalid JSON response: {content}", level="WARNING")
            return DEFAULT_MESSAGE
        
        return message
        
    async def set_doorbell_message(self, names=None):
        start_time = time.time()
        message = await self.get_chatgpt_response(", ".join(names), "gpt-3.5-turbo")
        duration = time.time() - start_time
        self.log(f"get_chatgpt_response duration: {duration:.4f} seconds", level="DEBUG")

        self.log(f"Setting Doorbell message to: {message}")

        #get current lcd message
        current_lcd_message = await self.get_g4_doorbell_lcdMessage(self.g4_camera_id)

        #set message
        await self.set_g4_doorbell_message(self.g4_camera_id, message)

        await self.sleep(5)

        #restore the original message
        await self.set_g4_doorbell_lcdMessage(self.g4_camera_id, current_lcd_message)
       

    async def set_g4_doorbell_message(self, camera_id: str, message: str, expires_in: int = None):
        """
        Sets a custom message on the G4 Doorbell using an async HTTP PATCH request.

        Args:
            message (str): The custom message to set on the doorbell.
        """
        # Define the API endpoint and headers
        url = f"https://{self.nvr_host}/proxy/protect/integration/v1/cameras/{camera_id}"
        headers = {
            "Content-Type": "application/json",
            "X-API-KEY": self.nvr_api_key
        }

        # Define the payload
        payload = {
            "lcdMessage": {
            "type": "CUSTOM_MESSAGE",
            "resetAt": None if expires_in is None else int(time.time() * 1000) + (expires_in * 1000),
            "text": message
            }
        }

        # Make the async HTTP PATCH request
        async with aiohttp.ClientSession() as session:
            try:
                async with session.patch(url, headers=headers, json=payload, ssl=False) as response:
                    if response.status == 200:
                        self.log(f"Successfully set G4 Doorbell message: {message}", level="DEBUG")
                    else:
                        self.log(f"Failed to set G4 Doorbell message. Status: {response.status}, Response: {await response.text()}", level="ERROR")
            except Exception as e:
                self.log(f"Error setting G4 Doorbell message: {e}", level="ERROR")

    async def set_g4_doorbell_image(self, camera_id: str, image_guid: str, expires_in: int = None):
        """
        Sets an image on the G4 Doorbell using an async HTTP PATCH request.

        Args:
            image_guid (str): The GUID of the image to display on the doorbell.
            expires_in (int, optional): The number of seconds before the image expires. Defaults to None (no expiration).
        """
        # Define the API endpoint and headers
        url = f"https://{self.nvr_host}/proxy/protect/integration/v1/cameras/{camera_id}"
        headers = {
            "Content-Type": "application/json",
            "X-API-KEY": self.nvr_api_key
        }

        # Define the payload
        payload = {
            "lcdMessage": {
                "type": "IMAGE",
                "resetAt": None if expires_in is None else int(time.time() * 1000) + (expires_in * 1000),
                "text": f"{image_guid}.png"
            }
        }

        # Make the async HTTP PATCH request
        async with aiohttp.ClientSession() as session:
            try:
                async with session.patch(url, headers=headers, json=payload, ssl=False) as response:
                    if response.status == 200:
                        self.log(f"Successfully set G4 Doorbell image: {image_guid}", level="DEBUG")
                    else:
                        self.log(f"Failed to set G4 Doorbell image. Status: {response.status}, Response: {await response.text()}", level="ERROR")
            except Exception as e:
                self.log(f"Error setting G4 Doorbell image: {e}", level="ERROR")
        
    async def set_g4_doorbell_lcdMessage(self, camera_id, payload: dict):
        """
        Sends a custom payload to set the LCD message on the G4 Doorbell.

        Args:
            payload (dict): The payload to send to the G4 Doorbell.
        """
        # Wrap the payload under the "lcdMessage" key
        full_payload = {"lcdMessage": payload}

        # Define the API endpoint and headers
        url = f"https://{self.nvr_host}/proxy/protect/integration/v1/cameras/{camera_id}"
        headers = {
            "Content-Type": "application/json",
            "X-API-KEY": self.nvr_api_key
        }

        # Make the async HTTP PATCH request
        async with aiohttp.ClientSession() as session:
            try:
                async with session.patch(url, headers=headers, json=full_payload, ssl=False) as response:
                    if response.status == 200:
                        self.log(f"Successfully set G4 Doorbell LCD message with payload: {full_payload}", level="DEBUG")
                    else:
                        self.log(f"Failed to set G4 Doorbell LCD message. Status: {response.status}, Response: {await response.text()}", level="ERROR")
            except Exception as e:
                self.log(f"Error setting G4 Doorbell LCD message: {e}", level="ERROR")

    async def get_g4_doorbell_lcdMessage(self, camera_id: str):
        """
        Retrieves the current LCD message configuration from the G4 Doorbell.

        Returns:
            dict: The current LCD message configuration, or None if the request fails.
        """
        # Define the API endpoint and headers
        url = f"https://{self.nvr_host}/proxy/protect/integration/v1/cameras/{camera_id}"

        headers = {
            "Content-Type": "application/json",
            "X-API-KEY": self.nvr_api_key
        }        

        # Make the async HTTP GET request
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=headers, ssl=False) as response:
                    if response.status == 200:
                        data = await response.json()
                        lcd_message = data.get("lcdMessage", {})
                        self.log(f"Successfully retrieved G4 Doorbell LCD message: {lcd_message}", level="DEBUG")
                        return lcd_message
                    else:
                        self.log(f"Failed to retrieve G4 Doorbell LCD message. Status: {response.status}, Response: {await response.text()}", level="ERROR")
                        return None
            except Exception as e:
                self.log(f"Error retrieving G4 Doorbell LCD message: {e}", level="ERROR")
                return None                