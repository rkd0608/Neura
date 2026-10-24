import type { SidebarsConfig } from "@docusaurus/plugin-content-docs";

const sidebars: SidebarsConfig = {
  docs: [
    "index",
    "architecture",
    "concepts",
    "privacy-and-access",
    "compatibility",
    "status",
    {
      type: "category",
      label: "Try it",
      collapsed: false,
      items: [
        "deploy-a-policy",
        "policies",
        "quickstart",
        "fault-suite",
        "hardware-qualification",
        "troubleshooting",
      ],
    },
    {
      type: "category",
      label: "Reference",
      collapsed: false,
      items: [
        "reference/api",
        "reference/sdk",
        "reference/protocol",
        "reference/reason-codes",
        "reference/outbound",
      ],
    },
  ],
};

export default sidebars;
