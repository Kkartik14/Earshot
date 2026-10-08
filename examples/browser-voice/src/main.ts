/**
 * The runnable browser voice app.
 *
 * It builds a small form, then starts a real capture session in one of two
 * modes — local WebRTC loopback, or a raw OpenAI Realtime WebRTC connection —
 * and POSTs the captured telemetry to the earshot backend's `POST /v1/capture`.
 * Everything below drives real browser objects; nothing fabricates a metric.
 *
 * Only Chrome has been exercised against this app in-repo; the Safari/Firefox
 * and real-hardware paths are made to EXIST and run here, but are not claimed as
 * validated. See README.md.
 */

import { LogPanel, el } from "./dom.js";
import { startLoopback } from "./loopback.js";
import type { VoiceModeHandle } from "./mode.js";
import { startOpenAiRealtime } from "./openai-realtime.js";
import { DEFAULT_OPENAI_REALTIME_MODEL } from "./realtime-url.js";
import { CaptureSession } from "./session.js";
import { isFinalPayloadReplaceable, isTerminalCapture413 } from "./session.js";
import type { CaptureSessionFinishResult } from "./session.js";
import { listOutputDevices, switchSink } from "./sinks.js";

function labeledInput(
  labelText: string,
  attrs: Partial<Record<string, string>>,
): { field: HTMLDivElement; input: HTMLInputElement } {
  const input = el("input", attrs);
  const field = el("div", {}, [el("label", {}, [labelText]), input]);
  return { field, input };
}

function option(value: string, label: string): HTMLOptionElement {
  return el("option", { value }, [label]);
}

