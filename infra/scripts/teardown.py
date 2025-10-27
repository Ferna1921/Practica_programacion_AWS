#!/usr/bin/env python3
import os
import time
import shelve
import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv
import sys

# ---------- Configuración y Clientes ----------
load_dotenv()
REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
DB_PATH = "aws_resources.db"

# Nombres base a buscar para la eliminación global
BUCKET_PREFIXES = ["inventory-uploads-", "inventory-web-"]
TABLE_NAME = "Inventory"
SNS_TOPIC_PREFIX = "NoStock-"
LAMBDA_PREFIXES = ["load_inventory-", "get_inventory_api-", "notify_low_stock-"]

# El rol que se usa en el lab. Solo se limpiarán sus políticas, NO se eliminará.
ROLE_NAME = os.getenv("LAMBDA_ROLE_NAME", "voclabs-LabRole") 

try:
    session = boto3.Session(region_name=REGION)
    s3 = session.client("s3")
    s3_resource = session.resource("s3")
    iam = session.client("iam")
    dynamodb = session.client("dynamodb")
    lambda_client = session.client("lambda")
    sns = session.client("sns")
    apigw = session.client("apigatewayv2") 
except Exception as e:
    sys.exit(f"❌ ERROR: No se pudieron inicializar los clientes de AWS. Verifica tus credenciales. Error: {e}")


# ---------- Helpers de Eliminación ----------

def delete_bucket_globally(bucket_name):
    """
    Vacía, limpia la configuración y elimina un bucket S3 específico.
    Utiliza s3_resource para manejar la eliminación de objetos y versiones.
    """
    if not bucket_name:
        return

    print(f"Buscando dependencias para s3://{bucket_name}...")
    try:
        # 1. Eliminar configuración de notificación (para Lambda A)
        try:
            s3.put_bucket_notification_configuration(
                Bucket=bucket_name,
                NotificationConfiguration={}
            )
        except ClientError:
            pass
            
        # 2. Eliminar política y BPA
        try:
             s3.delete_bucket_policy(Bucket=bucket_name)
        except ClientError:
            pass
        try:
             s3.delete_public_access_block(Bucket=bucket_name)
        except ClientError:
            pass

        # 3. Eliminar todos los objetos (¡CRUCIAL!)
        # Use s3_resource for object and version deletion
        bucket = s3_resource.Bucket(bucket_name)
        
        # ⚠️ CORRECTED LOGIC FOR VERSIONED OBJECTS
        # This single call deletes both objects and versioned history efficiently.
        response = bucket.objects.all().delete()
        if response and 'Errors' in response[0]:
            print(f"❌ ADVERTENCIA: Algunos objetos no se pudieron eliminar: {response[0]['Errors']}")
        
        # If versioning is enabled, delete versions explicitly
        bucket.object_versions.delete() 
        
        print(f"[S3] Objetos vaciados de: {bucket_name}")

        # 4. Eliminar el bucket (using the Client)
        s3.delete_bucket(Bucket=bucket_name)
        print(f"✅ [S3] Bucket eliminado: {bucket_name}")

    except ClientError as e:
        if 'NoSuchBucket' not in str(e):
            print(f"❌ ERROR al eliminar el bucket {bucket_name}: {e}")
        else:
            print(f"[S3] Bucket no encontrado (ya eliminado): {bucket_name}")
    except Exception as e:
        # Catch unexpected errors during listing/deletion
        print(f"❌ Error genérico en la eliminación del bucket {bucket_name}: {e}")


def delete_lambda_globally(function_name):
    """Busca y elimina una función Lambda, junto con sus ESMs (si los tiene)."""
    try:
        # 1. Eliminar Mapeos de Fuente de Eventos (DDB Streams/SQS)
        mappings = lambda_client.list_event_source_mappings(
            FunctionName=function_name
        ).get("EventSourceMappings", [])
        for mapping in mappings:
            lambda_client.delete_event_source_mapping(UUID=mapping['UUID'])
            print(f"[Lambda] Mapeo de eventos eliminado para {function_name} (UUID: {mapping['UUID']})")
        time.sleep(1) 
        
        # 2. Eliminar la función
        lambda_client.delete_function(FunctionName=function_name)
        print(f"✅ [Lambda] Función eliminada: {function_name}")
    except ClientError as e:
        if 'ResourceNotFoundException' not in str(e):
            print(f"❌ ERROR al eliminar Lambda {function_name}: {e}")
        else:
             print(f"[Lambda] Función no encontrada (ya eliminada): {function_name}")


def delete_dynamodb_table(table_name):
    """Elimina la tabla DynamoDB."""
    try:
        dynamodb.describe_table(TableName=table_name)
        dynamodb.delete_table(TableName=table_name)
        print(f"[DDB] Eliminando tabla: {table_name}")
        waiter = session.resource("dynamodb").meta.client.get_waiter("table_not_exists")
        waiter.wait(TableName=table_name)
        print(f"✅ [DDB] Tabla eliminada: {table_name}")
    except ClientError as e:
        if 'ResourceNotFoundException' not in str(e):
            print(f"❌ ERROR al eliminar tabla {table_name}: {e}")
        else:
            print(f"[DDB] Tabla no encontrada (ya eliminada): {table_name}")


