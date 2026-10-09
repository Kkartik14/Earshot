"""One registry for independently evolvable Earshot compatibility layers."""

PACKAGE_VERSION = "0.1.0"
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
API_VERSION = "0.12.0"
ANALYZER_VERSION = "0.6.0"
TURN_FACT_PROJECTION_VERSION = "0.1.0"
PIPELINE_ADAPTER_VERSION = "0.3.0"
LIVEKIT_ADAPTER_VERSION = "0.1.0"
PIPECAT_ADAPTER_VERSION = "0.1.0"
ELEVENLABS_NORMALIZER_VERSION = "0.1.0"
VAPI_NORMALIZER_VERSION = "0.1.0"
RETELL_NORMALIZER_VERSION = "0.1.0"
RINGG_NORMALIZER_VERSION = "0.1.0"
