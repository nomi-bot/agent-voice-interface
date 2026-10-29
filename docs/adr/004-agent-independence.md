# ADR-004: Agent independence
Status: accepted.

The orchestrator depends on AgentAdapter. Hermes-specific logic belongs in its
adapter. Do not invent Hermes endpoints: integration remains explicitly pending
until source or official documentation establishes the contract. Advertise status
and cancellation only if the actual adapter supports them.
