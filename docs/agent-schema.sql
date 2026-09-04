-- ADR 0010 / migration 0008. Applied by Alembic; no historical approvals are inferred.

CREATE TABLE agent_catalog_snapshots (
	catalog_hash VARCHAR(64) NOT NULL, 
	owner_id TEXT NOT NULL, 
	agent_id VARCHAR(64) NOT NULL, 
	snapshot JSON NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (catalog_hash)
)

;


CREATE TABLE agent_intents (
	id UUID NOT NULL, 
	owner_id TEXT NOT NULL, 
	agent_id VARCHAR(64) NOT NULL, 
	request_key VARCHAR(64) NOT NULL, 
	record_id UUID NOT NULL, 
	revision INTEGER NOT NULL, 
	action VARCHAR(16) NOT NULL, 
	snapshot JSON NOT NULL, 
	args_hash VARCHAR(64) NOT NULL, 
	state VARCHAR(24) NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_agent_intent_request UNIQUE (owner_id, agent_id, request_key), 
	CONSTRAINT ck_agent_intent_action CHECK (action IN ('confirm','reject')), 
	CONSTRAINT ck_agent_intent_state CHECK (state IN ('awaiting_human','approved','rejected','executed'))
)

;
CREATE INDEX idx_agent_intent_queue ON agent_intents (owner_id, state, created_at);

CREATE TABLE agent_query_runs (
	id UUID NOT NULL, 
	owner_id TEXT NOT NULL, 
	agent_id VARCHAR(64) NOT NULL, 
	catalog_hash VARCHAR(64) NOT NULL, 
	query_fingerprint VARCHAR(64) NOT NULL, 
	result JSON NOT NULL, 
	result_hash VARCHAR(64) NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
)

;
CREATE INDEX idx_agent_query_owner ON agent_query_runs (owner_id, created_at);

CREATE TABLE agent_policy_decisions (
	id UUID NOT NULL, 
	intent_id UUID NOT NULL, 
	verdict VARCHAR(12) NOT NULL, 
	reason_codes JSON NOT NULL, 
	policy_digest VARCHAR(64) NOT NULL, 
	state_hash VARCHAR(64) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(intent_id) REFERENCES agent_intents (id)
)

;


CREATE TABLE agent_approval_grants (
	id UUID NOT NULL, 
	intent_id UUID NOT NULL, 
	decision_id UUID NOT NULL, 
	owner_id TEXT NOT NULL, 
	approved_by TEXT NOT NULL, 
	args_hash VARCHAR(64) NOT NULL, 
	policy_digest VARCHAR(64) NOT NULL, 
	capability_hash VARCHAR(64) NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	consumed_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_agent_approval_intent UNIQUE (intent_id), 
	FOREIGN KEY(intent_id) REFERENCES agent_intents (id), 
	FOREIGN KEY(decision_id) REFERENCES agent_policy_decisions (id)
)

;


CREATE TABLE agent_execution_receipts (
	id UUID NOT NULL, 
	owner_id TEXT NOT NULL, 
	agent_id VARCHAR(64) NOT NULL, 
	intent_id UUID NOT NULL, 
	grant_id UUID NOT NULL, 
	payload JSON NOT NULL, 
	content_hash VARCHAR(64) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_agent_receipt_grant UNIQUE (grant_id), 
	FOREIGN KEY(intent_id) REFERENCES agent_intents (id), 
	FOREIGN KEY(grant_id) REFERENCES agent_approval_grants (id)
)

;


