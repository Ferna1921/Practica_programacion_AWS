import boto3
import os
from boto3.dynamodb.conditions import Key

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["TABLE_NAME"])

def lambda_handler(event, context):
    path = event.get("pathParameters") or {}
    store = path.get("store")

    if store:
        resp = table.query(KeyConditionExpression=Key("Store").eq(store))
        items = resp["Items"]
    else:
        scan = table.scan()
        items = scan["Items"]

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": str(items)
    }
