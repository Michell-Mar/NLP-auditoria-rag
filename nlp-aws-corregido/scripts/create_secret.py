"""Create the initial OpenAI secret without entering it in shell history."""
import argparse
import getpass
import json

import boto3

parser = argparse.ArgumentParser()
parser.add_argument("--profile")
parser.add_argument("--region", required=True)
args = parser.parse_args()
key = getpass.getpass("OpenAI API key (hidden): ").strip()
if not key:
    parser.error("The key cannot be empty")
session = boto3.Session(profile_name=args.profile, region_name=args.region)
result = session.client("secretsmanager").create_secret(
    Name="nlp-audit/openai", SecretString=json.dumps({"OPENAI_API_KEY": key}))
print(result["ARN"])
