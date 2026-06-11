import requests
import time
import logging
import json

# Configure logging for clear visual feedback in the Termux terminal
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ==========================================
# 1. AUTHENTICATION & IDs (Injected from Report)
# ==========================================
AUTH_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJFUzI1NiJ9.eyJzc29JZCI6IjIxMjQ1ODI0NyIsImFwcElkIjoid3g2ZTFhZjNmYTg0ZmJlNTIzIiwibWFjIjoiZGVmYXVsdCIsImV4cGlyZWREYXRlIjoiMTc4MTE3Mzc5OCJ9.8DzjfzDlH2TmIs5U4-0ucKYcu9eIWKzz27Hiujp-3O6aXUz6-QA8wWEl7OHFIpQ0KccAyxWhm4G4PP2xbyjUtg"
AC1_DEVICE_ID = "C-0JABFAAAI"
AC2_DEVICE_ID = "DfaxahFAAAE"

# API Endpoints
# Status checks typically go through the regional collector
STATUS_URL = "https://collector.ap-southeast-1.bd.tcljd.com/api/device/status"
# Commands are pushed directly to the AWS IoT shadow update topic
AC1_CMD_URL = f"https://data.iot.eu-central-1.amazonaws.com/topics/$aws/things/{AC1_DEVICE_ID}/shadow/update?qos=1"

# Using 'accesstoken' exactly as specified in the extraction report
HEADERS = {
    "accesstoken": AUTH_TOKEN,
    "Content-Type": "application/json",
    "User-Agent": "TCL-Home-App-Automation"
}

# ==========================================
# 2. DEVICE CONTROL LOGIC
# ==========================================
def check_ac2_is_on_gen():
    """Fetches AC 2 status and returns True if Auto Gen Mode is active."""
    payload = {"deviceId": AC2_DEVICE_ID}

    try:
        response = requests.post(STATUS_URL, headers=HEADERS, json=payload, timeout=10)
        response.raise_for_status()
        data = response.json()

        # IoT JSON payloads often hide variables in unpredictable nested dictionaries
        # (e.g., data.properties.state vs data.state.reported).
        # Converting the JSON to a string without spaces is a bulletproof way to find the state.
        data_str = json.dumps(data).replace(" ", "")

        # Checking for the exact shadow states from your report
        if '"autoGeneratorMode":1' in data_str or '"generatorMode":6' in data_str:
            return True
        return False

    except requests.exceptions.RequestException as e:
        logging.error(f"Network Error reaching TCL server for AC 2: {e}")
        return None
    except Exception as e:
        logging.error(f"Failed to parse AC 2 status: {e}")
        return None

def set_ac1_state(enable_gen_lvl_2=True):
    """Sends the exact AWS IoT shadow update payload to AC 1."""
    # Mimics the "mobile_1781171975407" format from the report
    client_token = f"mobile_{int(time.time() * 1000)}"

    if enable_gen_lvl_2:
        payload = {
            "state": {
                "desired": {
                    "generatorMode": 2
                }
            },
            "clientToken": client_token
        }
    else:
        payload = {
            "state": {
                "desired": {
                    "generatorMode": 0
                }
            },
            "clientToken": client_token
        }

    try:
        response = requests.post(AC1_CMD_URL, headers=HEADERS, json=payload, timeout=10)
        response.raise_for_status()

        state_text = "Manual Gen Mode (Level 2)" if enable_gen_lvl_2 else "National Grid Mode (0)"
        logging.info(f"Success! AC 1 commanded to: {state_text}")

    except requests.exceptions.RequestException as e:
        logging.error(f"Network Error sending command to AC 1: {e}")
    except Exception as e:
        logging.error(f"Unexpected error commanding AC 1: {e}")

# ==========================================
# 3. MAIN EXECUTION LOOP
# ==========================================
def main():
    logging.info("Starting TCL AC Automation Script...")
    logging.info(f"Monitoring AC 2 ({AC2_DEVICE_ID}) for power source changes...")

    last_known_state = None

    while True:
        is_ac2_on_gen = check_ac2_is_on_gen()

        # Only fire a command if the API call succeeded AND the state actually changed
        if is_ac2_on_gen is not None and is_ac2_on_gen != last_known_state:
            if last_known_state is not None:
                logging.info("-" * 40)
                logging.info("POWER STATE CHANGE DETECTED!")
                logging.info("-" * 40)

            if is_ac2_on_gen:
                logging.info(">>> AC 2 entered Auto Gen Mode. Switching AC 1 to Gen Mode Level 2.")
                set_ac1_state(enable_gen_lvl_2=True)
            else:
                logging.info(">>> AC 2 exited Auto Gen Mode (National Grid On). Reverting AC 1 to Normal.")
                set_ac1_state(enable_gen_lvl_2=False)

            last_known_state = is_ac2_on_gen

        # 30-second polling interval ensures quick switching without triggering rate limits
        time.sleep(30)

if __name__ == "__main__":
    main()
