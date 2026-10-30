// Where the console's links outside itself go, set at build time (VITE_* environment variables,
// the names the NucleiLabs site uses for the same addresses). The Dockerfile declares each one and
// every build for deployment passes it (tests/tools/test_web_build_urls.py).

/** The developer docs: a deployment's own site (deploy/gcp/deploy.sh), else the public docs. */
export const DOCS_URL: string =
  (import.meta.env.VITE_DOCS_URL as string | undefined) ?? "https://nucleilabs.ai/docs/";
