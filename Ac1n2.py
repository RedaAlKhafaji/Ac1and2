import os, json, time, logging, requests, boto3, urllib3
from botocore.config import Config
from botocore import UNSIGNED
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.basicConfig(level=logging.INFO)

REFRESH_TOKEN = os.environ.get("REFRESH_TOKEN")
AC1, AC2 = "C-0JABFAAAI", "DfaxahFAAAE"

class TCLCloud:
    def __init__(self): self.iot = None
    
    def refresh(self):
        logging.info("Attempting refresh...")
        # 1. Exchange Refresh Token
        resp = requests.post("https://eu-api-prod.aws.tcljd.com/v1/auth/login", 
                             json={"appid": "wx6e1af3fa84fbe523", "refreshtoken": REFRESH_TOKEN}, verify=False)
        res_json = resp.json()
        if "data" not in res_json:
            logging.error(f"Login failed! Server returned: {res_json}")
            raise KeyError("No 'data' in response")
            
        sso = res_json["data"]["ssoToken"]
        
        # 2. Get AWS credentials
        data = requests.get("https://eu-api-prod.aws.tcljd.com/v1/auth/service/loadBalance", 
                            headers={"appid": "wx6e1af3fa84fbe523", "ssotoken": sso}, verify=False).json()["data"]
        
        cognito = boto3.client('cognito-identity', region_name='eu-central-1', verify=False, config=Config(signature_version=UNSIGNED))
        creds = cognito.get_credentials_for_identity(IdentityId=data["cognitoId"], Logins={'cognito-identity.amazonaws.com': data["cognitoToken"]})['Credentials']
        self.iot = boto3.client('iot-data', region_name='eu-central-1', endpoint_url='https://data.iot.eu-central-1.amazonaws.com', verify=False,
                               aws_access_key_id=creds['AccessKeyId'], aws_secret_access_key=creds['SecretKey'], aws_session_token=creds['SessionToken'])

    def set_mode(self, target):
        self.iot.publish(topic=f"$aws/things/{AC1}/shadow/update", qos=1, payload=json.dumps({"state": {"desired": {"generatorMode": target, "turbo": 1 if target == 0 else 0}}}).encode('utf-8'))

    def get_ac2(self):
        return json.loads(self.iot.get_thing_shadow(thingName=AC2)['payload'].read().decode('utf-8'))["state"]["reported"]

cloud = TCLCloud()
cloud.refresh()
while True:
    try:
        ac2 = cloud.get_ac2()
        mode = int(ac2.get("generatorMode", 6))
        # Sync: Only target 2 if mode is 2
        target = 2 if mode == 2 else 0
        cloud.set_mode(target)
    except Exception as e:
        logging.error(f"Error occurred: {e}")
        cloud.refresh()
    time.sleep(60)
