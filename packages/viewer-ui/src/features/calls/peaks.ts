/** Parses the server's audiowaveform `.dat` peaks, including version 1 and 16-bit variants. */

export class PeaksFormatError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PeaksFormatError";
  }
}

export interface Peaks {
  version: 1 | 2;
  bits: 8 | 16;
  sampleRate: number;
  samplesPerPixel: number;
  /** Pixels per channel. */
  length: number;
  /** Per channel, each pixel's minimum then maximum, scaled to -1..1: the shape
   * wavesurfer draws (it takes the largest magnitude under each drawn pixel). */
  channels: Float32Array[];
  /** What the peaks cover: `length × samplesPerPixel / sampleRate`. */
  durationMs: number;
}

const V1_HEADER_BYTES = 20;
const V2_HEADER_BYTES = 24;

export function parsePeaksDat(buffer: ArrayBuffer): Peaks {
  if (buffer.byteLength < V1_HEADER_BYTES) {
    throw new PeaksFormatError("too short for a .dat header");
  }
  const view = new DataView(buffer);
  const version = view.getInt32(0, true);
  if (version !== 1 && version !== 2) {
    throw new PeaksFormatError(`unsupported .dat version ${version}`);
  }
  if (version === 2 && buffer.byteLength < V2_HEADER_BYTES) {
    throw new PeaksFormatError("too short for a version 2 header");
  }
  const bits = (view.getUint32(4, true) & 1) === 1 ? 8 : 16;
  const sampleRate = view.getInt32(8, true);
  const samplesPerPixel = view.getInt32(12, true);
  const length = view.getUint32(16, true);
  const channelCount = version === 2 ? view.getInt32(20, true) : 1;
  if (sampleRate <= 0 || samplesPerPixel <= 0 || channelCount <= 0) {
    throw new PeaksFormatError(
      "the header's rate, pixel size or channel count is not positive",
    );
  }
  const offset = version === 2 ? V2_HEADER_BYTES : V1_HEADER_BYTES;
  const valueBytes = bits / 8;
  const values = 2 * length * channelCount;
  if (buffer.byteLength !== offset + values * valueBytes) {
    throw new PeaksFormatError("the body does not match the header");
  }

  const raw =
    bits === 8
      ? new Int8Array(buffer, offset, values)
      : readInt16LittleEndian(view, offset, values);
  const scale = bits === 8 ? 128 : 32_768;
  const channels = Array.from(
    { length: channelCount },
    () => new Float32Array(2 * length),
  );
  for (let pixel = 0; pixel < length; pixel++) {
    for (let channel = 0; channel < channelCount; channel++) {
      const at = 2 * (pixel * channelCount + channel);
      channels[channel][2 * pixel] = raw[at] / scale;
      channels[channel][2 * pixel + 1] = raw[at + 1] / scale;
    }
  }
  return {
    version,
    bits,
    sampleRate,
    samplesPerPixel,
    length,
    channels,
    durationMs: (length * samplesPerPixel * 1000) / sampleRate,
  };
}

/** 16-bit values with no alignment requirement on `offset`, little-endian on any host. */
function readInt16LittleEndian(
  view: DataView,
  offset: number,
  count: number,
): Int16Array {
  const out = new Int16Array(count);
  for (let index = 0; index < count; index++) {
    out[index] = view.getInt16(offset + 2 * index, true);
  }
  return out;
}
