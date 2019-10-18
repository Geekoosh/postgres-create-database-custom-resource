import boto3
import postgresql
import logging
import uuid
import json
from crhelper import CfnResource

logger = logging.getLogger(__name__)
helper = CfnResource()

def simulateCall(event, success):
  data = {
    "Status": "SUCCESS" if success else "FAILED",
    "RequestId": event["RequestId"], 
    "LogicalResourceId": event["LogicalResourceId"],
    "StackId": event["StackId"],
    "PhysicalResourceId": event["PhysicalResourceId"] if "PhysicalResourceId" in event else str(uuid.uuid4()).replace("-", "")
  }
  if not success:
    data["Reason"] = "Simulated by user"
  return "curl -X PUT -H 'Content-Type:' --data-binary '%s' '%s'" % (json.dumps(data), event["ResponseURL"])

def get_param(event, name, error_msg=None, default=None):
  if name in event['ResourceProperties']:
    return event['ResourceProperties'][name]
  if default is None:
    raise Exception(error_msg)
  return default

def params(event):
  instance = get_param(event, 'Instance', 'Existing RDS instance is required')
  admin_user = get_param(event, 'Admin', 'postgres')
  admin_password = get_param(event, 'Password', 'Admin password is required')
  admin_db = get_param(event, 'AdminDatabase', 'postgres')
  username = get_param(event, 'Username', default=admin_user)
  database = get_param(event, 'Database', 'Database name is required')
  return instance, admin_user, admin_password, admin_db, username, database  

class RDSInstanceNotFoundError(Exception):
  pass

def get_instance_data(instance):
  client = boto3.client('rds')
  instance_data = client.describe_db_instances(DBInstanceIdentifier=instance)
  if instance_data is None or len(instance_data['DBInstances']) == 0:
    raise RDSInstanceNotFoundError("Cannot find instance %s" % (instance))
  return instance_data['DBInstances'][0]

def get_instance_endpoint(instance):
  instance_data = get_instance_data(instance)
  return instance_data['Endpoint']['Address']

def get_admin_db_connection(instance, admin_user, admin_password, admin_db):
  host = get_instance_endpoint(instance)
  return postgresql.open(
    "pq://%s:%s@%s:5432/%s" % (admin_user, admin_password, host, admin_db)
  )

def snapshot_name(instance, database):
  return "pre_delete_%s_%s" % (instance, database)

def create_snapshot(instance, database):
  client = boto3.client('rds')
  client.create_db_snapshot(
    DBSnapshotIdentifier=snapshot_name(instance, database),
    DBInstanceIdentifier=instance,
  )

def is_snapshot_ready(instance, database):
  client = boto3.client('rds')
  response = client.describe_db_snapshots(
    DBSnapshotIdentifier=snapshot_name(instance, database),
  )
  if response is None or len(response['DBSnapshots']) == 0:
    return False
  return response['DBSnapshots'][0]['Status'] == 'available'

def database_exists(instance, database, admin_user, admin_password, admin_db):
  db = get_admin_db_connection(instance, admin_user, admin_password, admin_db)
  sql = "select exists(SELECT datname FROM " \
    "pg_catalog.pg_database WHERE datname = '%s');" % (database.lower())
  logger.info('Checking if %s exists using: %s' % (database, sql))
  exists = db.query.first(sql)
  logger.info('DB exists result: %s' % (str(exists)))
  db.close()
  return exists
  
@helper.update
@helper.create
def create(event, context):
  instance, admin_user, admin_password, \
  admin_db, username, database = params(event)

  logger.info("Creating database %s in %s with owner %s" % (database, instance, username))

  db = get_admin_db_connection(instance, admin_user, admin_password, admin_db)
  db.execute("CREATE DATABASE %s OWNER %s;" % (database, username))
  db.close()

  # ID that will be used for the resource PhysicalResourceId
  return ("%s:%s:%s" % (instance, username, database)).replace("-", "_")


@helper.delete
def delete(event, context):
  instance, admin_user, admin_password, \
  admin_db, _, database = params(event)

  try:
    if not database_exists(instance, database, admin_user, admin_password, admin_db):
      return
    create_snapshot(instance, database)
  except RDSInstanceNotFoundError:
    return
  
@helper.poll_delete
def poll_delete(event, context):
    instance, admin_user, admin_password, \
    admin_db, _, database = params(event)

    # Notify complete if database does not exist or instance does not exist
    try:
      if not database_exists(instance, database, admin_user, admin_password, admin_db):
        return True
    except RDSInstanceNotFoundError:
      return True 

    # Delete database once snapshot is ready
    if is_snapshot_ready(instance, database):
      db = get_admin_db_connection(instance, admin_user, admin_password, admin_db)
      db.execute("DROP DATABASE IF EXISTS %s;" % database)
      db.close()
      return True

def handler(event, context):
  logger.info("Simulate success call using: %s", simulateCall(event, True))
  logger.info("Simulate failure call using: %s", simulateCall(event, False))
  helper(event, context)