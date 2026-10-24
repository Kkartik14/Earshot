"""One registry for independently evolvable Earshot compatibility layers."""

PACKAGE_VERSION = "0.3.0"
CONTRACT_VERSION = "0.2.0"
SEMANTIC_PROFILE_VERSION = "0.2.0"
# Producers emit the current version; readers accept every version they can
# interpret. This keeps version bumps backward-compatible.
SUPPORTED_CONTRACT_VERSIONS = ("0.1.0", "0.2.0")
SUPPORTED_SEMANTIC_PROFILE_VERSIONS = ("0.1.0", "0.2.0")
# Recovery, media custody, and coverage loss counts were added in 0.2.0. Reject
# them when an artifact claims the older contract.
RECOVERY_MIN_CONTRACT_VERSION = "0.2.0"
MEDIA_CUSTODY_MIN_CONTRACT_VERSION = "0.2.0"
COVERAGE_LOSS_COUNT_MIN_CONTRACT_VERSION = "0.2.0"
API_VERSION = "0.15.0"
ANALYZER_VERSION = "0.6.0"
TURN_FACT_PROJECTION_VERSION = "0.1.0"
# Stored per incident (``incidents.projection_version``). Bump it whenever an
# incident's projected rows would change for the same artifact: the graph or
# turn-fact projection, ``ANALYZER_VERSION`` or ``TURN_FACT_PROJECTION_VERSION``,
# or the expiry and export columns. Older rows are then re-projected in the
# background after the server starts.
INCIDENT_PROJECTION_VERSION = 1
PIPELINE_ADAPTER_VERSION = "0.3.0"
LIVEKIT_ADAPTER_VERSION = "0.1.0"
PIPECAT_ADAPTER_VERSION = "0.1.0"
TVIC_RUNTIME_ADAPTER_VERSION = "0.1.0"
# The hosted-provider translators (payload -> call evidence spans). Stamped on each
# delivery's evidence; a connector accepts deliveries whatever version created it.
ELEVENLABS_NORMALIZER_VERSION = "0.2.0"
VAPI_NORMALIZER_VERSION = "0.2.0"
RETELL_NORMALIZER_VERSION = "0.2.0"
RINGG_NORMALIZER_VERSION = "0.2.0"
