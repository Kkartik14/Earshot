import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MediaCustodyPanel } from "./MediaCustody";
import type { MediaCustodyView } from "./timeline";

const base: MediaCustodyView = {
  mediaId: "media-1",
  mediaKind: "audio",
  contentType: "audio/wav",
  custodian: "provider.vapi",
  integrity: "opaque_handle",
  digest: null,
  sizeBytes: null,
  integrityNote:
    "no digest — earshot never read these bytes and cannot attest to their integrity",
  coveredMs: 12_000,
  coveredNote: "covered window",
  consent: "granted",
  retention: { expiresAtUnixNano: null, ttlMs: 86_400_000, policyId: null },
  alignment: {
    state: "aligned",
    note: "via relation-media",
    method: "provider_declared",
    uncertaintyMs: 12,
    driftPpm: null,
  },
  locatorUri: "https://media.example.com/recordings/1.wav",
  locatorExpiresNano: null,
};

const view = (overrides: Partial<MediaCustodyView> = {}): MediaCustodyView => ({
  ...base,
  ...overrides,
});

describe("MediaCustodyPanel", () => {
  it("renders nothing for a session that references no media", () => {
    const { container } = render(<MediaCustodyPanel media={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("names the custodian holding the bytes and says earshot does not", () => {
    render(<MediaCustodyPanel media={[view()]} />);

    const panel = within(screen.getByRole("region", { name: /media custody/i }));
    expect(panel.getByText("provider.vapi")).toBeInTheDocument();
    expect(panel.getByText(/holds the bytes; earshot does not/i)).toBeInTheDocument();
    expect(
      screen.getByText(/earshot stores the reference, never the media/i),
    ).toBeInTheDocument();
  });

  it("states plainly that it cannot attest to an opaque handle", () => {
    render(<MediaCustodyPanel media={[view()]} />);

    const panel = within(screen.getByRole("region", { name: /media custody/i }));
    expect(panel.getByText("opaque handle")).toBeInTheDocument();
    expect(panel.getByText("no digest")).toBeInTheDocument();
    expect(panel.getByText(/cannot attest to their integrity/i)).toBeInTheDocument();
  });

  it("attributes a declared digest to its producer rather than to earshot", () => {
    render(
      <MediaCustodyPanel
        media={[
          view({
            integrity: "content_digest",
            digest: "a".repeat(64),
            sizeBytes: 4096,
            integrityNote:
              "digest declared by the producer — earshot did not read these bytes and has not verified it",
          }),
        ]}
      />,
    );

    const panel = within(screen.getByRole("region", { name: /media custody/i }));
    expect(panel.getByText(/^sha256 aaaaaaaaaaaa/)).toBeInTheDocument();
    expect(panel.getByText(/earshot did not read these bytes/i)).toBeInTheDocument();
  });

  it("reports the declared calibration and its own error bound", () => {
    render(<MediaCustodyPanel media={[view()]} />);

    const panel = within(screen.getByRole("region", { name: /media custody/i }));
    expect(panel.getByText("aligned")).toBeInTheDocument();
    expect(
      panel.getByText(/provider declared ±12ms · via relation-media/),
    ).toBeInTheDocument();
  });

  it("says a calibration with no declared bound is unknown, not zero", () => {
    render(
      <MediaCustodyPanel
        media={[
          view({
            alignment: {
              state: "aligned",
              note: "via relation-media",
              method: "manual_operator",
              uncertaintyMs: null,
              driftPpm: null,
            },
          }),
        ]}
      />,
    );

    expect(screen.getByText(/uncertainty not declared/i)).toBeInTheDocument();
    expect(screen.queryByText(/±0ms/)).toBeNull();
  });

  it("refuses to imply an overlay when nothing aligns the media", () => {
    render(
      <MediaCustodyPanel
        media={[
          view({
            alignment: {
              state: "unaligned",
              note: "no declared clock relation reaches this session's timeline, so no offset is assumed",
            },
          }),
        ]}
      />,
    );

    expect(screen.getByText("cannot align")).toBeInTheDocument();
    expect(screen.getByText(/no offset is assumed/i)).toBeInTheDocument();
  });

  it("surfaces the governance the artifact declares", () => {
    render(<MediaCustodyPanel media={[view()]} />);

    const panel = within(screen.getByRole("region", { name: /media custody/i }));
    expect(panel.getByText("granted")).toBeInTheDocument();
    expect(panel.getByText(/ttl 86400\.0s/)).toBeInTheDocument();
  });

  it("does not fill undeclared governance with a reassuring default", () => {
    render(<MediaCustodyPanel media={[view({ consent: null, retention: null })]} />);

    expect(screen.getByText(/consent not declared/i)).toBeInTheDocument();
    expect(screen.getByText(/no retention policy declared/i)).toBeInTheDocument();
  });

  it("never emits a media src, so rendering fetches nothing", () => {
    // The load-bearing guarantee: an <audio src>/<img src> would make the viewer
    // request the media on render, turning earshot into a media path and leaking
    // the reader's network position without anyone asking for playback.
    const { container } = render(<MediaCustodyPanel media={[view()]} />);

    expect(
      container.querySelectorAll("audio, video, img, iframe, source, embed"),
    ).toHaveLength(0);
    expect(container.querySelectorAll("[src]")).toHaveLength(0);
    expect(container.innerHTML).not.toMatch(/\ssrc=/);
  });

  it("offers the locator only as a user-initiated, direct hand-off", () => {
    render(<MediaCustodyPanel media={[view()]} />);

    const link = screen.getByRole("link", { name: /open at the custodian/i });
    expect(link).toHaveAttribute("href", "https://media.example.com/recordings/1.wav");
    // Opens away from the viewer, leaks no referrer, and shares no opener handle.
    expect(link).toHaveAttribute("target", "_blank");
    expect(link.getAttribute("rel")).toMatch(/noreferrer/);
    expect(link.getAttribute("rel")).toMatch(/noopener/);
    expect(link).toHaveAttribute("referrerpolicy", "no-referrer");
    expect(
      screen.getByText(/does not fetch, proxy, cache, or play this media/i),
    ).toBeInTheDocument();
  });

  it("says there is nowhere to go when no locator is declared", () => {
    render(<MediaCustodyPanel media={[view({ locatorUri: null })]} />);

    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText(/No locator is declared/i)).toBeInTheDocument();
  });
});
