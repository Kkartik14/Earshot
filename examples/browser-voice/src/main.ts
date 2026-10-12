import { LogPanel, el } from "./dom.js";
import { startLoopback } from "./loopback.js";
import type { VoiceModeHandle } from "./mode.js";
import { startOpenAiRealtime } from "./openai-realtime.js";
import { DEFAULT_OPENAI_REALTIME_MODEL } from "./realtime-url.js";
import { CaptureSession } from "./session.js";
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
  const apiKey = labeledInput(
    "Project API key (Bearer) — or leave blank for cookie auth",
    {
      type: "password",
      placeholder: "sk-earshot-…",
      autocomplete: "off",
    },
  );
  const csrf = labeledInput("Viewer CSRF token (only for cookie auth)", {
    type: "text",
    placeholder: "optional",
    autocomplete: "off",
  });
  const projectId = labeledInput("Project id header (optional)", {
    type: "text",
    placeholder: "optional",
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
  const status = el("p", { id: "status" }, ["idle"]);
  const logTarget = el("pre", { id: "log" });
  const log = new LogPanel(logTarget);

  app.append(
    el("h1", {}, ["earshot browser voice capture"]),
    el("p", { class: "muted" }, [
      "Captures a real getUserMedia / RTCPeerConnection / AudioContext session " +
        "with @earshot/browser and POSTs it to POST /v1/capture. Metadata only — " +
        "no audio leaves the browser.",
    ]),
    el("fieldset", {}, [
      el("legend", {}, ["Backend"]),
      el("div", { class: "row" }, [endpoint.field, apiKey.field]),
      el("div", { class: "row" }, [csrf.field, projectId.field]),
    ]),
    el("fieldset", {}, [
      el("legend", {}, ["Session"]),
      el("div", { class: "row" }, [modeField]),
      el("div", { class: "row" }, [openaiKey.field, openaiModel.field]),
      el("div", { class: "row" }, [sinkField, gainField]),
    ]),
    el("div", {}, [startBtn, stopBtn]),
    status,
    el("h2", { style: "font-size:1rem" }, ["Log"]),
    logTarget,
  );

  let session: CaptureSession | null = null;
  let handle: VoiceModeHandle | null = null;

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
    status.textContent = "starting…";
    void start().catch((error: unknown) => {
      log.line(`error: ${String(error)}`);
      status.textContent = "error";
      startBtn.removeAttribute("disabled");
    });
  });

  stopBtn.addEventListener("click", () => {
    stopBtn.setAttribute("disabled", "true");
    status.textContent = "stopping…";
    void stop();
  });

  async function start(): Promise<void> {
    session = new CaptureSession({
      endpoint: {
        endpoint: endpoint.input.value.trim(),
        apiKey: apiKey.input.value.trim() || undefined,
        csrfToken: csrf.input.value.trim() || undefined,
        projectId: projectId.input.value.trim() || undefined,
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
    try {
      if (session) await session.stop();
      if (handle) await handle.stop();
    } finally {
      session = null;
      handle = null;
      status.textContent = "idle";
      startBtn.removeAttribute("disabled");
      sink.setAttribute("disabled", "true");
    }
  }
}

boot();
