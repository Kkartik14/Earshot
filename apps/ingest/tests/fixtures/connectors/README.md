# Real provider deliveries

Use this directory for a local signed webhook when checking a connector against
the provider's live signature format. The test
[`test_real_provider_deliveries.py`](../../test_real_provider_deliveries.py)
verifies the signature and confirms that private values do not reach the stored
Incident.

## Keep captures private

Raw webhook bodies can contain caller data. These files are gitignored and must
stay out of commits. Put signing secrets in environment variables. Delete a
capture after the check passes.

## File format

Save one JSON file in the provider's folder: `elevenlabs/`, `vapi/`, `retell/`,
or `ringg/`.

```json
{
  "provider": "retell",
  "secret_env": "RETELL_WEBHOOK_SECRET",
  "expected_disposition": "applied",
  "headers": [
    ["X-Retell-Signature", "v=1752828600000,d=abc123..."],
    ["Content-Type", "application/json"]
  ],
  "body_base64": "<base64 of the exact raw request body>",
  "must_not_appear": ["a transcript phrase", "the raw call ID"]
}
```

The `secret_env` field names the environment variable. Do not put the secret in
the JSON. `body_base64` must encode the exact bytes that the provider signed.
Capture the body before parsing or re-serializing it. The optional
`must_not_appear` list gives the test private strings to search for after
normalization.

Create the base64 value with `base64 -i body.raw` on macOS or `base64 -w0
body.raw` on Linux.

## Getting one signed delivery

- **ElevenLabs Agents:** use the Webhook Signing Secret from the agent's post-call
  webhook settings. This is separate from the API key. Point the webhook at a
  capture endpoint, run a test conversation, and save the `ElevenLabs-Signature`
  header with the raw body. The dashboard's test event also works.
- **Vapi:** use the server URL secret configured for the assistant. Point the
  server URL at a capture endpoint, place a test call, and save the
  `end-of-call-report` request with its authorization header and raw body.
- **Retell:** use the webhook API key used for signing. Point the webhook at a
  capture endpoint, run a test call, and save the `X-Retell-Signature` header
  with the raw `call_analyzed` body.
- **Ringg:** use the receiver-controlled Bearer token or `X-Webhook-Secret` set
  on the subscription. Subscribe to `all_processing_completed`, run a sandbox
  call, and save the configured authorization header with the raw body. Ringg
  does not document a provider HMAC scheme.

A REST API key cannot exercise a signature check for a webhook signed by the
provider. Use a signed delivery, such as the provider's test event, to verify
that path.

## When verification fails

A `DeliveryTrustError` means the connector's expected signature scheme does not
match the request. Compare the header format, signed bytes, and digest with the
provider's current documentation, then update the connector if needed.
