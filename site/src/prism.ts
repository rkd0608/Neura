// The code blocks' colours: the design tokens' (console/src/styles/tokens.css), so one theme serves
// both modes and every colour meets WCAG 2.1 AA on the block's background in each, as the tokens
// do. tools/shoot.mjs checks it with axe on every page.
import type { PrismTheme } from "prism-react-renderer";

const token = (name: string): string => `var(--${name})`;

const codeColors: PrismTheme = {
  plain: { color: token("text"), backgroundColor: token("surface") },
  styles: [
    {
      types: ["comment", "prolog", "doctype", "cdata"],
      style: { color: token("text-3"), fontStyle: "italic" },
    },
    { types: ["punctuation", "operator"], style: { color: token("text-2") } },
    { types: ["keyword", "atrule", "selector", "important"], style: { color: token("accent") } },
    {
      types: ["string", "char", "attr-value", "inserted", "regex", "url"],
      style: { color: token("ok") },
    },
    { types: ["function", "builtin", "class-name"], style: { color: token("info") } },
    { types: ["number", "boolean", "constant", "symbol", "unit"], style: { color: token("warn") } },
    {
      types: ["property", "tag", "attr-name", "variable", "deleted", "key"],
      style: { color: token("danger") },
    },
  ],
};

export default codeColors;
