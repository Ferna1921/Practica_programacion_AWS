#!/usr/bin/env python3
import os
import io
import json
import time
import uuid
import shelve
import zipfile
import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv
import sys
from dbm import error as dbm_error

# ---------- Config ----------
load_dotenv()
REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")

# Constantes de la práctica de inventario
DB_PATH = "aws_resources.db"
INGEST_BUCKET_BASE = "inventory-uploads" # S3 (bucket de ingesta)
WEB_BUCKET_BASE = "inventory-web"       # S3 (bucket web estático)
TABLE_NAME = "Inventory"                # DynamoDB
SNS_TOPIC_BASE = "NoStock"              # SNS

# Nombres de funciones Lambda
LAMBDA_A_NAME = "load_inventory"
LAMBDA_B_NAME = "get_inventory_api"
LAMBDA_C_NAME = "notify_low_stock"

# **SOLUCIÓN AL ERROR IAM**: Usamos un rol fijo que asumimos existe en el lab.
ROLE_NAME = "LabRole"
SNS_SUBSCRIPTION_EMAIL = os.getenv("SNS_SUBSCRIPTION_EMAIL")

# Inicializar clientes de AWS
session = boto3.Session(region_name=REGION)
s3 = session.client("s3")
dynamodb = session.client("dynamodb")
lambda_client = session.client("lambda")
sns = session.client("sns")
sts = session.client("sts")
iam = session.client("iam")
apigw = session.client("apigatewayv2") # Para HTTP API

# ---------- Helpers ----------
def unique_suffix(length=6):
    """Genera un sufijo único basado en fecha y UUID."""
    return f"{time.strftime('%Y%m%d')}-{uuid.uuid4().hex[:length]}"

def bucket_exists(name):
    """Verifica si un bucket S3 existe."""
    try:
        s3.head_bucket(Bucket=name)
        return True
    except ClientError as e:
        if e.response['Error']['Code'] == '404':
             return False
        # Para cualquier otro error (permisos, etc.), asumimos que no es accesible/no existe
        return False

def create_bucket(name):
    """Crea un bucket S3 en la región correcta."""
    if REGION == "us-east-1":
        s3.create_bucket(Bucket=name)
    else:
        s3.create_bucket(
            Bucket=name,
            CreateBucketConfiguration={"LocationConstraint": REGION}
        )

def disable_bucket_bpa(bucket):
    """Desactiva las restricciones de Bloqueo de Acceso Público (BPA) en el bucket."""
    try:
        s3.put_public_access_block(
            Bucket=bucket,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": False,
                "IgnorePublicAcls": False,
                "BlockPublicPolicy": False, # Permite aplicar la política pública
                "RestrictPublicBuckets": False,
            },
        )
        print(f"[S3] BPA (Block Public Access) desactivado para s3://{bucket}")
    except ClientError as e:
        print(f"❌ ADVERTENCIA: No se pudo desactivar el BPA en {bucket}. (Puede requerir permisos 's3:PutPublicAccessBlock'). Error: {e}")

def apply_web_public_policy(bucket):
    """Aplica la política de bucket necesaria para el hosting estático."""
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "PublicReadGetObject",
                "Effect": "Allow",
                "Principal": "*",
                "Action": ["s3:GetObject"],
                "Resource": f"arn:aws:s3:::{bucket}/*",
            },
        ],
    }
    s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(policy))
    print(f"[S3] Política pública de GetObject aplicada a s3://{bucket}")

def ensure_bucket(name, is_website=False):
    """Asegura la creación del bucket y configura el hosting si es un sitio web."""
    if bucket_exists(name):
        print(f"[S3] Bucket ya existe: s3://{name}")
    else:
        create_bucket(name)
        print(f"[S3] Bucket creado: s3://{name}")

    if is_website:
        disable_bucket_bpa(name)
        apply_web_public_policy(name)
        s3.put_bucket_website(
            Bucket=name,
            WebsiteConfiguration={"IndexDocument": {"Suffix": "index.html"}},
        )
        print(f"[S3] Hosting estático activado para s3://{name}")


