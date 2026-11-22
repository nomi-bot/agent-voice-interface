"""Mock action contracts and fake/real ROS graph checks."""

import importlib.util
import json
from pathlib import Path
from time import monotonic
import unittest

from avip_client.mock_device import ActionRequest, MockDevice, SensorObservation
from clients.robot.ros2.bridge import ACTION_TOPIC, OBSERVATION_TOPIC, RosBridge, create_node


FIXTURE = json.loads((Path(__file__).parent / "fixtures/jev31-mock-device-v1.json").read_text())


def kinds(events):
    return [event.kind for event in events]


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.request = ActionRequest.parse(FIXTURE["action"])
        self.device = MockDevice(capabilities={"robot_actions"}, authorized=True)
        self.assertEqual(SensorObservation.parse(json.dumps(FIXTURE["observation"])).to_action(), self.request)

    def test_lifecycle_and_duplicate_share_one_recording(self):
        running = self.device.submit(self.request, now=0)
        completed = self.device.advance(now=FIXTURE["work_ms"] / 1000)
        self.assertEqual(kinds(running + completed), FIXTURE["lifecycle"])
        self.assertEqual(self.device.submit(self.request, now=1), running + completed)
        self.assertEqual(self.device.recorded_samples, {"sample-001": 37})
        conflicting = ActionRequest.parse({**FIXTURE["action"], "payload": {"sample": 38}})
        self.assertEqual(self.device.submit(conflicting, now=1)[0].payload, {"reason": "conflict"})
        self.assertEqual(self.device.recorded_samples, {"sample-001": 37})

    def test_capability_authorization_and_allowlist_refuse_admission(self):
        absent = MockDevice(capabilities=set(), authorized=True)
        denied = MockDevice(capabilities={"robot_actions"}, authorized=False)
        unknown = ActionRequest.parse({**FIXTURE["action"], "kind": "unknown"})
        for device, request, reason in (
            (absent, self.request, "capability"),
            (denied, self.request, "authorization"),
            (self.device, unknown, "allowlist"),
        ):
            event, = device.submit(request, now=0)
            self.assertEqual((event.kind, event.payload), ("denied", {"reason": reason}))
            self.assertEqual(device.advance(now=1), ())
            self.assertEqual(device.recorded_samples, {})

    def test_timeout_and_cancel_leave_no_recorded_sample(self):
        self.device.submit(self.request, now=0)
        deadline = FIXTURE["timeout_ms"] / 1000
        self.assertEqual(kinds(self.device.advance(now=deadline)), ["timed_out"])
        self.assertEqual(self.device.recorded_samples, {})
        another = ActionRequest.parse({**FIXTURE["action"], "event_id": "sample-002"})
        self.device.submit(another, now=deadline)
        self.assertEqual(kinds(self.device.cancel("sample-002")), ["cancelled"])
        self.assertEqual(self.device.advance(now=deadline + 1), ())
        self.assertEqual(self.device.recorded_samples, {})

    def test_per_device_action_history_is_bounded_without_reexecuting_old_ids(self):
        first = self.device.submit(self.request, now=0)
        for index in range(1, 32):
            action = ActionRequest.parse({**FIXTURE["action"], "event_id": f"event-{index}"})
            self.device.submit(action, now=0)
        next_action = ActionRequest.parse({**FIXTURE["action"], "event_id": "event-extra"})
        self.assertEqual(self.device.submit(next_action, now=0)[0].payload, {"reason": "capacity"})
        self.assertEqual(self.device.submit(self.request, now=0), first)

    def test_ros_fake_bus_rejects_oversized_and_non_sensor_messages(self):
        sent = []
        clock = [0.0]
        bridge = RosBridge(self.device, sent.append, clock=lambda: clock[0])
        bridge.receive(" " * (FIXTURE["max_message_bytes"] + 1))
        bridge.receive(json.dumps(FIXTURE["action"]))
        self.assertEqual(sent, [])
        bridge.receive(json.dumps(FIXTURE["observation"]))
        clock[0] = FIXTURE["work_ms"] / 1000
        bridge.poll()
        events = [json.loads(value) for value in sent]
        self.assertEqual([event["kind"] for event in events], FIXTURE["lifecycle"])
        self.assertEqual([event["event_id"] for event in events], ["sample-001"] * 3)


@unittest.skipUnless(importlib.util.find_spec("rclpy"), "ROS 2 Jazzy runtime not installed")
class RosGraphTests(unittest.TestCase):
    def test_ros_graph_accepts_observation_and_returns_correlated_lifecycle(self):
        import rclpy
        from rclpy.executors import SingleThreadedExecutor
        from std_msgs.msg import String

        rclpy.init()
        bridge = create_node(MockDevice(capabilities={"robot_actions"}, authorized=True))
        peer = rclpy.create_node("avi_mock_device_probe")
        executor = SingleThreadedExecutor()
        executor.add_node(bridge)
        executor.add_node(peer)
        received = []
        try:
            publisher = peer.create_publisher(String, OBSERVATION_TOPIC, 16)
            subscription = peer.create_subscription(
                String, ACTION_TOPIC, lambda message: received.append(json.loads(message.data)), 16,
            )
            deadline = monotonic() + 8
            while monotonic() < deadline and (
                not publisher.get_subscription_count() or not subscription.get_publisher_count()
            ):
                executor.spin_once(timeout_sec=0.05)
            self.assertGreater(publisher.get_subscription_count(), 0)
            self.assertGreater(subscription.get_publisher_count(), 0)
            publisher.publish(String(data=json.dumps(FIXTURE["observation"])))
            while monotonic() < deadline and len(received) < 3:
                executor.spin_once(timeout_sec=0.05)
            self.assertEqual([event["kind"] for event in received], FIXTURE["lifecycle"])
            self.assertEqual([event["event_id"] for event in received], ["sample-001"] * 3)
        finally:
            executor.shutdown()
            peer.destroy_node()
            bridge.destroy_node()
            rclpy.shutdown()


if __name__ == "__main__":
    unittest.main()
