import { useCallback, useEffect, useId, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ApiError } from "../../api/client";
import { useViewerQueryScope, viewerQueryKey } from "../../api/queryScope";
import { EmptyState } from "../../components/EmptyState";
import { formatRelativeTime } from "../../lib/format";
import { AnchorLink, type ViewerLinkComponent } from "../../navigation";
import {
  isRetryableCallApiError,
  useCall,
  useLiveCallTurns,
  type CallDetail,
  type CallLiveTurns,
  type CallProjection,
  type CallTurn,
  type TurnSpan,
} from "./api";
import { callAnalysisNote } from "./analysis";
import { costReadout, formatClock, formatSeconds } from "./format";
import type { MediaClock } from "./mediaClock";
import { CostPanel, SignalsPanel } from "./Panels";
import { Player } from "./Player";
import { SpanDrawer } from "./SpanDrawer";
import { PlacedTranscript } from "./Transcript";
import { TurnsTable } from "./TurnsTable";
import styles from "./CallView.module.css";

function isCallReadDenied(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 401 || error.status === 403);
}

/** One call: player, synced transcript, turns, signals and cost, from its
 * projection; or, until it is projected, the transcript read from its spans. */
export function CallView({
  callId,
  LinkComponent = AnchorLink,
}: {
  callId: string;
  LinkComponent?: ViewerLinkComponent;
}) {
  return <CallViewForCall key={callId} callId={callId} LinkComponent={LinkComponent} />;
}

function CallViewForCall({
  callId,
  LinkComponent = AnchorLink,
}: {
  callId: string;
  LinkComponent?: ViewerLinkComponent;
}) {
  const call = useCall(callId);
  const liveTurns = useLiveCallTurns(
    callId,
    call.data?.state,
    call.data?.projection_state,
  );
  const queryClient = useQueryClient();
  const scope = useViewerQueryScope();
  const [detailRefresh, setDetailRefresh] = useState<{
    etag: string;
    after: number;
  } | null>(null);
  const lastObservedLiveEtag = useRef<string | null>(null);
  const lastRequestedLiveEtag = useRef<string | null>(null);
  const firstClosedLiveNeedsDetailValidation = useRef(false);
  useEffect(() => {
    const live = liveTurns.data;
    if (live?.state !== "closed" || live.etag == null || liveTurns.error != null) return;
    const previousEtag = lastObservedLiveEtag.current;
    if (previousEtag == null) firstClosedLiveNeedsDetailValidation.current = true;
    lastObservedLiveEtag.current = live.etag;
    const validatorChanged = previousEtag != null && previousEtag !== live.etag;
    const revisionMismatch =
      call.data != null &&
      (call.data.state !== "closed" ||
        live.revision !== (call.data.latest_revision?.revision ?? null));
    const firstDetailNeedsValidation =
      firstClosedLiveNeedsDetailValidation.current &&
      call.data != null &&
      !call.isFetching &&
      (!call.isError || isRetryableCallApiError(call.error));
    const detailReadFailed = call.isError && isRetryableCallApiError(call.error);
    if (
      (revisionMismatch ||
        validatorChanged ||
        firstDetailNeedsValidation ||
        detailReadFailed) &&
      lastRequestedLiveEtag.current !== live.etag
    ) {
      firstClosedLiveNeedsDetailValidation.current = false;
      lastRequestedLiveEtag.current = live.etag;
      setDetailRefresh({ etag: live.etag, after: call.dataUpdatedAt });
      void queryClient.invalidateQueries({
        queryKey: viewerQueryKey(scope, ["call", callId]),
        exact: true,
      });
    }
  }, [
    call.data?.state,
    call.data?.latest_revision?.revision,
    call.dataUpdatedAt,
    call.error,
    call.isError,
    call.isFetching,
    callId,
    liveTurns.error,
    liveTurns.data?.state,
    liveTurns.data?.revision,
    liveTurns.data?.etag,
    queryClient,
    scope.authContextId,
    scope.projectId,
  ]);
  useEffect(() => {
    if (
      detailRefresh == null ||
      !call.isError ||
      call.isFetching ||
      !isRetryableCallApiError(call.error)
    ) {
      return;
    }
    const timeout = window.setTimeout(() => {
      void call.refetch();
    }, 2_000);
    return () => window.clearTimeout(timeout);
  }, [
    call.error,
    call.errorUpdatedAt,
    call.isError,
    call.isFetching,
    call.refetch,
    detailRefresh,
  ]);
  useEffect(() => {
    if (
      detailRefresh != null &&
      call.isSuccess &&
      call.dataUpdatedAt > detailRefresh.after
    ) {
      setDetailRefresh(null);
    }
  }, [call.dataUpdatedAt, call.isSuccess, detailRefresh]);
  const liveCallMissing =
    liveTurns.error instanceof ApiError && liveTurns.error.status === 404;
  const detailCallMissing = call.error instanceof ApiError && call.error.status === 404;
  if (liveCallMissing || detailCallMissing) {
    return (
      <div className={styles.page}>
        <EmptyState
          title="Call not found"
          hint="It does not exist, it was purged, or it belongs to another project."
        />
      </div>
    );
  }
  if (isCallReadDenied(liveTurns.error) || isCallReadDenied(call.error)) {
    return (
      <div className={styles.page}>
        <EmptyState
          title="Call access unavailable"
          hint="This Call cannot be read in the current access context."
          focusTitle
        />
      </div>
    );
  }
  if (call.data == null && call.isPending) {
    return (
      <div className={styles.page}>
        <p className={styles.loading} role="status">
          Loading call…
        </p>
      </div>
    );
  }
  if (call.data == null) {
    const missing = call.error instanceof ApiError && call.error.status === 404;
    return (
      <div className={styles.page}>
        <EmptyState
          title={missing ? "Call not found" : "The call could not be loaded"}
          hint={
            missing
              ? "It does not exist, it was purged, or it belongs to another project."
              : (call.error?.message ?? "The call could not be loaded.")
          }
        />
      </div>
    );
  }
  return (
    <>
      {call.isError ? (
        <p className={styles.notice} role="status">
          Call details could not be refreshed; showing the last loaded details.
        </p>
      ) : null}
      {liveTurns.isError ? (
        <p className={styles.notice} role="status">
          Live turn updates could not be refreshed; showing the latest available Call
          data.
        </p>
      ) : null}
      <CallViewBody
        call={call.data}
        liveRevision={liveTurns.data?.revision}
        liveTurns={liveTurns.data?.turns}
        liveAxisOrigin={liveTurns.data?.axis_origin}
        liveAxisOriginUnixNano={liveTurns.data?.axis_origin_unix_nano}
        LinkComponent={LinkComponent}
      />
    </>
  );
}