def labrole_arn():
    """
    Solución al error IAM: Devuelve el ARN de un rol IAM existente.
    Asume que el rol existe y tiene los permisos necesarios.
    """
    try:
        role_resp = iam.get_role(RoleName=ROLE_NAME)
        role_arn = role_resp["Role"]["Arn"]
        print(f"[IAM] Usando rol existente: {ROLE_NAME}")
        return role_arn
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchEntity":
            sys.exit(f"❌ ERROR CRÍTICO: El rol fijo '{ROLE_NAME}' no fue encontrado. Verifica su nombre o tus permisos. No se puede continuar.")
        raise


def ensure_dynamodb_table(name):
    """Crea la tabla DynamoDB Inventory con Streams habilitados."""
    try:
        dynamodb.create_table(
            TableName=name,
            KeySchema=[
                {"AttributeName": "Store", "KeyType": "HASH"},
                {"AttributeName": "Item", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "Store", "AttributeType": "S"},
                {"AttributeName": "Item", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
            StreamSpecification={
                "StreamEnabled": True,
                "StreamViewType": "NEW_AND_OLD_IMAGES"
            }
        )
        print(f"[DDB] Creando tabla: {name} (esperando ACTIVE)")
        waiter = session.resource("dynamodb").meta.client.get_waiter("table_exists")
        waiter.wait(TableName=name)
    except dynamodb.exceptions.ResourceInUseException:
        print(f"[DDB] Tabla ya existe: {name}")

    desc = dynamodb.describe_table(TableName=name)["Table"]
    return desc["TableArn"], desc["LatestStreamArn"]


def build_lambda_zip_bytes(lambda_name: str) -> bytes:
    """Crea el paquete ZIP de la Lambda desde /lambdas/{lambda_name}/lambda_handler.py."""
    source_dir = os.path.join("../../lambda_function", lambda_name)
    main_file = "lambda_handler.py"
    source_path = os.path.join(source_dir, main_file)
    
    if not os.path.isfile(source_path):
        raise FileNotFoundError(f"No se encontró el archivo de la Lambda en: {source_path}")

    # Crear ZIP en memoria
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        # Guardar el código en la RAÍZ del zip
        z.write(source_path, arcname=main_file)
        
        # Implementar lógica para incluir dependencias si es necesario (e.g., pandas)

    buf.seek(0)
    return buf.read()


def ensure_lambda(function_name, role_arn, handler, code_path):
    """Crea o actualiza una función Lambda."""
    # Nombre del Handler para todas: 'lambda_handler.lambda_handler'
    code_bytes = build_lambda_zip_bytes(code_path)
    env_vars = {"DYNAMODB_TABLE_NAME": TABLE_NAME}

    try:
        resp = lambda_client.create_function(
            FunctionName=function_name,
            Runtime="python3.11",
            Role=role_arn,
            Handler=handler,
            Code={"ZipFile": code_bytes},
            Timeout=15,
            MemorySize=128,
            Environment={"Variables": env_vars},
            Publish=True,
        )
        print(f"[Lambda] Función creada: {function_name}")
    except lambda_client.exceptions.ResourceConflictException:
        print(f"[Lambda] Función ya existe: {function_name}. Actualizando código...")
        lambda_client.update_function_code(
            FunctionName=function_name, ZipFile=code_bytes, Publish=True
        )
        waiter = lambda_client.get_waiter("function_updated")
        waiter.wait(FunctionName=function_name)
        
    return lambda_client.get_function(FunctionName=function_name)["Configuration"]["FunctionArn"]


def add_s3_trigger_to_lambda(bucket_name, lambda_arn, function_name):
    """
    Configures the S3 PutObject trigger for the Lambda A.
    It first grants permission to S3 to invoke the Lambda and then configures the bucket notification.
    """
    
    # 1. Grant permission to S3 to invoke the Lambda
    # Principal MUST be 's3.amazonaws.com'
    principal = "s3.amazonaws.com"
    statement_id = f"s3-invoke-permission-{function_name}-{bucket_name}"

    try:
        lambda_client.add_permission(
            FunctionName=function_name,
            StatementId=statement_id,
            Action="lambda:InvokeFunction",
            Principal=principal,
            SourceArn=f"arn:aws:s3:::{bucket_name}", # Restrict invocation to THIS bucket
            # SourceAccount is good practice but not strictly required by Boto3:
            # SourceAccount=sts.get_caller_identity()["Account"] 
        )
        print(lambda_arn)  # asegurarte que sea ARN completo
        print(f"[Lambda] Permission for S3 invocation granted to {function_name}")
        time.sleep(5) 
        
    except ClientError as e:
        # Ignore if the permission already exists (idempotency)
        if "StatementId already exists" in str(e):
            print(f"[Lambda] S3 invocation permission already exists for {function_name}")
        else:
            print(f"❌ Error adding S3 invocation permission: {e}")
            raise

    # 2. Configure the bucket notification (S3 PUT)
    config = {
        'LambdaFunctionConfigurations': [
            {
                # Use the FULL ARN of the Lambda function
                'LambdaFunctionArn': lambda_arn, 
                # Trigger on all object creation events
                'Events': ['s3:ObjectCreated:*'], 
                'Filter': {
                    'Key': {
                        'FilterRules': [
                            # Only trigger for .csv files (as per practice objective)
                            {'Name': 'suffix', 'Value': '.csv'} 
                        ]
                    }
                }
            }
        ]
    }
    # This is the line that was failing due to the timing issue:
    s3.put_bucket_notification_configuration(
        Bucket=bucket_name,
        NotificationConfiguration=config
    )
    print(f"[S3] Trigger 's3:ObjectCreated:*' configured for {function_name}.")


def ensure_sns_topic_and_subscribe(name):
    """Crea el tópico SNS y suscribe un email (si se proporciona)."""
    resp = sns.create_topic(Name=name)
    topic_arn = resp["TopicArn"]
    print(f"[SNS] Tópico creado: {topic_arn}")
    
    if SNS_SUBSCRIPTION_EMAIL:
        try:
            sns.subscribe(
                TopicArn=topic_arn,
                Protocol='email',
                Endpoint=SNS_SUBSCRIPTION_EMAIL,
                ReturnSubscriptionArn=True
            )
            print(f"[SNS] Email '{SNS_SUBSCRIPTION_EMAIL}' suscrito al tópico (PENDIENTE de confirmación).")
        except ClientError as e:
            print(f"[SNS] ERROR al suscribir email: {e}")
            
    return topic_arn


def ensure_dynamodb_stream_trigger(stream_arn, function_name, enabled=True, batch_size=10):
    """
    Crea o actualiza el Mapeo de Fuente de Eventos (Event Source Mapping - ESM) 
    para DynamoDB Streams, asegurando la idempotencia.
    """
    existing = lambda_client.list_event_source_mappings(
        EventSourceArn=stream_arn, 
        FunctionName=function_name
    ).get("EventSourceMappings", [])
    
    if existing:
        uuid = existing[0]["UUID"]
        # Actualizar, si es necesario, para idempotencia
        lambda_client.update_event_source_mapping(
            UUID=uuid, 
            Enabled=enabled, 
            BatchSize=batch_size
        )
        print(f"[Lambda] Trigger DDB Streams actualizado (UUID={uuid})")
        return uuid
    
    # Crear un nuevo Mapeo
    resp = lambda_client.create_event_source_mapping(
        EventSourceArn=stream_arn,
        FunctionName=function_name,
        Enabled=enabled,
        BatchSize=batch_size,
        StartingPosition='LATEST' 
    )
    uuid = resp["UUID"]
    print(f"[Lambda] Trigger DDB Streams creado (UUID={uuid})")
    return uuid


def get_website_url(bucket_name):
    """Calcula la URL del sitio web estático."""
    region = s3.meta.region_name or "us-east-1"
    website_host = (
        f"s3-website-{region}.amazonaws.com" if region != "us-east-1" else "s3-website-us-east-1.amazonaws.com"
    )
    return f"http://{bucket_name}.{website_host}"


# ---------- Main ----------

if __name__ == "__main__":
    suffix = unique_suffix()
    
    # Nombres de recursos con sufijo
    ingest_bucket = f"{INGEST_BUCKET_BASE}-{suffix}"
    web_bucket = f"{WEB_BUCKET_BASE}-{suffix}"
    topic_name = f"{SNS_TOPIC_BASE}-{suffix}"
    lambda_a_name = f"{LAMBDA_A_NAME}-{suffix}"
    lambda_b_name = f"{LAMBDA_B_NAME}-{suffix}"
    lambda_c_name = f"{LAMBDA_C_NAME}-{suffix}"
    
    print(f"⚙️ Iniciando despliegue con sufijo: {suffix} en región: {REGION}")
    
    # 1. IAM Role (Resuelve el error de permisos IAM)
    lambda_role_arn = labrole_arn()

    # 2. DynamoDB Table (Idempotente)
    table_arn, stream_arn = ensure_dynamodb_table(TABLE_NAME)

    # 3. SNS Topic (Idempotente + Suscripción)
    sns_topic_arn = ensure_sns_topic_and_subscribe(topic_name)

    # 4. S3 Buckets (Ingesta y Web)
    ensure_bucket(ingest_bucket, is_website=False)
    ensure_bucket(web_bucket, is_website=True)

    # Subir el index.html (Asegúrate de tener la carpeta /web/index.html)
    index_path = os.path.join("../../web", "index.html")
    if os.path.exists(index_path):
        s3.upload_file(
            Filename=index_path,
            Bucket=web_bucket,
            Key="index.html",
            ExtraArgs={"ContentType": "text/html; charset=utf-8"},
        )
        print(f"[S3] index.html subido a s3://{web_bucket}")
    else:
        print(f"❌ ADVERTENCIA: No se encontró {index_path}. El sitio web estará vacío. (Crea la carpeta 'web' y el archivo 'index.html').")
        
    website_url = get_website_url(web_bucket)
    print(f"[S3] Website URL: {website_url}")

    # 5. Lambdas y Triggers
    
    # Lambda A: load_inventory (S3 -> DDB)
    lambda_a_arn = ensure_lambda(
        function_name=lambda_a_name,
        role_arn=lambda_role_arn,
        handler="lambda_handler.lambda_handler",
        code_path="load_inventory"
    )
    add_s3_trigger_to_lambda(ingest_bucket, lambda_a_arn, lambda_a_name)

    # Lambda B: get_inventory_api (API Gateway)
    lambda_b_arn = ensure_lambda(
        function_name=lambda_b_name,
        role_arn=lambda_role_arn,
        handler="lambda_handler.lambda_handler",
        code_path="get_inventory_api"
    )

    # Lambda C: notify_low_stock (DDB Streams -> SNS)
    lambda_c_arn = ensure_lambda(
        function_name=lambda_c_name,
        role_arn=lambda_role_arn,
        handler="lambda_handler.lambda_handler",
        code_path="notify_low_stock"
    )
    dcc_mapping_uuid = ensure_dynamodb_stream_trigger(stream_arn, lambda_c_name)

    # 6. API Gateway (PENDIENTE)
    # Aquí iría la lógica para crear HTTP API Gateway, la integración
    # con Lambda B y las rutas /items y /items/{store} con CORS habilitado.

    # 7. Guardar Recursos (para Teardown)
    try:
        with shelve.open(DB_PATH) as db:
            db["UNIQUE_SUFFIX"] = suffix
            db["ingest-bucket"] = ingest_bucket
            db["web-bucket"] = web_bucket
            db["dynamodb-table"] = TABLE_NAME
            db["dynamodb-stream-arn"] = stream_arn
            db["lambda-a"] = lambda_a_name
            db["lambda-b"] = lambda_b_name
            db["lambda-c"] = lambda_c_name
            db["lambda-role-name"] = ROLE_NAME
            db["sns-topic-arn"] = sns_topic_arn
            db["dcc-mapping-uuid"] = dcc_mapping_uuid
            db["website-url"] = website_url
            
    except dbm_error as e:
        print(f"❌ ADVERTENCIA: No se pudo guardar la información de recursos en '{DB_PATH}'. Error: {e}")

    print("\n✅ Despliegue de infraestructura COMPLETO (API Gateway pendiente).")
    print(f"S3 Ingesta: s3://{ingest_bucket}")
    print(f"S3 Web/Dashboard: {website_url}")
    print(f"DynamoDB: {TABLE_NAME}")
    print(f"SNS Tópico (Stock Bajo): {sns_topic_arn}")
    print(f"Rol utilizado: {ROLE_NAME} (ARN: {lambda_role_arn})")