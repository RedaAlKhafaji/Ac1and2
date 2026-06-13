import os, json, time, logging, requests, boto3, urllib3
from botocore.config import Config
from botocore import UNSIGNED
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.basicConfig(level=logging.INFO)

# Use the token that we know is currently valid
SSO_TOKEN = "eyJhbGciOiJSUzI1NiJ9.eyJvZmZsaW5lIjpmYWxzZSwicmVnaW9uIjoiU0ciLCJleHAiOjE3ODM5MzkxMzQsImlhdCI6MTc4MTM0NzEzNCwic2NhbkNvZGUiOm51bGwsInVzZXJuYW1lIjoiMjEyNDU4MjQ3In0.DlLdnc4hF6JOk-6RXP7TIdP8OPjpIZdMcdt6qw6iqKAxxoK5tvwJTjK0X6RxOkeVNagL1sX12VsrpMEE0Da3Gr_eyEQdtnPKmvSNBqHRYh0LhcpcCC4sQ_tIIZkJV61ZMKqnGKxShyaoWvaJyRzuroBqZPuEFQua6BVEhmDuVHQ"
AC1, AC2 = "C-0JABFAAAI", "DfaxahFAAAE"

class TCLCloud:
    def __init__(self): self.iot = None
    def refresh(self):
        data = requests.get("https://eu-api-prod.aws.tcljd.com/v1/auth/service/loadBalance", 
                            headers={"appid": "wx6e1af3fa84fbe523", "ssotoken": SSO_TOKEN}, verify=False).json()["data"]
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
        # LOGIC FIX: Only target 2 if mode is EXACTLY 2. Ignore 0 or 6.
        target = 2 if ac2.get("generatorMode") == 2 else 0
        cloud.set_mode(target)
    except: cloud.refresh()
    time.sleep(60)
