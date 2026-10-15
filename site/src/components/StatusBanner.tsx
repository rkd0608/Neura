// The status label every page carries (design §26.2), shown above the page's content.
import Link from "@docusaurus/Link";
import clsx from "clsx";
import type { ReactNode } from "react";

import styles from "./StatusBanner.module.css";

export type Status = "Proposed" | "Experimental" | "Supported" | "Deprecated";

const MEANING: Record<Status, string> = {
  Proposed: "Designed, not implemented. Interfaces and samples here are sketches that do not run.",
  Experimental:
    "Implemented and testable, not qualified. It may change without notice; known limitations are listed.",
  Supported:
    "Passed the conformance, parity and fault suites for the named combination, with linked evidence.",
  Deprecated: "Still works, scheduled for removal.",
};

export default function StatusBanner({
  status,
  removal,
}: {
  status: Status;
  removal?: string;
}): ReactNode {
  return (
    <div className={clsx(styles.banner, styles[status.toLowerCase()])} role="note">
      <strong>{status}.</strong> {MEANING[status]}
      {status === "Deprecated" && removal ? ` Removal: ${removal}.` : null}{" "}
      <Link to="/status#status-labels">What the labels mean</Link>
    </div>
  );
}
