"""Hermes integration is pending verification of an upstream contract.

No HTTP endpoints or command-execution bridge are assumed. The orchestrator
depends only on AgentAdapter; a future adapter must preserve Hermes approvals.
"""

class HermesAgentAdapter:
    def __init__(self):
        raise NotImplementedError("Hermes integration pending a verified upstream contract")
