import boto3
import os
from boto3.dynamodb.conditions import Key
from decimal import Decimal
import json

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["TABLE_NAME"])


def convert_decimal(obj):
    if isinstance(obj, list):
        return [convert_decimal(i) for i in obj]
    elif isinstance(obj, dict):
        return {k: convert_decimal(v) for k, v in obj.items()}
    elif isinstance(obj, Decimal):
        # Si el valor tiene decimales, convertir a float; si no, a int
        return int(obj) if obj % 1 == 0 else float(obj)
    else:
        return obj

def lambda_handler(event, context):
    store = event.get("pathParameters", {}).get("store")
    
    if store:
        response = table.query(
            KeyConditionExpression=Key("Store").eq(store)
        )
        items = response["Items"]
    else:
        response = table.scan()
        items = response["Items"]

    # Convertir Decimals antes de devolver
    clean_items = convert_decimal(items)

    return {
        "statusCode": 200,
        "headers": {"Access-Control-Allow-Origin": "*"},
        "body": json.dumps(clean_items)
    }

