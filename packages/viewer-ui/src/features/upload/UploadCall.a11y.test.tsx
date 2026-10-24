import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { UploadCall } from "./UploadCall";

// The form's own checks and its accessibility. Every case here is refused before
// any request is made; what the server answers is tested against a real server in
// `upload.server.test.tsx`.

function file(name: string, type: string, size = 4): File {
  return new File([new Uint8Array(size)], name, { type });
}

function choose(label: string | RegExp, chosen: File | null) {
  fireEvent.change(screen.getByLabelText(label), {
    target: { files: chosen == null ? [] : [chosen] },
  });
}

const submit = () => fireEvent.click(screen.getByRole("button", { name: "Upload call" }));

describe("Upload a call form", () => {
  it("is a named form whose every control has a label and a description", () => {
    render(<UploadCall />);

    expect(
      screen.getByRole("heading", { level: 1, name: "Upload a call" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("form", { name: "Upload a call" })).toBeInTheDocument();
    expect(screen.getByLabelText("Call audio")).toHaveAccessibleDescription(
      "WAV, FLAC, Ogg Opus, MP3 or WebM.",
    );
    expect(
      screen.getByRole("combobox", { name: "Channels" }),
    ).toHaveAccessibleDescription(/Two channels need a WAV, FLAC or Ogg Opus file/);
    expect(screen.getByLabelText("Transcript (optional)")).toHaveAccessibleDescription(
      /WebVTT \(\.vtt\) keeps each line's time/,
    );
    expect(
      screen.getByRole("textbox", { name: "Session key (optional)" }),
    ).toHaveAccessibleDescription(/Leave it empty to start a new call/);
    expect(screen.getByLabelText("Call audio")).toBeRequired();
  });

  it("asks for the audio in place and moves focus to it", () => {
    render(<UploadCall />);

    submit();

    const audio = screen.getByLabelText("Call audio");
    expect(audio).toHaveAttribute("aria-invalid", "true");
    expect(audio).toHaveAccessibleDescription(
      "WAV, FLAC, Ogg Opus, MP3 or WebM. Choose the call's audio file.",
    );
    expect(audio).toHaveFocus();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("");
  });

  it("refuses audio Earshot cannot store, and a two-channel layout it cannot check", () => {
    render(<UploadCall />);

    choose("Call audio", file("notes.pdf", "application/pdf"));
    submit();
    expect(screen.getByLabelText("Call audio")).toHaveAccessibleDescription(
      "WAV, FLAC, Ogg Opus, MP3 or WebM. Choose a WAV, FLAC, Ogg Opus, MP3 or WebM file.",
    );

    choose("Call audio", file("call.mp3", "audio/mpeg"));
    fireEvent.change(screen.getByRole("combobox", { name: "Channels" }), {
      target: { value: "user-agent" },
    });
    submit();
    const channels = screen.getByRole("combobox", { name: "Channels" });
    expect(screen.getByLabelText("Call audio")).not.toHaveAttribute("aria-invalid");
    expect(channels).toHaveAttribute("aria-invalid", "true");
    expect(channels).toHaveAccessibleDescription(
      /Two channels need a WAV, FLAC or Ogg Opus file\.$/,
    );
    expect(channels).toHaveFocus();
  });

  it("refuses a transcript that is neither WebVTT nor plain text", () => {
    render(<UploadCall />);
    choose("Call audio", file("call.wav", "audio/wav"));

    choose("Transcript (optional)", file("call.srt", "application/x-subrip"));
    submit();

    const transcript = screen.getByLabelText("Transcript (optional)");
    expect(transcript).toHaveAttribute("aria-invalid", "true");
    expect(transcript).toHaveAccessibleDescription(
      /Choose a WebVTT \(\.vtt\) or plain text \(\.txt\) file\.$/,
    );
    expect(transcript).toHaveFocus();
  });

  it("refuses a session key longer than 256 bytes, counting UTF-8", () => {
    render(<UploadCall />);
    choose("Call audio", file("call.wav", "audio/wav"));
    const key = screen.getByRole("textbox", { name: "Session key (optional)" });

    // 129 two-byte characters: 258 bytes, though only 129 characters.
    fireEvent.change(key, { target: { value: "é".repeat(129) } });
    submit();

    expect(key).toHaveAttribute("aria-invalid", "true");
    expect(key).toHaveAccessibleDescription(/Use at most 256 bytes\.$/);
    expect(key).toHaveFocus();
  });
});
