import type { NextConfig } from "next";

const earshotApi = process.env.EARSHOT_API_URL ?? "http://127.0.0.1:4319";
const apiPrefix = process.env.NEXT_PUBLIC_EARSHOT_API_BASE_PATH ?? "";
const normalizedPrefix = apiPrefix === "/" ? "" : apiPrefix.replace(/\/+$/, "");

if (
  normalizedPrefix !== "" &&
  (!/^\/(?:[A-Za-z0-9._~-]+\/?)*$/.test(normalizedPrefix) ||
    normalizedPrefix.startsWith("//") ||
    normalizedPrefix.split("/").some((segment) => segment === "." || segment === ".."))
) {
  throw new Error("NEXT_PUBLIC_EARSHOT_API_BASE_PATH must be a same-origin URL path");
}

const apiPath = (path: string) => `${normalizedPrefix}${path}`;
const upstream = earshotApi.replace(/\/+$/, "");

const nextConfig: NextConfig = {
  transpilePackages: ["@earshot/viewer-ui"],
  async rewrites() {
    return [
      {
        source: apiPath("/v1/:path*"),
        destination: `${upstream}/v1/:path*`,
      },
      { source: apiPath("/healthz"), destination: `${upstream}/healthz` },
      { source: apiPath("/readyz"), destination: `${upstream}/readyz` },
    ];
  },
};

export default nextConfig;
