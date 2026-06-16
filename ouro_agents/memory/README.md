# Memory Namespace Policy

Team-associated vector memories remain in the writing agent's `agent_id`
namespace for this release. A team memory means "what this agent learned while
operating in this team", not a shared team-wide memory visible to every agent.

The schema records `team_ids`, `source`, `run_id`, semantic category, basis,
stability, and use-maintained strength so a future shared namespace strategy can
be added in the backend without changing the reflector or validator contract.

Vector memory is semantic: `direction`, `preference`, and `fact`. Episodic
activity is written to period logs and may be promoted by dream later.
