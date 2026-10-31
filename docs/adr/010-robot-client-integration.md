# ADR-010: Python robot simulator and optional ROS 2 bridge

**Status:** Accepted target direction; physical actuation excluded from v0.1.

**Date:** 2026-10-30

## Context

AVI needs a second, non-browser client to demonstrate that its voice and agent
contract is device-neutral. Requiring robot hardware or ROS 2 in the gateway would
make that validation dependent on hardware availability and robot-specific safety
systems before the common audio path is proven.

## Decision and lineage

Ship a Python headless SDK, a simulator with mocked audio/sensors/actions, and a
minimal optional ROS 2 bridge on the robot/client side. Gateway and common SDK
must run without ROS 2. This applies [ADR-006](006-hexagonal-architecture.md)'s
ports to another client and retains [ADR-005](005-security-boundaries.md)'s
untrusted-input and permission boundaries.

M4 validates two-way ROS 2 events and a bounded **mock action** lifecycle only.
Negotiated action capability does not grant permission or imply physical actuation.
No motors or physical robot control ship in v0.1. Any future physical integration
requires a separate decision and device-side allowlists, operator authorization and
independent safety/stop controls; model text must never become a direct motor command.

## Alternatives and consequences

Putting ROS 2 into the core increases installation and device coupling. Starting
with physical robots increases hardware and safety dependencies. Simulation validates
protocol portability reproducibly, but provides no evidence of real-world motion safety.

## Review trigger

Revisit the SDK/bridge split if M4 cannot demonstrate portability without core
device branches. Any physical-actuation proposal requires a new ADR and scope decision.
