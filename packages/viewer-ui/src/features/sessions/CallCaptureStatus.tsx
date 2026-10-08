import type { ObserveRoute } from "../../navigation";
import styles from "./CallCaptureStatus.module.css";

type CaptureState = Extract<ObserveRoute, { kind: "call-status" }>["capture"];

const TITLES: Record<CaptureState["state"], string> = {
  waiting: "Waiting for call evidence",
  delayed: "Call evidence is delayed",
  missing: "No call evidence was received",
  rejected: "Call evidence was rejected",
};

const DEFAULT_DETAILS: Record<CaptureState["state"], string> = {
  waiting: "The host has not reported a completed Earshot delivery yet.",
  delayed: "The call ended, but its evidence delivery has not arrived yet.",
  missing: "The host reports that no Earshot incident is available for this call.",
  rejected: "The host reports that Earshot did not accept this call's evidence.",
};

export function CallCaptureStatus({
  callId,
  capture,
}: {
  callId: string;
  capture: CaptureState;
}) {
  return (
    <div className={styles.page}>
      <p className={styles.eyebrow}>Platform call</p>
      <h1 className={styles.title}>{callId}</h1>
      <section className={styles.panel} aria-live="polite">
        <h2>{TITLES[capture.state]}</h2>
        <p>{capture.detail ?? DEFAULT_DETAILS[capture.state]}</p>
        {capture.state === "rejected" ? (
          <p className={styles.code}>Reason code: {capture.reasonCode}</p>
        ) : null}
        <p className={styles.source}>
          Host-reported delivery state. Earshot has not verified or displayed an incident
          for this call.
        </p>
      </section>
    </div>
  );
}
