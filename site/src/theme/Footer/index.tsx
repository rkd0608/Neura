// The site's footer on every docs page: the same columns as the NucleiLabs home page.
import Link from "@docusaurus/Link";
import useBaseUrl from "@docusaurus/useBaseUrl";
import useDocusaurusContext from "@docusaurus/useDocusaurusContext";
import type { ReactNode } from "react";

export default function Footer(): ReactNode {
  const { siteConfig } = useDocusaurusContext();
  const console = String(siteConfig.customFields?.consoleUrl ?? "/");
  const contact = String(siteConfig.customFields?.contactUrl ?? "mailto:hello@nucleilabs.ai");
  const mark = useBaseUrl("/img/favicon.svg");
  return (
    <footer className="site-footer">
      <div className="wrap inner">
        <div>
          <a href="/" className="brand">
            <img src={mark} alt="" />
            <span>NucleiLabs</span>
          </a>
          <p className="tagline">Infrastructure that makes robot AI easier to run.</p>
        </div>
        <nav aria-label="Footer">
          <div>
            <span>Product</span>
            <a href="/#neura">Neura</a>
            <a href="/#how">How it works</a>
            <a href={console}>Console</a>
          </div>
          <div>
            <span>Developers</span>
            <Link to="/">Documentation</Link>
            <Link to="/deploy-a-policy">Deploy a policy</Link>
            <Link to="/status">Development status</Link>
          </div>
          <div>
            <span>Company</span>
            <a href="/#partner">Design partners</a>
            <a href={contact}>Contact</a>
          </div>
        </nav>
      </div>
      <div className="wrap base">
        <span>© {new Date().getFullYear()} NucleiLabs</span>
        <span>Neura is in development. Nothing here is a released product.</span>
      </div>
    </footer>
  );
}
