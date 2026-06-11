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

AC1_CMD_URL = f"https://data.iot.eu-central-1.amazonaws.com/topics/$aws/things/{AC1_DEVICE_ID}/shadow/update?qos=1"
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
        # verify=False added to bypass Render's missing AWS Root CA issue
        response = requests.get(AC2_STATUS_URL, headers=HEADERS, timeout=10, verify=False)
        response.raise_for_status()
        
        data = response.json()
        
        # IoT JSON payloads often hide variables in unpredictable nested dictionaries.
        # Converting the JSON to a string without spaces is a bulletproof way to find the state.
        data_str = json.dumps(data).replace(" ", "")
        
        if '"autoGeneratorMode":1' in data_str or '"generatorMode":6' in data_str:
            return True
        return False
        
    except Exception as e:
        logging.error(f"Error checking AC 2 status: {e}")
        return None

def set_ac1
