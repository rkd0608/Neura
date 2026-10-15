"""What a site agent may send to the control plane (design §18.2).

GENERATED FILE: do not edit. Source: catalog/outbound.yaml.
Regenerate with `make gen`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OutboundField:
    minimal: bool
    description: str


@dataclass(frozen=True, slots=True)
class OutboundMetric:
    labels: tuple[str, ...]
    unit: str
    description: str


SCHEMA_VERSION = 1

MESSAGES: dict[str, dict[str, OutboundField]] = {
    'agent': {
        'hello.protocol_version': OutboundField(True, 'Protocol version the agent speaks.'),
        'hello.site_id': OutboundField(True, 'The site this server belongs to.'),
        'hello.agent_version': OutboundField(True, 'Agent software version.'),
        'hello.committed_generation': OutboundField(True, 'Highest desired-state generation committed at the site.'),
        'ack.desired_state_generation': OutboundField(True, 'Generation of desired state acknowledged.'),
        'ack.command_id': OutboundField(True, 'Command acknowledged.'),
        'ack.accepted': OutboundField(True, 'Whether it was accepted.'),
        'ack.reason.code': OutboundField(True, 'Reason code from the catalog.'),
        'ack.reason.message': OutboundField(False, 'Human-readable detail (free text).'),
        'status.agent_time_us': OutboundField(True, 'Agent monotonic time, for ordering reports.'),
        'status.components.kind': OutboundField(True, 'Kind of a reported component (robot, server, revision).'),
        'status.components.id': OutboundField(True, 'Its Neura identifier.'),
        'status.components.phase': OutboundField(True, 'Its phase (Ready, Running, NeedsAttention, ...).'),
        'status.components.reason.code': OutboundField(True, 'Reason code from the catalog.'),
        'status.components.reason.message': OutboundField(False, 'Human-readable detail (free text).'),
        'status.components.observed_revision_id': OutboundField(True, 'Revision observed on it.'),
        'status.sessions.session_id': OutboundField(True, 'Robot session identifier.'),
        'status.sessions.robot_name': OutboundField(True, 'Robot name, as registered in the control plane.'),
        'status.sessions.revision_id': OutboundField(True, 'Revision the robot runs.'),
        'status.sessions.lease_generation': OutboundField(True, 'Serving lease generation.'),
        'status.sessions.task_epoch': OutboundField(True, 'Robot task epoch.'),
        'status.sessions.state': OutboundField(True, 'Robot runtime state.'),
        'status.sessions.reason.code': OutboundField(True, 'Reason code from the catalog.'),
        'status.sessions.reason.message': OutboundField(False, 'Human-readable detail (free text).'),
        'status.sessions.metrics': OutboundField(False, 'Session counters: chunks, observations, rtt_p99_us, dispatched_actions, queued_actions.'),
        'status.faults.fault_key': OutboundField(True, 'Stable fault identifier, for deduplication.'),
        'status.faults.robot_name': OutboundField(True, 'Robot the fault is on.'),
        'status.faults.session_id': OutboundField(True, 'Session the fault is on.'),
        'status.faults.revision_id': OutboundField(True, 'Revision the robot ran.'),
        'status.faults.reason.code': OutboundField(True, 'Reason code from the catalog.'),
        'status.faults.reason.message': OutboundField(False, 'Human-readable detail (free text).'),
        'status.faults.latched': OutboundField(True, 'Whether the fault is latched.'),
        'status.faults.local_response': OutboundField(True, 'What the robot did locally (hold, stop, ...).'),
        'status.faults.occurred_at_unix_us': OutboundField(True, 'When it occurred (site wall clock).'),
        'status.offline_authorization_remaining_s': OutboundField(True, 'Offline authorization left.'),
        'status.contact_nonce': OutboundField(True, 'Random bytes the control plane signs to prove contact.'),
        'telemetry.schema_version': OutboundField(False, 'Version of the metric catalog below.'),
        'telemetry.sequence': OutboundField(False, 'Batch sequence number.'),
        'telemetry.metrics.name': OutboundField(False, 'A metric from the catalog below.'),
        'telemetry.metrics.labels': OutboundField(False, 'Its labels, from the catalog below.'),
        'telemetry.metrics.value': OutboundField(False, 'Its value.'),
        'telemetry.metrics.agent_time_us': OutboundField(False, 'Agent monotonic time.'),
        'operation.command_id': OutboundField(True, 'Command the report is for.'),
        'operation.operation_id': OutboundField(True, 'Operation the report is for.'),
        'operation.state': OutboundField(True, 'Running, Succeeded, Failed or Cancelled.'),
        'operation.reason.code': OutboundField(True, 'Reason code from the catalog.'),
        'operation.reason.message': OutboundField(False, 'Human-readable detail (free text).'),
        'operation.message': OutboundField(False, 'Progress or failure detail (free text).'),
        'operation.result_json': OutboundField(True, "The operation's result; only the keys under `results` below."),
        'facts.hostname': OutboundField(False, "The server's host name."),
        'facts.driver_version': OutboundField(True, 'NVIDIA driver version.'),
        'facts.cuda_version': OutboundField(True, 'CUDA version.'),
        'facts.gpus.index': OutboundField(True, 'GPU index.'),
        'facts.gpus.uuid': OutboundField(False, 'GPU UUID (a hardware identifier).'),
        'facts.gpus.model': OutboundField(True, 'GPU model.'),
        'facts.gpus.memory_mib': OutboundField(True, 'GPU memory.'),
        'facts.gpus.mig_enabled': OutboundField(True, 'Whether MIG is on.'),
        'facts.preflight.name': OutboundField(True, 'Preflight check name.'),
        'facts.preflight.passed': OutboundField(True, 'Whether it passed.'),
        'facts.preflight.reason': OutboundField(True, 'Reason code when it failed.'),
        'facts.preflight.detail': OutboundField(False, 'What was found (free text).'),
        'facts.preflight.fix': OutboundField(False, 'How to fix it (free text).'),
        'certificate_request.csr': OutboundField(True, "Signing request for the agent's next TLS client certificate (public key only)."),
    },
    'enroll': {
        'protocol_version': OutboundField(True, 'Protocol version the agent speaks.'),
        'enrollment_token': OutboundField(True, 'The one-time enrollment token.'),
        'public_key': OutboundField(True, "The agent's Ed25519 public key."),
        'agent_version': OutboundField(True, 'Agent software version.'),
        'tls_csr': OutboundField(True, "Signing request for the agent's TLS client certificate (public key only)."),
        'facts.hostname': OutboundField(False, "The server's host name."),
        'facts.driver_version': OutboundField(True, 'NVIDIA driver version.'),
        'facts.cuda_version': OutboundField(True, 'CUDA version.'),
        'facts.gpus.index': OutboundField(True, 'GPU index.'),
        'facts.gpus.uuid': OutboundField(False, 'GPU UUID (a hardware identifier).'),
        'facts.gpus.model': OutboundField(True, 'GPU model.'),
        'facts.gpus.memory_mib': OutboundField(True, 'GPU memory.'),
        'facts.gpus.mig_enabled': OutboundField(True, 'Whether MIG is on.'),
        'facts.preflight.name': OutboundField(True, 'Preflight check name.'),
        'facts.preflight.passed': OutboundField(True, 'Whether it passed.'),
        'facts.preflight.reason': OutboundField(True, 'Reason code when it failed.'),
        'facts.preflight.detail': OutboundField(False, 'What was found (free text).'),
        'facts.preflight.fix': OutboundField(False, 'How to fix it (free text).'),
    },
    'fetch_sealed_weights': {
        'digest': OutboundField(True, 'Digest of the weight file, as the artifact lists it.'),
    },
}

RESULTS: dict[str, OutboundField] = {
    'revision_digest': OutboundField(True, 'Prepare: the revision digest verified at the site.'),
    'capacity.max_robots': OutboundField(True, 'Prepare: robots this server can serve at the profile.'),
    'capacity.limiting_factor': OutboundField(True, 'Prepare: what limits that number.'),
    'capacity.source': OutboundField(True, 'Prepare: configured or measured latency profile.'),
    'capacity.sharing_case': OutboundField(True, 'Prepare: how the batch group shares its device.'),
    'capacity.assumptions.control_period_ms': OutboundField(False, 'Prepare: estimate input.'),
    'capacity.assumptions.starvation_budget_ms': OutboundField(False, 'Prepare: estimate input.'),
    'capacity.assumptions.request_interval_ms': OutboundField(False, 'Prepare: estimate input.'),
    'capacity.assumptions.link_allowance_ms': OutboundField(False, 'Prepare: estimate input.'),
    'capacity.assumptions.dispatch_freshness_ms': OutboundField(False, 'Prepare: estimate input.'),
    'capacity.assumptions.acceptance_freshness_ms': OutboundField(False, 'Prepare: estimate input.'),
    'capacity.assumptions.max_batch': OutboundField(False, 'Prepare: estimate input.'),
    'capacity.assumptions.batch_window_ms': OutboundField(False, 'Prepare: estimate input.'),
    'capacity.assumptions.reserve_fraction': OutboundField(False, 'Prepare: estimate input.'),
    'capacity.assumptions.latency_p99_ms_by_batch.*': OutboundField(False, 'Prepare: measured latency per batch size.'),
    'capacity.assumptions.memory_available_mib': OutboundField(False, 'Prepare: estimate input.'),
    'capacity.assumptions.device': OutboundField(False, 'Prepare: device the model was placed on.'),
    'capacity.assumptions.co_located_groups': OutboundField(False, 'Prepare: other models on that device.'),
    'approved_by': OutboundField(False, 'Activation: the site operator who approved.'),
    'refused': OutboundField(True, 'Activation: refused locally.'),
    'refused_by': OutboundField(False, 'Activation: the site operator who refused.'),
    'revision_id': OutboundField(True, 'Activation: the revision robots now run.'),
    'robot_ids': OutboundField(True, 'Activation: the robots that switched.'),
    'manifest_generation': OutboundField(True, 'Diagnostics: committed generation.'),
    'prepared': OutboundField(True, 'Diagnostics: prepared revisions.'),
    'assignments.*.current': OutboundField(True, 'Diagnostics: revision a robot runs.'),
    'assignments.*.next': OutboundField(True, 'Diagnostics: revision it switches to.'),
    'pending_activations': OutboundField(True, 'Diagnostics: activations awaiting approval.'),
    'gateway_running': OutboundField(True, 'Diagnostics: whether the gateway runs.'),
    'gateway_suspended': OutboundField(True, 'Diagnostics: whether serving is suspended.'),
}

METRICS: dict[str, OutboundMetric] = {
    'deadline_miss_rate': OutboundMetric((), 'ratio', "Share of dispatched inference requests that finished after their robot's deadline, per batch group."),
    'robots_per_gpu': OutboundMetric((), 'robots', 'Robot sessions served per GPU Neura may use.'),
}
