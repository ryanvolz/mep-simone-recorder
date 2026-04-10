#!/usr/bin/env python

import json
import logging
import os
import socket
import time

from paho.mqtt import client as mqtt_client

logger = logging.getLogger("simone_record")
logger.setLevel(os.environ.get("SIMONE_RECORD_LOG_LEVEL", "INFO"))
logger.propagate = False
_console_handler = logging.StreamHandler()
_console_handler.setLevel(logging.DEBUG)
logger.addHandler(_console_handler)


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        logger.info("Connected to MQTT broker!")
    else:
        logger.info(f"Failed to connect to MQTT broker, return code {rc}")


def on_message(client, userdata, msg):
    logger.info(msg.payload.decode())


def run(freq_mhz=31.65, channel_str="A,B", config_name="default"):
    node_id = socket.gethostname()
    client = mqtt_client.Client(client_id=f"simone_record_cli_{node_id}")
    client.on_connect = on_connect
    client.connect("localhost", 1883)
    client.on_message = on_message
    client.subscribe("rfsoc/status")
    client.subscribe(f"dt/simone/simone_recorder/{node_id}/status")
    client.subscribe(f"dt/simone/simone_recorder/{node_id}/request")
    client.loop_start()
    client.publish(
        "rfsoc/command",
        json.dumps({"task_name": "set", "arguments": f"freq_IF {freq_mhz}"}),
    )
    client.publish(
        "rfsoc/command",
        json.dumps({"task_name": "set", "arguments": f"channel {channel_str}"}),
    )
    client.publish(
        f"dt/simone/simone_recorder/{node_id}/request",
        json.dumps({"task_name": "config.load", "arguments": {"name": config_name}}),
    )
    client.publish(
        f"dt/simone/simone_recorder/{node_id}/request",
        json.dumps({"task_name": "enable"}),
    )
    time.sleep(10)
    client.publish("rfsoc/command", json.dumps({"task_name": "capture_next_pps"}))
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        client.publish(
            f"dt/simone/simone_recorder/{node_id}/request",
            json.dumps({"task_name": "disable"}),
        )
        client.loop_stop()


if __name__ == "__main__":
    run()
