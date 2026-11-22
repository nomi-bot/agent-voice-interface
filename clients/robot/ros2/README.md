# ROS 2 mock device bridge

The optional bridge runs on ROS 2 Jazzy with `rclpy` and `std_msgs`. The default SDK and gateway install are independent of ROS. The bridge runs from this checkout using the regular AVIP SDK.

| Topic | Direction relative to bridge | Message |
| --- | --- | --- |
| `/avi/sim/observation` | Subscribed | `std_msgs/msg/String` containing a fixture-shaped synthetic observation |
| `/avi/sim/action_events` | Published | `std_msgs/msg/String` containing one correlated mock action event |

Run in a Jazzy environment with the SDK installed. Keep the graph local to the machine.

```sh
source /opt/ros/jazzy/setup.bash
export ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
python3 -m clients.robot.ros2.bridge --allow-mock-action
```

The CLI flag grants permission to record a sample in the local simulator. The bridge refuses action admission by default. The ROS message supplies only an event ID and a bounded synthetic reading. The bridge maps the observation to the fixed in-memory action. Its action events reuse the observation ID. Ctrl-C shuts down the node, publisher, subscription, and timer.

From terminals using the same ROS environment, subscribe and then send the golden observation.

```sh
ros2 topic echo /avi/sim/action_events std_msgs/msg/String
```

```sh
ros2 topic pub --once /avi/sim/observation std_msgs/msg/String \
  '{data: "{\"event_id\":\"sample-001\",\"kind\":\"sensor.observation\",\"capability\":\"synthetic_sensor\",\"payload\":{\"sample\":37}}"}'
```

The subscriber receives `accepted`, `running`, and `completed` for `sample-001`. Physical devices are outside this workflow.

## Reproduce the graph check

Run this command from the repository root in the Jazzy environment.

```sh
python3 -m unittest discover -s tests -p test_jev31_mock_device.py -v
```

The graph check runs a bridge node and a distinct peer node through `rclpy`. The peer publishes the observation and receives the correlated events on DDS topics. The check passed on Linux arm64 with Python 3.12.3, rclpy 7.1.13, and the Jazzy ROS-base image manifest `sha256:ff9683e990606b51cf829e9f42b82bd59b33352158f4d4435b9bb7785a22b7bb`. CI also runs the same lifecycle with a fake publisher when ROS is absent.
