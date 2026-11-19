import type { NextConfig } from "next";
import path from "node:path";

const nextConfig: NextConfig = {
  // Pin the workspace root to this folder; otherwise Next walks up, finds a stray
  // lockfile in the home directory, and misresolves node_modules (same fix as TokenWatch).
  turbopack: { root: path.resolve(".") },
  outputFileTracingRoot: path.resolve("."),
};

export default nextConfig;
