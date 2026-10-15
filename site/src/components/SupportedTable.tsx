// The Supported combinations table (design §26.5): rendered from data/supported.json, which only
// qualification runs write, so nobody can add a supported combination by hand.
import type { ReactNode } from "react";

import supported from "@site/data/supported.json";

type Combination = {
  model: string;
  robot: string;
  execution_profile: string;
  gpu: string;
  qualified_on: string;
  evidence: string;
};

export default function SupportedTable(): ReactNode {
  const rows = supported as Combination[];
  if (rows.length === 0) {
    return (
      <p>
        <strong>No combination is Supported yet.</strong> The first one is listed here when it passes
        qualification, with a link to its evidence.
      </p>
    );
  }
  return (
    <table>
      <thead>
        <tr>
          <th>Model</th>
          <th>Robot</th>
          <th>Execution profile</th>
          <th>GPU</th>
          <th>Qualified</th>
          <th>Evidence</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={`${row.model}/${row.robot}/${row.execution_profile}/${row.gpu}`}>
            <td>{row.model}</td>
            <td>{row.robot}</td>
            <td>{row.execution_profile}</td>
            <td>{row.gpu}</td>
            <td>{row.qualified_on}</td>
            <td>
              <a href={row.evidence}>report</a>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
