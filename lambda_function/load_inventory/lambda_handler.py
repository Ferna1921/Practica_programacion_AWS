import boto3
import csv
import os

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["TABLE_NAME"])

def lambda_handler(event, context):
    s3 = boto3.client("s3")
    for record in event["Records"]:
        bucket = record["s3"]["bucket"]["name"]
        key = record["s3"]["object"]["key"]

        obj = s3.get_object(Bucket=bucket, Key=key)
        content = obj["Body"].read().decode("utf-8").splitlines()
        reader = csv.DictReader(content)

        for row in reader:
            table.put_item(Item={
                "Store": row["store"],
                "Item": row["item"],
                "Count": int(row["count"])
            })
    return {"statusCode": 200, "body": "CSV processed"}
