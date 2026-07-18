# Real captured provider deliveries

Drop a genuine captured webhook here and `test_real_provider_deliveries.py` will
prove our connector authenticates the **real** signature and normalizes the
**real** bytes. This is how we confirm each provider's signature format matches
the live contract — a failure means our HMAC scheme disagrees with what the
provider actually sends.

> **These files contain real signing secrets and caller PII. They are gitignored
> and must never be committed.** Only the raw bytes are needed; you can delete a
> capture after the test passes.

## File format

One JSON file per delivery, in the provider's subdirectory
(`elevenlabs/`, `vapi/`, or `retell/`):

```json
{
  "provider": "retell",
  "secret": "the signing secret the provider used",
  "headers": [
    ["X-Retell-Signature", "v=1752828600000,d=abc123..."],
    ["Content-Type", "application/json"]
  ],
  "body_base64": "<base64 of the EXACT raw request body>",
  "must_not_appear": ["a phrase from the transcript", "the raw call id"]
}
```

`body_base64` must be the **exact** bytes the provider signed — capture the raw
body before any parsing/re-serialization, or the signature will not validate.
`must_not_appear` is optional: list a few real transcript phrases / identifiers,
and the test asserts they never reach the canonical incident (privacy check).

To base64 a captured body: `base64 -i body.raw` (macOS) or
`base64 -w0 body.raw` (Linux).

## What to capture per provider — and what `secret` is

| Provider | `secret` is… | How to get a signed delivery |
|---|---|---|
| **ElevenLabs Agents** | the **Webhook Signing Secret** (`wsec_…`) from the agent's post-call webhook settings — **not** the API key | Configure the post-call webhook to a capture URL (e.g. an [RequestBin](https://pipedream.com/requestbin) / webhook.site), then run one real agent conversation, **or** use the dashboard's "send test event" on the webhook. Capture the `ElevenLabs-Signature` header + raw body. |
| **Vapi** | the **server-URL secret you configured** (`X-Vapi-Secret`, or the Bearer credential) — you choose this value | Point the assistant's server URL at a capture endpoint, place one test call, and grab the `end-of-call-report` request (`Authorization`/`X-Vapi-Secret` header + raw body). |
| **Retell** | the **Retell webhook API key** used to sign | Set the webhook URL to a capture endpoint, run one test call, and grab the `X-Retell-Signature` header + raw `call_analyzed` body. |

> Because webhooks are **pushed and signed** by the provider, a REST API key
> alone cannot exercise the signature path — you need a real *signed delivery*.
> The providers' "send test webhook" buttons are the cheapest way to get one
> without placing a live call.

## If a capture fails to authenticate

A `DeliveryTrustError` on a real capture is the signal we were looking for: it
means the connector's expected signature scheme (header name/format, signed
string, digest) does not match the provider's live behavior. Compare the capture
against the connector's `authenticate()` and adjust the connector — this is
exactly the caveat the connectors were shipped with.
