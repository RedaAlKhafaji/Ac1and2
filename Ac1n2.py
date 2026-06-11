import os
import sys
import json
import time
import logging
import threading
import requests
from http.server import BaseHTTPRequestHandler, HTTPServer

# Configure logging to output directly to standard output for clear Render logs
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

# ==================== CONFIGURATION ====================
ACCESS_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJFUzI1NiJ9.eyJzc29JZCI6IjIxMjQ1ODI0NyIsImFwcElkIjoid3g2ZTFhZjNmYTg0ZmJlNTIzIiwibWFjIjoiZGVmYXVsdCIsImV4cGlyZWREYXRlIjoiMTc4MTE3Mzc5OCJ9.8DzjfzDlH2TmIs5U4-0ucKYcu9eIWKzz27Hiujp-3O6aXUz6-QA8wWEl7OHFIpQ0KccAyxWhm4G4PP2xbyjUtg"
APP_ID = "wx6e1af3fa84fbe523"

AC1_DEVICE_ID = "C-0JABFAAAI"
AC2_DEVICE_ID = "DfaxahFAAAE"

AC1_CMD_URL = f"https://data.iot.eu-central-1.amazonaws.com/topics/$aws/things/{AC1_DEVICE_ID}/shadow/update?qos=1"
# Ensure this matches the exact polling endpoint used in your original ac.py script
AC2_STATUS_URL = f"https://eu-api-prod.aws.tcljd.com/v1/thing/error/{AC2_DEVICE_ID}" 

HEADERS = {
    "user-agent": "Dart/3.4 (dart:io)",
    "appid": APP_ID,
    "accept": "application/json; charset=utf-8",
    "accesstoken": ACCESS_TOKEN,
    "accept-language": "en",
    "content-type": "application/json"
}
# =======================================================


# ==================== RENDER HACK SERVER ====================
class RenderHealthCheckServer(BaseHTTPRequestHandler):
    """Answers Render's port ping requests to keep the free Web Service alive."""
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"TCL AC Automation Web Service is active.")
    
    def log_message(self, format, *args):
        # Overridden to prevent UptimeRobot pings from spamming your logs every 10 minutes
        return

def run_health_check_server():
    """Runs the dummy web server on the port assigned by Render."""
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), RenderHealthCheckServer)
    logging.info(f"Render health-check web server started on port {port}")
    server.serve_forever()
# ============================================================


# ==================== AUTOMATION LOGIC ====================
def check_ac2_is_on_gen():
    """Polls AC 2 status to check if it's currently running on generator power."""
    try:
        response = requests.get(AC2_STATUS_URL, headers=HEADERS, timeout=10)
        response.raise_for_status()
        
        # Insert your original logic here to parse the response payload.
        # Example assumes tracking generatorMode configuration state:
        data = response.json()
        return data.get("state", {}).get("reported", {}).get("generatorMode") == 2
        
    except Exception as e:
        logging.error(f"Error checking AC 2 status: {e}")
        return None

def set_ac1_state(enable_gen_lvl_2=True):
    """Sends the exact AWS IoT shadow update payload to AC 1."""
    client_token = f"mobile_{int(time.time() * 1000)}"
    
    if enable_gen_lvl_2:
        payload = {"state": {"desired": {"generatorMode": 2}}, "clientToken": client_token}
    else:
        payload = {"state": {"desired": {"generatorMode": 0}}, "clientToken": client_token}
    
    try:
        response = requests.post(AC1_CMD_URL, headers=HEADERS, json=payload, timeout=10)
        response.raise_for_status()
        
        state_text = "Manual Gen Mode (Level 2)" if enable_gen_lvl_2 else "National Grid Mode (0)"
        logging.info(f"Success! AC 1 commanded to: {state_text}")
        return True 
        
    except requests.exceptions.RequestException as e:
        logging.error(f"Network Error sending command to AC 1: {e}")
        return False 
    except Exception as e:
        logging.error(f"Unexpected error commanding AC 1: {e}")
        return False

def main():
    logging.info("Starting TCL AC Automation Script...")
    
    # Launch the dummy web server in a separate background thread
    logging.info("Initializing Render free-tier environment compatibility...")
    threading.Thread(target=run_health_check_server, daemon=True).start()
    
    logging.info(f"Monitoring AC 2 ({AC2_DEVICE_ID}) for power source changes...")
    last_known_state = None

    while True:
        is_ac2_on_gen = check_ac2_is_on_gen()
        
        if is_ac2_on_gen is not None and is_ac2_on_gen != last_known_state:
            if last_known_state is not None:
                logging.info("-" * 40)
                logging.info("POWER STATE CHANGE DETECTED!")
                logging.info("-" * 40)
            
            command_success = False 
            
            if is_ac2_on_gen:
                logging.info(">>> AC 2 entered Auto Gen Mode. Switching AC 1 to Gen Mode Level 2.")
                command_success = set_ac1_state(enable_gen_lvl_2=True)
            else:
                logging.info(">>> AC 2 exited Auto Gen Mode (National Grid On). Reverting AC 1 to Normal.")
                command_success = set_ac1_state(enable_gen_lvl_2=False)
                
            # Only advance state tracking if the cloud confirmed receipt of the command
            if command_success:
                last_known_state = is_ac2_on_gen
            else:
                logging.warning("Command delivery failed. Network unstable. Retrying in 30 seconds.")
            
        time.sleep(30)

if __name__ == "__main__":
    main()
