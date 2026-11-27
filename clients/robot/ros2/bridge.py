"""Optional ROS 2 mapping for the local mock device."""

import argparse
from collections.abc import Callable
import json
from time import monotonic

from avip_client.mock_device import ACTION_CAPABILITY, DeviceEvent, MockDevice, SensorObservation


OBSERVATION_TOPIC = "/avi/sim/observation"
ACTION_TOPIC = "/avi/sim/action_events"
_TOPIC_DEPTH = 16


class RosBridge:
    """Convert synthetic sensor text into local action lifecycle messages."""

    def __init__(
        self, device: MockDevice, publish: Callable[[str], None], *, clock: Callable[[], float] = monotonic,
    ):
        self._device = device
        self._publish = publish
        self._clock = clock

    def receive(self, raw: str) -> None:
        try:
            observation = SensorObservation.parse(raw)
        except ValueError:
            return
        self._send(self._device.submit(observation.to_action(), now=self._clock()))

    def poll(self) -> None:
        self._send(self._device.advance(now=self._clock()))

    def _send(self, events: tuple[DeviceEvent, ...]) -> None:
        for event in events:
            self._publish(json.dumps(event.message(), separators=(",", ":")))


def create_node(device: MockDevice):
    import rclpy
    from std_msgs.msg import String

    node = rclpy.create_node("avi_mock_device_bridge")
    publisher = node.create_publisher(String, ACTION_TOPIC, _TOPIC_DEPTH)

    def publish(raw: str) -> None:
        publisher.publish(String(data=raw))

    bridge = RosBridge(device, publish)
    node.create_subscription(
        String, OBSERVATION_TOPIC, lambda message: bridge.receive(message.data), _TOPIC_DEPTH,
    )
    node.create_timer(0.01, bridge.poll)
    return node


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bridge synthetic observations to a ROS 2 mock device.")
    parser.add_argument("--allow-mock-action", action="store_true")
    args = parser.parse_args(argv)

    import rclpy
    from rclpy.executors import ExternalShutdownException

    rclpy.init(args=[])
    try:
        device = MockDevice(capabilities={ACTION_CAPABILITY}, authorized=args.allow_mock_action)
        node = create_node(device)
        try:
            rclpy.spin(node)
        except (KeyboardInterrupt, ExternalShutdownException):
            pass
        finally:
            node.destroy_node()
    finally:
        if rclpy.ok():
            rclpy.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
