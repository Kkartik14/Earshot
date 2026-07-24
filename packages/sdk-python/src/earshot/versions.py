"""One registry for independently evolvable Earshot compatibility layers."""

PACKAGE_VERSION = "0.1.0"
CONTRACT_VERSION = "0.2.0"
SEMANTIC_PROFILE_VERSION = "0.2.0"
# Producers always emit the current version; readers accept every version whose
# artifacts they can interpret without misreading them. Shipping the read-side
# tolerance with the bump is what makes the bump a migration rather than a break.
SUPPORTED_CONTRACT_VERSIONS = ("0.1.0", "0.2.0")
SUPPORTED_SEMANTIC_PROFILE_VERSIONS = ("0.1.0", "0.2.0")
# 0.1.0 has no ``manifest.recovery`` member, so an artifact that claims 0.1.0 and
# carries one is asserting a contract it cannot express.
RECOVERY_MIN_CONTRACT_VERSION = "0.2.0"
API_VERSION = "0.5.0"
ANALYZER_VERSION = "0.5.0"
TURN_FACT_PROJECTION_VERSION = "0.1.0"
PIPELINE_ADAPTER_VERSION = "0.3.0"
LIVEKIT_ADAPTER_VERSION = "0.1.0"
PIPECAT_ADAPTER_VERSION = "0.1.0"
ELEVENLABS_NORMALIZER_VERSION = "0.1.0"
VAPI_NORMALIZER_VERSION = "0.1.0"
RETELL_NORMALIZER_VERSION = "0.1.0"
RINGG_NORMALIZER_VERSION = "0.1.0"
