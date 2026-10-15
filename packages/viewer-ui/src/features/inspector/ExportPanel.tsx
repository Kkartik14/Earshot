import { useState } from "react";
import { useExport, type ExportFormat } from "../../api/hooks";
import { EXPORT_FORMATS, exportRefusal } from "./exporters";
import styles from "./ExportPanel.module.css";

/** Project the current incident through a registered exporter and present the
 * result. The projection is read-only and the document is shown verbatim and
 * offered as a download; a policy refusal (`EARSHOT_EXPORT_DENIED`) or an
 * unregistered name (`EARSHOT_UNKNOWN_EXPORT_FORMAT`) renders as an explicit,
 * honest refusal rather than a crash or an empty document. */
export function ExportPanel({ bundleId }: { bundleId: string | undefined }) {
  const [format, setFormat] = useState<ExportFormat>(EXPORT_FORMATS[0]);
  const [requested, setRequested] = useState<ExportFormat | null>(null);
  const projection = useExport(bundleId, requested);

  const documentJson =
    projection.data == null ? null : JSON.stringify(projection.data.document, null, 2);
  const downloadHref =
    documentJson == null
      ? null
      : `data:application/json;charset=utf-8,${encodeURIComponent(documentJson)}`;

  return (
    <section className={styles.panel} aria-label="Export">
      <div className={styles.panelHead}>
        <h2>Export</h2>
        <span className={styles.note}>
          project this incident through a registered exporter
        </span>
      </div>

      <div className={styles.controls}>
        <label className={styles.field}>
          <span className={styles.fieldLabel}>Exporter format</span>
          <select
            className={styles.select}
            value={format}
            onChange={(event) => setFormat(event.target.value as ExportFormat)}
          >
            {EXPORT_FORMATS.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>
        <button type="button" className={styles.run} onClick={() => setRequested(format)}>
          Project through exporter
        </button>
      </div>

      {requested == null ? (
        <p className={styles.hint}>
          Choose an exporter and project this incident to see and download the projection.
        </p>
      ) : projection.isPending ? (
        <p className={styles.hint}>Projecting through the {requested} exporter…</p>
      ) : projection.data ? (
        <div className={styles.result}>
          <div className={styles.resultHead}>
            <span className={styles.resultMeta}>
              format <code>{projection.data.format}</code> · destination{" "}
              <code>{projection.data.destination}</code>
            </span>
            {downloadHref != null ? (
              <a
                className={styles.download}
                href={downloadHref}
                download={`${projection.data.bundle_id}.${projection.data.format}.json`}
              >
                Download projection
              </a>
            ) : null}
          </div>
          <div className={styles.docWrap}>
            <pre className={styles.doc}>{documentJson}</pre>
          </div>
        </div>
      ) : (
        (() => {
          const refusal = exportRefusal(projection.error);
          return (
            <div className={styles.refusal} role="status">
              <div className={styles.refusalHead}>
                <span className={styles.refusalTitle}>{refusal.title}</span>
                <span className={styles.code}>{refusal.code}</span>
              </div>
              <p className={styles.refusalDetail}>{refusal.detail}</p>
            </div>
          );
        })()
      )}
    </section>
  );
}
