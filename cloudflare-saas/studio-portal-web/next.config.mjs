/** @type {import("next").NextConfig} */
const config = {
  output: "export",
  poweredByHeader: false,
  reactStrictMode: true,
  turbopack: { root: import.meta.dirname },
};

export default config;