function Facts({ projection }: { projection: CallProjection }) {
  const cost = costReadout(projection.cost);
  const facts: [string, string][] = [
    ["Agent", projection.agent ?? "—"],
    ["Framework", projection.framework ?? "—"],
    ["Outcome", projection.outcome ?? "—"],
    ["Duration", formatSeconds(projection.duration_ms)],
    ["Turns", `${projection.turn_count} (${projection.measurable_turns} measurable)`],
    [
      "Reported latency",
      `p50 ${formatSeconds(projection.reported_latency_p50_ms)} · max ${formatSeconds(projection.reported_latency_max_ms)}`,
    ],
    [
      "Audible gap",
      `p50 ${formatSeconds(projection.audible_gap_p50_ms)} · max ${formatSeconds(projection.audible_gap_max_ms)}`,
    ],
    ["Cost", cost.estimate ? `${cost.amount} ${cost.estimate}` : cost.amount],
  ];
  return (
    <dl className={styles.headFacts}>
      {facts.map(([label, value]) => (
        <div key={label} className={styles.fact}>
          <dt>{label}</dt>
          <dd>{value}</dd>
        </div>
      ))}
      {projection.tags.length > 0 ? (
        <div className={styles.fact}>
          <dt>Tags</dt>
          <dd>
            {projection.tags.map((tag) => (
              <span key={tag} className={styles.tag}>
                {tag}
              </span>
            ))}
          </dd>
        </div>
      ) : null}
    </dl>
  );
}

/** The span transcript and recordings from the latest Call-detail read. Open Calls keep
 * this text as a snapshot until detail is loaded again. No analysis or cost without a
 * projection. */
