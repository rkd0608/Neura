import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// A static site: built once, served by any web server or CDN. It shares the console's design
// tokens (../console/src/styles/tokens.css), so the product and its site look like one thing.
export default defineConfig({
  plugins: [react()],
  server: { port: 5174, strictPort: true, fs: { allow: [".."] } },
  preview: { port: 4174, strictPort: true },
  build: { target: "es2023" },
});