function boot(): void {
  const app = document.getElementById("app");
  if (!app) throw new Error("missing #app root");

  const endpoint = labeledInput("Capture endpoint (POST /v1/capture)", {
    type: "url",
    value: "/v1/capture",
  });
  const csrf = labeledInput("Host BFF CSRF token (if required)", {
    type: "text",
    placeholder: "optional",
    autocomplete: "off",
  });
  const projectId = labeledInput("Project id assertion", {
    type: "text",
    value: "default",
    required: "true",
  });
  const authContextId = labeledInput("Opaque auth context id assertion", {
    type: "text",
    value: crypto.randomUUID(),
    required: "true",
    autocomplete: "off",
  });

  const mode = el("select", {}, [
    option("loopback", "Local WebRTC loopback (no key needed)"),
    option("openai", "Raw OpenAI Realtime (WebRTC)"),
  ]);
  const modeField = el("div", {}, [el("label", {}, ["Mode"]), mode]);

  const openaiKey = labeledInput(
    "OpenAI Realtime key (ephemeral client secret — runtime only)",
    {
      type: "password",
      placeholder: "read at runtime; never committed",
      autocomplete: "off",
    },
  );
  const openaiModel = labeledInput("OpenAI Realtime model", {
    type: "text",
    value: DEFAULT_OPENAI_REALTIME_MODEL,
  });

  const sink = el("select", { disabled: "true" }, [option("", "default output")]);
  const sinkField = el("div", {}, [el("label", {}, ["Output device (setSinkId)"]), sink]);

  const gain = el("input", {
    type: "range",
    min: "0",
    max: "1",
    step: "0.05",
    value: "0",
  });
  const gainField = el("div", {}, [el("label", {}, ["Monitor volume"]), gain]);

  const startBtn = el("button", {}, ["Start capture"]);
  const stopBtn = el("button", { disabled: "true" }, ["Stop"]);
  const retryBtn = el("button", { disabled: "true" }, ["Retry delivery"]);
  const reduceBtn = el("button", { disabled: "true", hidden: "true" }, [
    "Drop snapshots/events; send final marker",
  ]);
  const status = el("p", { id: "status" }, ["idle"]);
  const logTarget = el("pre", { id: "log" });
  const log = new LogPanel(logTarget);

  app.append(
    el("h1", {}, ["earshot browser voice capture"]),
    el("p", { class: "muted" }, [
      "Captures a real getUserMedia / RTCPeerConnection / AudioContext session " +
        "with @earshot/browser and POSTs telemetry metadata only to /v1/capture. " +
        "Loopback audio stays in the browser; OpenAI Realtime sends microphone " +
        "audio to OpenAI for processing.",
    ]),
    el("fieldset", {}, [
      el("legend", {}, ["Backend"]),
      el("div", { class: "row" }, [endpoint.field]),
      el("div", { class: "row" }, [csrf.field, projectId.field]),
      el("div", { class: "row" }, [authContextId.field]),
    ]),
    el("fieldset", {}, [
      el("legend", {}, ["Session"]),
      el("div", { class: "row" }, [modeField]),
      el("div", { class: "row" }, [openaiKey.field, openaiModel.field]),
      el("div", { class: "row" }, [sinkField, gainField]),
    ]),
    el("div", {}, [startBtn, stopBtn, retryBtn, reduceBtn]),
    status,
    el("h2", { style: "font-size:1rem" }, ["Log"]),
    logTarget,
  );

  let session: CaptureSession | null = null;
  let handle: VoiceModeHandle | null = null;
  let finalizationHadError = false;

  async function populateSinks(active: VoiceModeHandle): Promise<void> {
    sink.replaceChildren(option("", "default output"));
    if (!active.supportsSinkSwitch) {
      sink.setAttribute("disabled", "true");
      log.line(
        "output-device switching unavailable in this browser " +
          "(AudioContext.setSinkId missing) — the SDK will record no sink_change",
      );
      return;
    }
    const devices = await listOutputDevices();
    for (const device of devices) {
      sink.append(
        option(device.deviceId, device.label || `output ${device.deviceId.slice(0, 8)}`),
      );
    }
    sink.removeAttribute("disabled");
    log.line(`found ${devices.length} output device(s); switching is supported`);
  }

  sink.addEventListener("change", () => {
    if (!handle || sink.value === "") return;
    void switchSink(handle.audioCtx, sink.value)
      .then(() => log.line(`switched output sink; SDK will capture the sinkchange`))
      .catch((error: unknown) => log.line(`setSinkId failed: ${String(error)}`));
  });

  gain.addEventListener("input", () => {
    handle?.setMonitorGain(Number(gain.value));
  });

  startBtn.addEventListener("click", () => {
    startBtn.setAttribute("disabled", "true");
    retryBtn.setAttribute("disabled", "true");
    reduceBtn.setAttribute("disabled", "true");
    reduceBtn.setAttribute("hidden", "true");
    finalizationHadError = false;
    status.textContent = "starting…";
    void start().catch(async (error: unknown) => {
      const failedHandle = handle;
      const failedSession = session;
      handle = null;
      try {
        await failedHandle?.stop();
      } catch (cleanupError) {
        log.line(`voice mode cleanup failed: ${String(cleanupError)}`);
      }
      let cleanupResult: CaptureSessionFinishResult | undefined;
      try {
        cleanupResult = await failedSession?.stop();
      } catch (cleanupError) {
        log.line(`capture cleanup failed: ${String(cleanupError)}`);
      }
      log.line(`error: ${String(error)}`);
      finalizationHadError = true;
      sink.setAttribute("disabled", "true");
      if (failedSession && cleanupResult && cleanupResult.pendingExactRetryCount > 0) {
        session = failedSession;
        status.textContent = "delivery pending (startup failed)";
        retryBtn.removeAttribute("disabled");
        return;
      }
      if (failedSession && cleanupResult && isFinalPayloadReplaceable(cleanupResult)) {
        session = failedSession;
        status.textContent = "final payload too large (startup failed)";
        reduceBtn.removeAttribute("disabled");
        reduceBtn.removeAttribute("hidden");
        return;
      }
      if (cleanupResult && isTerminalCapture413(cleanupResult)) {
        log.line(
          "the terminal capture has no smaller replacement; repair or delete it through the host",
        );
        session = null;
        status.textContent = "host repair required (startup failed)";
        startBtn.removeAttribute("disabled");
        return;
      }
      session = null;
      status.textContent = "error";
      startBtn.removeAttribute("disabled");
    });
  });

  stopBtn.addEventListener("click", () => {
    stopBtn.setAttribute("disabled", "true");
    status.textContent = "stopping…";
    void stop();
  });

  retryBtn.addEventListener("click", () => {
    if (!session) return;
    retryBtn.setAttribute("disabled", "true");
    status.textContent = "retrying delivery…";
    void retryPendingDelivery();
  });

  reduceBtn.addEventListener("click", () => {
    if (!session) return;
    reduceBtn.setAttribute("disabled", "true");
    status.textContent = "sending reduced final marker…";
    void retryReducedFinalPayload();
  });

  async function start(): Promise<void> {
    session = new CaptureSession({
      endpoint: {
        endpoint: endpoint.input.value.trim(),
        csrfToken: csrf.input.value.trim() || undefined,
        projectId: projectId.input.value.trim(),
        getAuthContextId: () => authContextId.input.value.trim(),
      },
      log: (message) => log.line(message),
      onDrain: (payload) => log.json("drained payload:", payload),
    });

    handle =
      mode.value === "openai"
        ? await startOpenAiRealtime(session, {
            apiKey: openaiKey.input.value,
            model: openaiModel.input.value,
            log: (message) => log.line(message),
          })
        : await startLoopback(session, (message) => log.line(message));

    handle.setMonitorGain(Number(gain.value));
    await populateSinks(handle);
    session.start();
    status.textContent = `capturing (${mode.value})`;
    stopBtn.removeAttribute("disabled");
  }

  async function stop(): Promise<void> {
    let failure: unknown;
    let finalization: CaptureSessionFinishResult | undefined;
    const stoppedSession = session;
    try {
      if (handle) await handle.stop();
    } catch (error) {
      failure = error;
      log.line(`voice mode shutdown failed: ${String(error)}`);
    }
    try {
      if (stoppedSession) {
        finalization = await stoppedSession.endCall();
        if (
          !finalization.delivery.delivered &&
          finalization.pendingExactRetryCount === 0 &&
          !isFinalPayloadReplaceable(finalization)
        ) {
          failure ??= new Error("final capture delivery remains incomplete");
        }
        if (!finalization.delivery.delivered) {
          log.line(
            `final capture was not accepted after ${finalization.delivery.attempts} ` +
              `attempt(s); pending exact retries=${finalization.pendingExactRetryCount}`,
          );
        }
        if (finalization.delivery.delivered && finalization.pendingExactRetryCount > 0) {
          log.line(
            `${finalization.pendingExactRetryCount} earlier capture retry/retries remain pending`,
          );
        }
        if (finalization.acceptedObserverFailed) {
          failure ??= new Error("the accepted browser acknowledgement observer failed");
          log.line(
            "capture was accepted, but its browser acknowledgement observer failed",
          );
        }
      }
    } catch (error) {
      failure ??= error;
      log.line(`final capture delivery failed: ${String(error)}`);
    }
    handle = null;
    finalizationHadError = failure !== undefined;
    sink.setAttribute("disabled", "true");
    if (finalization && finalization.pendingExactRetryCount > 0) {
      status.textContent = "delivery pending";
      retryBtn.removeAttribute("disabled");
      return;
    }
    if (finalization && isFinalPayloadReplaceable(finalization)) {
      session = stoppedSession;
      status.textContent = "final payload too large";
      reduceBtn.removeAttribute("disabled");
      reduceBtn.removeAttribute("hidden");
      return;
    }
    if (finalization && isTerminalCapture413(finalization)) {
      log.line(
        "the terminal capture has no smaller replacement; repair or delete it through the host",
      );
      session = null;
      status.textContent = "host repair required";
      retryBtn.setAttribute("disabled", "true");
      startBtn.removeAttribute("disabled");
      return;
    }
    session = null;
    status.textContent = failure ? "error" : "idle";
    retryBtn.setAttribute("disabled", "true");
    startBtn.removeAttribute("disabled");
  }

  async function retryPendingDelivery(): Promise<void> {
    const pendingSession = session;
    if (!pendingSession) return;
    try {
      const result = await pendingSession.retryPending();
      if (result.pendingExactRetryCount > 0) {
        log.line(
          `delivery remains pending; exact retries=${result.pendingExactRetryCount}`,
        );
        status.textContent = "delivery pending";
        retryBtn.removeAttribute("disabled");
        return;
      }
      if (isFinalPayloadReplaceable(result)) {
        log.line(
          "final payload was rejected as too large; send a reduced final marker to preserve the call end",
        );
        status.textContent = "final payload too large";
        reduceBtn.removeAttribute("disabled");
        reduceBtn.removeAttribute("hidden");
        return;
      }
      if (isTerminalCapture413(result)) {
        log.line(
          "the terminal capture has no smaller replacement; repair or delete it through the host",
        );
        status.textContent = "host repair required";
        session = null;
        retryBtn.setAttribute("disabled", "true");
        startBtn.removeAttribute("disabled");
        return;
      }
      if (!result.delivery.delivered) {
        log.line("capture delivery could not be recovered; host repair may be required");
      }
      if (result.acceptedObserverFailed) {
        log.line("capture was accepted, but its browser acknowledgement observer failed");
      }
      session = null;
      retryBtn.setAttribute("disabled", "true");
      startBtn.removeAttribute("disabled");
      status.textContent =
        finalizationHadError ||
        !result.delivery.delivered ||
        result.acceptedObserverFailed
          ? "error"
          : "idle";
    } catch (error) {
      log.line(`delivery retry failed: ${String(error)}`);
      status.textContent = "delivery pending";
      retryBtn.removeAttribute("disabled");
    }
  }

  async function retryReducedFinalPayload(): Promise<void> {
    const pendingSession = session;
    if (!pendingSession) return;
    try {
      const result = await pendingSession.retryFinalWithReducedPayload();
      if (result.pendingExactRetryCount > 0) {
        status.textContent = "delivery pending";
        retryBtn.removeAttribute("disabled");
        reduceBtn.setAttribute("hidden", "true");
        return;
      }
      if (isTerminalCapture413(result)) {
        log.line(
          "the reduced final marker was also rejected; check the endpoint's configured limits and repair the call through the host",
        );
        status.textContent = "host repair required";
        reduceBtn.setAttribute("hidden", "true");
        session = null;
        startBtn.removeAttribute("disabled");
        return;
      }
      if (!result.delivery.delivered) {
        log.line("reduced final delivery failed; host repair may be required");
      }
      if (result.acceptedObserverFailed) {
        log.line("capture was accepted, but its browser acknowledgement observer failed");
      }
      session = null;
      reduceBtn.setAttribute("hidden", "true");
      startBtn.removeAttribute("disabled");
      status.textContent = result.delivery.delivered
        ? finalizationHadError || result.acceptedObserverFailed
          ? "delivered with reduced evidence; see log for other errors"
          : "delivered with reduced evidence"
        : "error";
    } catch (error) {
      log.line(`reduced final delivery could not be built: ${String(error)}`);
      status.textContent = "host repair required";
      reduceBtn.setAttribute("hidden", "true");
      session = null;
      startBtn.removeAttribute("disabled");
    }
  }
}

boot();
