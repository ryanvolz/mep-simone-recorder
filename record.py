#!/usr/bin/env python

import json
import logging
import os
import socket
import time

from paho.mqtt import client as mqtt

logger = logging.getLogger("simone_record")
logger.setLevel(os.environ.get("SIMONE_RECORD_LOG_LEVEL", "INFO"))
logger.propagate = False
_console_handler = logging.StreamHandler()
_console_handler.setLevel(logging.DEBUG)
logger.addHandler(_console_handler)


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        logger.info("Connected to MQTT broker!")
    else:
        logger.info(f"Failed to connect to MQTT broker, return code {reason_code}")


def on_message(client, userdata, msg):
    logger.info(msg.payload.decode())
    if (
        msg.topic.startswith("dt/simone/recorder")
        and msg.topic.split("/")[-1] == "status"
    ):
        payload = json.loads(msg.payload)
        if payload["state"] == "waiting":
            # recorder stopped, restart it
            node_id = socket.gethostname()
            client.publish(
                f"cmd/simone/recorder/{node_id}/request",
                json.dumps({"task_name": "enable"}),
            )


def run(freq_mhz=31.65, channel_str="A,B", config_name="default"):
    node_id = socket.gethostname()
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2, client_id=f"simone_record_cli_{node_id}"
    )
    client.on_connect = on_connect
    client.connect("localhost", 1883)
    client.on_message = on_message
    client.subscribe("rfsoc/status")
    client.subscribe(f"dt/simone/recorder/{node_id}/request")
    client.loop_start()
    client.publish("rfsoc/command", json.dumps({"task_name": "reset"}))
    client.publish(
        "rfsoc/command",
        json.dumps({"task_name": "set", "arguments": f"freq_IF {freq_mhz}"}),
    )
    client.publish(
        "rfsoc/command",
        json.dumps(
            {"task_name": "set", "arguments": f"freq_metadata {freq_mhz * 1e6}"}
        ),
    )
    client.publish(
        "rfsoc/command",
        json.dumps({"task_name": "set", "arguments": f"channel {channel_str}"}),
    )
    client.publish(
        f"cmd/simone/recorder/{node_id}/request",
        json.dumps({"task_name": "config.load", "arguments": {"name": config_name}}),
    )
    client.publish(
        f"cmd/simone/recorder/{node_id}/request",
        json.dumps({"task_name": "enable"}),
    )
    time.sleep(10)
    client.publish("rfsoc/command", json.dumps({"task_name": "capture_next_pps"}))
    client.publish("rfsoc/command", json.dumps({"task_name": "get tlm"}))
    # now that we've started, monitor recorder status to be able to restart if it stops
    client.subscribe(f"dt/simone/recorder/{node_id}/status")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        client.publish(
            f"cmd/simone/recorder/{node_id}/request",
            json.dumps({"task_name": "disable"}),
        )
        client.loop_stop()


if __name__ == "__main__":
    run()