def delete_sns_topic_globally(topic_arn):
    """Elimina un tópico SNS por ARN."""
    try:
        sns.delete_topic(TopicArn=topic_arn)
        print(f"✅ [SNS] Tópico eliminado: {topic_arn}")
    except ClientError as e:
        if 'NotFound' not in str(e):
            print(f"❌ ERROR al eliminar tópico {topic_arn}: {e}")
        else:
            print(f"[SNS] Tópico no encontrado (ya eliminado): {topic_arn}")

def delete_iam_policies(role_name):
    """Limpia las políticas in-line y adjuntas creadas por el script."""
    try:
        # 1. Desadjuntar políticas adjuntas (como AWSLambdaBasicExecutionRole)
        attached_policies = iam.list_attached_role_policies(RoleName=role_name)['AttachedPolicies']
        for policy in attached_policies:
            # Desadjuntamos solo si no es una política esencial de Lab que otros usan.
            if 'AWSLambdaBasicExecutionRole' in policy['PolicyName']:
                iam.detach_role_policy(RoleName=role_name, PolicyArn=policy['PolicyArn'])
                print(f"[IAM] Política básica desadjuntada: {policy['PolicyName']}")

        # 2. Eliminar políticas en línea (PolicyA, PolicyB, PolicyC)
        inline_policies = iam.list_role_policies(RoleName=role_name)['PolicyNames']
        for policy_name in inline_policies:
            if policy_name.startswith('PolicyA-') or policy_name.startswith('PolicyB-') or policy_name.startswith('PolicyC-'):
                iam.delete_role_policy(RoleName=role_name, PolicyName=policy_name)
                print(f"[IAM] Política en línea eliminada: {policy_name}")
            
    except ClientError as e:
        if 'NoSuchEntity' not in str(e):
            print(f"❌ ERROR al limpiar políticas del rol {role_name}: {e}")


# ---------- Main Teardown Global ----------

if __name__ == "__main__":
    
    print("\n⚠️ INICIANDO TEARDOWN GLOBAL: Eliminando TODOS los recursos de Inventario ⚠️")
    
    # --- 1. Eliminación de Buckets (Global) ---
    print("\n--- 1. ELIMINANDO S3 BUCKETS ---")
    
    # Listar todos los buckets y filtrar por prefijo
    for prefix in BUCKET_PREFIXES:
        try:
            response = s3.list_buckets()
            for bucket_info in response['Buckets']:
                bucket_name = bucket_info['Name']
                if bucket_name.startswith(prefix):
                    delete_bucket_globally(bucket_name)
        except Exception as e:
            print(f"❌ Error al listar buckets: {e}")


    # --- 2. Eliminación de Lambdas (Global) ---
    print("\n--- 2. ELIMINANDO FUNCIONES LAMBDA ---")
    for prefix in LAMBDA_PREFIXES:
        try:
            # Lista todas las funciones y filtra por el prefijo de la práctica
            response = lambda_client.list_functions()
            for func in response['Functions']:
                func_name = func['FunctionName']
                if func_name.startswith(prefix):
                    delete_lambda_globally(func_name)
        except Exception as e:
            print(f"❌ Error al listar funciones Lambda: {e}")

    # --- 3. Eliminación de DynamoDB ---
    print("\n--- 3. ELIMINANDO TABLA DYNAMODB ---")
    delete_dynamodb_table(TABLE_NAME)


    # --- 4. Eliminación de SNS Topics (Global) ---
    print("\n--- 4. ELIMINANDO TÓPICOS SNS ---")
    try:
        response = sns.list_topics()
        for topic in response['Topics']:
            topic_arn = topic['TopicArn']
            topic_name = topic_arn.split(':')[-1]
            if topic_name.startswith(SNS_TOPIC_PREFIX):
                delete_sns_topic_globally(topic_arn)
    except Exception as e:
        print(f"❌ Error al listar tópicos SNS: {e}")


    # --- 5. Limpieza de Políticas IAM ---
    print("\n--- 5. LIMPIANDO POLÍTICAS IAM ---")
    # Limpia las políticas CREADAS por el script del rol del Lab (NO elimina el rol)
    delete_iam_policies(ROLE_NAME) 

    # --- 6. Limpieza de Archivos Locales ---
    if os.path.exists(DB_PATH):
        try:
            os.remove(DB_PATH)
            print(f"\n✅ Archivo de registro '{DB_PATH}' eliminado.")
        except OSError as e:
            print(f"❌ ADVERTENCIA: No se pudo eliminar el archivo de registro: {e}")
        
    print("\n\n🎉 TEARDOWN GLOBAL COMPLETADO. Todos los recursos específicos de la práctica han sido eliminados. 🎉")