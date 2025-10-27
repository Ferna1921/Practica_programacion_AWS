import boto3
import os

sns = boto3.client("sns")
TOPIC_ARN = os.environ["TOPIC_ARN"]

def lambda_handler(event, context):
    for record in event["Records"]:
        if record["eventName"] == "INSERT":
            item = record["dynamodb"]["NewImage"]
            if int(item["Count"]["N"]) < 5:
                msg = f"⚠️ Low stock: {item['Item']['S']} in {item['Store']['S']} ({item['Count']['N']} left)"
                sns.publish(TopicArn=TOPIC_ARN, Message=msg)
    return {"statusCode": 200}