function SpanTranscript({
  call,
  axisOriginUnixNano,
}: {
  call: CallDetail;
  axisOriginUnixNano?: string | null;
}) {
  const headingId = useId();
  const origin = BigInt(axisOriginUnixNano ?? call.first_evidence_unix_nano);
  return (
    <>
      <section className={styles.panel} aria-labelledby={headingId}>
        <h2 id={headingId} className={styles.panelTitle}>
          Transcript from spans
        </h2>
        {call.transcript_coverage === "not_captured_by_preset" ? (
          <p className={styles.note}>
            The project&apos;s capture preset keeps no transcript text for this call.
          </p>
        ) : call.transcript.length === 0 ? (
          <p className={styles.note}>No transcript has been observed for this call.</p>
        ) : (
          <ol className={styles.transcript}>
            {call.transcript.map((line) => (
              <li key={`${line.trace_id}:${line.span_id}`} className={styles.line}>
                <div className={styles.lineStatic}>
                  <span className={styles.lineTime}>
                    {line.start_unix_nano === null
                      ? "—"
                      : formatClock(
                          Number((BigInt(line.start_unix_nano) - origin) / 1_000_000n),
                        )}
                  </span>
                  <span className={styles.speaker} data-speaker={line.speaker}>
                    {line.speaker}
                  </span>
                  <span className={styles.lineText}>{line.text}</span>
                </div>
              </li>
            ))}
          </ol>
        )}
      </section>
      {call.recordings.length > 0 ? (
        <section className={styles.panel} aria-label="Recordings">
          <h2 className={styles.panelTitle}>Recordings</h2>
          <ul className={styles.recordings}>
            {call.recordings.map((recording) => (
              <li key={recording.recording_id}>
                <span className={styles.mono}>{recording.recording_id}</span> ·{" "}
                {recording.channel_speakers?.join(" + ") ?? recording.speaker} ·{" "}
                {recording.state} · {formatSeconds(recording.duration_ms)}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </>
  );
}

export function CallViewBody({
  call,
  liveRevision,
  liveTurns,
  liveAxisOrigin,
  liveAxisOriginUnixNano,
  LinkComponent = AnchorLink,
}: {
  call: CallDetail;
  liveRevision?: number | null;
  liveTurns?: CallTurn[];
  liveAxisOrigin?: CallLiveTurns["axis_origin"];
  liveAxisOriginUnixNano?: string | null;
  LinkComponent?: ViewerLinkComponent;
}) {
  const projection = call.projection;
  const liveTurnsAreNewer =
    projection != null && liveRevision != null && liveRevision > projection.revision;
  const showLiveTurns =
    call.state === "open" ||
    projection == null ||
    call.projection_state !== "current" ||
    liveTurnsAreNewer;
  // A live response can be rebased or placed on a newly selected recording
  // timeline while the durable projection still owns the audio and transcript.
  // Only combine them once the closed Call and projection identify the same
  // revision and exact axis. Otherwise keep live turns on their own axis and
  // withhold the projection's media until the matching projection is ready.
  const liveTurnsMatchProjection =
    projection != null &&
    call.state === "closed" &&
    call.projection_state === "current" &&
    liveTurns != null &&
    liveRevision === projection.revision &&
    liveAxisOriginUnixNano === projection.axis_origin_unix_nano;
  const showUnalignedLiveTurns =
    projection != null && showLiveTurns && liveTurns != null && !liveTurnsMatchProjection;
  const turns =
    projection == null || showUnalignedLiveTurns
      ? (liveTurns ?? [])
      : liveTurnsMatchProjection
        ? (liveTurns ?? projection.turns)
        : projection.turns;
  const axisOrigin =
    showLiveTurns &&
    liveAxisOrigin != null &&
    (projection == null || showUnalignedLiveTurns)
      ? liveAxisOrigin
      : (projection?.axis_origin ?? null);
  const closedWithoutRevision = call.state === "closed" && call.latest_revision == null;
  const [clock, setClock] = useState<MediaClock | null>(null);
  const [openSpan, setOpenSpan] = useState<TurnSpan | null>(null);
  const layout = useRef<HTMLDivElement>(null);
  const callTimeline = useRef<HTMLDivElement>(null);
  const unalignedNotice = useRef<HTMLParagraphElement>(null);
  const hadProjection = useRef(projection != null);
  const timelineHadFocus = useRef(false);
  const wasUnaligned = useRef(false);
  const previousLiveAxisOrigin = useRef<string | null>(null);
  const [timelineNotice, setTimelineNotice] = useState("");
  const [axisRebaseCount, setAxisRebaseCount] = useState(0);
  const opener = useRef<HTMLElement | null>(null);
  const openerSpanRef = useRef<string | null>(null);
  const restoreFocus = useRef(false);

  const open = useCallback((span: TurnSpan, element: HTMLElement) => {
    opener.current = element;
    openerSpanRef.current = `${span.trace_id}:${span.span_id}`;
    setOpenSpan(span);
  }, []);
  const close = useCallback(() => {
    restoreFocus.current = true;
    setOpenSpan(null);
  }, []);
  useEffect(() => {
    if (openSpan == null && restoreFocus.current) {
      restoreFocus.current = false;
      const openerElement = opener.current;
      const replacement = Array.from(
        layout.current?.querySelectorAll<HTMLElement>("[data-span-ref]") ?? [],
      ).find((element) => element.dataset.spanRef === (openerSpanRef.current ?? ""));
      (openerElement?.isConnected
        ? openerElement
        : (replacement ?? layout.current)
      )?.focus();
    }
  }, [openSpan]);
  useEffect(() => {
    const wasUnalignedBefore = wasUnaligned.current;
    if (showUnalignedLiveTurns && !wasUnalignedBefore) {
      setTimelineNotice(
        `Live turns use the latest Call axis. Audio and transcript from revision ${projection?.revision} are hidden until a projection on this axis is ready.`,
      );
      if (timelineHadFocus.current) unalignedNotice.current?.focus();
    } else if (!showUnalignedLiveTurns && wasUnalignedBefore) {
      setTimelineNotice(
        projection != null && liveTurnsMatchProjection
          ? "Audio and transcript now match the current turn axis."
          : projection == null
            ? "Live turns now use the current Call axis."
            : "The Call timeline has updated.",
      );
      if (timelineHadFocus.current) unalignedNotice.current?.focus();
    }
    wasUnaligned.current = showUnalignedLiveTurns;
  }, [liveTurnsMatchProjection, projection?.revision, showUnalignedLiveTurns]);
  useEffect(() => {
    const hasProjection = projection != null;
    const projectionArrived = !hadProjection.current && hasProjection;
    if (projectionArrived && timelineHadFocus.current) {
      const active = document.activeElement;
      if (active === document.body || active == null || !active.isConnected) {
        callTimeline.current?.querySelector<HTMLElement>("[data-turns-heading]")?.focus();
      }
    }
    hadProjection.current = hasProjection;
  }, [projection != null]);
  useEffect(() => {
    const origin = liveAxisOriginUnixNano;
    if (origin == null) return;
    const previous = previousLiveAxisOrigin.current;
    previousLiveAxisOrigin.current = origin;
    if (previous != null && previous !== origin) {
      setAxisRebaseCount((count) => count + 1);
    }
  }, [liveAxisOriginUnixNano]);

  const revision = call.latest_revision;
  const analysisNote = projection == null ? null : callAnalysisNote(projection);

  return (
    <div
      ref={layout}
      className={styles.layout}
      data-drawer={openSpan ? "open" : undefined}
      tabIndex={-1}
      onFocusCapture={(event) => {
        timelineHadFocus.current =
          callTimeline.current?.contains(event.target as Node) ?? false;
      }}
      onBlurCapture={(event) => {
        const nextTarget = event.relatedTarget;
        const alignmentTransition = showUnalignedLiveTurns !== wasUnaligned.current;
        const projectionTransition = hadProjection.current !== (projection != null);
        if (
          (alignmentTransition || projectionTransition) &&
          (nextTarget == null || nextTarget === document.body)
        ) {
          return;
        }
        if (nextTarget instanceof Node) {
          timelineHadFocus.current = callTimeline.current?.contains(nextTarget) ?? false;
        } else {
          timelineHadFocus.current = false;
        }
      }}
    >
      <div className={styles.page}>
        <header className={styles.head}>
          <nav className={styles.crumbs} aria-label="Breadcrumb">
            <LinkComponent href="/calls" className={styles.crumb}>
              Calls
            </LinkComponent>
          </nav>
          <h1 className={styles.title}>
            Call <span className={styles.mono}>{call.session_key ?? call.call_id}</span>
          </h1>
          <p className={styles.subtitle}>
            <span className={styles.mono}>{call.call_id}</span> · {call.state} · first
            evidence {formatRelativeTime(call.first_evidence_unix_nano)} ·{" "}
            {call.span_count} spans · {call.recording_count} recordings
            {revision ? (
              <>
                {" · "}
                <LinkComponent
                  href={`/sessions/${encodeURIComponent(revision.bundle_id)}`}
                  className={styles.link}
                >
                  revision {revision.revision} incident
                </LinkComponent>
              </>
            ) : null}
          </p>
          {projection ? <Facts projection={projection} /> : null}
        </header>

        {call.materialize_error ? (
          <p className={styles.alert} role="alert">
            The latest revision could not be built: {call.materialize_error}
          </p>
        ) : null}
        {call.evidence_after_latest_revision ? (
          <p className={styles.notice}>
            Evidence arrived after the latest revision; the next revision will include it.
          </p>
        ) : null}
        {call.projection_state === "stale" && projection ? (
          <p className={styles.notice}>
            Being recomputed: this view is from revision {projection.revision}, and the
            call or its prices changed since.
          </p>
        ) : null}
        <p
          ref={unalignedNotice}
          className={timelineNotice ? styles.notice : styles.screenReaderOnly}
          role="status"
          aria-live="polite"
          tabIndex={-1}
        >
          {timelineNotice}
        </p>
        <p className={styles.screenReaderOnly} role="status" aria-live="polite">
          {axisRebaseCount === 0
            ? ""
            : `The live time axis changed ${axisRebaseCount === 1 ? "once" : `${axisRebaseCount} times`}. Turn times were updated.`}
        </p>

        <div ref={callTimeline}>
          {projection == null ? (
            <>
              <p className={styles.notice}>
                {closedWithoutRevision
                  ? "This call closed without a Revision, so turns stay unavailable until one captures the accepted evidence. The transcript below is read from stored spans."
                  : call.state === "open"
                    ? "This call is not projected yet, so it has no audio analysis or cost. Below are accepted turns and a span-transcript snapshot. Live turns update automatically; reload this view to read newer transcript lines."
                    : "This call is not projected yet, so it has no audio analysis or cost. Below are the accepted turns and transcript from its spans."}
              </p>
              {liveTurns != null ? (
                <TurnsTable
                  key={call.call_id}
                  turns={liveTurns}
                  clock={clock}
                  openSpanId={openSpan?.span_id ?? null}
                  onOpenSpan={open}
                  emptyMessage={
                    call.state === "open"
                      ? "Waiting for the first turn."
                      : closedWithoutRevision
                        ? "Turns are unavailable until a Revision captures the accepted evidence."
                        : "No parent turn spans were captured for this call."
                  }
                />
              ) : null}
              <SpanTranscript call={call} axisOriginUnixNano={liveAxisOriginUnixNano} />
            </>
          ) : (
            <>
              {analysisNote ? <p className={styles.notice}>{analysisNote}</p> : null}
              <div>
                {showUnalignedLiveTurns ? (
                  <TurnsTable
                    key={call.call_id}
                    turns={turns}
                    clock={null}
                    openSpanId={openSpan?.span_id ?? null}
                    onOpenSpan={open}
                  />
                ) : (
                  <>
                    {projection.media.map((media, index) => (
                      <Player
                        key={media.content_path}
                        media={media}
                        label={
                          projection.media.length === 1
                            ? "Recording"
                            : `Recording ${index + 1} of ${projection.media.length}${index === 0 ? " (the transcript follows this one)" : ""}`
                        }
                        onClock={index === 0 ? setClock : undefined}
                      />
                    ))}
                    <div className={styles.columns}>
                      <PlacedTranscript lines={projection.transcript} clock={clock} />
                      <TurnsTable
                        key={call.call_id}
                        turns={turns}
                        clock={clock}
                        openSpanId={openSpan?.span_id ?? null}
                        onOpenSpan={open}
                      />
                    </div>
                  </>
                )}
              </div>
              <div className={styles.columns}>
                <SignalsPanel signals={projection.signals} />
                <CostPanel cost={projection.cost} items={projection.cost_items} />
              </div>
            </>
          )}
        </div>
        {axisOrigin != null ? (
          <p className={styles.axisNote}>
            Times are on the call&apos;s axis, from{" "}
            {axisOrigin === "recording_start"
              ? "the start of the earliest recording"
              : "the call's first evidence"}
            .
          </p>
        ) : null}
      </div>
      {openSpan ? (
        <div className={styles.drawerCol}>
          <SpanDrawer callId={call.call_id} span={openSpan} onClose={close} />
        </div>
      ) : null}
    </div>
  );
}
