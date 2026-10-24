# Install on Kubernetes with Helm

The chart is in the repository at `deploy/helm/earshot`; it is not published to a chart
repository yet, so install it from a checkout of the release you run. It deploys:

- one Deployment with **exactly one replica** and the **`Recreate`** strategy. A second
  Earshot server on the same database exits with `EARSHOT_DATABASE_IN_USE`, so a rolling
  update would stall; `Recreate` stops the old pod before the new one starts. There is
  no replica count to set and no autoscaler;
- a Service on port 4319;
- an optional PersistentVolumeClaim for `/data` (SQLite and file recordings), kept when
  the release is uninstalled;
- an optional Ingress;
- startup, liveness (`/healthz`) and readiness (`/readyz`) probes;
- the same hardening as the Compose files: non-root (uid 999), read-only root
  filesystem, no capabilities, no privilege escalation, the `RuntimeDefault` seccomp
  profile, and no service-account token.

**The chart requires an operator token, with or without an Ingress.** It refuses to
render without `auth.existingSecret`, so the API is never served without a credential.

## 1. Create the Secrets

```bash
kubectl create namespace earshot
kubectl --namespace earshot create secret generic earshot \
  --from-literal=token="$(openssl rand -hex 32)"
```

For a Postgres catalog, a Secret with the URL and the instance correlation key. Back the
key up with the database: Call IDs and fingerprints are derived from it.

```bash
kubectl --namespace earshot create secret generic earshot-db \
  --from-literal=database-url='postgresql://earshot:<password>@<host>:5432/earshot' \
  --from-literal=correlation-key="$(openssl rand -base64 32)"
```

For recordings in S3 with an access key (skip it to use the pod's own cloud identity):

```bash
kubectl --namespace earshot create secret generic earshot-s3 \
  --from-literal=access-key-id='<id>' --from-literal=secret-access-key='<secret>'
```

## 2. Install

SQLite and recordings on a PersistentVolumeClaim (the default):

```bash
helm install earshot deploy/helm/earshot --namespace earshot \
  --set auth.existingSecret=earshot
```

Postgres and S3, with no volume:

```yaml
# values-production.yaml
auth:
  existingSecret: earshot
database:
  existingSecret: earshot-db
blobs:
  url: s3://my-bucket/earshot?region=eu-west-1
  existingSecret: earshot-s3
persistence:
  enabled: false
```

```bash
helm install earshot deploy/helm/earshot --namespace earshot -f values-production.yaml
```

The chart also refuses `persistence.enabled=false` while the catalog or the recordings
would live on the pod's own disk.

## 3. Open it

```bash
kubectl --namespace earshot port-forward service/earshot 4319:4319
```

Open <http://127.0.0.1:4319> and sign in with the token:

```bash
kubectl --namespace earshot get secret earshot -o jsonpath='{.data.token}' | base64 --decode
```

Agents in the cluster export OTLP/HTTP to `http://earshot.earshot.svc:4319` with the token,
or a project API key created with it, as `Authorization: Bearer <key>`.

## Ingress

```yaml
ingress:
  enabled: true
  className: nginx
  hosts:
    - host: earshot.example.com
      paths:
        - path: /
          pathType: Prefix
  tls:
    - secretName: earshot-tls
      hosts:
        - earshot.example.com
```

Enabling the Ingress sets `EARSHOT_BEHIND_TLS_PROXY`: terminate TLS at the Ingress. Without
an Ingress the chart sets `EARSHOT_TRUST_LOCAL_NETWORK` instead, for in-cluster clients and
`kubectl port-forward`. The token is required in both cases.

## Values

| Value                                                                                        | Default                                       | Meaning                                                                                                 |
| -------------------------------------------------------------------------------------------- | --------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `image.repository`, `image.tag`, `image.pullPolicy`                                          | GHCR, the chart's appVersion                  | The server image.                                                                                       |
| `auth.existingSecret`, `auth.tokenKey`                                                       | required, `token`                             | The operator token.                                                                                     |
| `database.existingSecret`                                                                    | empty: SQLite on `/data`                      | A Secret with `database-url` and `correlation-key` (keys renamable with `urlKey`, `correlationKeyKey`). |
| `blobs.url`                                                                                  | empty: files on `/data`                       | An `s3://` URL; see [storage and blobs](./storage-and-blobs.md#blob-urls).                              |
| `blobs.existingSecret`                                                                       | empty: the pod's identity                     | A Secret with `access-key-id` and `secret-access-key`.                                                  |
| `persistence.enabled`, `existingClaim`, `storageClass`, `accessMode`, `size`                 | `true`, -, -, `ReadWriteOnce`, `10Gi`         | The `/data` claim.                                                                                      |
| `extraEnv`                                                                                   | `[]`                                          | More variables, such as the webhook secrets a connector names as `env:NAME`.                            |
| `service.type`, `service.port`, `service.annotations`                                        | `ClusterIP`, `4319`                           | The Service.                                                                                            |
| `ingress.*`                                                                                  | disabled                                      | The Ingress, as above.                                                                                  |
| `resources`                                                                                  | 100m CPU, 512 MiB request; 2 GiB memory limit | Size memory with [production](./production.md#exposure-and-limits).                                     |
| `podAnnotations`, `podLabels`, `nodeSelector`, `tolerations`, `affinity`, `imagePullSecrets` | empty                                         | The usual pod placement and metadata.                                                                   |

## Upgrades and node loss

An upgrade stops the old pod, then starts the new one: expect a short gap. Agents behind
an OpenTelemetry Collector with a persistent queue lose nothing; see
[production](./production.md#put-a-collector-in-front).

If a node is lost, Postgres drops the old pod's sessions within about 10 seconds:
Earshot's lock sessions ask the database server for short TCP keepalives, so the
replacement pod can take the one-server lock instead of waiting for the operating
system's two-hour default. With SQLite on a `ReadWriteOnce` claim, the replacement also
waits until the cluster detaches the volume from the lost node.

## Uninstall

```bash
helm uninstall earshot --namespace earshot
```

The PersistentVolumeClaim is kept. Delete it by hand to delete the data:
`kubectl --namespace earshot delete pvc earshot`.

## What is checked

The repository's install tests run `helm lint`, validate every rendered variant with
`kubeconform -strict`, assert the token and replica rules on the rendered manifests, and
boot the image with the chart's rendered environment against Postgres and MinIO until
`/readyz` returns 200. No cluster install is part of those checks.
