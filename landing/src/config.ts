// Where the site's links go. Set them at build time (VITE_* environment variables); the defaults
// are the development stack's console and the developer docs this site serves at /docs/.
export const CONSOLE_URL: string =
  (import.meta.env.VITE_CONSOLE_URL as string | undefined) ?? "http://127.0.0.1:3000/sign-in";
export const DOCS_URL: string = (import.meta.env.VITE_DOCS_URL as string | undefined) ?? "/docs/";
export const CONTACT_URL: string =
  (import.meta.env.VITE_CONTACT_URL as string | undefined) ?? "mailto:hello@nucleilabs.ai";
