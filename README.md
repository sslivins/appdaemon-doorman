# appdaemon-doorman

An [AppDaemon](https://appdaemon.readthedocs.io/) app for Home Assistant that
turns a video doorbell into a **face-recognition door unlock**. When motion or a
person is detected at the door, Doorman grabs a snapshot, runs it through a
[CompreFace](https://github.com/exadel-inc/CompreFace) recognition service, and
— if it recognizes an allowed face — unlocks the door (e.g. an electric strike)
and optionally announces a greeting.

> This is a personal project shared as-is. You will need to adapt the entity IDs
> and hosts in `doorman.yaml` to your own setup.

## How it works

1. **Trigger** — wakes on a motion **or** person-detected binary sensor (it keeps
   recognizing while *either* is active, so it works whether you're walking up or
   standing still).
2. **Capture** — pulls a full-resolution snapshot from the camera / UniFi Protect NVR.
3. **Recognize** — sends the snapshot to CompreFace and checks the result against
   configurable accuracy thresholds (bounding-box size, box ratio, and face score).
4. **Unlock** — if a face in `allowed_faces` clears the thresholds, it unlocks the
   `lock` entity, then watches the door sensor and re-locks once the door closes.
5. **Greet** *(optional)* — uses OpenAI to generate a short spoken greeting.

Snapshots are archived to `all_faces/` (everything seen) and `accepted_faces/`
(matches), pruned by `retention_days`. **Both directories are gitignored** — they
contain private images and never leave your machine.

## Prerequisites

- Home Assistant with AppDaemon 4 (the [AppDaemon add-on](https://github.com/hassio-addons/addon-appdaemon) or a standalone container).
- A [CompreFace](https://github.com/exadel-inc/CompreFace) instance with a
  **Recognition** service, and faces enrolled for each person you want to allow.
- A camera that exposes motion / person binary sensors and a snapshot source
  (this app is wired for a UniFi Protect doorbell via the NVR API, but you can
  adapt `snapshot.py`).
- A lock entity (e.g. an electric strike via a relay) and a door-position sensor.

## Installation

1. Copy this folder into your AppDaemon `apps/` directory (e.g. `apps/doorman/`).
2. Install the Python dependencies into the AppDaemon environment:
   ```
   pip install -r requirements.txt
   ```
3. Create your secrets file:
   ```
   cp secrets.yaml.example secrets.yaml
   ```
   then fill in `nvr_api_key`, `compreface_api_key`, and `openai_key`.
4. Edit `doorman.yaml` to match your own entity IDs and hosts (see below).
5. Add the logger and HASS plugin config to your **`appdaemon.yaml`** (see [AppDaemon setup](#appdaemon-setup)).

AppDaemon auto-discovers `doorman.yaml` and (re)loads `doorman.py` on save.

## Configuration (`doorman.yaml`)

| Key | What it is |
| --- | --- |
| `allowed_faces` | List of CompreFace subject names permitted to unlock. |
| `face_storage.retention_days` | How long to keep archived snapshots. |
| `detection_sensor.entity` | One or more binary sensors that trigger recognition (motion and/or person). |
| `door_sensor.entity` | Door-position sensor used to detect when the door has closed so it can re-lock. |
| `lock.entity` | The `lock.*` entity to unlock/lock. |
| `camera.host` / `g4_doorbell_pro.*` | Snapshot source. `nvr_api_key` is pulled from `secrets.yaml`. |
| `compreface.host` / `api_key` | Your CompreFace Recognition service. `api_key` from `secrets.yaml`. |
| `match_accuracy.{box,box_ratio,face}` | Recognition thresholds — raise to reduce false positives. |
| `open_ai.openai_key` | Optional; for the spoken greeting. From `secrets.yaml`. |

## AppDaemon setup

Doorman logs to a dedicated logger named `doorman_log`. Add this to the `logs:`
section of your **`appdaemon.yaml`** (paths assume `apps/doorman/`):

```yaml
logs:
  doorman_log:
    name: Doorman
    filename: /conf/apps/doorman/doorman.log
    log_size: 1048576       # 1 MB per file
    log_generations: 10
    format: "{asctime}.{msecs:03.0f} {levelname:<7} {message}"
    date_format: "%Y-%m-%d %H:%M:%S"
```

You also need the standard HASS plugin configured in `appdaemon.yaml` so the app
can talk to Home Assistant (your `ha_url` + a long-lived access `token`). Keep the
token out of source control — use `!secret` or a private config.

## Files

| File | Purpose |
| --- | --- |
| `doorman.py` | Main app logic (trigger, recognize, unlock, greet). |
| `recognize.py` | CompreFace recognition helper. |
| `snapshot.py` | Snapshot capture from the camera / NVR. |
| `doorman.yaml` | App configuration (edit for your setup). |
| `requirements.txt` | Python dependencies. |
| `secrets.yaml.example` | Template for your secrets (copy to `secrets.yaml`). |

## License

MIT
