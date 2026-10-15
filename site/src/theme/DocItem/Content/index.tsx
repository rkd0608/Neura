// Wraps every docs page's content with its status banner, read from the page's front matter.
// tools/check_docs.py fails the build if a page has no valid label.
import { useDoc } from "@docusaurus/plugin-content-docs/client";
import Content from "@theme-original/DocItem/Content";
import type ContentType from "@theme/DocItem/Content";
import type { WrapperProps } from "@docusaurus/types";
import type { ReactNode } from "react";

import StatusBanner, { type Status } from "@site/src/components/StatusBanner";

type Props = WrapperProps<typeof ContentType>;

export default function ContentWrapper(props: Props): ReactNode {
  const { frontMatter } = useDoc();
  const status = (frontMatter as { status?: Status }).status;
  const removal = (frontMatter as { removal?: string }).removal;
  return (
    <>
      {status ? <StatusBanner status={status} removal={removal} /> : null}
      <Content {...props} />
    </>
  );
}
