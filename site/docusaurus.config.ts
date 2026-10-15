// NucleiLabs developer docs (design §26). Docs only, built to static HTML: no server, no
// database, no third-party scripts. Every page carries a status label (see src/theme).
import type * as Preset from "@docusaurus/preset-classic";
import type { Config } from "@docusaurus/types";
import { themes as prismThemes } from "prism-react-renderer";

const config: Config = {
  title: "NucleiLabs developer docs",
  tagline: "Neura: serving robot policies from the customer's own GPU servers",
  favicon: "img/favicon.svg",
  url: process.env.NEURA_DOCS_URL ?? "https://rkd0608.github.io",
  baseUrl: process.env.NEURA_DOCS_BASE_URL ?? "/neura/",
  organizationName: "rkd0608",
  projectName: "neura",
  trailingSlash: false,

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
          editUrl: "https://github.com/rkd0608/neura/edit/main/site/",
          showLastUpdateTime: false,
        },
        blog: false,
        theme: { customCss: "./src/css/custom.css" },
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
    colorMode: { respectPrefersColorScheme: true },
    navbar: {
      title: "NucleiLabs",
      logo: { alt: "NucleiLabs", src: "img/favicon.svg" },
      items: [
        { type: "docSidebar", sidebarId: "docs", position: "left", label: "Neura" },
        { to: "/reference/api", label: "Reference", position: "left" },
        { to: "/status", label: "Development status", position: "left" },
        { href: "https://github.com/rkd0608/neura", label: "GitHub", position: "right" },
      ],
    },
    footer: {
      style: "dark",
      copyright: `Copyright © ${new Date().getFullYear()} NucleiLabs. Built from the Neura repository.`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
      additionalLanguages: ["bash", "protobuf", "yaml", "json"],
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
