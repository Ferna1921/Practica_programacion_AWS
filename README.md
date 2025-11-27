# ☁️ Practica_programacion_AWS

Este proyecto implementa una arquitectura **serverless completa** con **AWS Lambda, S3, DynamoDB, API Gateway y SNS**.  
El sistema permite cargar inventarios mediante archivos CSV, almacenarlos en DynamoDB, exponerlos a través de una API REST y visualizar los datos en una **web estática hospedada en S3**.

---

## ⚙️ Arquitectura general

| Recurso | Descripción |
|----------|--------------|
| **Lambda A – load_inventory** | Procesa ficheros CSV subidos al bucket de ingesta y guarda los datos en DynamoDB. |
| **Lambda B – get_inventory_api** | Expone los datos de DynamoDB mediante un endpoint API Gateway `/items`. |
| **Lambda C – notify_low_stock** | Detecta cambios en DynamoDB y notifica por SNS si el stock es bajo. |
| **S3 (uploads)** | Bucket para subir los archivos CSV. |
| **S3 (web)** | Bucket público con hosting estático del dashboard. |
| **DynamoDB** | Tabla `Inventory` que almacena los artículos. |
| **SNS** | Tópico para alertas de bajo stock (notifica por email). |
| **API Gateway** | Endpoint REST que sirve los datos a la web. |

---

## 🧩 Requisitos previos

- Python **3.11+**
- AWS CLI configurado (`aws configure`)
- Permisos sobre IAM, S3, Lambda, DynamoDB, API Gateway y SNS
- Librerías instaladas:

```bash
pip install boto3 python-dotenv
```
---

## 🔑 Configuración del entorno

1. Copiar la plantilla de entorno:
```bash
   cp .env.sample .env
```

2. Editar `.env` con las credenciales AWS y parámetros necesarios.
```bash
    AWS_ACCESS_KEY_ID=TU_ACCESS_KEY
    AWS_SECRET_ACCESS_KEY=TU_SECRET_KEY
    AWS_SESSION_TOKEN=TU_SESSION_TOKEN
    AWS_DEFAULT_REGION=us-east-1
    SNS_SUBSCRIPTION_EMAIL=tu_correo@ejemplo.com
```
3. Despliegue automático de la infraestructura:
```bash
   python infra/scripts/setup.py
```
Al finalizar, se mostrarán un mensaje similar a este:
- ✅ Despliegue de infraestructura COMPLETO
- S3 Ingesta: s3://inventory-uploads-20251105-xxxxxx
- S3 Web/Dashboard: http://inventory-web-20251105-xxxxxx.s3-website-us-east-1.amazonaws.com
- DynamoDB: Inventory
- 🌐 API disponible en: https://v94xf8g3c8.execute-api.us-east-1.amazonaws.com/prod
- SNS Tópico: arn:aws:sns:us-east-1:xxxx:NoStock-xxxx

4. Para introducir inventarios, subir archivos CSV al bucket de ingesta S3 mostrado.
```bash
    aws s3 cp inventory-berlin.csv s3://inventory-uploads-YYYYMMDD-xxxxxx/
```
Verificación:
```bash
    aws dynamodb scan --table-name Inventory
```

5. Acceder al dashboard web:
   Abrir en el navegador la URL del bucket S3 web proporcionada tras el despliegue.

6. API REST: Hacer click en el link dispuesto en el paso 3 para ver los datos en formato JSON. Se tiene que ir a la ruta `/items`.

7. Notificar bajo stock:
   - Modificar el inventario en DynamoDB para que un artículo tenga menos de 5 unidades.
   - Comprobar que se recibe un email de alerta.

8. Cuando termines, eliminar la infraestructura:
```bash
   python infra/scripts/teardown.py
```
---
## 👨‍💻 Autor

Aitor Fernández de Retana

📧 aitor.fdezderetana@opendeusto.es





