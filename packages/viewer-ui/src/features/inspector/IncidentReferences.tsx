import { useExternalReferences } from "../../api/hooks";
import type { paths } from "../../api/schema";
import styles from "./IncidentReferences.module.css";

export type IncidentReference =
  paths["/v1/incidents/{bundle_id}/references"]["get"]["responses"][200]["content"]["application/json"]["items"][number];

function safeLocalHref(value: string | undefined): string | undefined {
  if (value == null || !value.startsWith("/") || value.startsWith("//")) return;
  try {
    const url = new URL(value, "https://earshot.invalid");
    return url.origin === "https://earshot.invalid" ? value : undefined;
  } catch {
    return;
  }
}

function labelFor(reference: IncidentReference): string {
  if (reference.namespace === "platform" && reference.record_type === "call") {
    return "Platform call";
  }
  if (reference.namespace === "voice_labs" && reference.record_type === "run") {
    return "Voice Labs run";
  }
  return `${reference.namespace} ${reference.record_type}`;
}

export function IncidentReferences({
  bundleId,
  hrefForReference,
}: {
  bundleId: string;
  hrefForReference?: (reference: IncidentReference) => string | undefined;
}) {
  const query = useExternalReferences(bundleId);

  return (
    <section className={styles.panel} aria-labelledby="incident-links-heading">
      <div className={styles.head}>
        <div>
          <h2 id="incident-links-heading">Related records</h2>
          <p>
            Cross-product IDs are project-scoped catalog links, separate from evidence.
          </p>
        </div>
      </div>
      {query.isPending ? <p className={styles.note}>Loading related records…</p> : null}
      {query.isError ? (
        <p className={styles.note} role="status">
          Related records are currently unavailable. The incident evidence remains
          available.
        </p>
      ) : null}
      {query.isSuccess && query.data.items.length === 0 ? (
        <p className={styles.note}>
          No Platform call or Voice Labs run is linked to this incident.
        </p>
      ) : null}
      {query.isSuccess && query.data.items.length > 0 ? (
        <ul className={styles.list}>
          {query.data.items.map((reference) => {
            const href = safeLocalHref(hrefForReference?.(reference));
            return (
              <li key={`${reference.namespace}:${reference.record_type}`}>
                <span className={styles.label}>{labelFor(reference)}</span>
                {href ? (
                  <a className={styles.id} href={href}>
                    {reference.external_id}
                  </a>
                ) : (
                  <code className={styles.id}>{reference.external_id}</code>
                )}
              </li>
            );
          })}
        </ul>
      ) : null}
    </section>
  );
}
