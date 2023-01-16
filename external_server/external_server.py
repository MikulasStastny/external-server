import random
import string
import time
from queue import Queue

from external_server.exceptions import ConnectSequenceException
from external_server.utils import check_file_exists
from external_server.modules import (
    CarAccessoryCreator,
    MissionCreator
)
from external_server.modules.module_type import ModuleType
import external_server.protobuf.ExternalProtocol_pb2 as external_protocol

import paho.mqtt.client as mqtt


class ExternalServer:

    def __init__(self, ip: str, port: int) -> None:
        self.ip = ip
        self.port = port
        self.queue = Queue()
        self._command_counter = 0
        self.message_creator = {
            ModuleType.MISSION_MODULE.value: MissionCreator(),
            ModuleType.CAR_ACCESSORY_MODULE.value: CarAccessoryCreator()
        }

    @property
    def command_counter(self):
        self._command_counter += 1
        return self._command_counter

    def set_tls(self, ca_certs: str, certfile: str,
                keyfile: str) -> None:
        if not check_file_exists(ca_certs):
            raise FileNotFoundError(ca_certs)
        if not check_file_exists(certfile):
            raise FileNotFoundError(certfile)
        if not check_file_exists(keyfile):
            raise FileNotFoundError(keyfile)
        self.mqtt_client.tls_set(
            ca_certs=ca_certs,
            certfile=certfile,
            keyfile=keyfile
        )
        self.mqtt_client.tls_insecure_set(True)

    def init_mqtt_client(self) -> None:
        self.mqtt_client = mqtt.Client(
            client_id=''.join(random.choices(string.ascii_uppercase + string.digits, k=20)),
            clean_session=None,
            userdata=None,
            protocol=mqtt.MQTTv5,
            transport='tcp')
        self.mqtt_client.on_connect = lambda client, userdata, flags, rc, properties:\
            client.subscribe('to-server/CAR1', qos=2)
        self.mqtt_client.on_disconnect = lambda client, userdata, rc, properties:\
            print("Unexpected disconnection.") if rc != 0 else print("Disconnect")
        self.mqtt_client.on_message = self._on_message

    def _on_message(self, client: mqtt.Client, userdata, message: mqtt.MQTTMessage) -> None:
        message_external_client = external_protocol.ExternalClient().FromString(message.payload)
        self.queue.put(message_external_client)

    def start(self) -> None:
        while True:
            try:
                self.mqtt_client.connect(self.ip, port=self.port, keepalive=60)
                print("Server connected to MQTT broker")
                self.mqtt_client.loop_start()
                self._init_sequence()
            except ConnectSequenceException:
                print("Connect sequence failed")
                time.sleep(1)
                continue
            except ConnectionRefusedError:
                print("Unable to connect, trying again")
                time.sleep(1)
                continue
            self._normal_communication()

    def _init_sequence(self):
        devices_num = self._init_connect()
        received_msgs = self._init_status(devices_num)
        self._init_command(devices_num, received_msgs)

    def _init_connect(self) -> int:
        received_msg = self.queue.get(block=True)
        if not received_msg.HasField("connect"):
            print("Connect sequence: Connect message has been expected")
            raise ConnectSequenceException()
        print("Connect sequence: Connect message has been received")
        connect_response = self.message_creator[ModuleType.MISSION_MODULE.value].create_connect_response(
            received_msg.connect.sessionId)
        sent_msg = external_protocol.ExternalServer()
        sent_msg.connectResponse.CopyFrom(connect_response)
        devices = received_msg.connect.devices

        self.mqtt_client.publish('to-client/CAR1', sent_msg.SerializeToString())
        print("Connect sequence: Connect message has been sent")
        return len(devices)

    def _init_status(self, devices_num: int) -> Queue:
        received_msgs = Queue()
        for i in range(devices_num):
            received_msg = self.queue.get(block=True)
            if not received_msg.HasField("status"):
                print("Connect sequence: Status message has been expected")
                raise ConnectSequenceException()
            print(f"Connect sequence: Status message has been received {i}")
            module = received_msg.status.deviceStatus.device.module
            if module not in self.message_creator.keys():
                print("Connect sequence: Module is not supported")
                raise ConnectSequenceException()
            received_msgs.put(received_msg)
            sent_msg = external_protocol.ExternalServer()
            sent_msg.statusResponse.CopyFrom(
                self.message_creator[module].create_status_response(
                    received_msg.status.sessionId,
                    received_msg.status.messageCounter
                )
            )
            self.mqtt_client.publish('to-client/CAR1', sent_msg.SerializeToString())
            print(f"Connect sequence: Status response message has been sent {i}")
        return received_msgs

    def _init_command(self, devices_num: int, received_msgs: Queue) -> None:
        for i in range(devices_num):
            received_msg = received_msgs.get()
            module = received_msg.status.deviceStatus.device.module
            sent_msg = external_protocol.ExternalServer()
            sent_msg.command.CopyFrom(
                self.message_creator[module].create_command(
                    received_msg.status.sessionId,
                    self.command_counter,
                    received_msg.status.deviceStatus
                )
            )
            self.mqtt_client.publish('to-client/CAR1', sent_msg.SerializeToString())
            print(f"Connect sequence: Command message has been sent {i}")

        for i in range(devices_num):
            received_msg = self.queue.get(block=True)
            if not received_msg.HasField("commandResponse"):
                print("Connect sequence: Command response message has been expected")
                raise ConnectSequenceException()
            print(f"Connect sequence: Command response message has been received {i}")

    def _normal_communication(self):
        while self.mqtt_client.is_connected():
            # TODO probably some timeout and after that reconnect and probably add it to the _init_sequence too
            received_msg = self.queue.get(block=True)
            session_id = received_msg.connect.sessionId

            if received_msg.HasField("connect"):
                print("Received unexpected connect message")

            elif received_msg.HasField("status"):
                print("Received status message")
                message_external_server = external_protocol.ExternalServer()
                module = received_msg.status.deviceStatus.device.module
                if module not in self.message_creator.keys():
                    print("Module is not supported")
                    continue
                status_response = self.message_creator[module].create_status_response(
                    session_id,
                    received_msg.status.messageCounter
                )
                message_external_server.statusResponse.CopyFrom(status_response)
                self.mqtt_client.publish('to-client/CAR1', message_external_server.SerializeToString())

                message_external_server = external_protocol.ExternalServer()
                message_external_server.command.CopyFrom(
                    self.message_creator[module].create_command(
                        session_id,
                        self.command_counter,
                        received_msg.status.deviceStatus
                    )
                )
                self.mqtt_client.publish('to-client/CAR1', message_external_server.SerializeToString())

            elif received_msg.HasField("commandResponse"):
                print("Received command response")
                # TODO check order

    def stop(self) -> None:
        self.mqtt_client.loop_stop()
