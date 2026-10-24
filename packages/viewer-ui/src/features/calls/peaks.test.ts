/// <reference types="node" />
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { parsePeaksDat, PeaksFormatError } from "./peaks";

// Expected peaks come from real server-generated LiveKit and Pipecat recordings, checked with the server reader.
function fixture(name: string): ArrayBuffer {
  const bytes = readFileSync(join(import.meta.dirname, "__fixtures__", name));
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
}

/** The raw 8-bit (min, max) of `channel` at `pixel`, from the parsed lane. */
function pair(lane: Float32Array, pixel: number): [number, number] {
  return [Math.round(lane[2 * pixel] * 128), Math.round(lane[2 * pixel + 1] * 128)];
}

function extremes(lane: Float32Array): { min: number; max: number; loud: number } {
  let min = 0;
  let max = 0;
  let loud = 0;
  for (let pixel = 0; pixel < lane.length / 2; pixel++) {
    const [low, high] = pair(lane, pixel);
    min = Math.min(min, low);
    max = Math.max(max, high);
    if (Math.max(Math.abs(low), Math.abs(high)) >= 8) loud++;
  }
  return { min, max, loud };
}

describe("parsePeaksDat on server-generated files", () => {
  it("reads the stereo recording's header and splits its two lanes", () => {
    const peaks = parsePeaksDat(fixture("livekit-1.8.stereo.peaks.dat"));

    expect(peaks).toMatchObject({
      version: 2,
      bits: 8,
      sampleRate: 48_000,
      samplesPerPixel: 256,
      length: 6376,
    });
    expect(peaks.channels).toHaveLength(2);
    expect(peaks.channels.map((lane) => lane.length)).toEqual([2 * 6376, 2 * 6376]);
    // 6376 pixels of 256 samples at 48 kHz: the 34.004 s the recording decodes to.
    expect(peaks.durationMs).toBeCloseTo(34_005.3, 0);
  });

  it("keeps each channel's own values, in file order", () => {
    const [user, agent] = parsePeaksDat(fixture("livekit-1.8.stereo.peaks.dat")).channels;

    expect(pair(user, 0)).toEqual([0, 0]);
    expect(pair(user, 1000)).toEqual([-1, 0]);
    expect(pair(agent, 1000)).toEqual([-21, 18]);
    expect(extremes(user)).toEqual({ min: -128, max: 127, loud: 1103 });
    expect(extremes(agent)).toEqual({ min: -128, max: 127, loud: 1504 });
  });

  it("reads a mono pair's rendition peaks as a user lane and an agent lane", () => {
    const peaks = parsePeaksDat(fixture("pipecat-1.5.rendition.peaks.dat"));
    const [user, agent] = peaks.channels;

    expect(peaks).toMatchObject({
      sampleRate: 16_000,
      samplesPerPixel: 256,
      length: 2031,
    });
    // 1.6 s in, the user is speaking and the agent is silent; at 17.6 s the reverse.
    expect([pair(user, 100), pair(agent, 100)]).toEqual([
      [-64, 101],
      [0, 0],
    ]);
    expect([pair(user, 1100), pair(agent, 1100)]).toEqual([
      [0, 0],
      [-17, 21],
    ]);
    expect(extremes(user)).toEqual({ min: -113, max: 127, loud: 404 });
    expect(extremes(agent)).toEqual({ min: -122, max: 127, loud: 412 });
  });

  it("scales every value into wavesurfer's -1..1 range", () => {
    for (const lane of parsePeaksDat(fixture("livekit-1.8.stereo.peaks.dat")).channels) {
      for (const value of lane) {
        expect(value).toBeGreaterThanOrEqual(-1);
        expect(value).toBeLessThanOrEqual(1);
      }
    }
  });
});

describe("parsePeaksDat refuses bytes that are not a whole .dat file", () => {
  const stereo = () => fixture("livekit-1.8.stereo.peaks.dat");

  it("a truncated body", () => {
    expect(() => parsePeaksDat(stereo().slice(0, 1000))).toThrow(PeaksFormatError);
  });

  it("a header alone, or less", () => {
    expect(() => parsePeaksDat(stereo().slice(0, 10))).toThrow(PeaksFormatError);
  });

  it("an unknown version", () => {
    const bytes = stereo();
    new DataView(bytes).setInt32(0, 3, true);
    expect(() => parsePeaksDat(bytes)).toThrow(/version/);
  });

  it("a server error body instead of peaks", () => {
    const body = new TextEncoder().encode(
      JSON.stringify({ error: { code: "EARSHOT_PEAKS_NOT_FOUND", message: "no peaks" } }),
    );
    expect(() => parsePeaksDat(body.buffer)).toThrow(PeaksFormatError);
  });
});
