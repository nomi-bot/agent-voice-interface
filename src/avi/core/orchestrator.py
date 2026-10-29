from dataclasses import asdict

from avi.agents.base import AgentAdapter


class VoiceOrchestrator:
    """Control-plane skeleton. Agent submission never awaits task completion."""

    def __init__(self, agent: AgentAdapter):
        self.agent = agent

    async def handle(self, message: dict) -> dict:
        if not isinstance(message, dict):
            raise ValueError("Expected a JSON object")
        kind = message.get("type")
        if kind == "ping":
            return {"type": "pong"}
        if kind == "submit":
            request = message.get("request")
            if not isinstance(request, str):
                raise ValueError("request must be a string")
            return {"type": "run", **asdict(await self.agent.submit(request))}
        if kind in {"status", "cancel_task"}:
            run_id = message.get("run_id")
            if not isinstance(run_id, str):
                raise ValueError("run_id must be a string")
            supported = self.agent.capabilities.status if kind == "status" else self.agent.capabilities.cancellation
            if not supported:
                return {"type": "unsupported", "operation": kind}
            operation = self.agent.status if kind == "status" else self.agent.cancel
            return {"type": "run", **asdict(await operation(run_id))}
        if kind == "stop_playback":
            return {"type": "stop_playback"}
        if kind == "cancel_response":
            return {"type": "unsupported", "operation": kind}
        raise ValueError("Unknown message type")
