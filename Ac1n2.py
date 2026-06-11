import os
import sys
import json
import time
import logging
import threading
import requests
import urllib3
from http.server import BaseHTTPRequestHandler, HTTPServer

# Suppress SSL warnings in the Render logs so they remain clean
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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

AC1_CMD_URL = f"https://eu-api-prod.aws.tcljd.com/v1/thing/control/{AC1_DEVICE_ID}"
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
        # Overridden to prevent UptimeRobot pings from spamming your logs
        return

def run_health_check_server():
    """Runs the dummy web server on the port assigned by Render."""
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), RenderHealthCheckServer)
    logging.info(f"Render health-check web server started on port {port}")
    server.serve_forever()
# ============================================================


# ==================== AUTOMATION LOGIC ====================
def get_ac1_target_mode():
    """Polls AC 2 status and determines what generatorMode AC 1 should be set to."""
    try:
        response = requests.get(AC2_STATUS_URL, headers=HEADERS, timeout=10, verify=False)
        response.raise_for_status()
        
        data = response.json()
        data_str = json.dumps(data).replace(" ", "")
        
        # If AC 2 is set to mode 6, AC 1 must be forced to 0
        if '"generatorMode":6' in data_str:
            return 0
            
        # If AC 2 is in auto mode 1, AC 1 should go to 2
        elif '"autoGeneratorMode":1' in data_str:
            return 2
            
        # Default to 0 for normal operation / National Grid
        return 0
        
    except Exception as e:
        logging.error(f"Error checking AC 2 status: {e}")
        return None

def set_ac1_state(target_mode):
    """Sends the command payload to AC 1 via the TCL Cloud API."""
    
    # Formatted exactly to the AWS IoT Device Shadow schema required by the physical unit
    payload = {
        "state": {
            "desired": {
                "generatorMode": target_mode
            }
        }
    }
    
    try:
        response = requests.post(AC1_CMD_URL, headers=HEADERS, json=payload, timeout=10, verify=False)
        response.raise_for_status()
        
        logging.info(f"Success! AC 1 commanded to: Mode {target_mode}")
        return True 
        
    except requests.exceptions.RequestException as e:
        logging.error(f"Network Error sending command to AC 1: {e}")
        return False 
    except Exception as e:
        logging.error(f"Unexpected error commanding AC 1: {e}")
        return False

def main():
    logging.info("Starting TCL AC Automation Script...")
    
    logging.info("Initializing Render free-tier environment compatibility...")
    threading.Thread(target=run_health_check_server, daemon=True).start()
    
    logging.info(f"Monitoring AC 2 ({AC2_DEVICE_ID}) for power source changes...")
    last_known_state = None

    while True:
        target_mode = get_ac1_target_mode()
        
        if target_mode is not None and target_mode != last_known_state:
            if last_known_state is not None:
                logging.info("-" * 40)
                logging.info("POWER STATE CHANGE DETECTED!")
                logging.info("-" * 40)
            
            if target_mode == 2:
                logging.info(">>> AC 2 entered Auto Gen Mode. Switching AC 1 to Mode 2.")
            elif target_mode == 0:
                logging.info(">>> AC 2 entered Mode 6 or Grid Mode. Reverting AC 1 to Mode 0.")
                
            command_success = set_ac1_state(target_mode)
                
            if command_success:
                last_known_state = target_mode
            else:
                logging.warning("Command delivery failed. Retrying in 30 seconds.")
            
        time.sleep(30)

if __name__ == "__main__":
    main()
