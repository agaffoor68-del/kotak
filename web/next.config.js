/** @type {import('next').NextConfig} */
module.exports = {
  // The repo holds several lockfiles, so Next would otherwise guess the wrong
  // workspace root and emit `standalone` outside the app.
  outputFileTracingRoot: __dirname,
  output: "standalone",
  reactStrictMode: true,
};
