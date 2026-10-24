// NucleiLabs developer docs (design §26). Docs only, built to static HTML: no server, no
// database, no third-party scripts. Every page carries a status label (see src/theme). They are
// served at /docs/ on the NucleiLabs site, in its design (src/css, src/theme/Footer).
import type * as Preset from "@docusaurus/preset-classic";
import type { Config } from "@docusaurus/types";
import { themes as prismThemes } from "prism-react-renderer";

// Where the site's other pages are: the console (sign in) and contact, as the home page has them.
const CONSOLE_URL = process.env.NEURA_CONSOLE_URL ?? "http://127.0.0.1:3000/sign-in";
const CONTACT_URL = process.env.NEURA_CONTACT_URL ?? "mailto:hello@nucleilabs.ai";

const config: Config = {
  title: "NucleiLabs developer docs",
  tagline: "Neura: serving robot policies from the customer's own GPU servers",
  favicon: "img/favicon.svg",
  url: process.env.NEURA_SITE_URL ?? "https://nucleilabs.ai",
  baseUrl: "/docs/",
  organizationName: "rkd0608",
  projectName: "neura",
  trailingSlash: false,
  customFields: { consoleUrl: CONSOLE_URL, contactUrl: CONTACT_URL },

  // The build fails on any broken link, anchor or image (design §26.7).
  onBrokenLinks: "throw",
  onBrokenAnchors: "throw",
  onDuplicateRoutes: "throw",
  markdown: {
    // .md is CommonMark (generated references stay literal); .mdx is MDX.
    format: "detect",
    mermaid: true,
    hooks: { onBrokenMarkdownLinks: "throw", onBrokenMarkdownImages: "throw" },
  },
  i18n: { defaultLocale: "en", locales: ["en"] },

  presets: [
    [
      "classic",
      {
        docs: {
          routeBasePath: "/",
          sidebarPath: "./sidebars.ts",
          showLastUpdateTime: false,
        },
        blog: false,
        theme: {
          // The design system's tokens, shared with the console and the home page.
          customCss: ["../console/src/styles/tokens.css", "./src/css/custom.css"],
        },
      } satisfies Preset.Options,
    ],
  ],

  themes: [
    "@docusaurus/theme-mermaid",
    [
      "@easyops-cn/docusaurus-search-local",
      { hashed: true, docsRouteBasePath: "/", indexBlog: false, highlightSearchTermsOnTargetPage: true },
    ],
  ],

  themeConfig: {
    announcementBar: {
      id: "in-development",
      content:
        "Neura is in early development. Nothing described here is a released product; every page says how far along it is.",
      isCloseable: false,
    },
    colorMode: { defaultMode: "dark", respectPrefersColorScheme: false },
    navbar: {
      title: "NucleiLabs",
      logo: { alt: "NucleiLabs", src: "img/favicon.svg", href: "pathname:///" },
      items: [
        { type: "docSidebar", sidebarId: "docs", position: "left", label: "Docs" },
        { to: "/deploy-a-policy", label: "Deploy a policy", position: "left" },
        { to: "/reference/api", label: "Reference", position: "left" },
        { to: "/status", label: "Status", position: "left" },
        { href: "pathname:///#neura", label: "Neura", position: "left", className: "site-link" },
        { href: CONSOLE_URL, label: "Sign in", position: "right", className: "nav-button ghost" },
        {
          href: "pathname:///#partner",
          label: "Become a design partner",
          position: "right",
          className: "nav-button primary",
        },
      ],
    },
    prism: {
      theme: prismThemes.oneLight,
      darkTheme: prismThemes.oneDark,
      additionalLanguages: ["bash", "protobuf", "yaml", "json"],
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
