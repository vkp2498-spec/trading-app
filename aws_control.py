from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")

DEFAULT_REGION = "ap-south-1"
DEFAULT_INSTANCE_NAME = "ai-trading-bot-vamsi"
DEFAULT_PUBLIC_IP = "13.234.86.47"
DEFAULT_PRIVATE_IP = "172.26.8.88"


def _client():
    try:
        import boto3
    except ImportError as error:
        raise RuntimeError("boto3 is not installed on this server") from error

    return boto3.client(
        "ec2",
        region_name=os.getenv("AWS_REGION", DEFAULT_REGION),
    )


def _instance_id(client) -> str:
    configured_id = os.getenv("AWS_INSTANCE_ID", "").strip()
    if configured_id:
        return configured_id

    instance_name = os.getenv(
        "AWS_INSTANCE_NAME",
        DEFAULT_INSTANCE_NAME,
    ).strip()
    response = client.describe_instances(
        Filters=[
            {"Name": "tag:Name", "Values": [instance_name]},
            {
                "Name": "instance-state-name",
                "Values": ["pending", "running", "stopping", "stopped"],
            },
        ]
    )
    matches = [
        instance
        for reservation in response.get("Reservations", [])
        for instance in reservation.get("Instances", [])
    ]
    if not matches:
        response = client.describe_instances(
            Filters=[
                {
                    "Name": "private-ip-address",
                    "Values": [DEFAULT_PRIVATE_IP],
                },
                {
                    "Name": "instance-state-name",
                    "Values": ["pending", "running", "stopping", "stopped"],
                },
            ]
        )
        matches = [
            instance
            for reservation in response.get("Reservations", [])
            for instance in reservation.get("Instances", [])
        ]
    if not matches:
        raise RuntimeError(
            f"EC2 instance not found for Name tag {instance_name} "
            f"or private IP {DEFAULT_PRIVATE_IP}"
        )
    if len(matches) > 1:
        raise RuntimeError(f"Multiple EC2 instances found for Name tag {instance_name}")
    return matches[0]["InstanceId"]


def aws_instance_status() -> dict:
    client = _client()
    instance_id = _instance_id(client)
    response = client.describe_instances(InstanceIds=[instance_id])
    instances = [
        instance
        for reservation in response.get("Reservations", [])
        for instance in reservation.get("Instances", [])
    ]
    if not instances:
        raise RuntimeError(f"EC2 instance not found: {instance_id}")

    instance = instances[0]
    state = instance.get("State", {}).get("Name", "unknown")
    name = next(
        (
            tag.get("Value")
            for tag in instance.get("Tags", [])
            if tag.get("Key") == "Name"
        ),
        os.getenv("AWS_INSTANCE_NAME", DEFAULT_INSTANCE_NAME),
    )
    return {
        "region": os.getenv("AWS_REGION", DEFAULT_REGION),
        "availabilityZone": instance.get("Placement", {}).get("AvailabilityZone", ""),
        "instanceId": instance_id,
        "instanceName": name,
        "state": state,
        "publicIpAddress": instance.get("PublicIpAddress") or DEFAULT_PUBLIC_IP,
        "privateIpAddress": instance.get("PrivateIpAddress") or DEFAULT_PRIVATE_IP,
        "lastChecked": datetime.now(IST).isoformat(),
        "controlAvailable": True,
        "message": "AWS EC2 control is available",
    }


def start_instance() -> dict:
    client = _client()
    instance_id = _instance_id(client)
    client.start_instances(InstanceIds=[instance_id])
    status = aws_instance_status()
    status["message"] = "Start requested"
    return status


def stop_instance() -> dict:
    client = _client()
    instance_id = _instance_id(client)
    client.stop_instances(InstanceIds=[instance_id])
    status = aws_instance_status()
    status["message"] = "Stop requested"
    return status
